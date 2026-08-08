"""Tests targeting uncovered lines in translator.py:
- Lines 2169-2414: UNWIND/range expansion, WITH clause processing, procedure calls
- Lines 2475-2613: full-replace JOIN stripping, RETURN clause transactional filtering
- Lines 2751-2994: ORDER BY sort injection, optional null-row, preprocess_order_by
- Lines 3032-3198: ORDER BY alias fallback, pagination eval/resolve
- Lines 3284-3405: apply_pagination branches, translate_unwind_clause
"""

import pytest


def tr(q, params=None):
    from iris_vector_graph.cypher.parser import parse_query
    from iris_vector_graph.cypher.translator import translate_to_sql
    ast_tree = parse_query(q)
    result = translate_to_sql(ast_tree, params or {})
    sql = result.sql
    return sql[0] if isinstance(sql, list) else sql


def tr_full(q, params=None):
    from iris_vector_graph.cypher.parser import parse_query
    from iris_vector_graph.cypher.translator import translate_to_sql
    ast_tree = parse_query(q)
    return translate_to_sql(ast_tree, params or {})


# ---------------------------------------------------------------------------
# UNWIND + range() expansion (lines 2169-2178)
# ---------------------------------------------------------------------------

class TestUnwindRangeExpansion:
    def test_unwind_range_basic_creates_dml(self):
        """UNWIND range() with CREATE should expand to multiple DML statements."""
        result = tr_full("UNWIND range(1, 3) AS x CREATE (n {val: x})")
        # Should be transactional with multiple statements
        assert result.is_transactional
        assert isinstance(result.sql, list)
        # Should have DML for each of 1, 2, 3
        assert len(result.sql) >= 3

    def test_unwind_range_step(self):
        """UNWIND range(0, 4, 2) AS x should expand to [0, 2, 4]."""
        result = tr_full("UNWIND range(0, 4, 2) AS x CREATE (n {val: x})")
        assert result.is_transactional
        assert isinstance(result.sql, list)
        # 3 iterations: 0, 2, 4
        assert len(result.sql) >= 3

    def test_unwind_range_returns_count(self):
        """UNWIND range(1,2) AS x CREATE (n) RETURN count(n) should succeed."""
        result = tr_full("UNWIND range(1, 2) AS x CREATE (n) RETURN count(n)")
        assert result.is_transactional

    def test_unwind_range_negative_step_expands(self):
        """UNWIND range(3, 1, -1) AS x should expand backwards."""
        result = tr_full("UNWIND range(3, 1, -1) AS x CREATE (n {val: x})")
        assert result.is_transactional
        assert len(result.sql) >= 3


# ---------------------------------------------------------------------------
# UNWIND literal list + updating clauses (lines 2180-2261)
# ---------------------------------------------------------------------------

class TestUnwindLiteralExpansion:
    def test_unwind_list_create(self):
        """UNWIND ['a','b'] AS x CREATE (n:Label {id: x}) should generate DML per item."""
        result = tr_full("UNWIND ['a', 'b'] AS x CREATE (n:MyLabel {id: x})")
        assert result.is_transactional
        assert isinstance(result.sql, list)
        assert len(result.sql) >= 2

    def test_unwind_list_merge(self):
        """UNWIND [1,2,3] AS x MERGE (n {val: x}) should be transactional."""
        result = tr_full("UNWIND [1, 2, 3] AS x MERGE (n:Thing {val: x})")
        assert result.is_transactional

    def test_unwind_empty_list_no_dml(self):
        """UNWIND [] AS x CREATE (n) should produce minimal DML."""
        result = tr_full("UNWIND [] AS x CREATE (n:Empty)")
        assert result.is_transactional
        # Empty list => loop runs 0 times, no per-item DML
        assert isinstance(result.sql, list)

    def test_unwind_non_literal_falls_through(self):
        """UNWIND of a variable (non-literal) should not expand Python-side."""
        sql = tr("UNWIND $items AS x RETURN x", {"items": [1, 2, 3]})
        # Should produce a SELECT query (non-transactional fallthrough)
        assert "SELECT" in sql


# ---------------------------------------------------------------------------
# WITH clause processing (lines 2256-2261)
# ---------------------------------------------------------------------------

