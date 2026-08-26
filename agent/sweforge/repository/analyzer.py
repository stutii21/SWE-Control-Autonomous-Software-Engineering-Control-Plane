"""Static repository analysis.

Scope and honesty note
----------------------
This module performs *static* analysis only. For Python it uses the standard
library ``ast`` module, which gives exact syntactic facts: which symbols a file
defines, which modules it imports, where tests live. It does **not** perform
type inference, cross-file call-graph resolution through aliases, dynamic
import tracking, or any semantic understanding of behaviour. Non-Python files
are inventoried and tokenised but not parsed.

Everything downstream (relevance ranking, planning context) is therefore a
*heuristic built on exact syntax*, not semantic comprehension. The limits are
restated in docs/ARCHITECTURE.md so the claim is never oversold.
"""

import ast
import os
import time
from dataclasses import dataclass, field
from pathlib import Path

LANGUAGE_BY_SUFFIX: dict[str, str] = {
    ".py": "python",
    ".pyi": "python",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".js": "javascript",
    ".jsx": "javascript",
    ".go": "go",
    ".rs": "rust",
    ".java": "java",
    ".rb": "ruby",
    ".md": "markdown",
    ".json": "json",
    ".toml": "toml",
    ".yml": "yaml",
    ".yaml": "yaml",
    ".sh": "shell",
    ".sql": "sql",
}

SKIP_DIRS = {
    ".git",
    ".hg",
    "node_modules",
    "__pycache__",
    ".venv",
    "venv",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    "dist",
    "build",
    ".next",
    ".turbo",
    "site-packages",
    ".eggs",
}

SENSITIVE_PATH_MARKERS = (
    ".github/workflows",
    "dockerfile",
    "docker-compose",
    "requirements.txt",
    "pyproject.toml",
    "package.json",
    "pnpm-lock.yaml",
    "uv.lock",
    "makefile",
    ".env",
    "auth",
    "security",
    "secret",
    "credential",
    "token",
    "permission",
    "middleware",
)


@dataclass
class SymbolInfo:
    """A top-level or nested definition discovered by the AST walk."""

    name: str
    kind: str  # "class" | "function" | "async_function" | "method"
    lineno: int
    parent: str | None = None
    docstring: str = ""

    @property
    def qualified_name(self) -> str:
        return f"{self.parent}.{self.name}" if self.parent else self.name


@dataclass
class FileInfo:
    path: str
    language: str
    loc: int
    is_test: bool = False
    is_sensitive: bool = False
    module: str | None = None
    symbols: list[SymbolInfo] = field(default_factory=list)
    imports: list[str] = field(default_factory=list)  # raw dotted module names
    parse_error: str | None = None
    docstring: str = ""

    @property
    def symbol_names(self) -> list[str]:
        return [s.name for s in self.symbols]


@dataclass
class RepositoryMap:
    root: str
    files: dict[str, FileInfo] = field(default_factory=dict)
    languages: dict[str, int] = field(default_factory=dict)
    analysis_seconds: float = 0.0
    skipped_files: int = 0
    truncated: bool = False

    @property
    def file_count(self) -> int:
        return len(self.files)

    @property
    def symbol_count(self) -> int:
        return sum(len(f.symbols) for f in self.files.values())

    @property
    def test_files(self) -> list[str]:
        return sorted(p for p, f in self.files.items() if f.is_test)

    def module_index(self) -> dict[str, str]:
        """Map importable dotted module name -> repository-relative path."""
        index: dict[str, str] = {}
        for path, info in self.files.items():
            if info.module:
                index[info.module] = path
        return index

    def to_summary(self) -> dict[str, object]:
        """Compact, JSON-safe view for prompts and state (never the whole AST)."""
        return {
            "root": self.root,
            "file_count": self.file_count,
            "symbol_count": self.symbol_count,
            "languages": dict(sorted(self.languages.items(), key=lambda kv: -kv[1])),
            "test_file_count": len(self.test_files),
            "analysis_seconds": round(self.analysis_seconds, 4),
            "truncated": self.truncated,
        }


def _is_test_path(rel: str) -> bool:
    lowered = rel.lower()
    name = os.path.basename(lowered)
    return (
        name.startswith("test_")
        or name.endswith("_test.py")
        or name.endswith(".test.ts")
        or name.endswith(".test.tsx")
        or name.endswith(".spec.ts")
        or "/tests/" in f"/{lowered}"
        or lowered.startswith("tests/")
    )


