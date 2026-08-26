"""Repository dependency graph and query interface.

Builds a directed import graph over the files discovered by
:class:`~agent.sweforge.repository.analyzer.RepositoryAnalyzer` and exposes the
queries the planner and recovery engine need:

* ``find_dependencies(file)``   - what this file imports (in-repo)
* ``find_dependents(file)``     - who imports this file (reverse edges)
* ``find_callers(symbol)``      - files referencing a symbol name
* ``find_tests_for_file(file)`` - tests that import or are named for a file
* ``find_related_files(task)``  - lexical relevance + graph proximity
* ``find_relevant_modules(task)``

Ranking is deterministic: identical inputs always produce identical output and
ties break on path order. That property is what makes the evaluation harness
reproducible.
"""

import re
from collections import defaultdict
from dataclasses import dataclass, field

from agent.sweforge.repository.analyzer import RepositoryMap

_TOKEN_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]+")

STOPWORDS = {
    "the",
    "and",
    "for",
    "with",
    "that",
    "this",
    "from",
    "into",
    "when",
    "should",
    "add",
    "fix",
    "make",
    "use",
    "using",
    "get",
    "set",
    "not",
    "are",
    "was",
    "has",
    "have",
    "but",
    "all",
    "any",
    "can",
    "will",
    "must",
    "then",
    "than",
    "them",
    "there",
    "their",
    "also",
    "does",
    "did",
    "you",
    "your",
    "our",
    "its",
    "it's",
    "file",
    "files",
    "code",
    "please",
    "need",
    "needs",
    "want",
    "test",
    "tests",
    "function",
    "class",
    "method",
    "module",
    "repo",
    "repository",
    "python",
    "new",
}


def tokenize(text: str) -> list[str]:
    """Lowercased identifier tokens, snake/camel case split, stopwords removed."""
    raw = _TOKEN_RE.findall(text or "")
    out: list[str] = []
    for token in raw:
        for piece in token.split("_"):
            if not piece:
                continue
            # split camelCase / PascalCase
            for sub in re.findall(r"[A-Z]+(?![a-z])|[A-Z][a-z0-9]*|[a-z0-9]+", piece):
                lowered = sub.lower()
                if len(lowered) > 2 and lowered not in STOPWORDS:
                    out.append(lowered)
    return out


@dataclass
class FileScore:
    path: str
    score: float
    reasons: list[str] = field(default_factory=list)


