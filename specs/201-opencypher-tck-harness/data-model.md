# Data Model: openCypher TCK Harness

## ScenarioContext

Passed via `behave`'s `context` object to all step definitions within a scenario.

```python
@dataclass
class TckScenarioContext:
    engine: IRISGraphEngine       # session-scoped, shared
    scenario_label: str           # UUID label for this scenario's nodes e.g. "TCK_a3f9b2"
    created_nodes: list[str]      # node_ids created this scenario (for teardown fallback)
    last_result: IVGResult | None # result of the most recent When step
    last_error: Exception | None  # exception raised by the most recent When step
    params: dict                  # populated by "And parameters are:" step
```

`scenario_label` is generated in `before_scenario` as `f"TCK_{uuid4().hex[:8]}"`.

## Named Graph Registry

Session-scoped. Pre-built once in `before_all`, each graph stored under its own
session label (e.g. `TCK_BINARY_TREE_1`).

```python
NAMED_GRAPHS: dict[str, str] = {
    "binary-tree-1": "TCK_BINARY_TREE_1",
    "binary-tree-2": "TCK_BINARY_TREE_2",
}
```

`Given the binary-tree-1 graph` step sets `context.scenario_label` to the
pre-built label so subsequent reads are scoped correctly. No teardown — named
graphs persist for the session.

## TCKValue (result comparison)

```python
@dataclass
class TCKValue:
    raw: str          # original TCK table cell text e.g. "'Alice'", "20", "null"
    python: Any       # parsed Python value: str, int, float, bool, None, list, dict

    @staticmethod
    def parse(cell: str) -> "TCKValue": ...
```

Type parsing rules (from `research.md`):
- `'...'` → `str` (strip quotes)
- unquoted integer → `int`
- unquoted float → `float`
- `null` → `None`
- `true`/`false` → `bool`
- `[...]` → `list` (recursive parse)
- `{...}` → `dict` (recursive parse)

## TCKResultTable

```python
@dataclass
class TCKResultTable:
    columns: list[str]
    rows: list[list[TCKValue]]
    ordered: bool       # True if step says "in order"
    list_unordered: bool  # True if step says "ignoring element order for lists"
```

## WipRegistry

Loaded from `tests/tck/wip.txt` at session start. Format:

```
# reason: procedure registration not supported
clauses/call/Call4.feature::Scenario: Calling a procedure that has an argument
# reason: Neo4j graph object functions not in standard Cypher
expressions/graph/Graph1.feature::Scenario: ...
```

A scenario is skipped (not failed) if its `feature_file::Scenario: title` appears in `WipRegistry`.

## Error Type Map

Maps TCK error step patterns to expected Python exception types from IVG:

```python
ERROR_TYPE_MAP = {
    "TypeError": (TypeError, IVGTypeError),
    "ArgumentError": (ValueError, IVGArgumentError),
    "EntityNotFound": (KeyError, IVGEntityNotFoundError),
    "SemanticError": (IVGSemanticError,),
    "SyntaxError": (SyntaxError, IVGSyntaxError),
    "ProcedureError": (IVGProcedureError,),
    "ParameterMissing": (KeyError, IVGParameterError),
    "ConstraintVerificationFailed": (IVGConstraintError,),
}
```

If IVG raises any exception in `ERROR_TYPE_MAP[error_type]`, the step passes.
If IVG raises a different exception type, the step fails with a diff showing
expected vs actual exception.
