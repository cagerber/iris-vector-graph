# Tasks: openCypher TCK Harness (201)

**Feature**: 201-opencypher-tck-harness
**Total tasks**: 42
**Phases**: Setup → Step Definitions → pytest Integration → Baseline → CI

---

## Phase 1 — Setup & Scaffold

Goal: submodule vendored, `behave` installed, directory structure in place, harness
skeleton can be invoked without crashing.

**Phase gate (E2E)**: `behave vendor/opencypher/tck/features/clauses/match/Match1.feature --dry-run` exits 0 (step definitions found, no import errors).

- [ ] T001 Add openCypher git submodule at `vendor/opencypher` pinned to commit `677cbafabb8c3c5eed458fd3b1ec0daec8d67d23` via `git submodule add https://github.com/opencypher/openCypher vendor/opencypher`
- [ ] T002 Pin the submodule: `cd vendor/opencypher && git checkout 677cbafabb8c3c5eed458fd3b1ec0daec8d67d23 && cd ../..`
- [ ] T003 Add `behave>=1.2.6` to `[project.optional-dependencies]` `tck` group in `pyproject.toml`
- [ ] T004 Create `tests/tck/` directory with `__init__.py`, `steps/__init__.py`
- [ ] T005 Create `tests/tck/steps/comparison.py` — skeleton `TCKValue.parse()` and `TCKResultTable` dataclasses from `specs/201-opencypher-tck-harness/data-model.md`
- [ ] T006 Create `tests/tck/steps/__init__.py` empty module
- [ ] T007 Create `tests/tck/environment.py` — skeleton `before_all`, `before_scenario`, `after_scenario` hooks (no-op bodies, imports only)
- [ ] T008 Create `tests/tck/wip.txt` empty file with header comment `# TCK wip baseline — scenarios IVG cannot yet pass. DO NOT grow this list.`
- [ ] T009 Write unit test `tests/unit/tck/test_tck_value_parse.py` covering `TCKValue.parse()` for all type cases: string, int, float, null, bool, list, dict — tests MUST fail before T005 implementation
- [ ] T010 [P] Write unit test `tests/unit/tck/test_result_comparison.py` covering ordered/unordered comparison, null equality, list-unordered variant — tests MUST fail before T005 implementation

---

## Phase 2 — Step Definitions (US1, US2, US3, US4)

Goal: All four standard step families plus all non-standard patterns implemented.
Step definitions call `IRISGraphEngine` via the session-scoped engine in `context.engine`.

**Phase gate (E2E)**: `pytest tests/tck/ -m tck -k "Match1"` runs against live `ivg-iris` with zero crashes (some failures expected; no import errors).