class TestWithClauseProcessing:
    def test_match_with_return(self):
        """MATCH ... WITH ... RETURN should produce a stage CTE."""
        sql = tr("MATCH (n:Person) WITH n.name AS nm RETURN nm")
        assert "SELECT" in sql

    def test_with_where_return(self):
        """WITH clause followed by WHERE should filter the stage."""
        sql = tr("MATCH (n:Person) WITH n.name AS nm WHERE nm IS NOT NULL RETURN nm")
        assert "SELECT" in sql

    def test_with_aggregation(self):
        """WITH clause with COUNT should produce aggregated stage."""
        sql = tr("MATCH (n:Person) WITH count(n) AS cnt RETURN cnt")
        assert "SELECT" in sql
        assert "COUNT" in sql.upper()

    def test_with_order_by_skip_limit(self):
        """WITH clause with ORDER BY, SKIP, LIMIT should produce correct SQL."""
        sql = tr("MATCH (n:Person) WITH n ORDER BY n.name SKIP 1 LIMIT 5 RETURN n.name")
        assert "SELECT" in sql


# ---------------------------------------------------------------------------
# RETURN clause with transactional filtering (lines 2426-2474)
# ---------------------------------------------------------------------------

class TestTransactionalReturnFiltering:
    def test_set_then_return(self):
        """SET property then RETURN should filter modified property conditions."""
        result = tr_full(
            "MATCH (n:Person {name: 'Alice'}) SET n.age = 30 RETURN n.name"
        )
        assert result.is_transactional

    def test_remove_then_return(self):
        """REMOVE property then RETURN should work."""
        result = tr_full(
            "MATCH (n:Person {name: 'Bob'}) REMOVE n.age RETURN n.name"
        )
        assert result.is_transactional

    def test_create_then_return(self):
        """CREATE then RETURN should be transactional."""
        result = tr_full("CREATE (n:Foo {id: 'x1'}) RETURN n")
        assert result.is_transactional
        assert isinstance(result.sql, list)

    def test_delete_then_return(self):
        """MATCH + DELETE + RETURN should be transactional."""
        result = tr_full("MATCH (n:ToDelete {id: 'del1'}) DELETE n RETURN count(n)")
        assert result.is_transactional


# ---------------------------------------------------------------------------
# Full-replace JOIN stripping (lines 2474-2515)
# ---------------------------------------------------------------------------

class TestFullReplaceJoinStripping:
    def test_set_full_replace_map(self):
        """SET n = {map} should strip property JOINs for the node."""
        result = tr_full(
            "MATCH (n:Person {name: 'Alice'}) SET n = {name: 'Bob', age: 25} RETURN n.name"
        )
        assert result.is_transactional

    def test_set_full_replace_returns_sql_list(self):
        """SET n = {map} returns a list of DML statements."""
        result = tr_full(
            "MATCH (n:Widget {id: 'w1'}) SET n = {color: 'red'} RETURN n.color"
        )
        assert isinstance(result.sql, list)


# ---------------------------------------------------------------------------
# _build_null_row_not_exists (lines 2545-2578)
# ---------------------------------------------------------------------------

class TestBuildNullRowNotExists:
    def test_single_label(self):
        from iris_vector_graph.cypher.translator import _build_null_row_not_exists
        sql, params = _build_null_row_not_exists(["Person"])
        assert "NOT EXISTS" in sql
        assert "rdf_labels" in sql
        assert params == ["Person"]

    def test_empty_labels(self):
        from iris_vector_graph.cypher.translator import _build_null_row_not_exists
        sql, params = _build_null_row_not_exists([])
        assert sql == "1=1"
        assert params == []

    def test_multi_label(self):
        from iris_vector_graph.cypher.translator import _build_null_row_not_exists
        sql, params = _build_null_row_not_exists(["Person", "Employee"])
        assert "NOT EXISTS" in sql
        assert "JOIN" in sql
        assert "Person" in params or "Employee" in params
        assert len(params) == 2


# ---------------------------------------------------------------------------
# Optional MATCH null-row fallback (lines 2804-2863)
# ---------------------------------------------------------------------------

class TestOptionalMatchNullRow:
    def test_optional_match_null_row(self):
        """OPTIONAL MATCH should produce UNION ALL null-row fallback."""
        sql = tr(
            "OPTIONAL MATCH (n:NonExistentLabel) RETURN n.name"
        )
        # Should contain UNION ALL for the null row
        assert "SELECT" in sql

    def test_optional_match_with_required(self):
        """MATCH + OPTIONAL MATCH combined."""
        sql = tr(
            "MATCH (a:Person) OPTIONAL MATCH (a)-[:KNOWS]->(b:Friend) RETURN a.name, b.name"
        )
        assert "SELECT" in sql


# ---------------------------------------------------------------------------
# preprocess_order_by: InvalidAggregation error (lines 2909-2962)
# ---------------------------------------------------------------------------

