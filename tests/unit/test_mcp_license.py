"""Unit tests for MCP module availability detection."""

from iris_vector_graph._engine.mcp_license import (
    MCP_MODULE_CLASS_RESOURCES,
    MCP_MODULE_NAME,
    mcp_module_load_error,
)


def test_mcp_module_class_resources() -> None:
    assert MCP_MODULE_CLASS_RESOURCES == frozenset(
        {
            "Graph.KG.MCPService.CLS",
            "Graph.KG.MCPToolSet.CLS",
            "Graph.KG.MCPTools.CLS",
        }
    )


def test_mcp_module_load_error_names_module() -> None:
    raw = "superclass %AI.MCP.Service does not exist"
    msg = mcp_module_load_error(raw)
    assert msg is not None
    assert MCP_MODULE_NAME in msg
    assert "%AI.MCP" in msg


def test_mcp_module_load_error_non_mcp_returns_none() -> None:
    assert mcp_module_load_error("syntax error in class definition") is None
