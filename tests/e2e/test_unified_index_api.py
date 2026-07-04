import pytest

from iris_vector_graph.engine import IRISGraphEngine
from iris_vector_graph.schema import GraphSchema
from iris_vector_graph import (
    VectorIndexConfig,
    FulltextIndexConfig,
    Index,
    IndexNotFoundError,
    IndexNotBuiltError,
)


@pytest.fixture()
def engine(iris_connection):
    # Detect the live container's actual kg_NodeEmbeddings dimension (ivg's own
    # primitive) instead of hardcoding — a mismatch here poisons the shared
    # session connection for every other test relying on it.
    dim = GraphSchema.get_embedding_dimension(iris_connection.cursor()) or 768
    return IRISGraphEngine(iris_connection, embedding_dimension=dim)


@pytest.fixture()
def seeded_vectors(engine, clean_test_data):
    prefix = clean_test_data
    ids = []
    dim = engine.embedding_dimension
    for i in range(20):
        nid = f"{prefix}n{i}"
        vec = [(i + j) % 7 / 7.0 for j in range(dim)]
        engine.create_node(nid)
        engine.store_embedding(nid, vec)
        ids.append(nid)
    engine.conn.commit()
    return ids


def test_create_index_returns_index_object(engine, clean_test_data):
    name = f"{clean_test_data}vidx"
    idx = engine.create_index(VectorIndexConfig(name=name, method="ivf", nlist=4))
    assert isinstance(idx, Index)
    assert idx.type == "vector"
    assert idx.status()["state"] == "empty"


def test_vector_index_build_status_search(engine, seeded_vectors, clean_test_data):
    name = f"{clean_test_data}vidx"
    idx = engine.create_index(VectorIndexConfig(name=name, method="ivf", nlist=4))
    idx.build(wait=True, node_ids=seeded_vectors)
    st = idx.status()
    assert st["state"] == "ready"
    assert st["rows"] > 0
    hits = idx.search([0.1]*128, k=5)
    assert isinstance(hits, list)


def test_search_before_build_raises_not_built(engine, clean_test_data):
    name = f"{clean_test_data}empty"
    idx = engine.create_index(VectorIndexConfig(name=name, method="ivf", nlist=4))
    with pytest.raises(IndexNotBuiltError):
        idx.search([0.0]*128, k=3)


def test_index_lookup_missing_raises_not_found(engine):
    with pytest.raises(IndexNotFoundError):
        engine.index("nonexistent_index_zzz")


def test_create_index_duplicate_requires_replace(engine, clean_test_data):
    name = f"{clean_test_data}dup"
    engine.create_index(VectorIndexConfig(name=name))
    with pytest.raises(ValueError):
        engine.create_index(VectorIndexConfig(name=name))
    idx = engine.create_index(VectorIndexConfig(name=name), replace=True)
    assert idx.name == name


def test_list_indexes_returns_index_objects(engine, clean_test_data):
    name = f"{clean_test_data}listed"
    engine.create_index(VectorIndexConfig(name=name))
    listed = engine.list_indexes()
    assert all(isinstance(i, Index) for i in listed)
    assert name in [i.name for i in listed]