def _is_sensitive_path(rel: str) -> bool:
    lowered = rel.lower()
    return any(marker in lowered for marker in SENSITIVE_PATH_MARKERS)


def _module_name(rel: str) -> str | None:
    if not rel.endswith((".py", ".pyi")):
        return None
    parts = Path(rel).with_suffix("").parts
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts) if parts else None


class _PythonVisitor(ast.NodeVisitor):
    """Collects definitions and imports in one pass."""

    def __init__(self) -> None:
        self.symbols: list[SymbolInfo] = []
        self.imports: list[str] = []
        self._class_stack: list[str] = []

    def visit_ClassDef(self, node: ast.ClassDef) -> None:  # noqa: N802
        self.symbols.append(
            SymbolInfo(
                name=node.name,
                kind="class",
                lineno=node.lineno,
                parent=self._class_stack[-1] if self._class_stack else None,
                docstring=(ast.get_docstring(node) or "")[:300],
            )
        )
        self._class_stack.append(node.name)
        self.generic_visit(node)
        self._class_stack.pop()

    def _function(self, node: ast.FunctionDef | ast.AsyncFunctionDef, kind: str) -> None:
        parent = self._class_stack[-1] if self._class_stack else None
        self.symbols.append(
            SymbolInfo(
                name=node.name,
                kind="method" if parent else kind,
                lineno=node.lineno,
                parent=parent,
                docstring=(ast.get_docstring(node) or "")[:300],
            )
        )
        self.generic_visit(node)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:  # noqa: N802
        self._function(node, "function")

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:  # noqa: N802
        self._function(node, "async_function")

    def visit_Import(self, node: ast.Import) -> None:  # noqa: N802
        for alias in node.names:
            self.imports.append(alias.name)
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:  # noqa: N802
        # Relative imports are recorded with leading dots preserved so the
        # graph builder can resolve them against the importing module.
        prefix = "." * (node.level or 0)
        base = node.module or ""
        self.imports.append(f"{prefix}{base}" if base else prefix)
        self.generic_visit(node)


class RepositoryAnalyzer:
    """Walks a repository and produces a :class:`RepositoryMap`."""

    def __init__(
        self,
        *,
        max_files: int = 4000,
        max_file_bytes: int = 400_000,
        skip_dirs: set[str] | None = None,
    ) -> None:
        self.max_files = max_files
        self.max_file_bytes = max_file_bytes
        self.skip_dirs = skip_dirs or SKIP_DIRS

    def analyze(self, root: str | Path) -> RepositoryMap:
        started = time.perf_counter()
        root_path = Path(root).resolve()
        if not root_path.is_dir():
            raise NotADirectoryError(f"repository root not found: {root_path}")

        repo_map = RepositoryMap(root=str(root_path))
        for path in self._iter_files(root_path):
            if repo_map.file_count >= self.max_files:
                repo_map.truncated = True
                break
            rel = path.relative_to(root_path).as_posix()
            info = self._analyze_file(path, rel, repo_map)
            if info is None:
                repo_map.skipped_files += 1
                continue
            repo_map.files[rel] = info
            repo_map.languages[info.language] = repo_map.languages.get(info.language, 0) + 1

        repo_map.analysis_seconds = time.perf_counter() - started
        return repo_map

    def _iter_files(self, root: Path):
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = sorted(
                d for d in dirnames if d not in self.skip_dirs and not d.startswith(".egg")
            )
            for filename in sorted(filenames):
                yield Path(dirpath) / filename

    def _analyze_file(self, path: Path, rel: str, repo_map: RepositoryMap) -> FileInfo | None:
        suffix = path.suffix.lower()
        language = LANGUAGE_BY_SUFFIX.get(suffix)
        if language is None:
            return None
        try:
            if path.stat().st_size > self.max_file_bytes:
                return None
            text = path.read_text(encoding="utf-8", errors="replace")
        except (OSError, ValueError):
            return None

        info = FileInfo(
            path=rel,
            language=language,
            loc=text.count("\n") + 1 if text else 0,
            is_test=_is_test_path(rel),
            is_sensitive=_is_sensitive_path(rel),
            module=_module_name(rel),
        )

        if language == "python":
            try:
                tree = ast.parse(text, filename=rel)
            except SyntaxError as exc:
                info.parse_error = f"{type(exc).__name__}: {exc.msg} (line {exc.lineno})"
                return info
            visitor = _PythonVisitor()
            visitor.visit(tree)
            info.symbols = visitor.symbols
            info.imports = visitor.imports
            info.docstring = (ast.get_docstring(tree) or "")[:400]

        return info
