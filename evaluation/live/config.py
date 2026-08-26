"""Live-model evaluation configuration.

Phase 23 remediation of audit row 2. The scripted track isolates orchestration;
this track answers the question scripted models cannot: how the system behaves
with a real model choosing its own plans, edits and diagnoses.

No API key is hard-coded or read into any artefact. Configuration is entirely
environmental:

    SWEFORGE_EVAL_PROVIDER=anthropic
    SWEFORGE_EVAL_MODEL=claude-sonnet-4-5
    SWEFORGE_EVAL_MAX_COST_USD=2.00
    SWEFORGE_EVAL_TIMEOUT_SECONDS=600

If credentials are absent the run is reported UNAVAILABLE. That is a deliberate
design property, not a limitation to be worked around: a fabricated live result
would be worse than no result.
"""

import os
from dataclasses import dataclass, field
from typing import Any

PROVIDER_ENV_KEYS: dict[str, str] = {
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
    "google": "GOOGLE_API_KEY",
    "fireworks": "FIREWORKS_API_KEY",
}


@dataclass
class LiveEvalConfig:
    """Resolved live-evaluation settings, with no secret values retained."""

    provider: str = "anthropic"
    model: str = ""
    max_cost_usd: float = 2.0
    timeout_seconds: float = 600.0
    credential_present: bool = False
    missing: list[str] = field(default_factory=list)

    @property
    def model_id(self) -> str:
        """LangChain provider-prefixed id."""
        return f"{self.provider}:{self.model}" if self.model else ""

    @property
    def available(self) -> bool:
        return not self.missing

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "LiveEvalConfig":
        environ = env if env is not None else dict(os.environ)
        provider = (environ.get("SWEFORGE_EVAL_PROVIDER") or "anthropic").strip().lower()
        model = (environ.get("SWEFORGE_EVAL_MODEL") or "").strip()

        missing: list[str] = []
        if not model:
            missing.append("SWEFORGE_EVAL_MODEL is not set")
        if provider not in PROVIDER_ENV_KEYS:
            missing.append(
                f"unknown provider {provider!r}; supported: {', '.join(sorted(PROVIDER_ENV_KEYS))}"
            )
            credential_present = False
        else:
            key_name = PROVIDER_ENV_KEYS[provider]
            credential_present = bool(environ.get(key_name))
            if not credential_present:
                missing.append(f"{key_name} is not set")

        def _number(name: str, default: float) -> float:
            raw = environ.get(name)
            if not raw:
                return default
            try:
                return float(raw)
            except ValueError:
                missing.append(f"{name}={raw!r} is not a number")
                return default

        return cls(
            provider=provider,
            model=model,
            max_cost_usd=_number("SWEFORGE_EVAL_MAX_COST_USD", 2.0),
            timeout_seconds=_number("SWEFORGE_EVAL_TIMEOUT_SECONDS", 600.0),
            credential_present=credential_present,
            missing=missing,
        )

    def unavailable_reason(self) -> str:
        return "; ".join(self.missing) or "configuration complete"

    def to_dict(self) -> dict[str, Any]:
        """Safe to log: reports credential *presence*, never a value."""
        return {
            "provider": self.provider,
            "model": self.model,
            "model_id": self.model_id,
            "max_cost_usd": self.max_cost_usd,
            "timeout_seconds": self.timeout_seconds,
            "credential_present": self.credential_present,
            "available": self.available,
            "unavailable_reason": self.unavailable_reason(),
        }

    def budget_limits(self) -> Any:
        """Cost ceiling for a live run, so a benchmark cannot overspend."""
        from agent.sweforge.budget import BudgetLimits

        return BudgetLimits(
            max_estimated_cost_usd=self.max_cost_usd,
            max_wall_time_seconds=self.timeout_seconds,
        )


def describe_live_availability(env: dict[str, str] | None = None) -> dict[str, Any]:
    config = LiveEvalConfig.from_env(env)
    return {
        "track": "C_live_model",
        "available": config.available,
        "reason": config.unavailable_reason(),
        "config": config.to_dict(),
    }
