"""SWE-Forge-owned LangChain tools.

These are real ``langchain_core.tools`` StructuredTools with typed Pydantic
argument schemas, bound to live SWE-Forge subsystems through
:class:`ToolContext`. They are not demonstrations: the graph nodes and agents
call them, and every call is counted in :class:`ToolCallLedger` so the
evaluation harness can report tool usage per workflow variant.

Error policy: a tool never raises into the agent loop. Failures are returned as
structured payloads with ``ok: false`` and a message, because a raised
exception inside a tool call tends to derail an agent turn, whereas a
structured error is something the model can read and route around.
"""

import time
from dataclasses import dataclass, field
from typing import Any

from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field

from agent.sweforge.memory.store import ExperienceStore
from agent.sweforge.recovery.classifier import FailureClassifier
from agent.sweforge.repository.analyzer import RepositoryAnalyzer
from agent.sweforge.repository.graph_index import RepositoryGraph
from agent.sweforge.schemas import VerificationResult
from agent.sweforge.security.risk import ChangeSet, RiskEngine, SecurityScanner
from agent.sweforge.tools.errors import ToolErrorPolicy
from agent.sweforge.verification.verifier import Verifier


@dataclass
class ToolCall:
    """One tool invocation, with enough provenance to audit a run.

    ``args_summary`` records argument shape rather than values: a tool payload
    can contain whole file contents, so logging it verbatim would bloat traces
    and risk leaking repository content into observability.
    """

    tool: str
    duration_seconds: float
    ok: bool
    error: str | None = None
    node: str | None = None
    agent: str | None = None
    model: str | None = None
    args_summary: str = ""
    status: str = "ok"