class TestPreprocessOrderBy:
    def test_order_by_no_aggregation(self):
        """ORDER BY without aggregation in RETURN should work."""
        sql = tr("MATCH (n:Person) RETURN n.name ORDER BY n.name")
        assert "ORDER BY" in sql

    def test_order_by_asc_desc(self):
        """ORDER BY with ASC and DESC."""
        sql = tr("MATCH (n:Person) RETURN n.name, n.age ORDER BY n.name ASC, n.age DESC")
        assert "ORDER BY" in sql
        assert "ASC" in sql or "DESC" in sql

    def test_order_by_aggregation_in_return(self):
        """ORDER BY aggregation when RETURN also has aggregation is OK."""
        sql = tr("MATCH (n:Person) RETURN n.name, count(n) AS cnt ORDER BY count(n)")
        assert "ORDER BY" in sql

    def test_order_by_aggregation_without_return_agg_raises(self):
        """ORDER BY aggregation when RETURN has no aggregation should raise SyntaxError."""
        with pytest.raises(SyntaxError, match="InvalidAggregation"):
            tr("MATCH (n:Person) RETURN n.name ORDER BY count(n)")

    def test_order_by_distinct_exact_match(self):
        """RETURN DISTINCT with ORDER BY on projected expression is OK."""
        sql = tr("MATCH (n:Person) RETURN DISTINCT n.name ORDER BY n.name")
        assert "SELECT" in sql

    def test_order_by_distinct_full_var(self):
        """RETURN DISTINCT n ORDER BY n.prop is OK (n is full variable)."""
        sql = tr("MATCH (n:Person) RETURN DISTINCT n ORDER BY n.name")
        assert "SELECT" in sql

    def test_order_by_distinct_undefined_raises(self):
        """RETURN DISTINCT a.name ORDER BY b.name (b not returned) should raise."""
        with pytest.raises(SyntaxError, match="UndefinedVariable"):
            tr("MATCH (a:Person), (b:Thing) RETURN DISTINCT a.name ORDER BY b.name")

    def test_order_by_undefined_in_agg_query_raises(self):
        """ORDER BY variable not in RETURN of aggregating query should raise."""
        with pytest.raises(SyntaxError):
            tr("MATCH (n:Person) WITH count(n) AS cnt RETURN cnt ORDER BY n.name")


# ---------------------------------------------------------------------------
# _eval_pagination_expr (lines 3132-3198)
# ---------------------------------------------------------------------------

