"""Detect IRIS deployments where Vector Search is unavailable (e.g. IRISHealth)."""

from __future__ import annotations

VECTOR_MODULE_NAME = "iris-vector-graph-vector"

VECTOR_MODULE_CLASS_RESOURCES: frozenset[str] = frozenset(
    {
        "Graph.KG.kgNodeEmbeddings.CLS",
        "Graph.KG.kgNodeEmbeddingsoptimized.CLS",
    }
)


def vector_init_partial_failure_message(msg: str) -> bool:
    """True when schema init failed only on vector / embeddings / proc install."""
    if not msg:
        return False
    m = msg.lower()
    if "15806" in msg:
        return True
    if "vector search" in m and "not permitted" in m:
        return True
    if "vector" in m and "not permitted" in m:
        return True
    if "kg_nodeembeddings" in m:
        return True
    if "server-side vector search will be unavailable" in m:
        return True
    return bool("failed to install" in m and "stored procedure" in m)


def vector_module_license_load_error(raw: str) -> str | None:
    """Return operator-facing error when iris-vector-graph-vector cannot load on restricted IRIS."""
    if not vector_init_partial_failure_message(raw):
        return None
    return (
        f"Cannot load {VECTOR_MODULE_NAME}: Vector Search license required "
        f"(IRIS #15806). Install Vector Search or omit the vector module; "
        f"core graph and Cypher work without it."
    )
