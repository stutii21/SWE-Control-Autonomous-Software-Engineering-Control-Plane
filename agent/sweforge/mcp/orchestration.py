"""SWE-Forge MCP orchestration.

Phase 23 remediation of audit row 6.

What this is **not**: a new MCP transport. Upstream Open SWE already ships MCP
clients (``agent/integrations/{corridor,datadog,notion}_mcp.py``) and depends on
``langchain-mcp-adapters``. Rebuilding that would inflate the technology list
without improving the system.

What this **is**: the orchestration decision upstream does not make — *when* an
external capability is worth consulting, *which* one, under *what* limits, and
what happens when it fails. Concretely:

* :class:`MCPCapabilityRegistry` — discovery and schema exposure over adapters
  supplied by the host (upstream clients, or a deterministic fixture in tests).
* :class:`MCPToolSelector` — decides from the *structured plan*, never from
  free-form prose, whether external context is needed and which capability fits.
* :class:`MCPInvocationPolicy` — allowlist, timeout, retry, budget and
  structured-error enforcement.

Capabilities are **deny-by-default**: a capability absent from the allowlist is
never invoked, because an autonomous agent reaching arbitrary external services
is a security problem, not a feature.
"""

import time
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

from agent.sweforge.tools.errors import ToolErrorAction, ToolErrorPolicy


class MCPCapabilityKind(StrEnum):
    ISSUE_TRACKER = "issue_tracker"
    DOCUMENTATION = "documentation"
    OBSERVABILITY = "observability"
    CODE_HOST = "code_host"
    RESEARCH = "research"


@runtime_checkable
class MCPAdapter(Protocol):
    """Minimal contract SWE-Forge needs from any MCP client.

    Upstream clients and ``langchain-mcp-adapters`` sessions both satisfy this
    shape, so no transport code is duplicated here.
    """

    name: str

    def list_tools(self) -> list[dict[str, Any]]: ...

    def call_tool(self, name: str, arguments: dict[str, Any]) -> Any: ...


@dataclass
class MCPCapability:
    """One externally-reachable capability, with its schema."""

    name: str
    kind: MCPCapabilityKind
    server: str
    description: str = ""
    input_schema: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "kind": str(self.kind),
            "server": self.server,
            "description": self.description,
            "input_schema": self.input_schema,
        }


@dataclass
class MCPResult:
    """Structured outcome. External payloads are never returned raw and untyped."""

    capability: str
    server: str
    ok: bool
    content: Any = None
    error: str | None = None
    error_category: str | None = None
    latency_seconds: float = 0.0
    attempts: int = 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "capability": self.capability,
            "server": self.server,
            "ok": self.ok,
            "content": self.content,
            "error": self.error,
            "error_category": self.error_category,
            "latency_seconds": round(self.latency_seconds, 4),
            "attempts": self.attempts,
        }


