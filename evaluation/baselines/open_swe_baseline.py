"""Real Open SWE baseline adapter.

Phase 23 remediation of audit row 1.

The previous ``A_baseline`` variant was a *stripped-down SWE-Forge graph*. That
is a legitimate ablation — it isolates SWE-Forge's own components — but it is
**not** an Open SWE baseline, and describing it as "the fixed single-agent
comparison" overstated it. Nothing in `evaluation/` invoked upstream code.

This adapter invokes the genuine upstream execution path:

    agent.server.get_agent()  ->  deepagents.create_deep_agent(...)  ->  ainvoke

It deliberately does **not** reimplement Open SWE. If upstream cannot execute
in this environment, the run is reported ``unavailable`` with the precise
missing dependency or credential. Nothing is simulated, and no baseline number
is fabricated.

Why upstream usually cannot run here
------------------------------------
Open SWE's agent construction imports the full server stack (``fastapi``,
``deepagents``, provider SDKs) and expects a GitHub App installation plus a
sandbox provider (Daytona/Modal/E2B/Runloop). A meaningful comparison also
needs the same model, which needs provider credentials. The adapter probes each
requirement separately so the report can say exactly which one is absent rather
than "it failed".
"""

import asyncio
import importlib.util
import os
import time
from dataclasses import dataclass, field
from typing import Any

#: Python modules upstream's agent construction requires at import time.
REQUIRED_MODULES = ("fastapi", "deepagents", "langchain", "langgraph")

#: Environment variables a real upstream run needs.
REQUIRED_ENV_ANY_MODEL = ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GOOGLE_API_KEY")
REQUIRED_ENV_SANDBOX_ANY = (
    "DAYTONA_API_KEY",
    "MODAL_TOKEN_ID",
    "E2B_API_KEY",
    "RUNLOOP_API_KEY",
)


@dataclass
class PreflightResult:
    """Exactly what is missing, so 'unavailable' is actionable."""

    modules_present: dict[str, bool] = field(default_factory=dict)
    model_credentials_present: bool = False
    sandbox_credentials_present: bool = False
    upstream_symbol_importable: bool = False
    import_error: str | None = None

    @property
    def missing_modules(self) -> list[str]:
        return sorted(name for name, present in self.modules_present.items() if not present)

    @property
    def can_run(self) -> bool:
        return (
            not self.missing_modules
            and self.upstream_symbol_importable
            and self.model_credentials_present
            and self.sandbox_credentials_present
        )

    def reason(self) -> str:
        parts: list[str] = []
        if self.missing_modules:
            parts.append(f"missing Python modules: {', '.join(self.missing_modules)}")
        if not self.upstream_symbol_importable:
            parts.append(
                "upstream agent factory not importable"
                + (f" ({self.import_error})" if self.import_error else "")
            )
        if not self.model_credentials_present:
            parts.append(
                "no model provider credential set (" + " / ".join(REQUIRED_ENV_ANY_MODEL) + ")"
            )
        if not self.sandbox_credentials_present:
            parts.append(
                "no sandbox provider credential set (" + " / ".join(REQUIRED_ENV_SANDBOX_ANY) + ")"
            )
        return "; ".join(parts) or "all preflight checks passed"

    def to_dict(self) -> dict[str, Any]:
        return {
            "modules_present": self.modules_present,
            "missing_modules": self.missing_modules,
            "upstream_symbol_importable": self.upstream_symbol_importable,
            "import_error": self.import_error,
            "model_credentials_present": self.model_credentials_present,
            "sandbox_credentials_present": self.sandbox_credentials_present,
            "can_run": self.can_run,
            "reason": self.reason(),
        }


def preflight(env: dict[str, str] | None = None) -> PreflightResult:
    """Probe every requirement for a genuine upstream run, without running it."""
    environ = env if env is not None else dict(os.environ)
    result = PreflightResult()

    for name in REQUIRED_MODULES:
        try:
            result.modules_present[name] = importlib.util.find_spec(name) is not None
        except (ImportError, ValueError):
            result.modules_present[name] = False

    # Probe the real upstream symbol, without importing when deps are absent
    # (the import itself would raise ModuleNotFoundError on fastapi).
    if not result.missing_modules:
        try:
            from agent.server import get_agent  # noqa: F401  (probe only)

            result.upstream_symbol_importable = True
        except Exception as exc:
            result.upstream_symbol_importable = False
            result.import_error = f"{type(exc).__name__}: {exc}"
    else:
        result.upstream_symbol_importable = False
        result.import_error = "skipped: prerequisite modules missing"

    result.model_credentials_present = any(environ.get(k) for k in REQUIRED_ENV_ANY_MODEL)
    result.sandbox_credentials_present = any(environ.get(k) for k in REQUIRED_ENV_SANDBOX_ANY)
    return result


