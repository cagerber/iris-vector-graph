"""Unit tests for non-vector IRIS license detection."""

from iris_vector_graph._engine.vector_license import (
    VECTOR_MODULE_CLASS_RESOURCES,
    VECTOR_MODULE_NAME,
    vector_init_partial_failure_message,
    vector_module_license_load_error,
)


def test_vector_license_15806_is_partial() -> None:
    msg = "ERROR #15806: Vector Search not permitted with current license"
    assert vector_init_partial_failure_message(msg)


def test_kg_nodeembeddings_stored_proc_is_partial() -> None:
    msg = (
        "initialize_schema() failed to install 1 stored procedure(s). "
        "Server-side vector search will be unavailable. "
        "First error: Table 'GRAPH_KG.KG_NODEEMBEDDINGS' not found"
    )
    assert vector_init_partial_failure_message(msg)


def test_unrelated_error_is_not_partial() -> None:
    assert not vector_init_partial_failure_message("Connection refused to IRIS host")


def test_vector_module_class_resources() -> None:
    assert VECTOR_MODULE_CLASS_RESOURCES == frozenset(
        {
            "Graph.KG.kgNodeEmbeddings.CLS",
            "Graph.KG.kgNodeEmbeddingsoptimized.CLS",
        }
    )


def test_vector_module_license_load_error_names_module() -> None:
    raw = "compile error (#15806)"
    msg = vector_module_license_load_error(raw)
    assert msg is not None
    assert VECTOR_MODULE_NAME in msg
    assert "15806" in msg
    assert "Vector Search" in msg


def test_vector_module_license_load_error_non_license_returns_none() -> None:
    assert vector_module_license_load_error("syntax error in class definition") is None