- [ ] T011 [US1] Write unit tests for `graph_setup.py` step definitions: `Given an empty graph` creates a fresh `scenario_label`; `Given having executed` runs a query and doesn't raise — `tests/unit/tck/test_steps_graph_setup.py`
- [ ] T012 [US1] Implement `tests/tck/steps/graph_setup.py`: `Given an empty graph`, `Given any graph`, `Given having executed [query]`, `Given the binary-tree-1 graph`, `Given the binary-tree-2 graph`
- [ ] T013 [US1] Implement `environment.py` `before_all` hook: create session engine via `iris_connection` fixture pattern (using `IVG_PORT` env var, container `iris_vector_graph`); build named graph fixtures (`binary-tree-1`, `binary-tree-2`) under session labels `TCK_BINARY_TREE_1` / `TCK_BINARY_TREE_2`
- [ ] T014 [US1] Implement `environment.py` `before_scenario` hook: generate `scenario_label = f"TCK_{uuid4().hex[:8]}"`, attach to `context`
- [ ] T015 [US1] Implement `environment.py` `after_scenario` hook: `MATCH (n:LABEL) DETACH DELETE n` teardown; runs even on failure
- [ ] T016 [US1] Write unit tests for `query.py` step: `When executing query` stores result in `context.last_result`; `When executing control query` runs and discards result — `tests/unit/tck/test_steps_query.py`
- [ ] T017 [US1] Implement `tests/tck/steps/query.py`: `When executing query [q]`, `When executing control query [q]`, `And parameters are: [table]` (parse TCK table into Python dict, store in `context.params`, pass to next `When executing query`)
- [ ] T018 [US1] Implement label injection in `query.py`: rewrite `CREATE` patterns to append `:<scenario_label>` to every node pattern using IVG's parser AST before execution
- [ ] T019 [US1] Implement label filter injection in `query.py`: for `MATCH` queries in `When` steps, append `:<scenario_label>` label filter so reads are scoped to this scenario's data
- [ ] T020 [US1] Write unit tests for `results.py`: `Then the result should be` (ordered/unordered/list-unordered variants), `Then the result should be empty`, `Then no side effects` — `tests/unit/tck/test_steps_results.py`
- [ ] T021 [US1] Implement `tests/tck/steps/results.py`: `Then the result should be [table]`, `Then the result should be, in order [table]`, `Then the result should be (ignoring element order for lists) [table]`, `Then the result should be empty`, `Then no side effects`
- [ ] T022 [US1] Implement error-type assertion steps in `results.py`: `Then a TypeError should be raised at runtime`, `Then a ArgumentError should be raised`, `Then a EntityNotFound should be raised`, `Then a SemanticError should be raised`, `Then a SyntaxError should be raised at compile time`, `Then a ProcedureError should be raised`, `Then a ParameterMissing should be raised`, `Then a ConstraintVerificationFailed should be raised` — using `ERROR_TYPE_MAP` from `data-model.md`
- [ ] T023 [US2] Write unit tests for `comparison.py` type normalisation: IRIS `None` → TCK `null`, IRIS `Decimal` → float, IRIS int-string → int — `tests/unit/tck/test_comparison_normalise.py`
- [ ] T024 [US2] Implement full type normalisation in `tests/tck/steps/comparison.py`: `normalise_iris_value(iris_val, expected_tck_val)` using expected TCK type as reference
- [ ] T025 [P] [US2] Implement `TCKResultTable.compare(actual_rows, actual_columns)` returning a diff string on mismatch or `None` on match — handles ordered, unordered, list-unordered variants

**Phase 2 E2E gate**: Run `pytest tests/tck/ -m tck -k "Match1 or Match2 or Match3"` against live `ivg-iris`. Must exit with no import errors or crashes. Record pass/fail counts (failures expected at this point; gate is infrastructure-only).

---

## Phase 3 — pytest Integration (US1, US5)

Goal: `pytest tests/tck/` works as a single invocation. `behave` runs as subprocess, JUnit XML collected, results appear in normal pytest output.

**Phase gate (E2E)**: `pytest tests/tck/ -m tck --tb=short` against live `ivg-iris` produces a pytest-format summary with individual scenario pass/fail/skip lines.

- [ ] T026 [US1] Write unit test for pytest collector: mock JUnit XML input → correct pytest items generated — `tests/unit/tck/test_tck_collector.py`
- [ ] T027 [US1] Implement `tests/tck/conftest.py` pytest plugin:
  - `collect_file` hook recognises `tck` marker
  - invokes `behave vendor/opencypher/tck/features/ --format json.pretty --outfile /tmp/tck_results.json --tags ~wip`
  - parses JSON output and registers each scenario as a `pytest.Item`
  - maps behave pass/fail/skip → pytest outcomes
- [ ] T028 [US1] Register `tck` pytest marker in `pyproject.toml` `[tool.pytest.ini_options]` `markers` list
- [ ] T029 [US5] Implement `@wip` skip logic in collector: load `tests/tck/wip.txt`; scenarios whose `feature::title` appears in wip.txt are collected as `pytest.skip` items
- [ ] T030 [US5] Implement `@wip` count summary: after collection, emit `# TCK wip: N scenarios skipped` in pytest session header via `pytest_sessionstart` hook

**Phase 3 E2E gate**: `pytest tests/tck/ -m tck -q 2>&1 | grep "TCK wip:"` — wip count line present. Suite runs to completion without crash.

---

## Phase 4 — Baseline Generation (US1, US5)

Goal: `wip.txt` populated from actual failures; harness can be run cleanly in CI.

**Phase gate (E2E)**: `pytest tests/tck/ -m tck` exits 0 — all non-wip scenarios pass, all failures are in `wip.txt`.