@dataclass
class BaselineRunResult:
    """Outcome of an attempted upstream baseline run."""

    available: bool
    status: str = "unavailable"
    unavailable_reason: str | None = None
    preflight: dict[str, Any] = field(default_factory=dict)
    metrics: dict[str, Any] = field(default_factory=dict)
    upstream_invoked: bool = False
    wall_time_seconds: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "available": self.available,
            "status": self.status,
            "unavailable_reason": self.unavailable_reason,
            "preflight": self.preflight,
            "metrics": self.metrics,
            "upstream_invoked": self.upstream_invoked,
            "wall_time_seconds": round(self.wall_time_seconds, 4),
        }


class OpenSWEBaseline:
    """Invokes the actual upstream Open SWE agent, or reports why it cannot.

    ``agent_factory`` is injectable purely so tests can prove the adapter
    really calls the upstream entry point (the test passes a recording double).
    In production it resolves ``agent.server.get_agent``, and nothing else.
    """

    name = "open_swe_upstream"

    def __init__(
        self,
        *,
        env: dict[str, str] | None = None,
        agent_factory: Any | None = None,
        timeout_seconds: float = 900.0,
    ) -> None:
        self._env = env if env is not None else dict(os.environ)
        self._agent_factory = agent_factory
        self.timeout_seconds = timeout_seconds
        self.invocations: list[dict[str, Any]] = []

    # -- upstream resolution ----------------------------------------------
    def resolve_agent_factory(self) -> Any:
        """Return upstream's real agent factory.

        Imported lazily and by its true path so this adapter cannot be mistaken
        for a reimplementation: if ``agent.server.get_agent`` disappears
        upstream, this raises rather than silently substituting our own graph.
        """
        if self._agent_factory is not None:
            return self._agent_factory
        from agent.server import get_agent

        return get_agent

    # -- execution ---------------------------------------------------------
    def run(
        self,
        *,
        task: str,
        repo_root: str,
        repository: str = "",
        model: str | None = None,
        timeout_seconds: float | None = None,
    ) -> BaselineRunResult:
        """Attempt a genuine upstream run for the given task."""
        check = preflight(self._env)
        result = BaselineRunResult(available=False, preflight=check.to_dict())

        if self._agent_factory is None and not check.can_run:
            result.unavailable_reason = check.reason()
            result.status = "unavailable"
            return result

        started = time.perf_counter()
        try:
            factory = self.resolve_agent_factory()
            agent = factory() if callable(factory) else factory
            payload = {
                "messages": [{"role": "user", "content": task}],
                "repository": repository or repo_root,
            }
            config: dict[str, Any] = {
                "configurable": {
                    "repo_root": repo_root,
                    **({"model": model} if model else {}),
                },
                "recursion_limit": 60,
            }
            self.invocations.append({"factory": getattr(factory, "__name__", str(factory))})

            invoke = getattr(agent, "ainvoke", None)
            if callable(invoke):
                final = asyncio.run(
                    asyncio.wait_for(
                        invoke(payload, config=config),
                        timeout=timeout_seconds or self.timeout_seconds,
                    )
                )
            else:
                final = agent.invoke(payload, config=config)

            result.upstream_invoked = True
            result.available = True
            result.status = "completed"
            result.metrics = self._extract_metrics(final)
        except Exception as exc:
            result.available = False
            result.status = "unavailable"
            result.unavailable_reason = f"upstream invocation failed: {type(exc).__name__}: {exc}"[
                :500
            ]
        finally:
            result.wall_time_seconds = time.perf_counter() - started
        return result

    @staticmethod
    def _extract_metrics(final: Any) -> dict[str, Any]:
        """Best-effort metric extraction from an upstream result.

        Upstream returns a deep-agent state (message list), not SWE-Forge's
        typed state, so only what is genuinely observable is reported. Fields
        SWE-Forge measures but upstream does not expose are ``None``, never 0 —
        an absent measurement is not a zero measurement.
        """
        messages = []
        if isinstance(final, dict):
            messages = final.get("messages") or []
        model_calls = sum(
            1
            for m in messages
            if getattr(m, "type", None) == "ai"
            or (isinstance(m, dict) and m.get("role") == "assistant")
        )
        tool_calls = 0
        for message in messages:
            calls = getattr(message, "tool_calls", None)
            if calls is None and isinstance(message, dict):
                calls = message.get("tool_calls")
            tool_calls += len(calls or [])
        return {
            "message_count": len(messages),
            "model_calls": model_calls or None,
            "tool_calls": tool_calls or None,
            # Upstream has no structured verification result, bounded recovery
            # counter, review gate or risk gate to read.
            "verification_passed": None,
            "first_attempt_success": None,
            "recovery_attempts": None,
            "recovery_success": None,
            "review_rejections": None,
            "security_interventions": None,
            "human_interventions": None,
            "input_tokens": None,
            "output_tokens": None,
            "estimated_cost_usd": None,
        }


def describe_baseline_availability(env: dict[str, str] | None = None) -> dict[str, Any]:
    """Report, without running anything, whether the baseline can execute."""
    check = preflight(env)
    return {
        "baseline": OpenSWEBaseline.name,
        "can_run": check.can_run,
        "reason": check.reason(),
        "details": check.to_dict(),
    }
