---
name: iris-vector-graph
description: IVG — openCypher + temporal property graph + hybrid vector search on IRIS. Covers IRISGraphEngine API, temporal edge queries, HNSW/BM25 retrieval, RDF/SHACL, and Arno acceleration.
managed_by: iris-vector-graph
source: intersystems-community/iris-vector-graph
tags: [iris, graph, cypher, temporal, vector, rdf, shacl]
---

# iris-vector-graph (IVG)

## What It Is

IVG turns InterSystems IRIS into a graph + vector database. It exposes an openCypher
query engine, temporal property graph storage, hybrid vector search (HNSW/IVF/BM25),
graph analytics (centrality, community detection), and a semantic layer (RDF export,
SHACL validation, PROV-O provenance) — all over the native IRIS SQL and globals layer.

**When to reach for IVG:**

- Query biomedical/clinical knowledge graphs stored in IRIS with Cypher
- Time-windowed edge queries (e.g. "which drugs were co-prescribed in 2024 Q3?")
- Hybrid retrieval: combine BM25 text search + HNSW vector similarity via RRF fusion
- Export an IRIS graph as RDF Turtle/N-Quads for downstream SPARQL tools
- Validate FHIR/biomedical data shapes with SHACL Core
- Attach PROV-O provenance to agentic pipeline outputs stored in IRIS

## Python API Quickstart

```python
import iris
from iris_vector_graph.engine import IRISGraphEngine

conn = iris.connect("localhost", 1972, "USER", "_SYSTEM", "SYS")
engine = IRISGraphEngine(conn, embedding_dimension=768)
engine.initialize_schema()   # idempotent; safe to call every time

# Nodes and edges
engine.create_node("gene:TP53", labels=["Gene"], properties={"name": "TP53"})
engine.create_node("disease:LUAD", labels=["Disease"], properties={"name": "lung adenocarcinoma"})
engine.create_edge("gene:TP53", "IMPLICATED_IN", "disease:LUAD")

# Cypher query
result = engine.execute_cypher(
    "MATCH (g:Gene)-[:IMPLICATED_IN]->(d:Disease) RETURN g.name, d.name LIMIT 10"
)
# result = {"columns": [...], "rows": [...]}

# Temporal edge (millisecond UNIX timestamp)
engine.add_temporal_edge("gene:TP53", "EXPRESSED_IN", "tissue:lung", timestamp=1735689600000)

# Window query — all edges active in 2025
rows = engine.execute_cypher(
    "MATCH (a)-[r]->(b) WHERE r.ts >= 1735689600000 AND r.ts < 1767225600000 RETURN a, b"
)["rows"]

# SHACL validation
from iris_vector_graph.shacl import ValidationReport
report: ValidationReport = engine.validate_shacl("path/to/shapes.ttl")
if not report.conforms:
    for v in report.violations:
        print(v.focus_node, v.message)

# RDF export — full graph as Turtle
engine.export_rdf("output.ttl", format="turtle")

# Subgraph export by label
engine.export_rdf("genes.ttl", format="turtle", label_filter="Gene")

# PROV-O provenance export
engine.prov_export("provenance.ttl", format="turtle")

# Leiden community detection
communities = engine.leiden_communities()  # {node_id: community_id}

# BM25 + HNSW hybrid RRF search
results = engine.kg_RRF_FUSE(k=10, k1=10, k2=10, c=60,
                              query_vector="[0.1, 0.2, ...]",
                              query_text="lung cancer driver mutation")
```

## Key Globals

| Global                                 | Purpose                                                             |
| -------------------------------------- | ------------------------------------------------------------------- |
| `^KG("out", 0, src, rel, dst)`         | Outbound adjacency index (BFS forward)                              |
| `^KG("in", 0, dst, rel, src)`          | Inbound adjacency index (BFS reverse)                               |
| `^KG("tout", ts, src, rel, dst)`       | Temporal outbound edges, keyed by timestamp                         |
| `^KG("tin", ts, dst, rel, src)`        | Temporal inbound edges, keyed by timestamp                          |
| `^KG("bucket", period, src, rel, dst)` | Pre-aggregated temporal bucket analytics                            |
| `^KG("tagg", src, rel, dst, key)`      | Aggregate edge properties                                           |
| `^NKG`                                 | Integer-keyed adjacency index; `[*1..N]` Cypher patterns route here |
| `^IVF(name, ...)`                      | IVF flat vector index clusters                                      |
| `^BM25Idx(name, ...)`                  | BM25 inverted index                                                 |

## Container Setup

```bash
# Start Community IRIS container (ivg-iris, port 21972)
scripts/test-container.sh up

# Connect
python -c "
import iris
conn = iris.connect('localhost', 21972, 'USER', '_SYSTEM', 'SYS')
print('connected:', conn)
"

# Debug shell
docker exec -it ivg-iris iris session iris -U USER
```

Community Edition limit: **≤ 2 CPU cores**. On machines with 16+ cores, use the
enterprise container instead:

```bash
export IVG_TEST_CONTAINER=ivg-iris-enterprise IVG_PORT=31972
scripts/enterprise-container.sh up
pytest tests/
```

## Common Gotchas

**Reserved words in Cypher**  
`MATCH (n) WHERE n.type = $t` fails — `type` is a reserved function. Quote it:
`n.\`type\`` or rename the property.

**Temporal edge timestamp format**  
Timestamps are millisecond UNIX integers, not ISO strings. Wrong: `"2025-01-01"`.
Right: `1735689600000`. Convert with `int(datetime.timestamp() * 1000)`.

**`initialize_schema()` compile warnings**  
On Community Edition, schema init prints `<CLASS DOES NOT EXIST>` warnings for
enterprise-only classes (`Graph.KG.MCPService`, etc.). These are safe to ignore.

**Community 2-CPU limit**  
IRIS Community Edition enforces a 2-core limit. Leiden/igraph parallel algorithms
silently fall back to single-threaded. Use the enterprise container for benchmarks.

**`create_node` on duplicate ID**  
`create_node` with an already-existing `node_id` silently returns `False` (UPSERT
semantics). Check the return value if you need to know whether insertion happened.

**BFS requires `BuildKG()`**  
Multi-hop `[*1..N]` patterns require `^KG` adjacency to be built. Call
`engine.build_kg()` after bulk-loading data. The engine warns if `^KG` is absent.

**Schema prefix**  
`set_schema_prefix("MySchema")` in `iris_vector_graph.cypher.translator` affects the
module-level table prefix. Instance attributes don't override it. Always use the
`set_schema_prefix` / `get_schema_prefix` functions, not engine attributes.
