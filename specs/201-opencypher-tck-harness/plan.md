# Implementation Plan: openCypher TCK Harness

**Branch**: `201-opencypher-tck-harness` | **Date**: 2026-08-01 | **Spec**: `specs/201-opencypher-tck-harness/spec.md`

## Summary

Build a `behave`-based harness that runs all **1,339** openCypher TCK scenarios
(verified count: 220 feature files, pinned at commit `677cbafabb8c3c5eed458fd3b1ec0daec8d67d23`)
against a live `ivg-iris` container. `behave` emits JUnit XML; a thin pytest
plugin at `tests/tck/conftest.py` collects it so `pytest tests/tck/` is a
single invocation. Per-scenario isolation uses UUID label tags (no modification
to vendored `.feature` files). The first run generates a `wip.txt` baseline
from all failures; subsequent runs fail CI on any regression.

## Technical Context

**Language/Version**: Python 3.11+ (matches existing project)
**Primary Dependencies**:
- `behave>=1.2.6` — Gherkin runner (no Python TCK runner exists; official is Scala)
- `lxml` or stdlib `xml.etree` — parse JUnit XML in pytest collector
- openCypher repo as git submodule at `vendor/opencypher` (pinned commit)
- `iris_vector_graph` engine (existing) — step definitions call `engine.execute_cypher()`

**Storage**: `ivg-iris` Community container — `iris_vector_graph` schema, port 21972 via `IVG_PORT` env var

**Testing**: `pytest` (existing harness) + `behave` subprocess

**Target Platform**: Linux (CI) + macOS (local dev, Docker Desktop/OrbStack)

**Performance Goals**: Full TCK suite (1,339 scenarios) ≤10 minutes on `ivg-iris`

**Additional dependencies discovered in research**:
- Non-standard step patterns require additional step definitions beyond the 4 standard families:
  `And parameters are:` (43 scenarios), `When executing control query:` (27),
  error-type assertions (`Then a TypeError/ArgumentError/... should be raised`) (~24 combined),
  list-order variants (17). Only procedure registration steps (~35) go `@wip`.
- Named graph fixtures (`binary-tree-1`, `binary-tree-2`) required by 19 scenarios —
  session-scoped pre-built fixture graphs, not per-scenario.
- Only **1** scenario is actually tagged `@NegativeTest` — error-type steps don't require
  exact message text matching.

**Constraints**:
- NEVER modify vendored `.feature` files
- NEVER hardcode port 21972 — use `IVG_PORT` env var (verified from `tests/conftest.py:107`)
- Container name `iris_vector_graph` (verified from `docker-compose.yml`)
- `SKIP_IRIS_TESTS` defaults to `"false"` — TCK tests always hit live DB

## Constitution Check

**Principle I (Library-First)**: ✅ Harness is test infrastructure, not application logic. Step definitions wrap the existing `IRISGraphEngine` public API.

**Principle III (Test-First)**: ✅ The harness IS the test. TCK scenario step definitions are the tests; they drive any translator fixes discovered.

**Principle IV (IRIS E2E)**: ✅
- [x] Container `iris_vector_graph` (Community, port 21972) via `IRISContainer.attach()` — verified from `docker-compose.yml` and `tests/conftest.py`
- [x] Explicit E2E phase (Phase 3) — non-optional, not in polish
- [x] `SKIP_IRIS_TESTS` defaults `"false"` — no new test files deviate from this
- [x] Port via `IVG_PORT` env var — no hardcoding

**Principle V (Simplicity)**: ✅ `behave` subprocess + JUnit XML collection is the minimum viable integration. `pytest-bdd` was considered and rejected (requires mapping all TCK Gherkin patterns to pytest-bdd fixtures; more work, same result).

**Principle VI (Grounding Rule)**: ✅ All infrastructure details verified:
- Container name: `iris_vector_graph` ← `docker-compose.yml` `container_name:`
- Port default: `21972` ← `tests/conftest.py:107` `IVG_PORT` default
- Test infra fixture: `iris_connection`, `execute_cypher` ← `tests/conftest.py`

**Principle VII (Cypher Conformance Gates)**: ✅ This spec IS the conformance obligation.

## Architecture Decisions

### AD-1: behave subprocess + JUnit XML (not pytest-bdd)

`behave` is the standard Python Gherkin runner. The TCK `.feature` files use
Gherkin verbatim; `behave` parses them without modification. `pytest-bdd` would
require translating all TCK step patterns into pytest fixture signatures — more
work for identical output. The subprocess approach keeps the boundary clean:
`behave` owns Gherkin execution; pytest owns test collection and reporting.

### AD-2: Label-based scenario isolation

TCK scenarios create nodes with names like `'Alice'`, `'Bob'` directly in
queries (e.g. `CREATE (n:Person {name: 'Alice'})`). Rewriting queries to inject
UUID prefixes would require modifying `.feature` files (forbidden). Instead,
every `CREATE` in a scenario's `Given`/`When` steps runs in an engine context
that adds a per-scenario UUID label to every created node. Teardown:
`MATCH (n:UUID_LABEL) DETACH DELETE n`. Read queries (MATCH) also add the UUID
label filter so scenarios don't bleed into each other.