- [ ] T031 [US5] Implement `tests/tck/wip_generate.py`: runs `behave` with `--format json.pretty`, collects all failure scenario titles + feature files, writes `tests/tck/wip.txt` with reason comments derived from known `@wip` categories (from spec Scope table)
- [ ] T032 [US5] Run `python tests/tck/wip_generate.py` against live `ivg-iris` to generate initial `wip.txt`; review output; commit `wip.txt`
- [ ] T033 [US5] Write unit test for `wip_generate.py`: given mock behave JSON with known failures, output file matches expected format — `tests/unit/tck/test_wip_generate.py`
- [ ] T034 [US1] Run full `pytest tests/tck/ -m tck` against live `ivg-iris`; verify exit 0; record final pass count and wip count in PR description

---

## Phase 5 — CI Integration (US5)

Goal: TCK suite runs automatically on every PR touching `cypher/` or `_engine/`.

**Phase gate**: Push a test branch, verify GitHub Actions `tck` job runs and reports pass/wip counts in job summary.

- [ ] T035 [US5] Add `tck` job to `.github/workflows/ci.yml`:
  - trigger: `paths: ['iris_vector_graph/cypher/**', 'iris_vector_graph/_engine/**', 'tests/tck/**']`
  - steps: checkout with submodules (`submodules: true`), start `ivg-iris` container (`IVG_AUTO_START_CONTAINER=1`), `pip install -e ".[dev,tck]"`, `pytest tests/tck/ -m tck -q`
- [ ] T036 [US5] Add submodule checkout to existing CI jobs that run `pytest tests/` to avoid submodule-absent errors on branches where tck tests land in scope
- [ ] T037 [US5] Add job summary step to `tck` job: emit `echo "TCK: $PASS passed, $WIP skipped (wip)" >> $GITHUB_STEP_SUMMARY` using pytest JSON output
- [ ] T038 [US5] Write test `tests/unit/tck/test_wip_enforcement.py`: reads `wip.txt` and asserts each entry has a non-empty reason comment — prevents blank entries that hide the "why"

---

## Phase 6 — Documentation & Polish

- [ ] T039 Update `README.md` or `CONTRIBUTING.md` with TCK section: how to run, how to add to wip.txt, how to update the pin
- [ ] T040 Update `pyproject.toml` dev dependency docs / extras table to include `[tck]` extras group
- [ ] T041 Add `tests/tck/README.md` linking to `specs/201-opencypher-tck-harness/quickstart.md` for full developer workflow
- [ ] T042 Verify `scripts/test-container.sh up` documentation notes that TCK requires the container running before `pytest tests/tck/`

---

## Dependencies

```
T001 → T002 → T003 → T004
T004 → T005 → T009 (unit tests first)
T004 → T006 → T007
T009, T010 must FAIL before T005 implementation
T011 must FAIL before T012
T016 must FAIL before T017
T020 must FAIL before T021
T023 must FAIL before T024
T026 must FAIL before T027

Phase 1 complete → T011..T025 (Phase 2)
Phase 2 E2E gate pass → T026..T030 (Phase 3)
Phase 3 E2E gate pass → T031..T034 (Phase 4)
Phase 4 E2E gate pass → T035..T038 (Phase 5)
Phase 5 complete → T039..T042 (Phase 6)
```

## Parallel opportunities

- T009, T010 parallelizable (different test files)
- T011, T016, T020, T023 parallelizable (different step families, different unit test files)
- T024, T025 parallelizable after T023 passes
- T036, T037, T038 parallelizable after T035

## Implementation strategy

MVP = Phase 1 + Phase 2 + Phase 3 (T001–T030). This delivers a working harness
against live IRIS that a developer can invoke with `pytest tests/tck/ -m tck`.
Phase 4 (baseline) and Phase 5 (CI) complete the Constitution VII obligation.

## Task counts

| Phase | Tasks | Key deliverable |
|---|---|---|
| 1 Setup | T001–T010 | Scaffold, `TCKValue`, unit tests |
| 2 Step Definitions | T011–T025 | All step families, label injection, comparison |
| 3 pytest Integration | T026–T030 | `pytest tests/tck/` single invocation |
| 4 Baseline | T031–T034 | `wip.txt` generated, suite exits 0 |
| 5 CI | T035–T038 | Auto-runs on PR, reports wip count |
| 6 Docs | T039–T042 | Quickstart, README |
| **Total** | **42** | |
