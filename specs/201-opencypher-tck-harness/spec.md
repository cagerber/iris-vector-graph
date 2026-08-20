# Feature Specification: openCypher TCK Harness

**Feature Branch**: `201-opencypher-tck-harness`
**Created**: 2026-08-01
**Status**: Draft

## Background

IVG has never run the openCypher TCK. The direction-symmetry cross-join bug
(fixed in v2.5.2) was caught only because the same bug appeared in an unrelated
project — not because any conformance test caught it. The openCypher TCK contains
1,339 scenarios across 220 `.feature` files and is the only authoritative
correctness reference for Cypher semantics. The official runner is Scala/JVM only;
no Python harness exists. This spec builds one.

**Constitution VII** mandates this work. The direction-symmetry E2E gate
(`test_integration_direction_symmetry_optional_match`) is the minimum bar until
this harness ships.

## Clarifications

### Session 2026-08-01

- Q: How should per-scenario isolation work without modifying `.feature` files? → A: Label-based — each scenario gets a unique UUID tag label; all created nodes receive it; teardown is `MATCH (n:UUID_label) DETACH DELETE n`.
- Q: How should `behave` integrate with `pytest`? → A: `behave` runs as a subprocess, emits JUnit XML; a pytest plugin collects results so `pytest tests/tck/` works as a single invocation.
- Q: When the harness finds a TCK failure IVG can't immediately fix? → A: First run auto-generates the `@wip` baseline from all failures; subsequent runs fail CI on any regression from that baseline.

---

## Scope

IVG implements translators for all major Cypher clauses and expression types.
The goal is to run **all 1,339 TCK scenarios** — not a curated subset.
Scenarios IVG genuinely cannot pass are tagged `@wip` with a filed reason; they
are excluded from the CI pass gate but remain visible in the report so the gap
shrinks over time.

**Default assumption: everything is in scope.** The `@wip` list is the
exception, not the rule. A scenario goes `@wip` only if it requires a feature
IVG does not implement *and* cannot reasonably implement within this spec.

**Known `@wip` categories at spec time** (all others run and must pass):

| TCK path | Reason for `@wip` |
|---|---|
| `expressions/graph` | Neo4j-specific graph object expressions (`graph.names()`, `graph.propertiesByName()`) — not standard Cypher, no IRIS equivalent |
| `expressions/temporal` | IVG's temporal model uses IRIS `^KG` globals + `Graph.KG.TemporalIndex`, not standard Cypher `date()`/`datetime()`/`duration()` functions |
| `expressions/pattern` | Inline pattern predicates beyond `EXISTS {}` (e.g. count pattern predicates) — partial support |
| `@NegativeTest` scenarios requiring exact error message text | IRIS error messages differ from Neo4j's; error *type* is correct but message text is not |

Everything else — `clauses/match*`, `clauses/optional-match`, `clauses/return*`,
`clauses/with*`, `clauses/create`, `clauses/delete`, `clauses/merge`,
`clauses/set`, `clauses/remove`, `clauses/unwind`, `clauses/union`,
`clauses/call`, all remaining `expressions/` categories, and both `useCases/`
— is **in scope and must pass at 100%** or be explicitly `@wip` with a
documented reason.

The `@wip` count MUST be reported in CI and MUST NOT increase on any PR unless
a new TCK category is added to scope.

---

## User Stories

### User Story 1 — Harness infrastructure (P1)

A developer running `pytest` can execute the in-scope TCK scenarios against a
live `ivg-iris` container and see a pass/fail count. The TCK `.feature` files
are consumed unchanged from a vendored submodule; IVG step definitions bridge
TCK actions to the IVG engine.

**Why this priority**: Nothing else is testable without the harness. Unblocks
all subsequent stories.

**Independent Test**: Running `pytest tests/tck/ -m tck` against a live
container produces results (passes, failures, or `@wip` skips) — no import
errors, no crashes.

**Acceptance Scenarios**:

1. **Given** `ivg-iris` is running, **When** `pytest tests/tck/ -m tck` runs,
   **Then** it exits 0 (all in-scope scenarios pass or are skipped with `@wip`).
2. **Given** a scenario tagged `@wip`, **When** the suite runs, **Then** it is
   reported as skipped, not failed — the `@wip` count is reported in the summary.
3. **Given** the openCypher submodule is absent, **When** the suite runs,
   **Then** a clear error instructs the developer to run `git submodule update --init`.

