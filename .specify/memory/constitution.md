<!--
SYNC IMPACT REPORT
==================
Version change: 1.1.1 → 1.2.0 (MINOR — new principle VII added)
Bump rationale: Added Principle VII (Cypher Conformance Gates) after the
  direction-symmetry cross-join bug was found independently in two Cypher
  implementations (cbm HEAD 571196db and IVG ≤v2.5.1).  The openCypher TCK
  (Match7) partially covers bound-at-nodes[1] patterns but has NO scenario
  asserting that (a)-[:R]->(b) and (b)<-[:R]-(a) return identical results when
  b is pre-bound.  This principle closes that gap for IVG by requiring a
  direction-symmetry E2E gate in every spec that touches the Cypher translator.

Modified principles:
  - VII: new principle added — Cypher Conformance Gates

No other principles changed.  Templates do not require updates.
-->

# iris-vector-graph Constitution

## Core Principles

### I. Library-First

All features MUST be implemented within the library codebase and remain self-contained,
independently testable, and documented. Application-specific workarounds are not acceptable
as the primary interface.

### II. Compatibility-First Interfaces

Existing public interfaces (library API and any CLI entry points) MUST remain backward
compatible unless a documented breaking change is explicitly approved. New features MUST
extend, not replace, existing usage patterns.

### III. Test-First (Non-Negotiable)

All feature work MUST follow a test-first approach. Tests are written or updated before
implementation and MUST fail before code changes are introduced.

### IV. Integration & End-to-End Testing for IRIS

Any feature that includes IRIS as a backend component MUST include comprehensive end-to-end
(e2e) tests that run against a live IRIS container. The following rules are non-negotiable:

- The IRIS container MUST be named and dedicated to this project (not shared or anonymous).
  This project uses two containers:
  - `ivg-iris` (Community Edition) — primary test container, port 21972.
  - `ivg-iris-enterprise` (Enterprise + Arno callout) — Arno/rzf acceleration tests, port 31972.
  NEVER use a container name from another project (e.g. `los-iris`, `posos-iris`).
- Container lifecycle MUST be managed exclusively via `scripts/test-container.sh` (Community)
  or `scripts/enterprise-container.sh` (Enterprise). IRIS ports MUST NOT be hardcoded in
  test code — use `IVG_PORT` / `IVG_ARNO_PORT` env vars with the registry defaults.
- Enterprise Arno callout tests MUST use the `arno_iris_connection` fixture, which auto-skips
  when `ivg-iris-enterprise` is not running. NEVER use `iris_connection` (Community) for tests
  requiring `libarno_callout.so`.
- The environment variable `SKIP_IRIS_TESTS` MUST default to `"false"`. Tests always hit the
  live database unless explicitly overridden by the developer.
- Changes that affect database behavior or SQL translation MUST additionally include
  integration tests (in `tests/integration/`) that validate behavior at the SQL layer.
- Unit tests alone are insufficient to satisfy this principle for IRIS-backend features.

**Rationale**: This project is a knowledge graph engine built on InterSystems IRIS. Behavior
that cannot be observed without a live database (vector indexing, SQL translation, schema
migration, Cypher execution) cannot be validated by mocks. Skipping live tests has
historically caused regressions discovered only by downstream consumers (`posos`, `iris-vector-rag`).

### V. Simplicity and Clarity

Prefer the simplest design that meets requirements. Avoid unnecessary abstractions or
over-engineering. Every layer of indirection MUST be justified by a concrete requirement.

### VI. Grounding Rule (Verify Before You Write)

Any infrastructure detail — container names, port numbers, schema names, credentials,
package names, file paths — written into specs, tests, templates, or commit messages MUST
first be verified against the authoritative source in this repository before use.

**Authoritative sources**:
- Container name → `docker-compose.yml` (`container_name:` field)
- IRIS port → `docker-compose.yml` (`ports:` field)
- Package name / version → `pyproject.toml`
- Schema prefix → `iris_vector_graph/engine.py` (`set_schema_prefix(...)` call)
- Test infrastructure → `tests/conftest.py`

**Never assume. Never copy from another project. Always look first.**

Violation of this rule caused the `los-iris` incident (Feb 2026): a container name from
an unrelated project was propagated into the constitution, all spec artifacts, and test
code before being caught. The fix required amending 8+ files. The cost is not acceptable.

### VII. Cypher Conformance Gates

Any spec or feature that modifies the Cypher translator (`cypher/translator.py`,
`cypher/parser.py`, or any `_engine/` mixin that generates SQL from a Cypher pattern)
MUST include an E2E integration test that asserts **direction-symmetry**:

> For any directed pattern `(a)-[:R]->(b)` and its mirror `(b)<-[:R]-(a)`, when one
> of the variables is pre-bound from a preceding MATCH clause, both forms MUST return
> identical result sets against a live IRIS database.

**Rationale**: The openCypher TCK (Match7.feature) partially covers bound-target
patterns but contains no scenario asserting direction equivalence for directed patterns.
This gap allowed the same cross-join bug to ship in two independent implementations
(cbm `571196db` and IVG ≤v2.5.1) without being caught by conformance tests.  The bug
is invisible in unit tests because the SQL strings differ but look plausible; only a
live database reveals the wrong row counts.

**Conformance resources to consult when changing the translator**:
- openCypher TCK: `tck/features/clauses/match/Match7.feature` (bound-target scenarios)
- openCypher OPTIONAL MATCH CIP: `CIP2015-09-16-OPTIONAL-MATCH.adoc`
- IVG regression: `tests/integration/test_cypher_advanced.py::test_integration_direction_symmetry_optional_match`
- Bug record: `specs/` or `productivity-framework/specs/073-suite-failure-repair/CBM-BUG-optional-match-cross-join.md`

**Unit tests alone are insufficient** for translator changes that affect JOIN shape.

## Additional Constraints

- Use the existing RDF schema (`nodes`, `rdf_labels`, `rdf_props`, `rdf_edges`,
  `kg_NodeEmbeddings`) unless a schema change is explicitly approved and documented.
- Numeric comparisons MUST be deterministic and documented when values are stored as strings.
- `iris-vector-rag` MUST NOT be added as a dependency of `iris-vector-graph`. The two
  packages are siblings; shared behavior belongs in `iris-vector-graph` or a new shared
  package, not through cross-dependency.

## Development Workflow

- All work MUST be traceable to a spec and plan.
- Feature changes MUST be grouped by user story to support incremental delivery.
- Every plan for a feature with an IRIS backend component MUST include an explicit e2e test
  task group (per Principle IV) as a non-optional phase, not as a polish/optional item.
- Before writing any infrastructure detail into a spec or test, verify it against the
  authoritative source (Principle VI). This is a blocking prerequisite, not a suggestion.

## Governance

This constitution supersedes all other development guidance for this repository. Any
amendments MUST be documented and explicitly approved before implementation begins.
Version increments follow semantic versioning: MAJOR for backward-incompatible governance
changes, MINOR for new or materially expanded principles, PATCH for clarifications.

**Version**: 1.2.0 | **Ratified**: 2026-01-31 | **Last Amended**: 2026-08-01