**Implementation**: `behave` `environment.py` hooks inject the UUID label into
the `IRISGraphEngine` context before each scenario and teardown after.

### AD-3: wip.txt auto-baseline, not manual tagging

First run (`make tck-baseline`) executes all scenarios, collects all failures,
and writes them to `tests/tck/wip.txt` (one scenario title + file per line,
with a `# TODO:` comment). Subsequent runs skip scenarios in `wip.txt` and fail
on any result that differs from the baseline (new failure = regression; scenario
in wip.txt now passing = "remove the tag" warning). This avoids blocking the
initial merge on fixing all TCK failures while still tracking the gap honestly.

### AD-4: TCK submodule pinned, never auto-updated

`vendor/opencypher` is a git submodule pinned to a specific commit. TCK updates
are deliberate — new scenarios don't silently land as failures. Pin update is its
own PR with a changelog entry noting which new scenarios were added/changed.

### AD-5: Result comparison strategy

TCK `Then` steps use a typed table notation. Comparison order:
1. If step says `in order` — ordered comparison
2. Otherwise — sort both result sets and compare (unordered)
3. Type normalisation: IRIS `None` → TCK `null`, integer strings → int, IRIS
   `Decimal` → float where TCK expects float
4. Node/relationship values: compare by properties only (TCK doesn't assert
   internal IDs)

## Project Structure

```text
vendor/
└── opencypher/           # git submodule — openCypher repo, pinned

tests/tck/
├── conftest.py           # pytest plugin: runs behave, collects JUnit XML
├── environment.py        # behave hooks: scenario UUID label, teardown
├── steps/
│   ├── __init__.py
│   ├── graph_setup.py    # Given an empty graph / Given having executed
│   ├── query.py          # When executing query
│   ├── results.py        # Then the result should be / no side effects / error
│   └── comparison.py     # Type normalisation + ordered/unordered comparison
├── wip.txt               # Auto-generated baseline — scenario titles to skip
└── wip_generate.py       # Script: runs full suite, writes wip.txt from failures

specs/201-opencypher-tck-harness/
├── plan.md               # This file
├── research.md           # Phase 0 output
├── data-model.md         # Phase 1 output
└── tasks.md              # Phase 2 output (/speckit.tasks)
```

## Phase 0: Research

### R-1: Exact TCK scenario count and category breakdown

Fetch the openCypher repo, count `.feature` files and scenarios per category.
Establish the exact pin commit. Confirm which scenarios use step patterns not in
the four standard families (if any exist, they need additional step definitions).

**Output**: `research.md` with scenario counts, pin commit, and complete step
pattern inventory.

### R-2: TCK step pattern inventory

The four standard TCK step families cover most scenarios. Confirm by grepping
all `Given`/`When`/`Then` lines in the target `.feature` files for patterns
outside the standard four. Any non-standard patterns need additional step
definitions before Phase 2.

### R-3: IVG execute_cypher API surface

Confirm the exact call signature for running a Cypher query through
`IRISGraphEngine` and getting tabular results back. The step definitions need to:
- Parse TCK's `CREATE (n:Person {name: 'Alice'})` and inject the UUID label
- Run `MATCH` queries scoped to the UUID label
- Return results in a form comparable to the TCK table

**Output**: `research.md` section on IVG API binding.

## Phase 1: Design

### D-1: Step definition interface

Specify the exact Python signatures for each step family and how they bind to
IVG's `execute_cypher` / engine API. Define the `ScenarioContext` dataclass
that carries the UUID label, connection, and accumulated created-node tracking
across steps within a scenario.

### D-2: Result comparison spec

Define the full type normalisation table (IRIS type → TCK type), the comparison
algorithm (ordered vs unordered), and the diff format shown on failure.

### D-3: pytest collector design

Specify how `tests/tck/conftest.py` invokes `behave`, passes the JUnit XML path,
and registers the TCK results as pytest items so they appear in normal `pytest`
output with correct pass/fail/skip status.

### D-4: CI integration

Specify the GitHub Actions job: trigger on PRs touching `cypher/` or `_engine/`,
run `pytest tests/tck/ -m tck`, report `@wip` count in job summary.

**Output**: `data-model.md` (ScenarioContext, comparison types), `quickstart.md`
(developer workflow for running TCK locally and adding to wip.txt).

## Phase 2: Implementation (tasks.md)

Five phases in tasks.md:

1. **Setup** — submodule, `behave` dependency, directory scaffold
2. **Step definitions** — all four step families + comparison engine
3. **pytest integration** — collector, conftest, marker registration
4. **Baseline generation** — `wip_generate.py`, initial `wip.txt`
5. **CI** — GitHub Actions job, `@wip` count reporting

## Phase 3: E2E Gate

After baseline is generated, the suite must:
- Run all 1,339 scenarios (pass + skip, zero crashes)
- Report `@wip` count
- Pass in ≤10 minutes on `ivg-iris`

This phase is the delivery gate. The `@wip` count at merge is the documented
baseline for all future PRs.