---

### User Story 2 — Match / pattern matching scenarios (P1)

All in-scope `clauses/match*` and `clauses/optional-match` scenarios pass.
This is the category that contains the direction-symmetry gap (Match7) that
triggered this spec.

**Why this priority**: Pattern matching is IVG's core operation. A passing
Match suite means the translator handles every TCK-defined pattern combination
correctly, not just the ones we happened to think of.

**Independent Test**: `pytest tests/tck/ -k "Match"` passes with zero failures
against `ivg-iris`. Any new failure is a translator regression.

**Acceptance Scenarios**:

1. **Given** the match test fixtures, **When** `clauses/match/Match7.feature`
   runs, **Then** all scenarios — including Scenario 3 (bound-at-nodes[1]) —
   pass.
2. **Given** a directed pattern `(a)-[:R]->(b)` and its mirror `(b)<-[:R]-(a)`
   with `b` pre-bound, **When** both scenarios run, **Then** both pass with
   identical result sets (direction-symmetry invariant).
3. **Given** `clauses/optional-match` scenarios, **When** the suite runs,
   **Then** all pass, including left-outer-join null-propagation cases.

---

### User Story 3 — Expression and aggregation scenarios (P2)

All in-scope `expressions/aggregation`, `expressions/boolean`,
`expressions/comparison`, `expressions/null`, `expressions/string`,
`expressions/mathematical`, and `expressions/list` scenarios pass.

**Why this priority**: Aggregation and expression correctness are the most
common sources of subtle wrong-result bugs (invisible to type checkers).

**Independent Test**: `pytest tests/tck/ -k "Aggregation or Boolean or
Comparison or Null or String or Mathematical or List"` passes with zero
failures.

**Acceptance Scenarios**:

1. **Given** `expressions/aggregation` scenarios, **When** run, **Then** all
   `count()`, `sum()`, `avg()`, `min()`, `max()`, `collect()` scenarios pass.
2. **Given** `expressions/null` scenarios, **When** run, **Then** null
   propagation through arithmetic, comparisons, and function calls is correct.
3. **Given** `expressions/existentialSubqueries`, **When** run, **Then** all
   `EXISTS { }` and `NOT EXISTS { }` scenarios pass.

---

### User Story 4 — WITH / RETURN pipeline scenarios (P2)

All in-scope `clauses/return*` and `clauses/with*` scenarios pass.

**Why this priority**: WITH and RETURN are used in every non-trivial query;
incorrect pipelining produces silent wrong results.

**Independent Test**: `pytest tests/tck/ -k "Return or With"` passes.

**Acceptance Scenarios**:

1. **Given** `clauses/with` scenarios, **When** run, **Then** variable scoping
   across WITH boundaries is correct.
2. **Given** ORDER BY, SKIP, LIMIT scenarios, **When** run, **Then** results
   are ordered and bounded correctly.

---

### User Story 5 — CI integration and gap reporting (P3)

The TCK suite runs in CI on every PR that touches `cypher/`. A failure in any
previously-passing scenario blocks merge. The `@wip` count is reported so the
gap is visible, not hidden.

**Why this priority**: The harness is only useful if it catches regressions
automatically. Manual invocation is not sufficient.

**Independent Test**: A GitHub Actions workflow runs `pytest tests/tck/ -m
tck` and posts a summary comment showing pass count and `@wip` count.

**Acceptance Scenarios**:

1. **Given** a PR that breaks a previously-passing TCK scenario, **When** CI
   runs, **Then** the check fails and the broken scenario is named in the output.
2. **Given** a PR that adds `@wip` coverage (a previously-skipped scenario now
   passes), **When** CI runs, **Then** the `@wip` count decreases and the PR
   author is prompted to remove the tag.

---

## Edge Cases

- TCK scenarios use typed value notation (`{name: 'Alice'}`, integer `1` vs
  float `1.0`). The result-comparison step must normalise IRIS return types to
  TCK types without false failures.
- Some TCK `Given` steps require an empty graph. IVG's schema is shared; the
  harness MUST use an isolated test namespace (node-id prefix, separate label)
  and clean up after each scenario.
- TCK `null` values must round-trip correctly — IRIS returns `None`; the
  comparison step must treat these as equal.
- Scenarios that require specific Neo4j error codes or messages MUST be tagged
  `@wip`; IVG may raise equivalent errors with different text.
