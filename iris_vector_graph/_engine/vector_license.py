"""Detect IRIS deployments where Vector Search is unavailable (e.g. IRISHealth)."""


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
