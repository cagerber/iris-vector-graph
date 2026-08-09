# Trifour fork patches (ODS)

This fork of [intersystems-community/iris-vector-graph](https://github.com/intersystems-community/iris-vector-graph) carries patches required for **ODS** deployments on IRIS instances **without Vector Search license** (e.g. IRISHealth / CREST).

## Motivation

Upstream `initialize_schema()` / `IVG.CypherEngine.InitSchema()` assumes Vector Search is licensed. On IRISHealth, IRIS returns:

```text
ERROR #15806: Vector Search not permitted with current license
```

Core property-graph tables and openCypher can still work; vector/RAG features cannot.

See ODS ADR: `docs/dev/journal/adr-2026-06-ivg-vendor-non-vector-license.md`.

## Patches in this fork

| Area | Change |
|------|--------|
| `iris_vector_graph/_engine/vector_license.py` | Shared classifier for vector-only init failures |
| `iris_vector_graph/_engine/schema.py` | Skip vector DDL/procedures on license errors; return `status: partial` |
| `iris_src/src/IVG/CypherEngine.cls` | `InitSchemaJson` partial handling; deploy helpers (`DeployInitSchemaOutput`, `EnsureIrisVectorGraphPythonJSON`, …) |
| `module.xml` / `module-core.xml` / `module-vector.xml` / `module-mcp.xml` / `module-embed.xml` | **IPM completeness** — all 44 `iris_src/src/**/*.cls` resources; version `2.5.0-trifour.6` (wheel `2.5.0+trifour.6`) |
| `scripts/generate_module_xml_resources.py` | Regenerates module resource lists; **`SCHEMA_DEPENDENT_CLASSES`** — `EdgeScan`, `Subgraph`, `TraversalBuild` in full module (embedded `&sql` requires `Graph_KG` tables at compile) |
| `IVG.Exec` (renamed from `User.Exec`, trifour.5) | **IRIS Health ZPM fix** — `User` package triggers `Config.MapPackages` `<SUBSCRIPT>` during full-module Reload |
| `IVG.PageRankEmbedded` in **`iris-vector-graph-embed`** (trifour.6) | Root-level `PageRankEmbedded` also triggered MapPackages SUBSCRIPT; moved to optional embed module (not loaded on CREST-ODS graph deploy) |
| `tests/unit/test_module_xml_drift.py` | Fails CI when module XML drifts from `iris_src/src` |
| `iris_vector_graph/_engine/vector_license.py` | `vector_module_license_load_error()` — named failure when `iris-vector-graph-vector` cannot compile (#15806) |
| `iris_vector_graph/_engine/mcp_license.py` | `mcp_module_load_error()` — named failure when `iris-vector-graph-mcp` cannot compile (%AI.MCP missing on IRIS Health) |

### IPM module split (generator rules)

- **iris-vector-graph-core**: pure ObjectScript primitives (VecIndex, GraphIndex, Traversal*, NKGAccel*, PageRank, Algorithms, Meta, `IVG.Exec`, …). **No VECTOR license required. No %AI.MCP required.** **No embedded `&sql` against `Graph_KG` tables** — compiles before `InitSchema`.
- **`iris-vector-graph`**: depends on core; classes with `Language = python`, `SCHEMA_DEPENDENT_CLASSES` (`EdgeScan`, `Subgraph`, `TraversalBuild`), plus explicit bridge entries (`PyOps`, `Service`, `BM25Index`, `GraphOperators`, …). **Does not depend on vector, MCP, or embed modules.** Load after core; `InitSchema` runs post-IPM via deploy / `IVG.CypherEngine`.
- **`iris-vector-graph-embed`** (optional): `IVG.PageRankEmbedded` — heavy embedded Python; **not loaded** on IRIS Health ODS graph deploy.
- **`iris-vector-graph-vector`** (optional): `Graph.KG.kgNodeEmbeddings` + `kgNodeEmbeddingsoptimized` — **#15806** on IRISHealth.
- **`iris-vector-graph-mcp`** (optional): `Graph.KG.MCPService` + `MCPToolSet` + `MCPTools` — **%AI.MCP missing** on IRIS Health.

Regenerate after adding or renaming `.cls` files:

```bash
python3 scripts/generate_module_xml_resources.py
python3 scripts/generate_module_xml_resources.py --check   # CI drift gate
```

## ODS consumption

- Python: `pyproject.toml` `[tool.uv.sources]` git pin to this repo (same pattern as `sqlglot`, `graphifyy`).
- ObjectScript: `src/Graph/`, `src/IVG/`, `src/iris/` synced from `iris_src/src/` at the pinned revision.

## Upstream contribution

These changes are intended for upstream PR once validated on CREST-ODS. Until merged, ODS pins this fork.