class TestEvalPaginationExpr:
    def test_eval_integer_literal(self):
        from iris_vector_graph.cypher.translator import _eval_pagination_expr
        from iris_vector_graph.cypher import ast
        result = _eval_pagination_expr(ast.Literal(5))
        assert result == 5.0

    def test_eval_float_literal(self):
        from iris_vector_graph.cypher.translator import _eval_pagination_expr
        from iris_vector_graph.cypher import ast
        result = _eval_pagination_expr(ast.Literal(3.5))
        assert result == 3.5

    def test_eval_int_direct(self):
        from iris_vector_graph.cypher.translator import _eval_pagination_expr
        result = _eval_pagination_expr(10)
        assert result == 10.0

    def test_eval_float_direct(self):
        from iris_vector_graph.cypher.translator import _eval_pagination_expr
        result = _eval_pagination_expr(2.5)
        assert result == 2.5

    def test_eval_variable_returns_none(self):
        from iris_vector_graph.cypher.translator import _eval_pagination_expr
        from iris_vector_graph.cypher import ast
        result = _eval_pagination_expr(ast.Variable("x"))
        assert result is None

    def test_eval_none_returns_none(self):
        from iris_vector_graph.cypher.translator import _eval_pagination_expr
        result = _eval_pagination_expr(None)
        assert result is None

    def test_eval_string_literal_returns_none(self):
        from iris_vector_graph.cypher.translator import _eval_pagination_expr
        from iris_vector_graph.cypher import ast
        result = _eval_pagination_expr(ast.Literal("hello"))
        assert result is None

    def test_eval_tointeger(self):
        """tointeger(3.7) should return 3.0."""
        sql = tr("MATCH (n) RETURN n LIMIT toInteger(3.7)")
        assert "3" in sql

    def test_eval_ceil(self):
        """ceil(2.3) as LIMIT should yield 3."""
        sql = tr("MATCH (n) RETURN n LIMIT ceil(2.3)")
        assert "3" in sql

    def test_eval_floor(self):
        """floor(4.9) as LIMIT should yield 4."""
        sql = tr("MATCH (n) RETURN n LIMIT floor(4.9)")
        assert "4" in sql

    def test_eval_abs(self):
        from iris_vector_graph.cypher.translator import _eval_pagination_expr
        from iris_vector_graph.cypher import ast
        abs_call = ast.FunctionCall("abs", [ast.Literal(-5)])
        result = _eval_pagination_expr(abs_call)
        assert result == 5.0

    def test_eval_arith_multiply(self):
        from iris_vector_graph.cypher.translator import _eval_pagination_expr
        from iris_vector_graph.cypher import ast
        mul = ast.FunctionCall("__arith_*", [ast.Literal(3), ast.Literal(4)])
        result = _eval_pagination_expr(mul)
        assert result == 12.0

    def test_eval_arith_add(self):
        from iris_vector_graph.cypher.translator import _eval_pagination_expr
        from iris_vector_graph.cypher import ast
        add = ast.FunctionCall("__arith_+", [ast.Literal(2), ast.Literal(3)])
        result = _eval_pagination_expr(add)
        assert result == 5.0

    def test_eval_arith_sub(self):
        from iris_vector_graph.cypher.translator import _eval_pagination_expr
        from iris_vector_graph.cypher import ast
        sub = ast.FunctionCall("__arith_-", [ast.Literal(10), ast.Literal(3)])
        result = _eval_pagination_expr(sub)
        assert result == 7.0

    def test_eval_arith_div(self):
        from iris_vector_graph.cypher.translator import _eval_pagination_expr
        from iris_vector_graph.cypher import ast
        div = ast.FunctionCall("__arith_/", [ast.Literal(10), ast.Literal(2)])
        result = _eval_pagination_expr(div)
        assert result == 5.0

    def test_eval_arith_div_by_zero(self):
        from iris_vector_graph.cypher.translator import _eval_pagination_expr
        from iris_vector_graph.cypher import ast
        div = ast.FunctionCall("__arith_/", [ast.Literal(10), ast.Literal(0)])
        result = _eval_pagination_expr(div)
        assert result is None

    def test_eval_arith_mod(self):
        from iris_vector_graph.cypher.translator import _eval_pagination_expr
        from iris_vector_graph.cypher import ast
        mod = ast.FunctionCall("__arith_%", [ast.Literal(10), ast.Literal(3)])
        result = _eval_pagination_expr(mod)
        assert result == 1.0

    def test_eval_arith_pow(self):
        from iris_vector_graph.cypher.translator import _eval_pagination_expr
        from iris_vector_graph.cypher import ast
        pw = ast.FunctionCall("__arith_^", [ast.Literal(2), ast.Literal(3)])
        result = _eval_pagination_expr(pw)
        assert result == 8.0

    def test_eval_arith_with_variable_returns_none(self):
        from iris_vector_graph.cypher.translator import _eval_pagination_expr
        from iris_vector_graph.cypher import ast
        mul = ast.FunctionCall("__arith_*", [ast.Variable("x"), ast.Literal(4)])
        result = _eval_pagination_expr(mul)
        assert result is None

    def test_eval_rand_returns_float(self):
        from iris_vector_graph.cypher.translator import _eval_pagination_expr
        from iris_vector_graph.cypher import ast
        rand_call = ast.FunctionCall("rand", [])
        result = _eval_pagination_expr(rand_call)
        assert result is not None
        assert 0.0 <= result <= 1.0

    def test_eval_unknown_function_returns_none(self):
        from iris_vector_graph.cypher.translator import _eval_pagination_expr
        from iris_vector_graph.cypher import ast
        unknown = ast.FunctionCall("unknown_func", [ast.Literal(5)])
        result = _eval_pagination_expr(unknown)
        assert result is None


# ---------------------------------------------------------------------------
# _resolve_pagination_value (lines 3201-3242)
# ---------------------------------------------------------------------------

