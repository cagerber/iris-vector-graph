"""behave environment hooks for TCK harness."""
from __future__ import annotations

import contextlib
import logging
import os
import subprocess
from uuid import uuid4

logger = logging.getLogger(__name__)

# Named graphs: scenario-scoped fixtures rebuilt as needed.
NAMED_GRAPHS = {
    "binary-tree-1": "TCK_BINARY_TREE_1",
    "binary-tree-2": "TCK_BINARY_TREE_2",
}

# Binary tree structures (from openCypher tck/graphs/ directory)
_BINARY_TREE_1_CQL = """
CREATE
  (root:A {name: 'root'}),
  (n1:A {name: 'n1'}), (n2:A {name: 'n2'}),
  (n3:B {name: 'n3'}), (n4:B {name: 'n4'}),
  (n5:B {name: 'n5'}), (n6:B {name: 'n6'}),
  (root)-[:KNOWS]->(n1), (root)-[:KNOWS]->(n2),
  (n1)-[:KNOWS]->(n3), (n1)-[:KNOWS]->(n4),
  (n2)-[:KNOWS]->(n5), (n2)-[:KNOWS]->(n6)
"""

_BINARY_TREE_2_CQL = """
CREATE
  (root:A {name: 'root'}),
  (n1:A {name: 'n1'}), (n2:A {name: 'n2'}),
  (n3:B {name: 'n3'}), (n4:B {name: 'n4'}),
  (n5:B {name: 'n5'}), (n6:B {name: 'n6'}),
  (n7:C {name: 'n7'}),
  (root)-[:KNOWS]->(n1), (root)-[:KNOWS]->(n2),
  (n1)-[:KNOWS]->(n3), (n1)-[:KNOWS]->(n4),
  (n2)-[:KNOWS]->(n5), (n2)-[:KNOWS]->(n6),
  (n3)-[:KNOWS]->(n7)
"""


def before_all(context):
    """Session setup: connect to ivg-iris, initialize schema only."""
    container_name = os.environ.get("IVG_CONTAINER", "ivg-iris")
    port = int(os.environ.get("IVG_PORT", "21972"))

    conn = _connect(container_name, port)
    context.conn = conn
    context.named_graphs = dict(NAMED_GRAPHS)
    # Cache: graph_name -> label, only when currently built in DB
    context._named_graph_labels = {}

    from iris_vector_graph.engine import IRISGraphEngine
    context.engine = IRISGraphEngine(conn, embedding_dimension=768)
    try:
        context.engine.initialize_schema(auto_deploy_objectscript=False)
    except Exception as e:
        logger.warning("Schema init: %s", e)

    # Clean up any leftover data from previous test sessions
    _flush_all_tck_data(context)


def before_scenario(context, scenario):
    context.scenario_label = f"TCK_{uuid4().hex[:8]}"
    context.params = {}
    context.last_result = None
    context.last_error = None
    context._used_named_graph = None  # track if this scenario uses a named graph


def after_scenario(context, scenario):
    label = getattr(context, "scenario_label", None)
    if label:
        if label.startswith("TCK_BINARY_TREE"):
            # Named-graph scenario: tear down this scenario's named graph data
            _teardown_label(context, label)
            # Remove from the built-cache so next scenario recreates if needed
            if hasattr(context, "_named_graph_labels"):
                context._named_graph_labels = {
                    k: v for k, v in context._named_graph_labels.items()
                    if v != label
                }
        else:
            _teardown_label(context, label)


def after_all(context):
    # Final cleanup
    _flush_all_tck_data(context)
    with contextlib.suppress(Exception):
        context.conn.close()


def _connect(container_name: str, port: int):
    """Connect to the IRIS container using iris_devtester."""
    try:
        from iris_devtester import IRISContainer as IRC
        fresh = IRC.attach(container_name)
        fresh._connection = None
        conn = fresh.get_connection()
        logger.info("TCK harness connected to %s via iris_devtester", container_name)
        return conn
    except Exception as e:
        raise RuntimeError(
            f"Cannot connect to IRIS container '{container_name}' (port {port}). "
            f"Start it with: scripts/test-container.sh up\n"
            f"Original error: {e}"
        ) from e


def _db_schema(context) -> str:
    """Return the schema prefix used by this engine (e.g. 'Graph_KG' or 'SQLUser')."""
    engine = getattr(context, "engine", None)
    if engine is None:
        return "SQLUser"
    return getattr(engine, "_schema_prefix", "SQLUser")


def _flush_all_tck_data(context):
    """Delete all TCK data from the DB (for session start/end cleanup).

    Delete order must respect FK constraints: rdf_props/rdf_labels/rdf_edges
    must be deleted before nodes (FK rdf_labels.s -> nodes.node_id).
    """
    with contextlib.suppress(Exception):
        store = getattr(context.engine, "_store", None)
        if store and hasattr(store, "conn"):
            schema = _db_schema(context)
            cursor = store.conn.cursor()
            # Delete in FK-safe order: dependents first
            cursor.execute(f"DELETE FROM {schema}.rdf_props")
            cursor.execute(f"DELETE FROM {schema}.rdf_labels")
            cursor.execute(f"DELETE FROM {schema}.rdf_edges")
            cursor.execute(f"DELETE FROM {schema}.nodes")
            store.conn.commit()
            cursor.close()


def _teardown_label(context, label: str):
    with contextlib.suppress(Exception):
        context.engine.execute_cypher(
            f"MATCH (n:{label}) DETACH DELETE n", {}
        )
    # Clean up orphaned nodes (no labels) left by anonymous CREATE endpoints
    # in main test queries that weren't given the isolation label.
    with contextlib.suppress(Exception):
        store = getattr(context.engine, "_store", None)
        if store and hasattr(store, "conn"):
            schema = _db_schema(context)
            cursor = store.conn.cursor()
            cursor.execute(
                f"DELETE FROM {schema}.nodes WHERE node_id NOT IN "
                f"(SELECT DISTINCT s FROM {schema}.rdf_labels)"
            )
            store.conn.commit()


def _ensure_named_graph(context, graph_name: str, label: str):
    """Build a named graph fixture under its session label if not already present."""
    try:
        result = context.engine.execute_cypher(
            f"MATCH (n:{label}) RETURN count(n) AS cnt", {}
        )
        cnt = result.rows[0].get("cnt", 0) if result.rows else 0
        if cnt > 0:
            return  # already built
    except Exception:
        pass

    cql_map = {
        "binary-tree-1": _BINARY_TREE_1_CQL,
        "binary-tree-2": _BINARY_TREE_2_CQL,
    }
    cql = cql_map.get(graph_name)
    if not cql:
        return

    # Inject session label into CREATE
    from tests.tck.steps.graph_setup import _inject_label
    labeled_cql = _inject_label(cql.strip(), label)
    try:
        context.engine.execute_cypher(labeled_cql, {})
    except Exception as e:
        logger.warning("Failed to create named graph '%s': %s", graph_name, e)
