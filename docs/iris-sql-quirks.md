# IRIS SQL Quirks and Undocumented Behaviors

Discovered during openCypher TCK conformance work (branch 201-opencypher-tck-harness).
Each entry includes the discovered behavior, expected behavior, and our workaround.

---

## 1. `JSON_TABLE` with `$[i]` on a nested array expands the sub-array

**Context:** Extracting a sub-array element from a nested JSON array.

**IRIS behavior:**

```sql
SELECT __sa FROM JSON_TABLE('[[1,2,3]]', '$[0]' COLUMNS(__sa VARCHAR(4096) PATH '$')) jt
-- Returns: (1,), (2,), (3,)  ← three rows, not one
```

**Expected:** One row containing the string `'[1,2,3]'`.

**Workaround:** Use `JSON_VALUE` instead:

```sql
SELECT JSON_VALUE('[[1,2,3]]', '$[0]')
-- Returns: '[1,2,3]'  ← correct single-value result
```

**Impact:** `x IN list[0]` where `list` contains a nested array — the IN subquery
was getting multiple rows instead of the sub-array string, causing false negatives.

**Fixed in:** commit `b44b686` (fix(tck): use JSON_VALUE not JSON_TABLE for subscript-as-IN-source)

---

## 2. SQL type coercion: `'1' IN (1, 2)` returns true

**Context:** Cypher `IN` operator is type-strict: integer `1` ≠ string `'1'`.

**IRIS behavior:**

```sql
SELECT CASE WHEN ('1' IN (1, 2)) THEN 1 ELSE 0 END
-- Returns: 1 (true) — IRIS coerces string '1' to integer 1
```

**Expected:** 0 (false) — Cypher types don't coerce.

**Workaround:** Static analysis at translation time — filter out type-mismatched
literal items from the IN list before generating SQL. If all items are filtered,
emit `(1=0)` (false).

**Impact:** `1 IN [2, '1']` and `'1' IN [1, 2]` returned wrong results.

**Fixed in:** commit `96716a2` (fix(tck): type-strict IN for literals; keyword map keys)

---

## 3. FETCH FIRST + multi-table JOIN causes SIGSEGV in `%qaqpre`

**Context:** IRIS AI preview builds (2026.2/2026.3) — `%qaqpre` optimizer.

**IRIS behavior:**

```sql
SELECT TOP 10 ... FROM t1 JOIN t2 ON ... FETCH FIRST 10 ROWS ONLY
-- SIGSEGV in %qaqpre (native crash, no exception)
```

**Expected:** Normal result with limit applied.

**Workaround:** IVG probes the IRIS version at startup and emits `TOP n` instead of
`FETCH FIRST n ROWS ONLY` when AI builds are detected.

**Note:** Incoming diagnosis (INNER JOIN keyword as cause) was wrong. The real trigger
is `FETCH FIRST` combined with multi-table JOINs on VARCHAR keys.

**Fixed in:** v2.4.3 (memory: `qaqpre_fetch_first_join_crash.md`)

---

## 4. `JSON_VALUE` returns sub-array as string (correct behavior worth documenting)

**Context:** Positively useful — `JSON_VALUE` on a path that yields an array returns
the array as a serialized JSON string, not expanded rows.

```sql
SELECT JSON_VALUE('[[1,2],[3,4]]', '$[0]')
-- Returns: '[1,2]'  ← string representation of the sub-array
```

This is useful for feeding into downstream `JSON_TABLE('$[*]')` calls. Document
this as the correct approach for sub-array extraction.

---

## 5. `ORDER BY` not allowed inside CTE body definitions

**Context:** Some queries need ORDER BY in intermediate stages.

**IRIS behavior:**

```sql
WITH Stage1 AS (SELECT x FROM t ORDER BY x)  -- ERROR
```

**Expected (standard SQL):** ORDER BY inside CTE is technically non-standard but many
databases allow it.

**Workaround:** Push ORDER BY to the outer query: `SELECT * FROM (SELECT ... FROM Stage1) ORDER BY x`

---

## 6. `INTEGER` type coercion in parameter binding

**Context:** When binding parameters, IRIS implicitly converts numeric strings to
integers for numeric comparisons.

**IRIS behavior:**

```sql
SELECT CASE WHEN 1 IN (?, ?) THEN 1 ELSE 0 END -- params: [2, '1']
-- Returns: 1 (true) — '1' coerced to 1
```

This is the same as quirk #2 but manifests in parameter binding (not literal SQL).

---

## 7. `rdf_props.val` is VARCHAR — arithmetic requires explicit CAST

**Context:** All property values are stored as VARCHAR in IVG's EAV schema.

**IRIS behavior:**

```sql
SELECT val + val FROM rdf_props WHERE "key" = 'num'
-- Returns string concatenation: '11' not arithmetic 2
```

**Workaround:** `CAST(val AS DOUBLE)` before arithmetic. IVG wraps `PropertyReference`
in `CAST(... AS DOUBLE)` when used in arithmetic contexts, but NOT for string
comparisons (where the cast would be wrong).

---

## 8. `CAST('NaN' AS DOUBLE)` and `CAST('Infinity' AS DOUBLE)` work

**Context:** IRIS supports special float values via CAST from string literals.

```sql
SELECT CAST('NaN' AS DOUBLE)       -- Returns NaN
SELECT CAST('Infinity' AS DOUBLE)  -- Returns +Infinity
SELECT CAST('-Infinity' AS DOUBLE) -- Returns -Infinity
```

These can be used for Cypher NaN/infinity semantics (0.0/0.0 → NaN).

**Fixed in:** commit `76f96d8` (fix(tck): NaN for 0.0/0.0 division and modulo)

---

## 9. `SQLUser.JSON_VALUE` function required (not standard `JSON_VALUE`)

**Context:** IRIS requires the `SQLUser.` schema prefix for user-defined SQL functions
like `JSON_VALUE`, `JSON_ARRAYLENGTH`, `JSON_ARRAYAGG`.

**Note:** Standard SQL `JSON_VALUE` without prefix may not work or may behave
differently. Always use `SQLUser.JSON_VALUE(...)` in IVG-generated SQL.

---

## 10. Keyword tokens as property keys in map literals require special handling

**Context:** Cypher allows keywords (END, START, TYPE, etc.) as property key names.

**Problem:** When tokenizing `{end: 1}`, `end` becomes a `TokenType.END` keyword
token, not `TokenType.IDENTIFIER`. The parser's map literal handler only accepted
`IDENTIFIER` tokens as keys.

**Workaround:** Accept any token as a map key if the NEXT token is a COLON — i.e.,
use lookahead (`peek_ahead(1)`) to confirm `key:` pattern before consuming.

**Fixed in:** commit `96716a2` (fix(tck): type-strict IN for literals; keyword map keys)

---

## 11. `intersystems-irispython` driver: `None` version metadata

**Context:** `intersystems-irispython` package has flaky `None` version metadata
that breaks `iris-devtester` integration fixtures.

**Workaround:** Use `iris-embedded-python-wrapper` / `EmbeddedConnection`.

_(memory: `irispython_version_and_embedded_wrapper.md`)_

---

## 12. `intersystems-iris` 5.3.3 driver SIGSEGV on specific multi-JOIN+EXISTS+LIKE query shape

**Context:** The ivg Cypher translator generates a SELECT with multiple JOINs +
EXISTS subquery + LIKE concatenation. At scale (>100k rows), this SIGSEGV
(native crash, no Python exception) with driver 5.3.3.

**Workaround:** Subprocess guard test. Not an IVG bug — the driver itself crashes.

_(memory: `driver_segfault_query_shape.md`)_