@dataclass
class ToolCallLedger:
    calls: list[ToolCall] = field(default_factory=list)

    def record(self, call: ToolCall) -> None:
        self.calls.append(call)

    @property
    def total(self) -> int:
        return len(self.calls)

    def by_tool(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for call in self.calls:
            out[call.tool] = out.get(call.tool, 0) + 1
        return out


def summarize_args(kwargs: dict[str, Any]) -> str:
    """Redacted argument description: keys, types and sizes only."""
    parts: list[str] = []
    for key, value in sorted(kwargs.items()):
        if isinstance(value, str):
            parts.append(f"{key}=str[{len(value)}]")
        elif isinstance(value, dict):
            parts.append(f"{key}=dict[{len(value)}]")
        elif isinstance(value, list):
            parts.append(f"{key}=list[{len(value)}]")
        else:
            parts.append(f"{key}={type(value).__name__}")
    return ", ".join(parts)


@dataclass
class ToolContext:
    """Live subsystems the tools operate on.

    ``repo_root`` is required; everything else is lazily built or optional so
    tools degrade gracefully instead of failing when a subsystem is absent.
    """

    repo_root: str
    graph: RepositoryGraph | None = None
    verifier: Verifier | None = None
    memory: ExperienceStore | None = None
    risk_engine: RiskEngine | None = None
    scanner: SecurityScanner | None = None
    classifier: FailureClassifier | None = None
    ledger: ToolCallLedger = field(default_factory=ToolCallLedger)
    #: Attribution for the next tool call, set by the graph node or agent.
    current_node: str | None = None
    current_agent: str | None = None
    current_model: str | None = None
    #: Local trace sink. Set by the runtime so every tool call — graph-owned or
    #: agent bind_tools — is recorded in exactly one place.
    tracer: Any | None = None

    def ensure_graph(self) -> RepositoryGraph:
        if self.graph is None:
            self.graph = RepositoryGraph(RepositoryAnalyzer().analyze(self.repo_root))
        return self.graph


# --------------------------------------------------------------------------
# Argument schemas
# --------------------------------------------------------------------------
class AnalyzeRepositoryArgs(BaseModel):
    max_files: int = Field(
        default=4000, ge=1, le=20000, description="Upper bound on files to inventory."
    )


class GraphQueryArgs(BaseModel):
    file: str = Field(description="Repository-relative path, e.g. 'agent/utils/model.py'.")


class SymbolQueryArgs(BaseModel):
    symbol: str = Field(description="Exact class or function name, e.g. 'make_model'.")


class TaskQueryArgs(BaseModel):
    task: str = Field(description="Natural-language task or issue description.")
    limit: int = Field(default=10, ge=1, le=50, description="Maximum results to return.")
    include_tests: bool = Field(default=True, description="Include test files in results.")


class RunValidationArgs(BaseModel):
    changed_files: list[str] = Field(
        default_factory=list,
        description="Files the change touched; drives targeted test selection.",
    )
    full_suite: bool = Field(
        default=False, description="Run the entire test suite instead of targeted tests."
    )


class AnalyzeFailureArgs(BaseModel):
    output: str = Field(description="Raw stdout/stderr from the failing command.")
    errors: list[str] = Field(default_factory=list, description="Pre-extracted error lines.")


class ChangeRiskArgs(BaseModel):
    files: dict[str, str] = Field(
        description="Mapping of repository-relative path to the file's new full content."
    )
    deleted: list[str] = Field(default_factory=list, description="Paths being deleted.")
    verification_passed: bool | None = Field(
        default=None, description="Whether verification is currently green."
    )


class SecurityScanArgs(BaseModel):
    files: dict[str, str] = Field(description="Mapping of path to new content to screen.")


class SimilarTasksArgs(BaseModel):
    task: str = Field(description="The new task, used as the retrieval query.")
    limit: int = Field(default=3, ge=1, le=10)


class GitDiffArgs(BaseModel):
    base: str = Field(default="HEAD", description="Git ref to diff against.")
    paths: list[str] = Field(default_factory=list, description="Restrict the diff to these paths.")


# --------------------------------------------------------------------------
# Tool construction
# --------------------------------------------------------------------------
def build_tools(context: ToolContext) -> list[StructuredTool]:
    """Build the SWE-Forge tool set bound to ``context``."""

    def _guard(name: str, fn: Any) -> Any:
        """Wrap a tool implementation with timing, counting and error capture."""

        def wrapper(**kwargs: Any) -> dict[str, Any]:
            started = time.perf_counter()
            try:
                payload = fn(**kwargs)
            except Exception as exc:  # structured error, never a raised exception
                message = f"{type(exc).__name__}: {exc}"
                classification = ToolErrorPolicy().classify_exception(exc)
                context.ledger.record(
                    ToolCall(
                        tool=name,
                        duration_seconds=round(time.perf_counter() - started, 6),
                        ok=False,
                        error=message,
                        node=context.current_node,
                        agent=context.current_agent,
                        model=context.current_model,
                        args_summary=summarize_args(kwargs),
                        status=classification.category,
                    )
                )
                # The category lets the caller (and the agent) distinguish a bad
                # argument from a transient failure, which a bare error string
                # cannot express.
                if context.tracer is not None:
                    context.tracer.tool(
                        name,
                        status=classification.category,
                        node=context.current_node,
                        agent=context.current_agent,
                        duration_seconds=round(time.perf_counter() - started, 6),
                        detail={"args": kwargs, "error": message},
                    )
                return {
                    "ok": False,
                    "error": message,
                    "error_category": classification.category,
                }
            context.ledger.record(
                ToolCall(
                    tool=name,
                    duration_seconds=round(time.perf_counter() - started, 6),
                    ok=True,
                    node=context.current_node,
                    agent=context.current_agent,
                    model=context.current_model,
                    args_summary=summarize_args(kwargs),
                    status="ok",
                )
            )
            if context.tracer is not None:
                context.tracer.tool(
                    name,
                    status="ok",
                    node=context.current_node,
                    agent=context.current_agent,
                    duration_seconds=round(time.perf_counter() - started, 6),
                    detail={"args": kwargs},
                )
            return {"ok": True, **payload}

        return wrapper

    # -- repository intelligence ------------------------------------------
    def analyze_repository(max_files: int = 4000) -> dict[str, Any]:
        repo_map = RepositoryAnalyzer(max_files=max_files).analyze(context.repo_root)
        context.graph = RepositoryGraph(repo_map)
        summary = repo_map.to_summary()
        summary["graph"] = context.graph.stats()
        summary["parse_errors"] = [
            {"file": p, "error": f.parse_error} for p, f in repo_map.files.items() if f.parse_error
        ][:10]
        return summary

    def build_repository_graph() -> dict[str, Any]:
        graph = context.ensure_graph()
        return {"stats": graph.stats(), "modules": graph.find_relevant_modules("", limit=0) or []}

    def find_relevant_files(
        task: str, limit: int = 10, include_tests: bool = True
    ) -> dict[str, Any]:
        graph = context.ensure_graph()
        hits = graph.find_related_files(task, limit=limit, include_tests=include_tests)
        return {
            "task": task,
            "results": [{"path": h.path, "score": h.score, "reasons": h.reasons[:3]} for h in hits],
            "modules": graph.find_relevant_modules(task),
        }

    def find_dependencies(file: str) -> dict[str, Any]:
        graph = context.ensure_graph()
        if file not in graph.map.files:
            return {"file": file, "known": False, "imports": [], "imported_by": []}
        return {
            "file": file,
            "known": True,
            "imports": graph.find_dependencies(file),
            "imported_by": graph.find_dependents(file),
        }

    def find_callers(symbol: str) -> dict[str, Any]:
        graph = context.ensure_graph()
        return {
            "symbol": symbol,
            "defined_in": graph.find_definition(symbol),
            "referencing_files": graph.find_callers(symbol),
            "note": "static analysis only; aliased and dynamic call sites are not resolved",
        }

    def find_related_tests(file: str) -> dict[str, Any]:
        graph = context.ensure_graph()
        return {"file": file, "tests": graph.find_tests_for_file(file)}

    # -- verification / diagnosis -----------------------------------------
    def run_validation(changed_files: list[str], full_suite: bool = False) -> dict[str, Any]:
        if context.verifier is None:
            raise RuntimeError("no verifier configured; verification requires an execution backend")
        result = context.verifier.verify(changed_files, full_suite=full_suite)
        return {"verification": result.model_dump(exclude={"output"}), "summary": result.summary()}

    def analyze_failure(output: str, errors: list[str] | None = None) -> dict[str, Any]:
        classifier = context.classifier or FailureClassifier(
            known_files=set(context.graph.map.files) if context.graph else None
        )
        stub = VerificationResult(
            passed=False, errors=list(errors or []), output=output, tests_failed=1, tests_run=1
        )
        classification = classifier.classify(stub)
        return {
            "category": classification.category,
            "confidence": classification.confidence,
            "evidence": classification.evidence,
            "suspect_files": classification.suspect_files,
            "failing_tests": classification.failing_tests,
            "matched_rules": classification.matched_rules,
        }

    def inspect_git_diff(base: str = "HEAD", paths: list[str] | None = None) -> dict[str, Any]:
        if context.verifier is None:
            raise RuntimeError("git inspection requires an execution backend")
        scope = " ".join(paths or [])
        command = f"git --no-pager diff --stat {base} -- {scope}".strip()
        result = context.verifier.backend.run(command, timeout=60)
        name_only = context.verifier.backend.run(
            f"git --no-pager diff --name-only {base} -- {scope}".strip(), timeout=60
        )
        return {
            "base": base,
            "stat": result.stdout[-4000:],
            "changed_files": [p for p in name_only.stdout.split("\n") if p.strip()],
            "exit_code": result.exit_code,
        }

    # -- risk / security ---------------------------------------------------
    def calculate_change_risk(
        files: dict[str, str],
        deleted: list[str] | None = None,
        verification_passed: bool | None = None,
    ) -> dict[str, Any]:
        engine = context.risk_engine or RiskEngine()
        verification = (
            None if verification_passed is None else VerificationResult(passed=verification_passed)
        )
        score = engine.assess(
            ChangeSet(files=files, deleted=deleted or []), verification=verification
        )
        return {
            "risk": score.model_dump(),
            "requires_human_approval": score.requires_human_approval,
        }

    def security_scan(files: dict[str, str]) -> dict[str, Any]:
        scanner = context.scanner or SecurityScanner()
        findings = scanner.scan(ChangeSet(files=files))
        return {
            "finding_count": len(findings),
            "findings": [f.model_dump() for f in findings],
            "note": "pattern-based screening; not a substitute for SAST or human review",
        }

    # -- memory ------------------------------------------------------------
    def retrieve_similar_tasks(task: str, limit: int = 3) -> dict[str, Any]:
        if context.memory is None:
            return {"results": [], "note": "experience memory not configured"}
        retrieved = context.memory.retrieve(task, limit=limit)
        return {
            "results": [
                {
                    "task": r.record.task,
                    "outcome": r.record.final_status,
                    "score": r.score,
                    "relevant_files": r.record.relevant_files[:5],
                    "lesson": r.record.lesson,
                    "matched_terms": r.matched_terms[:8],
                }
                for r in retrieved
            ],
            "corpus_size": len(context.memory),
        }

    specs: list[tuple[str, str, Any, type[BaseModel] | None]] = [
        (
            "analyze_repository",
            "Inventory the repository: file counts, languages, symbol counts, test files, "
            "and Python AST parse errors. Run this before planning to ground file references "
            "in what actually exists.",
            analyze_repository,
            AnalyzeRepositoryArgs,
        ),
        (
            "build_repository_graph",
            "Build (or return) the in-repo import dependency graph and report its size.",
            build_repository_graph,
            None,
        ),
        (
            "find_relevant_files",
            "Rank repository files by relevance to a task description using identifier overlap "
            "on paths, defined symbols and docstrings, expanded one hop along import edges. "
            "Use this to choose which files to open instead of guessing paths.",
            find_relevant_files,
            TaskQueryArgs,
        ),
        (
            "find_dependencies",
            "For one file, list the in-repo modules it imports and the files that import it. "
            "Use this to assess blast radius before editing.",
            find_dependencies,
            GraphQueryArgs,
        ),
        (
            "find_callers",
            "Find where a class or function name is defined and which in-repo files import its "
            "module. Static analysis only.",
            find_callers,
            SymbolQueryArgs,
        ),
        (
            "find_related_tests",
            "List tests covering a file, resolved via import edges and test naming conventions. "
            "Use this to pick targeted tests instead of running the whole suite.",
            find_related_tests,
            GraphQueryArgs,
        ),
        (
            "run_validation",
            "Execute validation (targeted pytest, optional lint/typecheck) inside the sandbox and "
            "return structured pass/fail counts. This is the ground truth for whether a change works.",
            run_validation,
            RunValidationArgs,
        ),
        (
            "analyze_failure",
            "Classify failing command output into a category (syntax, type, dependency, "
            "test_assertion, runtime, configuration, environment, lint, timeout) and extract "
            "suspect files and failing tests.",
            analyze_failure,
            AnalyzeFailureArgs,
        ),
        (
            "inspect_git_diff",
            "Return `git diff --stat` and the changed-file list for the working tree.",
            inspect_git_diff,
            GitDiffArgs,
        ),
        (
            "calculate_change_risk",
            "Score a change set for risk (0-100, LOW/MEDIUM/HIGH) from sensitive paths, secret "
            "patterns, destructive operations, diff size and verification state. Drives the "
            "human-approval gate.",
            calculate_change_risk,
            ChangeRiskArgs,
        ),
        (
            "security_scan",
            "Screen changed file contents for committed secrets, destructive shell commands, "
            "unsafe deserialisation, disabled TLS verification and weakened auth checks.",
            security_scan,
            SecurityScanArgs,
        ),
        (
            "retrieve_similar_tasks",
            "Retrieve similar previously-completed tasks from experience memory, with the files "
            "that mattered and lessons learned. Use as planning context.",
            retrieve_similar_tasks,
            SimilarTasksArgs,
        ),
    ]

    tools: list[StructuredTool] = []
    for name, description, fn, schema in specs:
        tools.append(
            StructuredTool.from_function(
                func=_guard(name, fn),
                name=name,
                description=description,
                args_schema=schema,
                handle_tool_error=True,
            )
        )
    for tool in tools:
        # Back-reference so an agent tool loop can set call attribution.
        object.__setattr__(tool, "_sweforge_context", context)
    return tools


def tools_by_name(tools: list[StructuredTool]) -> dict[str, StructuredTool]:
    return {t.name: t for t in tools}
