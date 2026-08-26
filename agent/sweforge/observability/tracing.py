"""LangSmith observability, optional by construction.

SWE-Forge must run identically with or without LangSmith credentials, so all
tracing goes through this module. When ``LANGSMITH_TRACING`` (or the legacy
``LANGCHAIN_TRACING_V2``) is enabled and an API key is present, runs are
grouped under a single root so a trace shows the real control flow:

    sweforge:full
      task_intake -> repository_analysis -> planning -> implementation
      -> verification -> failure_analysis -> recovery -> verification
      -> independent_review -> security_analysis -> risk_gate -> finalization

Because the graph is an explicit ``StateGraph``, each node is its own span with
its own inputs and outputs — which is precisely the diagnostic advantage over a
single opaque agent loop.

No credential is ever read into a log, echoed, or committed.
"""

import contextlib
import os
from collections.abc import Iterator
from typing import Any

TRACING_ENV_VARS = ("LANGSMITH_TRACING", "LANGCHAIN_TRACING_V2")
API_KEY_ENV_VARS = ("LANGSMITH_API_KEY", "LANGCHAIN_API_KEY")


def tracing_enabled(env: dict[str, str] | None = None) -> bool:
    """True only when tracing is switched on AND a key is configured."""
    environ = env if env is not None else os.environ
    switched_on = any(
        str(environ.get(var, "")).strip().lower() in {"1", "true", "yes"}
        for var in TRACING_ENV_VARS
    )
    has_key = any(environ.get(var) for var in API_KEY_ENV_VARS)
    return switched_on and has_key


def project_name(env: dict[str, str] | None = None) -> str:
    environ = env if env is not None else os.environ
    return environ.get("LANGSMITH_PROJECT") or environ.get("LANGCHAIN_PROJECT") or "sweforge"


@contextlib.contextmanager
def trace_run(*, name: str, metadata: dict[str, Any] | None = None) -> Iterator[None]:
    """Group a run under one LangSmith root, or do nothing.

    Any tracing failure is swallowed: observability must never be able to fail
    a software-engineering task.
    """
    if not tracing_enabled():
        yield
        return
    try:
        from langsmith import tracing_context

        with tracing_context(enabled=True, project_name=project_name(), metadata=metadata or {}):
            yield
        return
    except Exception:
        # Older/newer langsmith versions, network problems, bad config.
        yield


@contextlib.contextmanager
def trace_node(
    *,
    node: str,
    task_id: str | None = None,
    agent: str | None = None,
    model: str | None = None,
    tier: str | None = None,
    attempt: int | None = None,
    recovery_attempt: int | None = None,
    tool: str | None = None,
    budget_remaining: dict[str, Any] | None = None,
    risk_score: int | None = None,
    final_status: str | None = None,
    **extra: Any,
) -> Iterator[None]:
    """Attach node-level metadata to the active trace.

    Because the SWE-Forge workflow is an explicit ``StateGraph``, each node is
    already its own span; this adds the SWE-Forge-specific dimensions needed to
    answer "why did this run behave that way" from a trace alone: which agent
    ran, on which model tier, at which recovery attempt, with what budget left.

    Never emits a credential: only model *identifiers*, never keys, and budget
    *headroom*, never configuration secrets.
    """
    if not tracing_enabled():
        yield
        return
    payload = node_metadata(
        node=node,
        task_id=task_id,
        agent=agent,
        model=model,
        tier=tier,
        attempt=attempt,
        recovery_attempt=recovery_attempt,
        tool=tool,
        budget_remaining=budget_remaining,
        risk_score=risk_score,
        final_status=final_status,
        **extra,
    )
    try:
        from langsmith import tracing_context

        with tracing_context(enabled=True, project_name=project_name(), metadata=payload):
            yield
        return
    except Exception:
        yield


def node_metadata(**fields: Any) -> dict[str, Any]:
    """Namespaced, secret-free metadata dict. Pure function, always testable."""
    payload: dict[str, Any] = {}
    for key, value in fields.items():
        if value is None:
            continue
        if _looks_secret(key):
            continue
        payload[f"sweforge.{key}"] = value
    return payload


_SECRET_KEY_HINTS = ("key", "token", "secret", "password", "credential", "authorization")


def _looks_secret(name: str) -> bool:
    lowered = name.lower()
    # `api_key_configured` style booleans are fine; raw key-like fields are not.
    return any(hint in lowered for hint in _SECRET_KEY_HINTS)


def run_metadata(variant: str, repository: str, **extra: Any) -> dict[str, Any]:
    """Standard metadata attached to every traced run."""
    payload: dict[str, Any] = {
        "sweforge.variant": variant,
        "sweforge.repository": repository,
    }
    payload.update({f"sweforge.{k}": v for k, v in extra.items()})
    return payload


def describe_configuration(env: dict[str, str] | None = None) -> dict[str, Any]:
    """Non-secret view of the tracing configuration, safe to print or log."""
    environ = env if env is not None else os.environ
    return {
        "enabled": tracing_enabled(environ),
        "project": project_name(environ),
        "api_key_configured": any(bool(environ.get(v)) for v in API_KEY_ENV_VARS),
        "endpoint": environ.get("LANGSMITH_ENDPOINT", "default"),
    }
