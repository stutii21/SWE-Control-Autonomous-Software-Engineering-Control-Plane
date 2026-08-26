"""Execution budgets: hard limits, not accounting.

Phase 23 remediation of audit row 9. Previously ``ModelUsageLedger`` measured
cost and tokens after the fact, and only the recovery and review loops were
bounded. Nothing stopped a run from making a hundred model calls.

The distinction that matters: this class is **checked before** an expensive
operation and raises, rather than reporting afterwards. Every limit is enforced
in Python, so no prompt, plan or model output can raise a ceiling — an LLM
cannot talk its way past a budget it cannot see.

When a limit is hit the graph routes to the explicit ``budget_exhausted``
terminal state rather than failing silently or looping.
"""

import time
from dataclasses import dataclass
from typing import Any


class BudgetExceeded(RuntimeError):
    """Raised when a hard limit would be crossed."""

    def __init__(self, limit_name: str, limit: Any, attempted: Any) -> None:
        self.limit_name = limit_name
        self.limit = limit
        self.attempted = attempted
        super().__init__(
            f"execution budget exceeded: {limit_name} limit={limit} attempted={attempted}"
        )


@dataclass
class BudgetLimits:
    """Configurable ceilings. ``None`` disables an individual limit."""

    max_model_calls: int | None = 40
    max_tool_calls: int | None = 60
    max_input_tokens: int | None = 2_000_000
    max_output_tokens: int | None = 400_000
    max_estimated_cost_usd: float | None = 5.0
    max_wall_time_seconds: float | None = 900.0
    max_recovery_attempts: int = 3
    max_review_cycles: int = 2

    @classmethod
    def generous(cls) -> "BudgetLimits":
        """Limits high enough not to interfere, still finite."""
        return cls(
            max_model_calls=500,
            max_tool_calls=1000,
            max_input_tokens=50_000_000,
            max_output_tokens=10_000_000,
            max_estimated_cost_usd=1000.0,
            max_wall_time_seconds=7200.0,
        )


@dataclass
class BudgetSnapshot:
    """Remaining headroom, safe to attach to a trace."""

    model_calls_used: int
    tool_calls_used: int
    input_tokens_used: int
    output_tokens_used: int
    cost_used_usd: float
    wall_time_used_seconds: float
    model_calls_remaining: int | None
    tool_calls_remaining: int | None
    cost_remaining_usd: float | None
    wall_time_remaining_seconds: float | None
    exhausted: bool
    exhausted_reason: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "model_calls_used": self.model_calls_used,
            "tool_calls_used": self.tool_calls_used,
            "input_tokens_used": self.input_tokens_used,
            "output_tokens_used": self.output_tokens_used,
            "cost_used_usd": round(self.cost_used_usd, 6),
            "wall_time_used_seconds": round(self.wall_time_used_seconds, 3),
            "model_calls_remaining": self.model_calls_remaining,
            "tool_calls_remaining": self.tool_calls_remaining,
            "cost_remaining_usd": (
                None if self.cost_remaining_usd is None else round(self.cost_remaining_usd, 6)
            ),
            "wall_time_remaining_seconds": (
                None
                if self.wall_time_remaining_seconds is None
                else round(self.wall_time_remaining_seconds, 3)
            ),
            "exhausted": self.exhausted,
            "exhausted_reason": self.exhausted_reason,
        }


