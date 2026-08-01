# Research: openCypher TCK Harness

**Date**: 2026-08-01
**Submodule pin**: `677cbafabb8c3c5eed458fd3b1ec0daec8d67d23` (opencypher/openCypher HEAD)

## TCK Scenario Counts

| Path | Feature files | Scenarios |
|------|--------------|-----------|
| `tck/features/clauses` | 93 | 753 |
| `tck/features/expressions` | 125 | 556 |
| `tck/features/useCases` | 2 | 30 |
| **TOTAL** | **220** | **1,339** |

Note: earlier estimates of ~1,615 were incorrect. Actual count is **1,339**.

## Step Pattern Inventory

### Standard families (4) — must implement

| Pattern family | Notes |
|---|---|
| `Given an empty graph` / `Given any graph` | Also: `Given the binary-tree-N graph` — 19 scenarios; need pre-built fixture graphs |
| `Given having executed [query]` | Setup queries before the test query |
| `When executing query [q]` | The query under test |
| `Then the result should be [table]` / `Then the result should be empty` | Result assertion |
| `Then no side effects` | Assert no mutations occurred |

### Non-standard patterns — require additional step definitions

| Pattern | Scenario count | Handling |
|---|---|---|
| `And parameters are: [table]` | 43 | Pass param dict to `execute_cypher`; must parse TCK table format into Python dict |
| `When executing control query: [q]` | 27 | Same as `When executing query` but result is not asserted — used to set up state mid-scenario |
| `And there exists a procedure test.*` | ~35 | Procedure registration — `CALL` clause scenarios. Tag `@wip` (IVG procedure support is passthrough, not registerable) |
| `Then a TypeError should be raised` | ~10 | Error type assertion — catch exception and check type |
| `Then a ArgumentError should be raised` | 5 | Error type assertion |
| `Then a EntityNotFound should be raised` | 3 | Error type assertion |
| `Then a SemanticError should be raised` | 2 | Error type assertion |
| `Then a ProcedureError should be raised` | 2 | Error type assertion |
| `Then a ParameterMissing should be raised` | 1 | Error type assertion |
| `Then a ConstraintVerificationFailed should be raised` | 1 | Error type assertion |
| `Then the result should be (ignoring element order for lists):` | 16 | Variant result assertion — same as standard but list elements within cells are unordered |
| `Then the result should be, in order (ignoring element order for lists):` | 1 | Ordered rows, unordered list elements within cells |

**Decision**: Only `@NegativeTest` scenarios require exact Neo4j error *message* text — and only **1** scenario is actually tagged `@NegativeTest`. The error-type steps (`Then a TypeError should be raised`) only assert the error class, not message text. IVG can implement these by mapping exception types; they are NOT automatically `@wip`.

Revised `@wip` categories:
- `expressions/graph` — Neo4j-specific graph object functions
- `expressions/temporal` — conflicts with IVG's IRIS temporal extension
- `expressions/pattern` — inline pattern predicates beyond EXISTS
- Scenarios using `And there exists a procedure test.*` — procedure registration not supported
- The 1 `@NegativeTest` scenario if message text doesn't match

## Named Graph Fixtures

19 scenarios use named graphs (`binary-tree-1`, `binary-tree-2`). These require
pre-built fixture graphs. The `environment.py` `before_all` hook MUST create
these graphs once per session and scope them with a session-level UUID label
(not per-scenario) so they're available across all scenarios that reference them.

Named graphs used:
- `binary-tree-1` — 10 scenarios (`useCases/` and some `clauses/`)
- `binary-tree-2` — 9 scenarios (`useCases/`)

Structure (from TCK source, `tck/graphs/`):
- `binary-tree-1`: 15 nodes in a binary tree with `:A` and `:B` labels, `KNOWS` edges
- `binary-tree-2`: similar with additional structure for triadic selection tests

## IVG API Binding

The step definitions call `IRISGraphEngine` through the `execute_cypher` surface:

```python
# From tests/conftest.py — execute_cypher fixture pattern
def execute_cypher(query, params=None):
    result = engine.execute_cypher(query, params or {})
    return result  # IVGResult with .rows, .columns
```

For the harness, `environment.py` creates a single session-scoped engine and
passes it to all step definitions via `context.engine`. Each scenario's UUID
label is injected by a `before_scenario` hook that patches the engine's
`_tck_label` context attribute used by the step definitions.

**Label injection approach**: Step definitions rewrite `CREATE` queries to append
`:<UUID_LABEL>` to every node pattern before execution. `MATCH` queries in
`Then` steps also append `:<UUID_LABEL>` as a filter. This is done in the step
definition Python layer (not in the `.feature` files) by a lightweight query
rewriter that uses the IVG parser's AST.

## Comparison Engine

TCK result table format:
```gherkin
Then the result should be:
  | name    | age |
  | 'Alice' | 20  |
  | 'Bob'   | 30  |
```

Type normalisation rules:
| TCK notation | Python/IRIS type |
|---|---|
| `'Alice'` (quoted) | `str` |
| `20` (unquoted integer) | `int` |
| `20.0` (float) | `float` |
| `null` | `None` |
| `true` / `false` | `bool` |
| `[1, 2, 3]` | `list` |
| `{name: 'Alice'}` | `dict` |

IRIS returns strings for everything from `rdf_props.val`. The comparison step
must cast IRIS values to the expected TCK type for each column. This requires
the TCK table header to carry type hints OR the comparison to use TCK value as
the reference type (parse TCK value first, then cast IRIS value to match).
Decision: parse TCK value first to determine expected type, then cast IRIS result.