- The TCK submodule pin MUST be updated deliberately (not auto-updated), so
  new TCK scenarios don't silently land as failures.

---

## Requirements

### Functional Requirements

- **FR-001**: MUST vendor the openCypher repo as a git submodule at
  `vendor/opencypher` pinned to a specific commit.
- **FR-002**: MUST implement `behave` step definitions for the four TCK step
  families: `Given an empty graph`, `Given having executed [q]`,
  `When executing query [q]`, `Then the result should be [table] / no side
  effects / an error`.
- **FR-002a**: MUST also implement non-standard TCK step patterns identified in
  research: `And parameters are: [table]` (43 scenarios — parse into param dict),
  `When executing control query: [q]` (27 scenarios — run, discard result),
  error-type assertion steps `Then a <Type> should be raised` (~24 scenarios —
  assert exception class, not message text), and list-order variants (17 scenarios —
  `Then the result should be (ignoring element order for lists)`). Procedure
  registration steps (`And there exists a procedure test.*`, ~35 scenarios) go
  `@wip`.
- **FR-003**: Step definitions MUST use `ivg-iris` (Community, port 21972) via
  the standard `IVG_PORT` env var. NEVER hardcode the port.
- **FR-004**: Each scenario MUST run in an isolated namespace using label-based
  isolation: a unique UUID tag label is attached to every node created during
  the scenario; teardown runs `MATCH (n:UUID_label) DETACH DELETE n` even on
  failure. Node names like `'Alice'` remain unchanged in queries.
- **FR-005**: Scenarios IVG cannot pass MUST be recorded in a local `wip.txt`
  overlay (one scenario title per line, with reason comment). The first harness
  run auto-generates this baseline from all failures. Never modify vendored
  `.feature` files. The `@wip` set MUST shrink over time — it MUST NOT grow on
  any subsequent PR.
- **FR-006**: `behave` runs as a subprocess emitting JUnit XML; a thin pytest
  plugin at `tests/tck/conftest.py` collects that XML so `pytest tests/tck/`
  is a single invocation. `pytest-bdd` is not used.
- **FR-007**: CI MUST run the suite on every PR touching `cypher/translator.py`,
  `cypher/parser.py`, or `_engine/`.
- **FR-008**: MUST report the `@wip` scenario count in the test summary so the
  gap is visible, not hidden.
- **FR-009**: Result comparison MUST handle: unordered rows (unless TCK says
  `in order`), `null` ↔ `None`, integer/float type normalisation, and string
  values.

### Key Entities

- **TCK scenario**: a `.feature` file `Scenario:` block with `Given`/`When`/`Then` steps
- **Step definition**: Python function registered with `behave` matching a step pattern
- **`@wip` overlay**: a local file listing scenario titles that are skipped in CI;
  must not modify vendored `.feature` files
- **Test namespace**: per-scenario isolated node-id prefix ensuring scenarios
  don't interfere with each other or with the permanent graph data

---

## Success Criteria

### Measurable Outcomes

- **SC-001**: All 1,339 TCK scenarios run (pass or `@wip` skip) with zero
  crashes or import errors.
- **SC-002**: Every scenario that runs (not `@wip`) passes at 100%. A failing
  non-`@wip` scenario blocks merge — it must either be fixed or explicitly
  promoted to `@wip` with a filed reason.
- **SC-003**: `@wip` count is reported in CI and documented at merge. It MUST
  NOT increase on any PR unless a new TCK category is added to scope.
- **SC-004**: CI runtime for the TCK suite ≤10 minutes against `ivg-iris`.
- **SC-005**: No vendored `.feature` file is modified. All local adaptations
  live in the `@wip` overlay and step definitions.
- **SC-006**: The direction-symmetry regression
  (`test_integration_direction_symmetry_optional_match`) remains in
  `tests/integration/` as a standalone gate independent of the TCK suite.

---

## Out of Scope

- A GQL / ISO 39075 conformance suite (no public test suite exists as of 2026-08-01).
- Modifying the openCypher `.feature` files.
- Replacing the existing `tests/integration/test_cypher_*.py` tests — the TCK
  harness supplements them.
- Fixing IVG translator bugs discovered by TCK failures (each becomes a separate
  spec/PR; the harness spec ends when the infrastructure is in place and the
  initial `@wip` baseline is documented).