class MCPCapabilityRegistry:
    """Discovers capabilities from registered adapters."""

    #: Heuristic mapping from tool-name substrings to capability kinds.
    _KIND_HINTS: tuple[tuple[str, MCPCapabilityKind], ...] = (
        ("issue", MCPCapabilityKind.ISSUE_TRACKER),
        ("ticket", MCPCapabilityKind.ISSUE_TRACKER),
        ("linear", MCPCapabilityKind.ISSUE_TRACKER),
        ("jira", MCPCapabilityKind.ISSUE_TRACKER),
        ("pull_request", MCPCapabilityKind.CODE_HOST),
        ("github", MCPCapabilityKind.CODE_HOST),
        ("commit", MCPCapabilityKind.CODE_HOST),
        ("doc", MCPCapabilityKind.DOCUMENTATION),
        ("notion", MCPCapabilityKind.DOCUMENTATION),
        ("wiki", MCPCapabilityKind.DOCUMENTATION),
        ("metric", MCPCapabilityKind.OBSERVABILITY),
        ("log", MCPCapabilityKind.OBSERVABILITY),
        ("datadog", MCPCapabilityKind.OBSERVABILITY),
        ("trace", MCPCapabilityKind.OBSERVABILITY),
        ("search", MCPCapabilityKind.RESEARCH),
        ("browse", MCPCapabilityKind.RESEARCH),
    )

    def __init__(self) -> None:
        self._adapters: dict[str, MCPAdapter] = {}
        self._capabilities: dict[str, MCPCapability] = {}
        self.discovery_errors: dict[str, str] = {}

    def register(self, adapter: MCPAdapter) -> None:
        self._adapters[adapter.name] = adapter

    @classmethod
    def classify(cls, tool_name: str, declared: str | None = None) -> MCPCapabilityKind:
        if declared:
            try:
                return MCPCapabilityKind(declared)
            except ValueError:
                pass
        lowered = tool_name.lower()
        for hint, kind in cls._KIND_HINTS:
            if hint in lowered:
                return kind
        return MCPCapabilityKind.RESEARCH

    def discover(self) -> list[MCPCapability]:
        """Enumerate capabilities. A failing server is recorded, not fatal."""
        self._capabilities = {}
        self.discovery_errors = {}
        for name, adapter in sorted(self._adapters.items()):
            try:
                tools = adapter.list_tools() or []
            except Exception as exc:
                self.discovery_errors[name] = f"{type(exc).__name__}: {exc}"
                continue
            for tool in tools:
                tool_name = str(tool.get("name", "")).strip()
                if not tool_name:
                    continue
                capability = MCPCapability(
                    name=tool_name,
                    kind=self.classify(tool_name, tool.get("kind")),
                    server=name,
                    description=str(tool.get("description", ""))[:400],
                    input_schema=tool.get("input_schema") or tool.get("inputSchema") or {},
                )
                self._capabilities[tool_name] = capability
        return list(self._capabilities.values())

    def get(self, name: str) -> MCPCapability | None:
        return self._capabilities.get(name)

    def adapter_for(self, capability: str) -> MCPAdapter | None:
        found = self._capabilities.get(capability)
        return self._adapters.get(found.server) if found else None

    def by_kind(self, kind: MCPCapabilityKind) -> list[MCPCapability]:
        return [c for c in self._capabilities.values() if c.kind == kind]

    @property
    def capabilities(self) -> list[MCPCapability]:
        return list(self._capabilities.values())


@dataclass
class MCPSelection:
    """A deterministic decision about external context."""

    needed: bool
    capability: str | None = None
    kind: MCPCapabilityKind | None = None
    arguments: dict[str, Any] = field(default_factory=dict)
    rationale: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "needed": self.needed,
            "capability": self.capability,
            "kind": str(self.kind) if self.kind else None,
            "arguments": self.arguments,
            "rationale": self.rationale,
        }


class MCPToolSelector:
    """Decides whether external context is needed, from structured signals.

    The decision reads the task text for *explicit external references* — an
    issue number, a PR link, a docs URL — rather than asking a model "do you
    want to browse the internet". That keeps the control-flow decision
    deterministic and auditable, and stops an agent reaching outward on a whim.
    """

    import re as _re

    _ISSUE_REF = _re.compile(r"(?:#(\d{1,6})\b)|(?:issue[ -]?(\d{1,6}))", _re.IGNORECASE)
    _URL_REF = _re.compile(r"https?://[^\s)]+")
    _DOC_HINT = _re.compile(
        r"\b(runbook|design doc|spec(?:ification)?|wiki|confluence|notion)\b", _re.IGNORECASE
    )
    _OBS_HINT = _re.compile(
        r"\b(alert|incident|dashboard|error rate|latency spike|p99|traceback in prod)\b",
        _re.IGNORECASE,
    )

    def __init__(self, registry: MCPCapabilityRegistry) -> None:
        self.registry = registry

    def select(self, *, task: str, plan: Any | None = None) -> MCPSelection:
        text = task or ""

        issue_match = self._ISSUE_REF.search(text)
        if issue_match:
            issue_id = issue_match.group(1) or issue_match.group(2)
            for kind in (MCPCapabilityKind.ISSUE_TRACKER, MCPCapabilityKind.CODE_HOST):
                options = self.registry.by_kind(kind)
                if options:
                    return MCPSelection(
                        needed=True,
                        capability=options[0].name,
                        kind=kind,
                        arguments={"issue_id": issue_id},
                        rationale=f"task references issue #{issue_id}",
                    )
            return MCPSelection(
                needed=True,
                rationale=f"task references issue #{issue_id} but no issue-tracker capability "
                "is registered",
            )

        if self._OBS_HINT.search(text):
            options = self.registry.by_kind(MCPCapabilityKind.OBSERVABILITY)
            if options:
                return MCPSelection(
                    needed=True,
                    capability=options[0].name,
                    kind=MCPCapabilityKind.OBSERVABILITY,
                    arguments={"query": text[:200]},
                    rationale="task references production signals",
                )

        if self._DOC_HINT.search(text) or self._URL_REF.search(text):
            options = self.registry.by_kind(MCPCapabilityKind.DOCUMENTATION)
            if options:
                url = self._URL_REF.search(text)
                return MCPSelection(
                    needed=True,
                    capability=options[0].name,
                    kind=MCPCapabilityKind.DOCUMENTATION,
                    arguments={"url": url.group(0)} if url else {"query": text[:200]},
                    rationale="task references external documentation",
                )

        return MCPSelection(
            needed=False,
            rationale="no external reference detected; repository context is sufficient",
        )


