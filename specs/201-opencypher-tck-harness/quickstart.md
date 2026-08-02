# Quickstart: openCypher TCK Harness

## Prerequisites

```bash
# 1. Initialize the submodule (first time only)
git submodule update --init vendor/opencypher

# 2. Install behave
pip install -e ".[dev,tck]"

# 3. Start the test container
scripts/test-container.sh up
```

## Run the full TCK suite

```bash
# Via pytest (recommended — single invocation, normal pytest output)
pytest tests/tck/ -m tck -v

# Via behave directly (for debugging a specific feature file)
behave vendor/opencypher/tck/features/clauses/match/ \
  --no-capture --format pretty
```

## Run a specific category

```bash
# All match scenarios
pytest tests/tck/ -m tck -k "Match"

# A single feature file
pytest tests/tck/ -m tck -k "Match7"
```

## Generate / update the wip.txt baseline

Run this when the harness is first set up, or after a TCK pin update:

```bash
python tests/tck/wip_generate.py
# Writes tests/tck/wip.txt with all current failures
# Review, commit alongside the code
```

To promote a `@wip` scenario to passing (remove from wip.txt after fixing):
1. Fix the translator bug
2. Remove the scenario line from `tests/tck/wip.txt`
3. Run `pytest tests/tck/ -m tck -k "<scenario name>"` — it must pass
4. Commit the wip.txt change alongside the fix

## CI

The TCK job runs automatically on any PR touching:
- `iris_vector_graph/cypher/translator.py`
- `iris_vector_graph/cypher/parser.py`
- `iris_vector_graph/_engine/`

The job reports:
- ✅ Pass count
- ⏭ Wip (skipped) count — must not increase
- ❌ Failure count — must be 0

## Updating the TCK pin

```bash
cd vendor/opencypher
git fetch origin
git checkout <new-commit-sha>
cd ../..
git add vendor/opencypher
# Run baseline to see what changed
python tests/tck/wip_generate.py
# Review new failures, add to wip.txt if needed, then commit
```
