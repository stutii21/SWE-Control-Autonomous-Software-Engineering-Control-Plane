"""Adaptive model routing.

Different steps in an autonomous SWE loop have genuinely different
requirements. Ranking candidate files or classifying a stack trace is cheap
pattern work; deciding a multi-file refactor plan or judging whether a diff
introduces an auth bypass is not. Upstream Open SWE resolves one agent model
(plus a fallback) per run; SWE-Forge instead selects a *tier* per node and
records what that choice cost.

Design constraints:

* No model id is hard-coded. Tiers map to env vars with documented defaults.
* No API key is ever read or logged by this module.
* Every call is recorded in a ledger so the evaluation harness can report
  model calls, latency and estimated cost per workflow variant.

Honesty note: routing is *measured*, not assumed. Nothing here claims that
routing improves task success. The evaluation harness reports call counts and
cost; docs/EVALUATION.md states plainly what was and was not measured.
"""

import os
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Literal

from agent.sweforge.schemas import ModelCallRecord

Tier = Literal["fast", "balanced", "coding", "reasoning"]

#: Environment variable per tier, with a conservative default.
TIER_ENV_VARS: dict[Tier, tuple[str, str]] = {
    "fast": ("SWEFORGE_MODEL_FAST", "anthropic:claude-haiku-4-5-20251001"),
    "balanced": ("SWEFORGE_MODEL_BALANCED", "anthropic:claude-sonnet-4-5"),
    "coding": ("SWEFORGE_MODEL_CODING", "anthropic:claude-sonnet-4-5"),
    "reasoning": ("SWEFORGE_MODEL_REASONING", "anthropic:claude-opus-4-1"),
}

#: USD per 1M tokens (input, output). Overridable via SWEFORGE_PRICE_<TIER>.
DEFAULT_PRICES: dict[Tier, tuple[float, float]] = {
    "fast": (1.00, 5.00),
    "balanced": (3.00, 15.00),
    "coding": (3.00, 15.00),
    "reasoning": (15.00, 75.00),
}

#: Base policy: which tier each logical role wants.
ROLE_TIER: dict[str, Tier] = {
    "repository_analysis": "fast",
    "complexity_analysis": "fast",
    "planning": "reasoning",
    "agent_selection": "fast",
    "implementation": "coding",
    "test_authoring": "coding",
    "failure_analysis": "balanced",
    "recovery": "coding",
    "review": "reasoning",
    "security_analysis": "reasoning",
    "documentation": "fast",
    "summarisation": "fast",
}

_ESCALATE: dict[Tier, Tier] = {
    "fast": "balanced",
    "balanced": "reasoning",
    "coding": "reasoning",
    "reasoning": "reasoning",
}
_DEESCALATE: dict[Tier, Tier] = {
    "reasoning": "balanced",
    "coding": "balanced",
    "balanced": "fast",
    "fast": "fast",
}


@dataclass
class ModelSpec:
    role: str
    tier: Tier
    model_id: str
    reason: str

    @property
    def provider(self) -> str:
        return self.model_id.split(":", 1)[0] if ":" in self.model_id else "unknown"


@dataclass
class RoutingDecision:
    spec: ModelSpec
    considered: list[str] = field(default_factory=list)