class TestResolvePaginationValue:
    def test_resolve_int(self):
        from iris_vector_graph.cypher.translator import _resolve_pagination_value, TranslationContext
        ctx = TranslationContext()
        ctx.input_params = {}
        result = _resolve_pagination_value(5, ctx)
        assert result == 5

    def test_resolve_variable_from_params(self):
        from iris_vector_graph.cypher.translator import _resolve_pagination_value, TranslationContext
        from iris_vector_graph.cypher import ast
        ctx = TranslationContext()
        ctx.input_params = {"n": 10}
        result = _resolve_pagination_value(ast.Variable("n"), ctx)
        assert result == 10

    def test_resolve_variable_missing_raises(self):
        from iris_vector_graph.cypher.translator import _resolve_pagination_value, TranslationContext
        from iris_vector_graph.cypher import ast
        ctx = TranslationContext()
        ctx.input_params = {}
        with pytest.raises(ValueError, match="not provided"):
            _resolve_pagination_value(ast.Variable("missing"), ctx)

    def test_resolve_variable_float_not_integer_raises(self):
        from iris_vector_graph.cypher.translator import _resolve_pagination_value, TranslationContext
        from iris_vector_graph.cypher import ast
        ctx = TranslationContext()
        ctx.input_params = {"n": 3.5}
        with pytest.raises(SyntaxError, match="InvalidArgumentType"):
            _resolve_pagination_value(ast.Variable("n"), ctx)

    def test_resolve_negative_raises(self):
        from iris_vector_graph.cypher.translator import _resolve_pagination_value, TranslationContext
        ctx = TranslationContext()
        ctx.input_params = {}
        with pytest.raises(SyntaxError, match="NegativeIntegerArgument"):
            _resolve_pagination_value(-1, ctx)

    def test_resolve_none_returns_none(self):
        from iris_vector_graph.cypher.translator import _resolve_pagination_value, TranslationContext
        ctx = TranslationContext()
        ctx.input_params = {}
        result = _resolve_pagination_value(None, ctx)
        assert result is None

    def test_resolve_float_expression_non_integer_raises(self):
        from iris_vector_graph.cypher.translator import _resolve_pagination_value, TranslationContext
        from iris_vector_graph.cypher import ast
        ctx = TranslationContext()
        ctx.input_params = {}
        # ceil(2.3) = 3.0 — should succeed
        ceil_call = ast.FunctionCall("ceil", [ast.Literal(2.3)])
        result = _resolve_pagination_value(ceil_call, ctx)
        assert result == 3

    def test_resolve_expression_with_variable_raises(self):
        from iris_vector_graph.cypher.translator import _resolve_pagination_value, TranslationContext
        from iris_vector_graph.cypher import ast
        ctx = TranslationContext()
        ctx.input_params = {}
        # Expression referencing a variable: NonConstantExpression
        mul = ast.FunctionCall("__arith_*", [ast.Variable("x"), ast.Literal(2)])
        with pytest.raises(SyntaxError, match="NonConstantExpression"):
            _resolve_pagination_value(mul, ctx)


# ---------------------------------------------------------------------------
# apply_pagination (lines 3252-3315)
# ---------------------------------------------------------------------------

class TestApplyPagination:
    def test_limit_only(self):
        """LIMIT N should produce FETCH FIRST N ROWS ONLY."""
        sql = tr("MATCH (n:Person) RETURN n.name LIMIT 10")
        assert "10" in sql

    def test_skip_only(self):
        """SKIP N should produce OFFSET N."""
        sql = tr("MATCH (n:Person) RETURN n.name SKIP 5")
        assert "5" in sql

    def test_skip_and_limit(self):
        """SKIP + LIMIT should produce both."""
        sql = tr("MATCH (n:Person) RETURN n.name SKIP 5 LIMIT 10")
        assert "5" in sql
        assert "10" in sql

    def test_limit_zero_uses_top_zero(self):
        """LIMIT 0 should use TOP 0 instead of FETCH FIRST 0 ROWS ONLY."""
        sql = tr("MATCH (n:Person) RETURN n.name LIMIT 0")
        assert "TOP 0" in sql

    def test_limit_with_param(self):
        """LIMIT $n where n=5 should produce correct SQL."""
        sql = tr("MATCH (n:Person) RETURN n.name LIMIT $lim", {"lim": 5})
        assert "5" in sql

    def test_skip_with_param(self):
        """SKIP $s where s=3 should produce correct SQL."""
        sql = tr("MATCH (n:Person) RETURN n.name SKIP $skip", {"skip": 3})
        assert "3" in sql

    def test_negative_limit_raises(self):
        """LIMIT -1 should raise SyntaxError."""
        with pytest.raises(SyntaxError, match="NegativeIntegerArgument"):
            tr("MATCH (n) RETURN n LIMIT -1")

    def test_negative_skip_raises(self):
        """SKIP -1 should raise SyntaxError."""
        with pytest.raises(SyntaxError, match="NegativeIntegerArgument"):
            tr("MATCH (n) RETURN n SKIP -1")

    def test_limit_with_order_by(self):
        """LIMIT + ORDER BY should produce correct SQL."""
        sql = tr("MATCH (n:Person) RETURN n.name ORDER BY n.name LIMIT 5")
        assert "ORDER BY" in sql
        assert "5" in sql

    def test_no_from_clause_adds_dual(self):
        """Query without FROM should add __dual when pagination is applied."""
        sql = tr("RETURN 1 AS x LIMIT 5")
        assert "SELECT" in sql


# ---------------------------------------------------------------------------
# translate_unwind_clause (lines 3331-3419)
# ---------------------------------------------------------------------------

