"""Local execution tracing, always available.

Phase 25 remediation of audit row 14. Before this, the only trace was LangSmith,
so with tracing disabled a run produced **no durable record of what happened**.
That is the wrong dependency direction: observability must never be something an
external SaaS can take away, and a post-mortem on a failed autonomous run cannot
require a vendor account.

So the local recorder is always on, and LangSmith becomes an optional *sink*:

    LangSmith OFF  ->  local traces.jsonl
    LangSmith ON   ->  local traces.jsonl + LangSmith spans

Events are written as JSON Lines because a run is an append-only sequence and
JSONL survives a crash mid-run — a partially written file is still readable up
to the last complete line, which is exactly when you most want the trace.

Redaction is applied at write time, not at read time, so a secret never reaches
disk in the first place.
"""

import json
import os
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

#: Keys whose *values* are never recorded, matched case-insensitively as
#: substrings so `anthropic_api_key`, `authToken`, `client_secret` all match.
SECRET_KEY_HINTS = (
    "key",
    "token",
    "secret",
    "password",
    "passwd",
    "credential",
    "authorization",
    "auth_header",
)

#: Values longer than this are summarised rather than stored: a tool payload can
#: contain whole file contents, which would bloat the trace and risk leaking
#: repository content into an artefact that gets shared.
MAX_VALUE_CHARS = 500

REDACTED = "<redacted>"


def _looks_secret(name: str) -> bool:
    lowered = str(name).lower()
    # `api_key_configured` style booleans are safe and useful; raw key fields are not.
    if lowered.endswith(("_configured", "_present", "_set")):
        return False
    return any(hint in lowered for hint in SECRET_KEY_HINTS)


def redact(value: Any, *, _depth: int = 0) -> Any:
    """Recursively redact secret-looking keys and truncate large values."""
    if _depth > 6:
        return "<max-depth>"
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, item in value.items():
            if _looks_secret(key):
                out[str(key)] = REDACTED
            else:
                out[str(key)] = redact(item, _depth=_depth + 1)
        return out
    if isinstance(value, (list, tuple)):
        return [redact(v, _depth=_depth + 1) for v in value[:50]]
    if isinstance(value, str):
        if len(value) > MAX_VALUE_CHARS:
            return f"{value[:MAX_VALUE_CHARS]}... [truncated, {len(value)} chars]"
        return value
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return str(value)[:MAX_VALUE_CHARS]


@dataclass
class TraceEvent:
    """One recorded step. Field set matches Phase 25 Part 18."""

    run_id: str
    seq: int
    timestamp: float
    event: str  # "node" | "agent" | "model" | "tool" | "mcp" | "state" | "final"
    task_id: str | None = None
    node: str | None = None
    agent: str | None = None
    model: str | None = None
    tier: str | None = None
    tool: str | None = None
    attempt: int | None = None
    recovery_attempt: int | None = None
    status: str | None = None
    duration_seconds: float | None = None
    budget: dict[str, Any] = field(default_factory=dict)
    risk_score: int | None = None
    security_findings: int | None = None
    final_status: str | None = None
    detail: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "run_id": self.run_id,
            "seq": self.seq,
            "timestamp": round(self.timestamp, 6),
            "event": self.event,
            "task_id": self.task_id,
            "node": self.node,
            "agent": self.agent,
            "model": self.model,
            "tier": self.tier,
            "tool": self.tool,
            "attempt": self.attempt,
            "recovery_attempt": self.recovery_attempt,
            "status": self.status,
            "duration_seconds": self.duration_seconds,
            "budget": self.budget,
            "risk_score": self.risk_score,
            "security_findings": self.security_findings,
            "final_status": self.final_status,
            "detail": self.detail,
        }
        return {k: v for k, v in payload.items() if v is not None and v != {}}


class TraceRecorder:
    """Append-only, thread-safe, redacting local trace.

    Thread safety matters because subtasks may execute concurrently
    (``subtask_workers > 1``); each recorder belongs to one run, so traces stay
    task-local and cannot cross-contaminate.
    """

    def __init__(
        self,
        *,
        run_id: str | None = None,
        task_id: str | None = None,
        path: str | Path | None = None,
        enabled: bool = True,
    ) -> None:
        self.run_id = run_id or uuid.uuid4().hex[:12]
        self.task_id = task_id
        self.enabled = enabled
        self.path = Path(path) if path else None
        self.events: list[TraceEvent] = []
        self._lock = threading.Lock()
        self._seq = 0
        self._started = time.perf_counter()
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)

    def record(self, event: str, **fields: Any) -> TraceEvent | None:
        """Record one event. Never raises: tracing must not fail a task."""
        if not self.enabled:
            return None
        try:
            detail = redact(fields.pop("detail", {}) or {})
            budget = redact(fields.pop("budget", {}) or {})
            with self._lock:
                self._seq += 1
                entry = TraceEvent(
                    run_id=self.run_id,
                    seq=self._seq,
                    timestamp=time.perf_counter() - self._started,
                    event=event,
                    task_id=fields.pop("task_id", self.task_id),
                    budget=budget,
                    detail=detail,
                    **{k: v for k, v in fields.items() if k in TraceEvent.__annotations__},
                )
                self.events.append(entry)
                if self.path:
                    with self.path.open("a", encoding="utf-8") as handle:
                        handle.write(json.dumps(entry.to_dict()) + "\n")
            return entry
        except Exception:
            return None

    # -- convenience -------------------------------------------------------
    def node(self, name: str, **fields: Any) -> None:
        self.record("node", node=name, **fields)

    def tool(self, name: str, *, status: str = "ok", **fields: Any) -> None:
        self.record("tool", tool=name, status=status, **fields)

    def model_call(self, model: str, *, tier: str | None = None, **fields: Any) -> None:
        self.record("model", model=model, tier=tier, **fields)

    def final(self, status: str, **fields: Any) -> None:
        self.record("final", final_status=status, **fields)

    # -- output ------------------------------------------------------------
    def to_jsonl(self) -> str:
        return "\n".join(json.dumps(e.to_dict()) for e in self.events)

    def write(self, path: str | Path) -> Path:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(self.to_jsonl() + ("\n" if self.events else ""), encoding="utf-8")
        return target

    def summary(self) -> dict[str, Any]:
        counts: dict[str, int] = {}
        for entry in self.events:
            counts[entry.event] = counts.get(entry.event, 0) + 1
        nodes = [e.node for e in self.events if e.event == "node" and e.node]
        tools = [e.tool for e in self.events if e.event == "tool" and e.tool]
        agents = sorted({e.agent for e in self.events if e.agent})
        final = next((e.final_status for e in reversed(self.events) if e.final_status), None)
        return {
            "run_id": self.run_id,
            "task_id": self.task_id,
            "event_counts": counts,
            "total_events": len(self.events),
            "node_sequence": nodes,
            "tool_sequence": tools,
            "agents": agents,
            "final_status": final,
            "wall_time_seconds": round(self.events[-1].timestamp if self.events else 0.0, 4),
        }


def default_trace_path(run_id: str, base: str | Path | None = None) -> Path:
    root = Path(base or os.environ.get("SWEFORGE_TRACE_DIR", ".sweforge/traces"))
    return root / f"{run_id}.jsonl"