class ModelUsageLedger:
    """Accumulates per-call telemetry for the evaluation harness."""

    def __init__(self) -> None:
        self.records: list[ModelCallRecord] = []

    def record(self, record: ModelCallRecord) -> ModelCallRecord:
        self.records.append(record)
        return record

    @property
    def total_calls(self) -> int:
        return len(self.records)

    @property
    def total_cost_usd(self) -> float:
        return round(sum(r.estimated_cost_usd for r in self.records), 8)

    @property
    def total_input_tokens(self) -> int:
        return sum(r.input_tokens or 0 for r in self.records)

    @property
    def total_output_tokens(self) -> int:
        return sum(r.output_tokens or 0 for r in self.records)

    def by_tier(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for record in self.records:
            out[record.tier] = out.get(record.tier, 0) + 1
        return out

    def summary(self) -> dict[str, Any]:
        return {
            "calls": self.total_calls,
            "by_tier": self.by_tier(),
            "input_tokens": self.total_input_tokens,
            "output_tokens": self.total_output_tokens,
            "estimated_cost_usd": self.total_cost_usd,
            "failures": sum(1 for r in self.records if not r.ok),
        }


class ModelRouter:
    """Selects a model tier per role and records usage.

    ``history`` lets the router de-escalate roles that have been reliably
    succeeding and escalate ones that keep failing, using observed outcomes
    from the ledger rather than a static table.
    """

    def __init__(
        self,
        *,
        env: dict[str, str] | None = None,
        ledger: ModelUsageLedger | None = None,
        model_factory: Any | None = None,
    ) -> None:
        self._env = env if env is not None else dict(os.environ)
        self.ledger = ledger or ModelUsageLedger()
        self._model_factory = model_factory
        self._role_failures: dict[str, int] = {}
        self._role_successes: dict[str, int] = {}

    # -- policy ------------------------------------------------------------
    def resolve_tier(
        self,
        role: str,
        *,
        complexity: str = "moderate",
        latency_sensitive: bool = False,
    ) -> tuple[Tier, str]:
        base: Tier = ROLE_TIER.get(role, "balanced")
        reason = f"role '{role}' base tier '{base}'"

        # Complexity adjusts the plan/implement/review path only; cheap
        # bookkeeping roles stay cheap regardless of task size.
        if role in {"planning", "implementation", "review", "recovery", "security_analysis"}:
            if complexity == "trivial":
                base = _DEESCALATE[base]
                reason += "; de-escalated (trivial task)"
            elif complexity == "complex":
                base = _ESCALATE[base]
                reason += "; escalated (complex task)"

        # Repeated failures on a role earn a stronger model.
        if self._role_failures.get(role, 0) >= 2:
            escalated = _ESCALATE[base]
            if escalated != base:
                base = escalated
                reason += f"; escalated after {self._role_failures[role]} failures"

        if latency_sensitive and base == "reasoning":
            base = "balanced"
            reason += "; capped at balanced (latency sensitive)"

        return base, reason

    def select(
        self,
        role: str,
        *,
        complexity: str = "moderate",
        latency_sensitive: bool = False,
    ) -> RoutingDecision:
        tier, reason = self.resolve_tier(
            role, complexity=complexity, latency_sensitive=latency_sensitive
        )
        env_var, default = TIER_ENV_VARS[tier]
        model_id = self._env.get(env_var, default)
        return RoutingDecision(
            spec=ModelSpec(role=role, tier=tier, model_id=model_id, reason=reason),
            considered=sorted(TIER_ENV_VARS),
        )

    def tier_model(self, tier: str) -> tuple[str, str]:
        """Resolve a tier to (env_var, model_id). Used to build fallback chains."""
        env_var, default = TIER_ENV_VARS[tier]  # type: ignore[index]
        return env_var, self._env.get(env_var, default)

    # -- instantiation -----------------------------------------------------
    def build_model(self, spec: ModelSpec, **kwargs: Any) -> Any:
        """Instantiate a chat model for a spec.

        A ``model_factory`` may be injected (the evaluation harness and unit
        tests inject a deterministic scripted model). Otherwise LangChain's
        ``init_chat_model`` resolves the provider-prefixed id, which requires
        provider credentials to be present in the environment.
        """
        if self._model_factory is not None:
            return self._model_factory(spec, **kwargs)
        from langchain.chat_models import init_chat_model  # local import: optional dep path

        return init_chat_model(spec.model_id, **kwargs)

    # -- telemetry ---------------------------------------------------------
    def price_for(self, tier: Tier) -> tuple[float, float]:
        raw = self._env.get(f"SWEFORGE_PRICE_{tier.upper()}")
        if raw:
            try:
                inp, out = (float(x) for x in raw.split(","))
                return inp, out
            except ValueError:
                pass
        return DEFAULT_PRICES[tier]

    def estimate_cost(self, tier: Tier, input_tokens: int, output_tokens: int) -> float:
        price_in, price_out = self.price_for(tier)
        return round(
            (input_tokens / 1_000_000) * price_in + (output_tokens / 1_000_000) * price_out, 8
        )

    @contextmanager
    def track(self, node: str, spec: ModelSpec):
        """Time a model call and append a ledger record.

        Yields a mutable dict; set ``input_tokens`` / ``output_tokens`` on it
        when the provider reports usage metadata.
        """
        usage: dict[str, Any] = {"input_tokens": None, "output_tokens": None}
        started = time.perf_counter()
        ok, error = True, None
        try:
            yield usage
        except Exception as exc:
            ok, error = False, f"{type(exc).__name__}: {exc}"
            self._role_failures[spec.role] = self._role_failures.get(spec.role, 0) + 1
            raise
        finally:
            elapsed = time.perf_counter() - started
            if ok:
                self._role_successes[spec.role] = self._role_successes.get(spec.role, 0) + 1
            inp = usage.get("input_tokens") or 0
            out = usage.get("output_tokens") or 0
            self.ledger.record(
                ModelCallRecord(
                    node=node,
                    role=spec.role,
                    tier=spec.tier,
                    model=spec.model_id,
                    latency_seconds=round(elapsed, 6),
                    input_tokens=usage.get("input_tokens"),
                    output_tokens=usage.get("output_tokens"),
                    estimated_cost_usd=self.estimate_cost(spec.tier, inp, out),
                    ok=ok,
                    error=error,
                )
            )


def extract_usage(response: Any) -> tuple[int | None, int | None]:
    """Best-effort token extraction across LangChain response shapes."""
    meta = getattr(response, "usage_metadata", None) or {}
    if isinstance(meta, dict) and meta:
        return meta.get("input_tokens"), meta.get("output_tokens")
    raw = getattr(response, "response_metadata", None) or {}
    usage = raw.get("usage") or raw.get("token_usage") or {}
    if isinstance(usage, dict) and usage:
        return (
            usage.get("input_tokens") or usage.get("prompt_tokens"),
            usage.get("output_tokens") or usage.get("completion_tokens"),
        )
    return None, None
