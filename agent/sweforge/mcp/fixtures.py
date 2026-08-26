"""Deterministic in-process MCP adapters for testing.

No live MCP server exists in this environment, so the real adapter path is
exercised against these fixtures. They are explicitly test doubles: they
satisfy the :class:`~agent.sweforge.mcp.orchestration.MCPAdapter` protocol and
return **fixture data clearly labelled as such**, so no external result is ever
fabricated and presented as real.
"""

from typing import Any


class FixtureMCPAdapter:
    """A deterministic adapter returning canned, clearly-labelled payloads."""

    def __init__(
        self,
        name: str = "fixture-issues",
        tools: list[dict[str, Any]] | None = None,
        responses: dict[str, Any] | None = None,
    ) -> None:
        self.name = name
        self._tools = (
            tools
            if tools is not None
            else [
                {
                    "name": "get_issue",
                    "kind": "issue_tracker",
                    "description": "Fetch an issue by id.",
                    "input_schema": {
                        "type": "object",
                        "properties": {"issue_id": {"type": "string"}},
                        "required": ["issue_id"],
                    },
                }
            ]
        )
        self._responses = responses or {
            "get_issue": {
                "_fixture": True,
                "id": "412",
                "title": "restock_needed excludes the boundary value",
                "body": (
                    "Items sitting exactly at the reorder threshold are not being "
                    "flagged for restock. Expected: inclusive comparison."
                ),
                "labels": ["bug", "inventory"],
            }
        }
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def list_tools(self) -> list[dict[str, Any]]:
        return list(self._tools)

    def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        self.calls.append((name, dict(arguments)))
        if name not in self._responses:
            raise FileNotFoundError(f"fixture has no response for tool {name!r}")
        return self._responses[name]


class FailingMCPAdapter:
    """Adapter that always fails, for exercising the error/retry policy."""

    def __init__(self, name: str = "fixture-flaky", error: Exception | None = None) -> None:
        self.name = name
        self._error = error or TimeoutError("connection timed out")
        self.call_count = 0

    def list_tools(self) -> list[dict[str, Any]]:
        return [{"name": "flaky_lookup", "kind": "research", "description": "Always fails."}]

    def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        self.call_count += 1
        raise self._error


class RecoveringMCPAdapter:
    """Fails a fixed number of times, then succeeds. Proves retry works."""

    def __init__(self, name: str = "fixture-recovering", failures_before_success: int = 1) -> None:
        self.name = name
        self.failures_before_success = failures_before_success
        self.call_count = 0

    def list_tools(self) -> list[dict[str, Any]]:
        return [{"name": "doc_lookup", "kind": "documentation", "description": "Docs fetch."}]

    def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        self.call_count += 1
        if self.call_count <= self.failures_before_success:
            raise TimeoutError("temporarily unavailable, try again")
        return {"_fixture": True, "content": "fixture documentation payload"}
