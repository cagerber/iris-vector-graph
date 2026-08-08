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

## ODS consumption

- Python: `pyproject.toml` `[tool.uv.sources]` git pin to this repo (same pattern as `sqlglot`, `graphifyy`).
- ObjectScript: `src/Graph/`, `src/IVG/`, `src/iris/` synced from `iris_src/src/` at the pinned revision.

## Upstream contribution

These changes are intended for upstream PR once validated on CREST-ODS. Until merged, ODS pins this fork.