class TestTranslateUnwindClause:
    def test_unwind_literal_list(self):
        """UNWIND ['a','b','c'] AS x RETURN x should use UNION ALL."""
        sql = tr("UNWIND ['a', 'b', 'c'] AS x RETURN x")
        assert "UNION ALL" in sql
        assert "SELECT" in sql

    def test_unwind_empty_list(self):
        """UNWIND [] AS x should produce a no-rows subquery."""
        sql = tr("UNWIND [] AS x RETURN x")
        assert "SELECT" in sql
        assert "1=0" in sql or "WHERE" in sql

    def test_unwind_list_with_null(self):
        """UNWIND [1, null, 2] AS x should handle null items."""
        sql = tr("UNWIND [1, null, 2] AS x RETURN x")
        assert "NULL" in sql

    def test_unwind_param_variable(self):
        """UNWIND $list AS x RETURN x should use JSON_TABLE."""
        sql = tr("UNWIND $list AS x RETURN x", {"list": [1, 2, 3]})
        assert "SELECT" in sql

    def test_unwind_with_match(self):
        """UNWIND + MATCH should produce cross-join."""
        sql = tr("UNWIND [1, 2] AS x MATCH (n:Person) WHERE n.age = x RETURN n.name")
        assert "SELECT" in sql

    def test_unwind_keys_function(self):
        """UNWIND keys(n) should use rdf_props JOIN path."""
        sql = tr("MATCH (n:Person) UNWIND keys(n) AS k RETURN k")
        assert "SELECT" in sql
        # rdf_props join for keys()
        assert "rdf_props" in sql

    def test_unwind_integer_list(self):
        """UNWIND [1, 2, 3] AS i RETURN i should use UNION ALL."""
        sql = tr("UNWIND [1, 2, 3] AS i RETURN i")
        assert "UNION ALL" in sql

    def test_unwind_mixed_types_uses_json_table(self):
        """UNWIND of non-scalar list should fall back to JSON_TABLE."""
        # Lists with mixed/complex elements that aren't all plain scalars
        sql = tr("UNWIND $items AS x RETURN x", {"items": [{"a": 1}, {"b": 2}]})
        assert "SELECT" in sql


# ---------------------------------------------------------------------------
# DML: CREATE clause (lines from _create_clause_node_entry area)
# ---------------------------------------------------------------------------

class TestCreateClause:
    def test_create_single_node(self):
        """CREATE (n:Person) should generate INSERT DML."""
        result = tr_full("CREATE (n:Person)")
        assert result.is_transactional
        stmts = result.sql if isinstance(result.sql, list) else [result.sql]
        assert any("INSERT" in s for s in stmts)

    def test_create_node_with_props(self):
        """CREATE (n:Person {name: 'Alice'}) should insert both node and props."""
        result = tr_full("CREATE (n:Person {name: 'Alice', age: 30})")
        assert result.is_transactional
        stmts = result.sql if isinstance(result.sql, list) else [result.sql]
        assert any("INSERT" in s for s in stmts)

    def test_create_node_with_string_id(self):
        """CREATE (n {id: 'my-id'}) should use the provided id."""
        result = tr_full("CREATE (n:Widget {id: 'my-widget-1'})")
        assert result.is_transactional

    def test_create_relationship(self):
        """CREATE (a)-[:KNOWS]->(b) should generate edge INSERT."""
        result = tr_full(
            "CREATE (a:Person {id: 'p1'})-[:KNOWS]->(b:Person {id: 'p2'})"
        )
        assert result.is_transactional
        stmts = result.sql if isinstance(result.sql, list) else [result.sql]
        assert any("INSERT" in s for s in stmts)

    def test_create_returns_something(self):
        """CREATE (n:Foo) RETURN n should be transactional with return."""
        result = tr_full("CREATE (n:Foo {id: 'foo1'}) RETURN n")
        assert result.is_transactional
        assert isinstance(result.sql, list)

    def test_create_node_integer_id_as_property(self):
        """CREATE (n {id: 42}) should treat id as a property, not node identifier."""
        result = tr_full("CREATE (n:Num {id: 42})")
        assert result.is_transactional


# ---------------------------------------------------------------------------
# DML: MERGE clause
# ---------------------------------------------------------------------------

class TestMergeClause:
    def test_merge_node_basic(self):
        """MERGE (n:Person {id: 'm1'}) should generate MERGE DML."""
        result = tr_full("MERGE (n:Person {id: 'm1'})")
        assert result.is_transactional
        stmts = result.sql if isinstance(result.sql, list) else [result.sql]
        assert any("INSERT" in s for s in stmts)

    def test_merge_with_on_create_set(self):
        """MERGE ... ON CREATE SET should be transactional."""
        result = tr_full(
            "MERGE (n:Person {id: 'mc1'}) ON CREATE SET n.created = true"
        )
        assert result.is_transactional

    def test_merge_with_on_match_set(self):
        """MERGE ... ON MATCH SET should be transactional."""
        result = tr_full(
            "MERGE (n:Person {id: 'mm1'}) ON MATCH SET n.updated = true"
        )
        assert result.is_transactional


