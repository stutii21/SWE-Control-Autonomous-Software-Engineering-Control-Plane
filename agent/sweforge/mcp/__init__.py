"""SWE-Forge MCP orchestration layer.

Reuses upstream Open SWE MCP clients and ``langchain-mcp-adapters`` for
transport. Contributes the orchestration decision: when external capability is
worth consulting, which one, under what limits.
"""

from agent.sweforge.mcp.orchestration import (
    MCPAdapter,
    MCPCapability,
    MCPCapabilityKind,
    MCPCapabilityRegistry,
    MCPInvocationPolicy,
    MCPResult,
    MCPSelection,
    MCPToolSelector,
)

__all__ = [
    "MCPAdapter",
    "MCPCapability",
    "MCPCapabilityKind",
    "MCPCapabilityRegistry",
    "MCPInvocationPolicy",
    "MCPResult",
    "MCPSelection",
    "MCPToolSelector",
]
