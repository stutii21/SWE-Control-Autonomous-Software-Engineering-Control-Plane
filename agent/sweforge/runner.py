"""SWE-Forge entry point.

Assembles a runtime (execution backend, model router, repository intelligence,
memory) and executes the compiled graph. Two backends are supported:

* ``sandbox`` — the production path, delegating to Open SWE's sandbox.
* ``local``   — gated host execution, for SWE-Forge's own evaluation fixtures.

The observability wrapper is applied here so a single run produces one
LangSmith trace root when tracing is configured, and is a no-op otherwise.
"""

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agent.sweforge.graph.workflow import SWEForgeRuntime, WorkflowConfig, build_workflow
from agent.sweforge.memory.store import ExperienceStore
from agent.sweforge.observability.tracing import trace_run, tracing_enabled
from agent.sweforge.recovery.classifier import FailureClassifier
from agent.sweforge.repository.analyzer import RepositoryAnalyzer
from agent.sweforge.repository.graph_index import RepositoryGraph
from agent.sweforge.routing.model_router import ModelRouter
from agent.sweforge.security.risk import RiskEngine, SecurityScanner
from agent.sweforge.state.graph_state import SWEForgeState, initial_state
from agent.sweforge.tools.registry import ToolContext, build_tools
from agent.sweforge.verification.backends import (
    LocalSubprocessBackend,
)
from agent.sweforge.verification.verifier import Verifier


@dataclass
class RunOutcome:
    """Everything a caller (CLI, evaluator, dashboard) needs from one run."""

    state: SWEForgeState
    wall_time_seconds: float
    variant: str

    @property
    def final_status(self) -> str:
        return str(self.state.get("final_status", "unknown"))

    @property
    def verification_passed(self) -> bool:
        result = self.state.get("test_results")
        return bool(result and result.passed)

    @property
    def recovery_attempts(self) -> int:
        return len(self.state.get("recovery_attempts", []))

    @property
    def recovered(self) -> bool:
        """True when recovery ran and verification ended green."""
        return self.recovery_attempts > 0 and self.verification_passed

    @property
    def node_trace(self) -> list[str]:
        return list(self.state.get("node_trace", []))

    def metrics(self) -> dict[str, Any]:
        metrics = self.state.get("execution_metrics")
        review = self.state.get("review_results")
        risk = self.state.get("risk_score")
        verification = self.state.get("test_results")
        return {
            "variant": self.variant,
            "final_status": self.final_status,
            "verification_passed": self.verification_passed,
            "first_attempt_success": self.verification_passed and self.recovery_attempts == 0,
            "recovery_attempts": self.recovery_attempts,
            "recovered": self.recovered,
            "tests_run": verification.tests_run if verification else 0,
            "tests_passed": verification.tests_passed if verification else 0,
            "tests_failed": verification.tests_failed if verification else 0,
            "model_calls": metrics.model_calls if metrics else 0,
            "tool_calls": metrics.tool_calls if metrics else 0,
            "input_tokens": metrics.input_tokens if metrics else 0,
            "output_tokens": metrics.output_tokens if metrics else 0,
            "estimated_cost_usd": metrics.estimated_cost_usd if metrics else 0.0,
            "verification_runs": metrics.verification_runs if metrics else 0,
            "review_approved": (None if review is None else review.approved),
            "review_rejections": metrics.review_rejections if metrics else 0,
            "risk_level": (risk.level if risk else None),
            "risk_score": (risk.score if risk else None),
            "security_gate_triggered": (metrics.security_gate_triggered if metrics else False),
            "security_findings": len(self.state.get("security_findings", [])),
            "wall_time_seconds": round(self.wall_time_seconds, 4),
            "node_count": len(self.node_trace),
        }


def build_runtime(
    *,
    repo_root: str,
    config: WorkflowConfig | None = None,
    router: ModelRouter | None = None,
    backend: Any | None = None,
    backend_kind: str = "local",
    memory_path: str | None = None,
    enable_lint: bool = True,
) -> SWEForgeRuntime:
    """Wire up every subsystem for one run."""
    config = config or WorkflowConfig()
    router = router or ModelRouter()

    if backend is None:
        if backend_kind == "local":
            backend = LocalSubprocessBackend(repo_root)
        else:
            raise ValueError(
                "sandbox backend must be supplied explicitly via "
                "OpenSWESandboxBackend.for_thread(thread_id); it requires a live "
                "Open SWE thread."
            )

    graph_index: RepositoryGraph | None = None
    if config.enable_repository_intelligence:
        graph_index = RepositoryGraph(RepositoryAnalyzer().analyze(repo_root))

    verifier = Verifier(backend, graph=graph_index, enable_lint=enable_lint)
    memory = (
        ExperienceStore(memory_path or Path(repo_root) / ".sweforge" / "experience.jsonl")
        if config.enable_memory
        else None
    )
    classifier = FailureClassifier(known_files=set(graph_index.map.files) if graph_index else None)

    runtime = SWEForgeRuntime(
        repo_root=repo_root,
        backend=backend,
        router=router,
        config=config,
        memory=memory,
        graph_index=graph_index,
        verifier=verifier,
        risk_engine=RiskEngine(),
        scanner=SecurityScanner(),
        classifier=classifier,
    )
    # Attach the local tracer at construction so the first tool call is captured.
    runtime.tool_context = ToolContext(
        repo_root=repo_root,
        graph=graph_index,
        verifier=verifier,
        memory=memory,
        risk_engine=runtime.risk_engine,
        scanner=runtime.scanner,
        classifier=classifier,
        tracer=runtime.tracer,
    )
    build_tools(runtime.tool_context)
    return runtime


def run_task(
    *,
    task: str,
    repo_root: str,
    repository: str = "",
    config: WorkflowConfig | None = None,
    router: ModelRouter | None = None,
    backend: Any | None = None,
    backend_kind: str = "local",
    memory_path: str | None = None,
    recursion_limit: int = 60,
) -> RunOutcome:
    """Execute one SWE-Forge task end to end."""
    config = config or WorkflowConfig()
    runtime = build_runtime(
        repo_root=repo_root,
        config=config,
        router=router,
        backend=backend,
        backend_kind=backend_kind,
        memory_path=memory_path,
    )
    graph = build_workflow(runtime)
    state = initial_state(task=task, repository=repository or repo_root, repo_root=repo_root)

    started = time.perf_counter()
    with trace_run(
        name=f"sweforge:{config.variant_name}",
        metadata={
            "variant": config.variant_name,
            "repository": repository or repo_root,
            "recovery_enabled": config.enable_recovery,
            "review_enabled": config.enable_review,
            "repo_intelligence": config.enable_repository_intelligence,
            "tracing": tracing_enabled(),
        },
    ):
        final = graph.invoke(state, config={"recursion_limit": recursion_limit})
    elapsed = time.perf_counter() - started

    # Fold tool-call totals in, since tools are invoked outside node metrics.
    metrics = final.get("execution_metrics")
    if metrics is not None:
        metrics.tool_calls = max(metrics.tool_calls, runtime.tool_calls())
        metrics.input_tokens = runtime.router.ledger.total_input_tokens
        metrics.output_tokens = runtime.router.ledger.total_output_tokens
        metrics.estimated_cost_usd = runtime.router.ledger.total_cost_usd
        metrics.model_calls = max(metrics.model_calls, runtime.router.ledger.total_calls)

    return RunOutcome(state=final, wall_time_seconds=elapsed, variant=config.variant_name)
