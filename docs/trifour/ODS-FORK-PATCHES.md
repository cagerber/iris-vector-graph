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
| `module.xml` / `module-core.xml` | **IPM completeness** — all 44 `iris_src/src/**/*.cls` resources; version `2.5.0-trifour.1` (wheel `2.5.0+trifour.1`) |
| `scripts/generate_module_xml_resources.py` | Regenerates module resource lists from the filesystem (core = pure ObjectScript; full = embedded Python + bridge classes) |
| `tests/unit/test_module_xml_drift.py` | Fails CI when module XML drifts from `iris_src/src` |

### IPM module split (generator rules)

- **`iris-vector-graph-core`**: pure ObjectScript primitives (VecIndex, GraphIndex, Traversal*, NKGAccel*, PageRank, Algorithms, Meta, …).
- **`iris-vector-graph`**: depends on core; classes with `Language = python` plus explicit bridge entries (`PyOps`, `Service`, `BM25Index`, `GraphOperators`, `User.Exec`, …).

Regenerate after adding or renaming `.cls` files:

```bash
python3 scripts/generate_module_xml_resources.py
python3 scripts/generate_module_xml_resources.py --check   # CI drift gate
```

Prior to this patch, only **16** of **44** classes were listed in the module manifests (IPM load could silently omit new ObjectScript).

## ODS consumption

- Python: `pyproject.toml` `[tool.uv.sources]` git pin to this repo (same pattern as `sqlglot`, `graphifyy`).
- ObjectScript: `src/Graph/`, `src/IVG/`, `src/iris/` synced from `iris_src/src/` at the pinned revision.

## Upstream contribution

These changes are intended for upstream PR once validated on CREST-ODS. Until merged, ODS pins this fork.
