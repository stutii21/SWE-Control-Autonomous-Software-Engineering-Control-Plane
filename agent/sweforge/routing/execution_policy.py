"""Model execution policy: retry, then fallback to a different model.

Phase 23 remediation of audit rows 7 and 8. Tier *escalation* (giving a role a
stronger model after repeated failures across a run) was previously described
as fallback. It is not: escalation happens between operations, while fallback
must happen *within* one operation when the chosen model fails.

Order of response to a failing call:

1. **Retry the same model** while the error is retryable (timeout, rate limit,
   transient) and the retry bound allows it.
2. **Fall back to the next model** in an ordered chain — a different tier, so a
   provider-wide outage or a context-length failure has a chance of succeeding
   elsewhere.
3. **Give up** and raise, letting the graph route to an explicit terminal state.

Every attempt is recorded separately. A fallback is *not* counted as the same
call, because pretending two provider round-trips were one would understate
both cost and latency.
"""

import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from agent.sweforge.budget import BudgetExceeded, ExecutionBudget
from agent.sweforge.routing.model_router import ModelSpec

#: Errors worth retrying on the same model.
RETRYABLE_PATTERN = re.compile(
    r"\btimeout\b|timed out|rate.?limit|429|50[0234]|overloaded|"
    r"temporarily unavailable|connection (reset|error|aborted)|try again",
    re.IGNORECASE,
)
#: Errors where a different model may still succeed.
FALLBACK_PATTERN = re.compile(
    r"context.?length|too many tokens|maximum context|model not found|"
    r"does not exist|unsupported|not available|deprecated|overloaded|50[023]",
    re.IGNORECASE,
)


@dataclass
class ModelAttempt:
    """One provider round-trip."""

    model: str
    tier: str
    attempt: int
    status: str  # "success" | "timeout" | "rate_limit" | "transient" | "error"
    latency_seconds: float = 0.0
    error: str | None = None
    was_fallback: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "model": self.model,
            "tier": self.tier,
            "attempt": self.attempt,
            "status": self.status,
            "latency_seconds": round(self.latency_seconds, 4),
            "error": self.error,
            "was_fallback": self.was_fallback,
        }


class AllModelsFailed(RuntimeError):
    """Raised when the primary and every fallback failed."""

    def __init__(self, attempts: list[ModelAttempt]) -> None:
        self.attempts = attempts
        detail = "; ".join(f"{a.model}#{a.attempt}={a.status}" for a in attempts)
        super().__init__(f"all models failed after {len(attempts)} attempt(s): {detail}")


@dataclass
class ModelExecutionPolicy:
    """Retry-then-fallback execution for a single logical model operation."""

    max_retries_per_model: int = 2
    base_backoff_seconds: float = 0.0  # 0 in tests; set >0 in production
    timeout_seconds: float | None = 120.0
    attempts: list[ModelAttempt] = field(default_factory=list)

    @staticmethod
    def classify(exc: BaseException) -> str:
        text = f"{type(exc).__name__}: {exc}"
        if re.search(r"\btimeout\b|timed out", text, re.IGNORECASE):
            return "timeout"
        if re.search(r"rate.?limit|429", text, re.IGNORECASE):
            return "rate_limit"
        if RETRYABLE_PATTERN.search(text):
            return "transient"
        return "error"

    def _is_retryable(self, exc: BaseException) -> bool:
        return bool(RETRYABLE_PATTERN.search(f"{type(exc).__name__}: {exc}"))

    def _should_fallback(self, exc: BaseException) -> bool:
        text = f"{type(exc).__name__}: {exc}"
        # Fall back on model-specific failures, and also after exhausting
        # retries on a transient failure — a different model may be healthy.
        return bool(FALLBACK_PATTERN.search(text) or RETRYABLE_PATTERN.search(text))

    def execute(
        self,
        *,
        specs: list[ModelSpec],
        operation: Callable[[ModelSpec], Any],
        budget: ExecutionBudget | None = None,
    ) -> tuple[Any, list[ModelAttempt]]:
        """Run ``operation`` against ``specs`` in order, retrying each.

        ``specs[0]`` is the primary; the rest are fallbacks. Returns the result
        and the attempt log. Raises :class:`AllModelsFailed` if none succeed.
        """
        if not specs:
            raise ValueError("no model specs supplied")

        last_exc: BaseException | None = None
        for index, spec in enumerate(specs):
            attempt = 0
            while attempt < self.max_retries_per_model + 1:
                attempt += 1
                if budget is not None:
                    # A budget failure is not a model failure: propagate it so
                    # the graph routes to budget_exhausted rather than
                    # burning fallbacks.
                    budget.check_model_call()
                started = time.perf_counter()
                try:
                    result = operation(spec)
                except BudgetExceeded:
                    raise
                except Exception as exc:  # provider/transport failure
                    elapsed = time.perf_counter() - started
                    status = self.classify(exc)
                    self.attempts.append(
                        ModelAttempt(
                            model=spec.model_id,
                            tier=spec.tier,
                            attempt=attempt,
                            status=status,
                            latency_seconds=elapsed,
                            error=f"{type(exc).__name__}: {exc}"[:300],
                            was_fallback=index > 0,
                        )
                    )
                    last_exc = exc
                    if self._is_retryable(exc) and attempt <= self.max_retries_per_model:
                        if self.base_backoff_seconds:
                            time.sleep(self.base_backoff_seconds * (2 ** (attempt - 1)))
                        continue
                    break  # move to the next model, if any
                else:
                    self.attempts.append(
                        ModelAttempt(
                            model=spec.model_id,
                            tier=spec.tier,
                            attempt=attempt,
                            status="success",
                            latency_seconds=time.perf_counter() - started,
                            was_fallback=index > 0,
                        )
                    )
                    return result, self.attempts

            if last_exc is not None and not self._should_fallback(last_exc):
                # A deterministic failure (e.g. a bad prompt) will fail
                # identically elsewhere; do not spend a fallback on it.
                break

        raise AllModelsFailed(self.attempts)

    # -- reporting ---------------------------------------------------------
    @property
    def fallback_used(self) -> bool:
        return any(a.was_fallback for a in self.attempts)

    @property
    def retry_count(self) -> int:
        return sum(1 for a in self.attempts if a.attempt > 1)

    def summary(self) -> dict[str, Any]:
        return {
            "attempts": [a.to_dict() for a in self.attempts],
            "total_attempts": len(self.attempts),
            "fallback_used": self.fallback_used,
            "retry_count": self.retry_count,
            "succeeded": any(a.status == "success" for a in self.attempts),
        }


def fallback_chain(router: Any, role: str, complexity: str = "moderate") -> list[ModelSpec]:
    """Build primary + ordered fallback specs for a role.

    The chain crosses tiers deliberately: if the reasoning tier is unavailable
    or over its context limit, the balanced and fast tiers are genuinely
    different models and may still complete the operation.
    """
    primary = router.select(role, complexity=complexity).spec
    order = ["reasoning", "coding", "balanced", "fast"]
    seen = {primary.model_id}
    chain = [primary]
    start = order.index(primary.tier) if primary.tier in order else 0
    for tier in order[start + 1 :]:
        env_var, default = router.tier_model(tier)
        if default not in seen:
            seen.add(default)
            chain.append(
                ModelSpec(
                    role=role,
                    tier=tier,  # type: ignore[arg-type]
                    model_id=default,
                    reason=f"fallback tier '{tier}' for role '{role}'",
                )
            )
    return chain
