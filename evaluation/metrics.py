"""Evaluation metrics.

Aggregates raw :class:`~evaluation.runner.RunRecord` payloads into the metric
set the project reports. Two rules are enforced here:

1. A run marked unavailable is **excluded from rates**, never silently counted
   as a failure. Infrastructure gaps and genuine task failures are different
   facts and are reported separately.
2. Every metric carries its own denominator. "Recovery success rate" over zero
   recovery attempts is reported as ``None``, not ``0.0`` — an undefined rate
   printed as a number is how misleading benchmark tables get made.
"""

from dataclasses import dataclass, field
from typing import Any


def _rate(numerator: int, denominator: int) -> float | None:
    """A rate, or None when undefined."""
    if denominator <= 0:
        return None
    return round(numerator / denominator, 4)


@dataclass
class VariantMetrics:
    """Aggregated metrics for one workflow variant."""

    variant: str
    runs_attempted: int = 0
    runs_available: int = 0
    runs_unavailable: int = 0

    # outcome counts
    completed: int = 0
    completed_with_findings: int = 0
    awaiting_human_approval: int = 0
    escalated: int = 0
    failed: int = 0

    # verification
    verification_passed: int = 0
    first_attempt_success: int = 0
    tests_run: int = 0
    tests_passed: int = 0
    tests_failed: int = 0

    # recovery
    runs_entering_recovery: int = 0
    recovery_successes: int = 0
    total_recovery_attempts: int = 0

    # assurance
    review_rejections: int = 0
    runs_reviewed: int = 0
    security_gate_interventions: int = 0
    security_findings: int = 0

    # cost / effort
    model_calls: int = 0
    tool_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    estimated_cost_usd: float = 0.0
    wall_time_seconds: float = 0.0
    verification_runs: int = 0
    node_count: int = 0

    unavailable_reasons: list[str] = field(default_factory=list)

    # -- derived rates -----------------------------------------------------
    @property
    def task_success_rate(self) -> float | None:
        """Terminal states a human would accept as 'done'."""
        return _rate(self.completed + self.completed_with_findings, self.runs_available)

    @property
    def verification_pass_rate(self) -> float | None:
        return _rate(self.verification_passed, self.runs_available)

    @property
    def first_attempt_success_rate(self) -> float | None:
        return _rate(self.first_attempt_success, self.runs_available)

    @property
    def recovery_success_rate(self) -> float | None:
        """Of runs that needed recovery, how many ended verified green."""
        return _rate(self.recovery_successes, self.runs_entering_recovery)

    @property
    def avg_recovery_attempts(self) -> float | None:
        if self.runs_entering_recovery <= 0:
            return None
        return round(self.total_recovery_attempts / self.runs_entering_recovery, 3)

    @property
    def test_pass_rate(self) -> float | None:
        return _rate(self.tests_passed, self.tests_run)

    @property
    def reviewer_rejection_rate(self) -> float | None:
        return _rate(self.review_rejections, self.runs_reviewed)

    @property
    def avg_wall_time(self) -> float | None:
        if self.runs_available <= 0:
            return None
        return round(self.wall_time_seconds / self.runs_available, 4)

    @property
    def avg_model_calls(self) -> float | None:
        if self.runs_available <= 0:
            return None
        return round(self.model_calls / self.runs_available, 3)

    @property
    def avg_cost_usd(self) -> float | None:
        if self.runs_available <= 0:
            return None
        return round(self.estimated_cost_usd / self.runs_available, 6)

    def to_dict(self) -> dict[str, Any]:
        return {
            "variant": self.variant,
            "runs_attempted": self.runs_attempted,
            "runs_available": self.runs_available,
            "runs_unavailable": self.runs_unavailable,
            "outcomes": {
                "completed": self.completed,
                "completed_with_findings": self.completed_with_findings,
                "awaiting_human_approval": self.awaiting_human_approval,
                "escalated": self.escalated,
                "failed": self.failed,
            },
            "task_success_rate": self.task_success_rate,
            "verification_pass_rate": self.verification_pass_rate,
            "first_attempt_success_rate": self.first_attempt_success_rate,
            "test_pass_rate": self.test_pass_rate,
            "tests_run": self.tests_run,
            "tests_passed": self.tests_passed,
            "runs_entering_recovery": self.runs_entering_recovery,
            "recovery_success_rate": self.recovery_success_rate,
            "avg_recovery_attempts": self.avg_recovery_attempts,
            "total_recovery_attempts": self.total_recovery_attempts,
            "runs_reviewed": self.runs_reviewed,
            "reviewer_rejection_rate": self.reviewer_rejection_rate,
            "security_gate_interventions": self.security_gate_interventions,
            "security_findings": self.security_findings,
            "model_calls": self.model_calls,
            "avg_model_calls": self.avg_model_calls,
            "tool_calls": self.tool_calls,
            "verification_runs": self.verification_runs,
            "input_tokens_synthetic": self.input_tokens,
            "output_tokens_synthetic": self.output_tokens,
            "estimated_cost_usd_synthetic": round(self.estimated_cost_usd, 6),
            "avg_cost_usd_synthetic": self.avg_cost_usd,
            "total_wall_time_seconds": round(self.wall_time_seconds, 4),
            "avg_wall_time_seconds": self.avg_wall_time,
            "total_nodes_executed": self.node_count,
            "unavailable_reasons": self.unavailable_reasons,
        }