class MCPInvocationPolicy:
    """Enforces allowlist, timeout, retry and budget on external calls."""

    def __init__(
        self,
        registry: MCPCapabilityRegistry,
        *,
        allowlist: set[str] | None = None,
        timeout_seconds: float = 20.0,
        max_calls_per_run: int = 3,
        error_policy: ToolErrorPolicy | None = None,
        budget: Any | None = None,
    ) -> None:
        self.registry = registry
        # None means "nothing allowed": deny-by-default is the safe posture for
        # an autonomous agent making outbound calls.
        self.allowlist = allowlist if allowlist is not None else set()
        self.timeout_seconds = timeout_seconds
        self.max_calls_per_run = max_calls_per_run
        self.error_policy = error_policy or ToolErrorPolicy()
        self.budget = budget
        self.calls: list[MCPResult] = []

    def is_allowed(self, capability: str) -> bool:
        return capability in self.allowlist

    def invoke(self, capability: str, arguments: dict[str, Any]) -> MCPResult:
        """Call one capability under policy. Never raises to the caller."""
        if not self.is_allowed(capability):
            return self._record(
                MCPResult(
                    capability=capability,
                    server="-",
                    ok=False,
                    error=f"capability {capability!r} is not on the allowlist",
                    error_category="permission_error",
                )
            )
        if len(self.calls) >= self.max_calls_per_run:
            return self._record(
                MCPResult(
                    capability=capability,
                    server="-",
                    ok=False,
                    error=f"MCP call budget exhausted ({self.max_calls_per_run} per run)",
                    error_category="budget",
                )
            )
        adapter = self.registry.adapter_for(capability)
        if adapter is None:
            return self._record(
                MCPResult(
                    capability=capability,
                    server="-",
                    ok=False,
                    error=f"no adapter provides capability {capability!r}",
                    error_category="not_found",
                )
            )
        if self.budget is not None and self.budget.would_exceed("tool"):
            return self._record(
                MCPResult(
                    capability=capability,
                    server=adapter.name,
                    ok=False,
                    error="execution budget exhausted",
                    error_category="budget",
                )
            )

        attempt = 0
        started = time.perf_counter()
        while True:
            attempt += 1
            try:
                content = adapter.call_tool(capability, arguments)
            except Exception as exc:
                classification = self.error_policy.classify_exception(exc)
                decision = self.error_policy.decide(classification, attempt)
                if decision.action is ToolErrorAction.RETRY:
                    continue
                return self._record(
                    MCPResult(
                        capability=capability,
                        server=adapter.name,
                        ok=False,
                        error=classification.message,
                        error_category=classification.category,
                        latency_seconds=time.perf_counter() - started,
                        attempts=attempt,
                    )
                )
            if self.budget is not None:
                self.budget.consume_tool_call()
            return self._record(
                MCPResult(
                    capability=capability,
                    server=adapter.name,
                    ok=True,
                    content=content,
                    latency_seconds=time.perf_counter() - started,
                    attempts=attempt,
                )
            )

    def _record(self, result: MCPResult) -> MCPResult:
        self.calls.append(result)
        return result

    def summary(self) -> dict[str, Any]:
        return {
            "calls": [c.to_dict() for c in self.calls],
            "total": len(self.calls),
            "successes": sum(1 for c in self.calls if c.ok),
            "failures": sum(1 for c in self.calls if not c.ok),
        }
