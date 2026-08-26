"""The SWE-Forge shared graph state.

This is a LangGraph state schema, not an Open SWE one. Upstream Open SWE
carries its state inside a deep-agent message list plus sandbox filesystem;
SWE-Forge instead keeps an explicit, typed, inspectable record of the whole
task so that routing functions can make decisions from structured data rather
than by re-reading a conversation transcript.

Reducer choice matters here. Fields written by exactly one node use the
default "last write wins". Fields that may be written by concurrently
executing subtask branches use an append reducer, so a fan-out never drops a
sibling's result.
"""

import operator
from typing import Annotated, Any, Literal, TypedDict

from agent.sweforge.schemas import (
    ExecutionMetrics,
    ExperienceRecord,
    FailureDiagnosis,
    ImplementationResult,
    ModelCallRecord,
    RecoveryAttempt,
    ReviewResult,
    RiskScore,
    SecurityFinding,
    TaskPlan,
    VerificationResult,
)

FinalStatus = Literal[
    "pending",
    "budget_exhausted",
    "completed",
    "completed_with_findings",
    "awaiting_human_approval",
    "escalated_recovery_exhausted",
    "escalated_review_rejected",
    "failed",
]


def _merge_metrics(
    left: ExecutionMetrics | None, right: ExecutionMetrics | None
) -> ExecutionMetrics:
    """Additive merge so concurrent branches accumulate rather than clobber."""
    if left is None:
        return right or ExecutionMetrics()
    if right is None:
        return left
    return ExecutionMetrics(
        node_transitions=[*left.node_transitions, *right.node_transitions],
        model_calls=left.model_calls + right.model_calls,
        tool_calls=left.tool_calls + right.tool_calls,
        input_tokens=left.input_tokens + right.input_tokens,
        output_tokens=left.output_tokens + right.output_tokens,
        estimated_cost_usd=round(left.estimated_cost_usd + right.estimated_cost_usd, 8),
        wall_time_seconds=max(left.wall_time_seconds, right.wall_time_seconds),
        recovery_attempts=max(left.recovery_attempts, right.recovery_attempts),
        review_rejections=max(left.review_rejections, right.review_rejections),
        security_gate_triggered=left.security_gate_triggered or right.security_gate_triggered,
        verification_runs=left.verification_runs + right.verification_runs,
    )


class SWEForgeState(TypedDict, total=False):
    """Typed state threaded through every SWE-Forge node."""

    # --- inputs -----------------------------------------------------------
    task: str
    repository: str
    repo_root: str

    # --- repository intelligence ------------------------------------------
    repository_map: dict[str, Any]
    relevant_files: list[str]

    # --- planning ---------------------------------------------------------
    complexity: str
    plan: TaskPlan | None
    selected_agents: list[str]
    memory_context: list[ExperienceRecord]

    # --- execution --------------------------------------------------------
    implementation_results: Annotated[list[ImplementationResult], operator.add]
    #: Which specialised agents actually executed (plan-driven dispatch).
    agents_executed: Annotated[list[str], operator.add]
    #: Per-attempt model records from ModelExecutionPolicy (retry/fallback).
    model_attempts: Annotated[list[dict[str, Any]], operator.add]
    #: Budget headroom snapshot, refreshed at gates.
    budget_snapshot: dict[str, Any]
    #: External context retrieved via MCP, if any.
    external_context: list[dict[str, Any]]
    test_results: VerificationResult | None
    failures: Annotated[list[FailureDiagnosis], operator.add]
    recovery_attempts: list[RecoveryAttempt]

    # --- assurance --------------------------------------------------------
    review_results: ReviewResult | None
    security_findings: list[SecurityFinding]
    risk_score: RiskScore | None

    # --- bookkeeping ------------------------------------------------------
    model_usage: Annotated[list[ModelCallRecord], operator.add]
    execution_metrics: Annotated[ExecutionMetrics, _merge_metrics]
    node_trace: Annotated[list[str], operator.add]
    errors: Annotated[list[str], operator.add]

    # --- terminal ---------------------------------------------------------
    final_status: FinalStatus
    final_summary: str


def initial_state(task: str, repository: str, repo_root: str) -> SWEForgeState:
    """Build a fully-populated starting state (no missing-key surprises)."""
    return SWEForgeState(
        task=task,
        repository=repository,
        repo_root=repo_root,
        repository_map={},
        relevant_files=[],
        complexity="moderate",
        plan=None,
        selected_agents=[],
        memory_context=[],
        implementation_results=[],
        agents_executed=[],
        model_attempts=[],
        budget_snapshot={},
        external_context=[],
        test_results=None,
        failures=[],
        recovery_attempts=[],
        review_results=None,
        security_findings=[],
        risk_score=None,
        model_usage=[],
        execution_metrics=ExecutionMetrics(),
        node_trace=[],
        errors=[],
        final_status="pending",
        final_summary="",
    )