class ExecutionBudget:
    """Tracks and enforces resource consumption for one run."""

    def __init__(self, limits: BudgetLimits | None = None, *, clock: Any = None) -> None:
        self.limits = limits or BudgetLimits()
        self._clock = clock or time.monotonic
        self.started_at = self._clock()
        self.model_calls = 0
        self.tool_calls = 0
        self.input_tokens = 0
        self.output_tokens = 0
        self.cost_usd = 0.0
        self.exhausted_reason: str | None = None
        self.violations: list[str] = []

    # -- time --------------------------------------------------------------
    def elapsed(self) -> float:
        return self._clock() - self.started_at

    # -- checks (call BEFORE the operation) --------------------------------
    def check_wall_time(self) -> None:
        limit = self.limits.max_wall_time_seconds
        if limit is not None and self.elapsed() >= limit:
            self._fail("max_wall_time_seconds", limit, round(self.elapsed(), 3))

    def check_model_call(self, *, count: int = 1) -> None:
        self.check_wall_time()
        limit = self.limits.max_model_calls
        if limit is not None and self.model_calls + count > limit:
            self._fail("max_model_calls", limit, self.model_calls + count)
        self.check_cost()

    def check_tool_call(self, *, count: int = 1) -> None:
        self.check_wall_time()
        limit = self.limits.max_tool_calls
        if limit is not None and self.tool_calls + count > limit:
            self._fail("max_tool_calls", limit, self.tool_calls + count)

    def check_cost(self, *, additional: float = 0.0) -> None:
        limit = self.limits.max_estimated_cost_usd
        if limit is not None and self.cost_usd + additional > limit:
            self._fail("max_estimated_cost_usd", limit, round(self.cost_usd + additional, 6))

    def check_tokens(self) -> None:
        if (
            self.limits.max_input_tokens is not None
            and self.input_tokens > self.limits.max_input_tokens
        ):
            self._fail("max_input_tokens", self.limits.max_input_tokens, self.input_tokens)
        if (
            self.limits.max_output_tokens is not None
            and self.output_tokens > self.limits.max_output_tokens
        ):
            self._fail("max_output_tokens", self.limits.max_output_tokens, self.output_tokens)

    def check_recovery(self, attempts_so_far: int) -> None:
        if attempts_so_far >= self.limits.max_recovery_attempts:
            self._fail("max_recovery_attempts", self.limits.max_recovery_attempts, attempts_so_far)

    def _fail(self, name: str, limit: Any, attempted: Any) -> None:
        message = f"{name} limit={limit} attempted={attempted}"
        if message not in self.violations:
            self.violations.append(message)
        self.exhausted_reason = message
        raise BudgetExceeded(name, limit, attempted)

    # -- consumption (call AFTER the operation) ----------------------------
    def consume_model_call(
        self, *, input_tokens: int = 0, output_tokens: int = 0, cost_usd: float = 0.0
    ) -> None:
        self.model_calls += 1
        self.input_tokens += max(0, input_tokens)
        self.output_tokens += max(0, output_tokens)
        self.cost_usd += max(0.0, cost_usd)

    def consume_tool_call(self, *, count: int = 1) -> None:
        self.tool_calls += max(0, count)

    def sync_from_ledger(self, ledger: Any) -> None:
        """Adopt authoritative token/cost totals from the model usage ledger.

        The ledger is the source of truth for spend; the budget mirrors it so
        cost ceilings reflect what was actually recorded.
        """
        try:
            self.input_tokens = int(ledger.total_input_tokens)
            self.output_tokens = int(ledger.total_output_tokens)
            self.cost_usd = float(ledger.total_cost_usd)
        except Exception:
            return

    # -- reporting ---------------------------------------------------------
    def would_exceed(self, kind: str) -> bool:
        """Non-raising predicate, for routing decisions."""
        try:
            if kind == "model":
                self.check_model_call()
            elif kind == "tool":
                self.check_tool_call()
            elif kind == "time":
                self.check_wall_time()
            elif kind == "cost":
                self.check_cost()
            else:
                return False
        except BudgetExceeded:
            return True
        return False

    @property
    def is_exhausted(self) -> bool:
        return self.exhausted_reason is not None

    def _remaining(self, limit: int | None, used: int) -> int | None:
        return None if limit is None else max(0, limit - used)

    def snapshot(self) -> BudgetSnapshot:
        limits = self.limits
        return BudgetSnapshot(
            model_calls_used=self.model_calls,
            tool_calls_used=self.tool_calls,
            input_tokens_used=self.input_tokens,
            output_tokens_used=self.output_tokens,
            cost_used_usd=self.cost_usd,
            wall_time_used_seconds=self.elapsed(),
            model_calls_remaining=self._remaining(limits.max_model_calls, self.model_calls),
            tool_calls_remaining=self._remaining(limits.max_tool_calls, self.tool_calls),
            cost_remaining_usd=(
                None
                if limits.max_estimated_cost_usd is None
                else max(0.0, limits.max_estimated_cost_usd - self.cost_usd)
            ),
            wall_time_remaining_seconds=(
                None
                if limits.max_wall_time_seconds is None
                else max(0.0, limits.max_wall_time_seconds - self.elapsed())
            ),
            exhausted=self.is_exhausted,
            exhausted_reason=self.exhausted_reason,
        )
