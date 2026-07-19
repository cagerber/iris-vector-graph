# ivg-arno — Arno Rust Acceleration for IVG

## What It Is

Arno (`libarno_callout.so`) is a Rust callout library that accelerates IVG's most
compute-intensive graph operations. It replaces Python-side BFS and community detection
with native Rust implementations invoked via IRIS `$ZF(-5)` callout.

## When It Matters

| Operation                   | Without Arno                      | With Arno                  | Threshold    |
| --------------------------- | --------------------------------- | -------------------------- | ------------ |
| BFS / multi-hop traversal   | LazyKG Python (~seconds at scale) | Rust BFS (~ms)             | > ~10K nodes |
| Leiden community detection  | leidenalg Python                  | leiden-rs Rust crate       | > ~5K nodes  |
| PPR (Personalized PageRank) | Not available                     | Available                  | Any size     |
| Graph serialization to Rust | ~944ms (20K Native-API hops)      | ~9–60ms (1 SQL round-trip) | > ~2K edges  |

For small graphs (< 5K nodes) the Python fallback is fast enough. Use Arno when
running production workloads, benchmarks, or Shaarpec/FHIR-scale data.

## How to Enable

Arno requires the **enterprise container** (`ivg-iris-enterprise`):

```bash
# Start enterprise container (loads libarno_callout.so automatically)
scripts/enterprise-container.sh up

# Verify Arno is loaded
python - <<'EOF'
import iris
conn = iris.connect("localhost", 31972, "USER", "_SYSTEM", "SYS")
from iris_vector_graph.engine import IRISGraphEngine
engine = IRISGraphEngine(conn)
print("arno:", engine._detect_arno())   # True if loaded
EOF
```

The `up` command runs `tcp-load-arno` which streams `docker/enterprise/libarno_callout.so`
into the container via TCP and calls `Graph.KG.ArnoAccel.Load` to register it.

## ASQ Query Syntax vs Cypher

IVG exposes two query languages:

|                 | Cypher                                | ASQ                              |
| --------------- | ------------------------------------- | -------------------------------- |
| Syntax          | `MATCH (a)-[:REL]->(b) RETURN b`      | JSON/structured adjacency spec   |
| Surface         | `engine.execute_cypher(q)`            | `engine.execute_asq(q)`          |
| Multi-hop       | `[*1..N]` variable-length             | native BFS depth parameter       |
| When Arno helps | Routes to Rust BFS internally         | Direct Rust BFS                  |
| Best for        | Ad-hoc queries, Cypher-fluent callers | Programmatic traversal, bulk BFS |

ASQ example:

```python
result = engine.execute_asq({
    "start": "gene:TP53",
    "direction": "out",
    "max_depth": 3,
    "rel_filter": ["INTERACTS_WITH", "REGULATES"],
})
```

## Fixtures in Tests

Tests that require Arno use the `arno_iris_connection` fixture (port 31972).
They auto-skip when the enterprise container is not running — they never hard-fail
on Community-only machines.

```python
class TestMyArnoFeature:
    @pytest.fixture(autouse=True)
    def setup(self, arno_master_cleanup, arno_iris_connection):
        from iris_vector_graph.engine import IRISGraphEngine
        self.engine = IRISGraphEngine(arno_iris_connection)
        self.engine.initialize_schema()
        yield
```

## Key ObjectScript Classes

| Class                         | Purpose                                         |
| ----------------------------- | ----------------------------------------------- |
| `Graph.KG.ArnoAccel`          | Callout bridge — `Load`, `BFS`, `PPR`, `Leiden` |
| `Graph.KG.NKGAccel`           | Integer-keyed adjacency operations over `^NKG`  |
| `Graph.KG.NKGAccelTraversal`  | Traversal methods (BFS, k-hop) on `^NKG`        |
| `Graph.KG.NKGAccelCentrality` | Closeness/eigenvector/betweenness on `^NKG`     |

## Deploy / Recompile

If ObjectScript classes change, redeploy to the enterprise container:

```bash
scripts/enterprise-container.sh tcp-deploy    # write + compile all .cls via TCP
scripts/enterprise-container.sh tcp-load-arno # reload libarno_callout.so
```

Direct irispython or `docker cp` won't work — the enterprise container uses a NoPWS
HealthShare image where TCP and irispython route to different database files (dual-DB
split). Always use the `tcp-deploy` subcommand.
