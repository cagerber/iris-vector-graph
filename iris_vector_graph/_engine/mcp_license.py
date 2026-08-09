"""Detect IRIS deployments where %AI.MCP is unavailable (e.g. IRIS Health)."""

from __future__ import annotations

MCP_MODULE_NAME = "iris-vector-graph-mcp"

MCP_MODULE_CLASS_RESOURCES: frozenset[str] = frozenset(
    {
        "Graph.KG.MCPService.CLS",
        "Graph.KG.MCPToolSet.CLS",
        "Graph.KG.MCPTools.CLS",
    }
)


def mcp_module_load_error(raw: str) -> str | None:
    """Return operator-facing error when iris-vector-graph-mcp cannot load on restricted IRIS."""
    if not raw:
        return None
    m = raw.lower()
    if "%ai.mcp" not in m and "mcp.service" not in m:
        return None
    if "does not exist" not in m and "not found" not in m:
        return None
    return (
        f"Cannot load {MCP_MODULE_NAME}: %AI.MCP requires full IRIS "
        f"(not IRIS Health). Omit the MCP module; core graph and Cypher "
        f"work without it."
    )
