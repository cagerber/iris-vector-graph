"""Unit tests for non-vector IRIS license detection."""

from iris_vector_graph._engine.vector_license import vector_init_partial_failure_message


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