def aggregate(records: list[dict[str, Any]]) -> dict[str, VariantMetrics]:
    """Group raw run records into per-variant metrics."""
    variants: dict[str, VariantMetrics] = {}

    for record in records:
        variant = record["variant"]
        metrics = variants.setdefault(variant, VariantMetrics(variant=variant))
        metrics.runs_attempted += 1

        if not record.get("available", True):
            metrics.runs_unavailable += 1
            reason = record.get("error") or "unknown"
            metrics.unavailable_reasons.append(f"{record['scenario_id']}: {reason}")
            continue

        metrics.runs_available += 1
        payload = record.get("metrics", {})
        status = record.get("status", "unknown")

        if status == "completed":
            metrics.completed += 1
        elif status == "completed_with_findings":
            metrics.completed_with_findings += 1
        elif status == "awaiting_human_approval":
            metrics.awaiting_human_approval += 1
        elif status.startswith("escalated"):
            metrics.escalated += 1
        else:
            metrics.failed += 1

        if payload.get("verification_passed"):
            metrics.verification_passed += 1
        if payload.get("first_attempt_success"):
            metrics.first_attempt_success += 1

        metrics.tests_run += int(payload.get("tests_run") or 0)
        metrics.tests_passed += int(payload.get("tests_passed") or 0)
        metrics.tests_failed += int(payload.get("tests_failed") or 0)

        attempts = int(payload.get("recovery_attempts") or 0)
        if attempts > 0:
            metrics.runs_entering_recovery += 1
            metrics.total_recovery_attempts += attempts
            if payload.get("verification_passed"):
                metrics.recovery_successes += 1

        if payload.get("review_approved") is not None:
            metrics.runs_reviewed += 1
        # `review_rejections` counts rejection events during the run, including
        # ones later resolved by a recovery cycle. Using the final approval flag
        # alone would hide exactly the interventions we want to measure.
        metrics.review_rejections += int(payload.get("review_rejections") or 0)

        if payload.get("security_gate_triggered"):
            metrics.security_gate_interventions += 1
        metrics.security_findings += int(payload.get("security_findings") or 0)

        metrics.model_calls += int(payload.get("model_calls") or 0)
        metrics.tool_calls += int(payload.get("tool_calls") or 0)
        metrics.input_tokens += int(payload.get("input_tokens") or 0)
        metrics.output_tokens += int(payload.get("output_tokens") or 0)
        metrics.estimated_cost_usd += float(payload.get("estimated_cost_usd") or 0.0)
        metrics.wall_time_seconds += float(payload.get("wall_time_seconds") or 0.0)
        metrics.verification_runs += int(payload.get("verification_runs") or 0)
        metrics.node_count += int(payload.get("node_count") or 0)

    return variants


def expectation_check(records: list[dict[str, Any]]) -> dict[str, Any]:
    """Did each run reach the terminal state its scenario predicted?

    This is the harness's own correctness check: it validates that the graph
    routed as designed, independently of whether the task 'succeeded'.
    """
    results: list[dict[str, Any]] = []
    for record in records:
        if not record.get("available", True):
            continue
        expectations = record.get("expectations", {})
        payload = record.get("metrics", {})
        # Expectations describe the FULL variant only; other variants
        # intentionally lack the machinery to reach those states.
        if record["variant"] != "E_full":
            continue
        checks = {
            "status": record.get("status") == expectations.get("expected_status"),
            "verification": bool(payload.get("verification_passed"))
            == bool(expectations.get("expected_verification")),
            "recovery": (int(payload.get("recovery_attempts") or 0) > 0)
            == bool(expectations.get("expects_recovery")),
            "high_risk": (payload.get("risk_level") == "HIGH")
            == bool(expectations.get("expects_high_risk")),
        }
        results.append(
            {
                "scenario_id": record["scenario_id"],
                "passed": all(checks.values()),
                "checks": checks,
                "observed_status": record.get("status"),
                "expected_status": expectations.get("expected_status"),
            }
        )
    passed = sum(1 for r in results if r["passed"])
    return {
        "checked": len(results),
        "passed": passed,
        "rate": _rate(passed, len(results)),
        "details": results,
    }