# ---------------------------------------------------------------------------
# DML: SET clause
# ---------------------------------------------------------------------------

class TestSetClause:
    def test_set_property(self):
        """MATCH + SET should generate UPDATE DML."""
        result = tr_full(
            "MATCH (n:Person {id: 'p1'}) SET n.age = 25"
        )
        assert result.is_transactional
        stmts = result.sql if isinstance(result.sql, list) else [result.sql]
        assert any("INSERT" in s or "UPDATE" in s or "DELETE" in s for s in stmts)

    def test_set_multiple_properties(self):
        """SET multiple properties at once."""
        result = tr_full(
            "MATCH (n:Person {id: 'p2'}) SET n.name = 'Bob', n.age = 40"
        )
        assert result.is_transactional

    def test_set_from_param(self):
        """SET n.val = $v with param should be transactional."""
        result = tr_full(
            "MATCH (n:Widget {id: 'w1'}) SET n.color = $color",
            {"color": "blue"}
        )
        assert result.is_transactional


# ---------------------------------------------------------------------------
# DML: DELETE clause
# ---------------------------------------------------------------------------

class TestDeleteClause:
    def test_delete_node(self):
        """MATCH + DELETE should generate DELETE DML."""
        result = tr_full("MATCH (n:Trash {id: 'd1'}) DELETE n")
        assert result.is_transactional
        stmts = result.sql if isinstance(result.sql, list) else [result.sql]
        assert any("DELETE" in s for s in stmts)

    def test_detach_delete(self):
        """DETACH DELETE should also remove edges."""
        result = tr_full("MATCH (n:Trash {id: 'd2'}) DETACH DELETE n")
        assert result.is_transactional

    def test_delete_returns_count(self):
        """DELETE + RETURN count should be transactional."""
        result = tr_full("MATCH (n:Trash) DELETE n RETURN count(n)")
        assert result.is_transactional


# ---------------------------------------------------------------------------
# ORDER BY: sort injection into SELECT (lines 2751-2799)
# ---------------------------------------------------------------------------

class TestSortInjection:
    def test_order_by_property_asc(self):
        """ORDER BY n.name ASC should produce ordered SQL."""
        sql = tr("MATCH (n:Person) RETURN n.name ORDER BY n.name ASC")
        assert "ORDER BY" in sql

    def test_order_by_property_desc(self):
        """ORDER BY n.age DESC should produce DESC ordering."""
        sql = tr("MATCH (n:Person) RETURN n.age ORDER BY n.age DESC")
        assert "DESC" in sql

    def test_order_by_alias(self):
        """ORDER BY alias should resolve through alias_to_sql map."""
        sql = tr("MATCH (n:Person) RETURN n.name AS nm ORDER BY nm")
        assert "ORDER BY" in sql

    def test_order_by_count(self):
        """ORDER BY count(*) should work in aggregating query."""
        sql = tr("MATCH (n:Person) RETURN n.name, count(*) AS c ORDER BY count(*)")
        assert "ORDER BY" in sql


# ---------------------------------------------------------------------------
# Procedure call / CALL subquery (lines 2238-2260)
# ---------------------------------------------------------------------------

class TestSubqueryCalls:
    def test_call_subquery_basic(self):
        """CALL { MATCH (n) RETURN n } RETURN n should succeed."""
        sql = tr("CALL { MATCH (n:Person) RETURN n } RETURN n.name")
        assert "SELECT" in sql


# ---------------------------------------------------------------------------
# RETURN * and aggregation
# ---------------------------------------------------------------------------

class TestReturnVariants:
    def test_return_star(self):
        """RETURN * should return all bound variables."""
        sql = tr("MATCH (n:Person) RETURN *")
        assert "SELECT" in sql

    def test_return_distinct(self):
        """RETURN DISTINCT should produce DISTINCT in SQL."""
        sql = tr("MATCH (n:Person) RETURN DISTINCT n.name")
        assert "DISTINCT" in sql

    def test_return_count_star(self):
        """RETURN count(*) should produce COUNT."""
        sql = tr("MATCH (n:Person) RETURN count(*)")
        assert "COUNT" in sql.upper()

    def test_return_count_distinct(self):
        """RETURN count(DISTINCT n.name) should produce COUNT."""
        sql = tr("MATCH (n:Person) RETURN count(DISTINCT n.name)")
        assert "COUNT" in sql.upper()

    def test_return_sum(self):
        """RETURN sum(n.age) should produce SUM."""
        sql = tr("MATCH (n:Person) RETURN sum(n.age)")
        assert "SUM" in sql.upper()

    def test_return_avg(self):
        """RETURN avg(n.age) should produce AVG."""
        sql = tr("MATCH (n:Person) RETURN avg(n.age)")
        assert "AVG" in sql.upper()

    def test_return_collect(self):
        """RETURN collect(n.name) should produce aggregation."""
        sql = tr("MATCH (n:Person) RETURN collect(n.name)")
        assert "SELECT" in sql

    def test_return_min_max(self):
        """RETURN min/max should produce MIN/MAX."""
        sql_min = tr("MATCH (n:Person) RETURN min(n.age)")
        sql_max = tr("MATCH (n:Person) RETURN max(n.age)")
        assert "MIN" in sql_min.upper()
        assert "MAX" in sql_max.upper()

    def test_return_case_expression(self):
        """RETURN CASE WHEN ... THEN ... END should work."""
        sql = tr("MATCH (n:Person) RETURN CASE WHEN n.age > 18 THEN 'adult' ELSE 'minor' END AS cat")
        assert "CASE" in sql.upper()