class RepositoryGraph:
    """Directed in-repo import graph plus lexical index."""

    def __init__(self, repo_map: RepositoryMap) -> None:
        self.map = repo_map
        self._modules = repo_map.module_index()
        self._edges: dict[str, set[str]] = defaultdict(set)
        self._reverse: dict[str, set[str]] = defaultdict(set)
        self._symbol_owners: dict[str, set[str]] = defaultdict(set)
        self._file_tokens: dict[str, set[str]] = {}
        self._build()

    # -- construction ------------------------------------------------------
    def _build(self) -> None:
        for path, info in self.map.files.items():
            for symbol in info.symbols:
                self._symbol_owners[symbol.name].add(path)
            tokens: set[str] = set(tokenize(path.replace("/", " ")))
            tokens.update(t for s in info.symbols for t in tokenize(s.name))
            tokens.update(tokenize(info.docstring))
            self._file_tokens[path] = tokens
            for raw_import in info.imports:
                target = self._resolve_import(raw_import, info.module)
                if target and target != path:
                    self._edges[path].add(target)
                    self._reverse[target].add(path)

    def _resolve_import(self, raw: str, importer_module: str | None) -> str | None:
        """Resolve a dotted (possibly relative) import to an in-repo file path."""
        if not raw:
            return None
        name = raw
        if raw.startswith("."):
            if not importer_module:
                return None
            level = len(raw) - len(raw.lstrip("."))
            suffix = raw[level:]
            parts = importer_module.split(".")
            # a module's own package is parts[:-1]; each extra dot climbs one more
            base = parts[: max(0, len(parts) - level)]
            name = ".".join([*base, suffix] if suffix else base)
        # Longest-prefix match against known modules (handles `from pkg.mod import X`).
        candidate = name
        while candidate:
            if candidate in self._modules:
                return self._modules[candidate]
            if "." not in candidate:
                return None
            candidate = candidate.rsplit(".", 1)[0]
        return None

    # -- queries -----------------------------------------------------------
    def find_dependencies(self, file: str) -> list[str]:
        return sorted(self._edges.get(file, set()))

    def find_dependents(self, file: str) -> list[str]:
        return sorted(self._reverse.get(file, set()))

    def find_callers(self, symbol: str) -> list[str]:
        """Files that define or import a module defining the symbol.

        Static-only: this finds *definition sites* and their in-repo importers.
        It does not resolve aliased or dynamic call sites.
        """
        owners = self._symbol_owners.get(symbol, set())
        callers: set[str] = set()
        for owner in owners:
            callers.update(self._reverse.get(owner, set()))
        return sorted(callers)

    def find_definition(self, symbol: str) -> list[str]:
        return sorted(self._symbol_owners.get(symbol, set()))

    def find_tests_for_file(self, file: str) -> list[str]:
        """Tests linked by import edge, or by the test_<stem> naming convention."""
        results: set[str] = set()
        for dependent in self._reverse.get(file, set()):
            if self.map.files[dependent].is_test:
                results.add(dependent)
        stem = file.rsplit("/", 1)[-1].rsplit(".", 1)[0]
        if stem:
            for path, info in self.map.files.items():
                if not info.is_test:
                    continue
                test_stem = path.rsplit("/", 1)[-1].rsplit(".", 1)[0]
                if test_stem in {f"test_{stem}", f"{stem}_test"} or stem in test_stem:
                    results.add(path)
        return sorted(results)

    def find_related_files(
        self,
        task: str,
        *,
        limit: int = 10,
        include_tests: bool = True,
        expand_graph: bool = True,
    ) -> list[FileScore]:
        """Rank files by lexical overlap with the task, then expand by imports.

        Scoring is intentionally simple and inspectable:
          +3.0 per task token matched in the file path
          +2.0 per task token matched in a defined symbol name
          +1.0 per task token matched in the module docstring
          x1.15 bonus for source (non-test) files so implementation targets lead
          +1.5 for a file adjacent (1 hop) to a top-ranked seed
        """
        task_tokens = set(tokenize(task))
        if not task_tokens:
            return []

        scored: dict[str, FileScore] = {}
        for path, info in self.map.files.items():
            if not include_tests and info.is_test:
                continue
            path_tokens = set(tokenize(path.replace("/", " ")))
            symbol_tokens = {t for s in info.symbols for t in tokenize(s.name)}
            doc_tokens = set(tokenize(info.docstring))

            path_hits = task_tokens & path_tokens
            symbol_hits = task_tokens & symbol_tokens
            doc_hits = task_tokens & doc_tokens
            score = 3.0 * len(path_hits) + 2.0 * len(symbol_hits) + 1.0 * len(doc_hits)
            if score <= 0:
                continue
            if not info.is_test:
                score *= 1.15
            reasons = []
            if path_hits:
                reasons.append(f"path matches {sorted(path_hits)}")
            if symbol_hits:
                reasons.append(f"defines {sorted(symbol_hits)}")
            if doc_hits:
                reasons.append(f"docstring mentions {sorted(doc_hits)}")
            scored[path] = FileScore(path=path, score=round(score, 4), reasons=reasons)

        if expand_graph and scored:
            seeds = sorted(scored.values(), key=lambda f: (-f.score, f.path))[:3]
            for seed in seeds:
                neighbours = set(self.find_dependencies(seed.path)) | set(
                    self.find_dependents(seed.path)
                )
                for neighbour in neighbours:
                    if neighbour not in self.map.files:
                        continue
                    if not include_tests and self.map.files[neighbour].is_test:
                        continue
                    existing = scored.get(neighbour)
                    if existing:
                        existing.score = round(existing.score + 0.5, 4)
                        existing.reasons.append(f"imports/imported-by {seed.path}")
                    else:
                        scored[neighbour] = FileScore(
                            path=neighbour,
                            score=1.5,
                            reasons=[f"graph neighbour of {seed.path}"],
                        )

        ranked = sorted(scored.values(), key=lambda f: (-f.score, f.path))
        return ranked[:limit]

    def find_relevant_modules(self, task: str, *, limit: int = 5) -> list[str]:
        """Top-level packages/directories most implicated by the task."""
        counts: dict[str, float] = defaultdict(float)
        for hit in self.find_related_files(task, limit=limit * 4):
            top = hit.path.split("/")[0] if "/" in hit.path else hit.path
            counts[top] += hit.score
        return [k for k, _ in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:limit]]

    # -- stats -------------------------------------------------------------
    def stats(self) -> dict[str, int]:
        return {
            "files": len(self.map.files),
            "import_edges": sum(len(v) for v in self._edges.values()),
            "symbols": self.map.symbol_count,
            "distinct_symbol_names": len(self._symbol_owners),
            "test_files": len(self.map.test_files),
        }