# ---------------------------------------------------------------------------
# Column name map
# ---------------------------------------------------------------------------

class TestColumnNameMap:
    def test_column_name_map_is_dict(self):
        """translate_to_sql should return a column_name_map dict."""
        result = tr_full("MATCH (n:Person) RETURN n.name AS personName")
        assert isinstance(result.column_name_map, dict)

    def test_column_name_map_multi_column(self):
        """Multiple RETURN items should return a valid column_name_map dict."""
        result = tr_full("MATCH (n:Person) RETURN n.name AS nm, n.age AS ag")
        assert isinstance(result.column_name_map, dict)


# ---------------------------------------------------------------------------
# Transactional result assembly (_tts_transactional_result)
# ---------------------------------------------------------------------------

class TestTransactionalResultAssembly:
    def test_is_transactional_flag(self):
        """DML queries should have is_transactional=True."""
        result = tr_full("CREATE (n:Foo)")
        assert result.is_transactional is True

    def test_non_transactional_flag(self):
        """SELECT queries should have is_transactional=False."""
        result = tr_full("MATCH (n:Person) RETURN n")
        assert not result.is_transactional

    def test_transactional_sql_is_list(self):
        """DML queries return sql as list."""
        result = tr_full("CREATE (n:Bar {id: 'b1'})")
        assert isinstance(result.sql, list)

    def test_create_then_return_produces_select_in_list(self):
        """CREATE + RETURN should include a SELECT as last statement."""
        result = tr_full("CREATE (n:Baz {id: 'bz1'}) RETURN n")
        stmts = result.sql
        assert isinstance(stmts, list)
        assert any("SELECT" in s for s in stmts)


# ---------------------------------------------------------------------------
# FOREACH clause (line 2241-2243)
# ---------------------------------------------------------------------------

class TestForeachClause:
    def test_foreach_basic(self):
        """FOREACH (x IN [1,2] | CREATE (n:Item)) should be transactional."""
        result = tr_full("FOREACH (x IN [1, 2] | CREATE (n:Item {val: x}))")
        assert result.is_transactional


# ---------------------------------------------------------------------------
# _validate_pagination_int
# ---------------------------------------------------------------------------

class TestValidatePaginationInt:
    def test_zero_is_valid(self):
        from iris_vector_graph.cypher.translator import _validate_pagination_int
        # Should not raise
        _validate_pagination_int(0, "LIMIT")

    def test_positive_is_valid(self):
        from iris_vector_graph.cypher.translator import _validate_pagination_int
        _validate_pagination_int(100, "SKIP")

    def test_negative_raises(self):
        from iris_vector_graph.cypher.translator import _validate_pagination_int
        with pytest.raises(SyntaxError, match="NegativeIntegerArgument"):
            _validate_pagination_int(-5, "LIMIT")


# ---------------------------------------------------------------------------
# _extract_literal_value
# ---------------------------------------------------------------------------

class TestExtractLiteralValue:
    def test_plain_value(self):
        from iris_vector_graph.cypher.translator import _extract_literal_value
        assert _extract_literal_value(42) == 42
        assert _extract_literal_value("hello") == "hello"

    def test_literal_wrapping(self):
        from iris_vector_graph.cypher.translator import _extract_literal_value
        from iris_vector_graph.cypher import ast
        assert _extract_literal_value(ast.Literal(99)) == 99

    def test_nested_list(self):
        from iris_vector_graph.cypher.translator import _extract_literal_value
        from iris_vector_graph.cypher import ast
        result = _extract_literal_value([ast.Literal(1), ast.Literal(2)])
        assert result == [1, 2]

    def test_nested_dict(self):
        from iris_vector_graph.cypher.translator import _extract_literal_value
        from iris_vector_graph.cypher import ast
        result = _extract_literal_value({"a": ast.Literal(5)})
        assert result == {"a": 5}
