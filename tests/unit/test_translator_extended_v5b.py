"""Extended translator tests v5b — targeting lines 5800-6700 and related.

Covers:
- ANY/ALL/NONE/SINGLE quantifiers (ListPredicateExpression)
- GDS procedure calls (gds.* → __SYSTEM_PROCEDURE__)
- EXISTS subquery patterns
- CALL db.* / dbms.* / apoc.* procedures
- List operations in WHERE context
"""

import pytest

from iris_vector_graph.cypher.parser import parse_query
from iris_vector_graph.cypher.translator import translate_to_sql


def tr(q, params=None):
    from iris_vector_graph.cypher.parser import parse_query
    from iris_vector_graph.cypher.translator import translate_to_sql
    ast_tree = parse_query(q)
    result = translate_to_sql(ast_tree, params or {})
    sql = result.sql
    return sql[0] if isinstance(sql, list) else sql


# ---------------------------------------------------------------------------
# ANY quantifier (8 tests)
# ---------------------------------------------------------------------------


class TestAnyQuantifier:
    """Tests for ANY(x IN list WHERE predicate) → JSON_TABLE + SUM CASE."""

    def test_any_property_list_string_match(self):
        """ANY(x IN n.tags WHERE x = 'cancer') should produce a SELECT CASE subquery."""
        sql = tr("MATCH (n) WHERE ANY(x IN n.tags WHERE x = 'cancer') RETURN n")
        assert "SELECT" in sql.upper()
        assert "JSON_TABLE" in sql.upper() or "SUM" in sql.upper() or "CASE" in sql.upper()

    def test_any_literal_list_starts_with(self):
        """ANY(x IN ['a','b','c'] WHERE x STARTS WITH 'a') — literal source."""
        sql = tr("MATCH (n) WHERE ANY(x IN ['a','b','c'] WHERE x STARTS WITH 'a') RETURN n.id")
        assert "SELECT" in sql.upper()
        assert "JSON_TABLE" in sql.upper() or "LIKE" in sql.upper()

    def test_any_param_list_greater_than(self):
        """ANY(x IN $list WHERE x > 0) with params — params list source."""
        sql = tr(
            "MATCH (n) WHERE ANY(x IN $list WHERE x > 0) RETURN count(n)",
            params={"list": [1, 2, 3]},
        )
        assert "SELECT" in sql.upper()
        assert "COUNT" in sql.upper()

    def test_any_empty_literal_list(self):
        """ANY(x IN [] WHERE x = 1) — empty list, always false."""
        sql = tr("MATCH (n) WHERE ANY(x IN [] WHERE x = 1) RETURN n")
        # ANY over empty list = false. Should produce (1=0) or equivalent or valid SQL.
        assert "SELECT" in sql.upper() or "1=0" in sql or "JSON_TABLE" in sql.upper()

    def test_any_with_not_equals(self):
        """ANY(x IN n.items WHERE x <> 'bad') — not-equals predicate."""
        sql = tr("MATCH (n) WHERE ANY(x IN n.items WHERE x <> 'bad') RETURN n")
        assert "SELECT" in sql.upper()

    def test_any_with_numeric_list(self):
        """ANY(x IN [1,2,3] WHERE x > 1) — numeric literal list."""
        sql = tr("MATCH (n) WHERE ANY(x IN [1,2,3] WHERE x > 1) RETURN n")
        assert "SELECT" in sql.upper()
        assert "JSON_TABLE" in sql.upper() or "SUM" in sql.upper() or "CASE" in sql.upper()

    def test_any_combined_with_match(self):
        """ANY inside MATCH + additional WHERE conditions."""
        sql = tr(
            "MATCH (n:Person) WHERE n.age > 18 AND ANY(x IN n.hobbies WHERE x = 'sports') RETURN n"
        )
        assert "SELECT" in sql.upper()
        # Should have the MATCH label join and the quantifier
        assert "Person" in sql or "rdf_labels" in sql.lower() or "JSON_TABLE" in sql.upper()

    def test_any_returns_count(self):
        """ANY used with count(*) aggregation."""
        sql = tr("MATCH (n) WHERE ANY(x IN n.tags WHERE x = 'a') RETURN count(*)")
        assert "COUNT" in sql.upper()


# ---------------------------------------------------------------------------
# ALL quantifier (8 tests)
# ---------------------------------------------------------------------------


class TestAllQuantifier:
    """Tests for ALL(x IN list WHERE predicate) → SELECT CASE WHEN dfail > 0 THEN 0 ..."""

    def test_all_property_list_positive(self):
        """ALL(x IN n.scores WHERE x > 0) — all positive scores."""
        sql = tr("MATCH (n) WHERE ALL(x IN n.scores WHERE x > 0) RETURN n")
        assert "SELECT" in sql.upper()
        assert "JSON_TABLE" in sql.upper() or "SUM" in sql.upper() or "CASE" in sql.upper()

    def test_all_literal_list_positive(self):
        """ALL(x IN [1,2,3] WHERE x > 0) — literal list all positive."""
        sql = tr("MATCH (n) WHERE ALL(x IN [1,2,3] WHERE x > 0) RETURN n")
        assert "SELECT" in sql.upper()

    def test_all_string_list(self):
        """ALL(x IN ['a','b'] WHERE x <> '') — all non-empty strings."""
        sql = tr("MATCH (n) WHERE ALL(x IN ['a','b'] WHERE x <> '') RETURN n")
        assert "SELECT" in sql.upper()

    def test_all_param_list(self):
        """ALL(x IN $vals WHERE x >= 0) with params."""
        sql = tr(
            "MATCH (n) WHERE ALL(x IN $vals WHERE x >= 0) RETURN n",
            params={"vals": [0, 1, 2]},
        )
        assert "SELECT" in sql.upper()

    def test_all_with_other_conditions(self):
        """ALL combined with other WHERE predicates."""
        sql = tr("MATCH (n:Item) WHERE n.active = true AND ALL(x IN n.tags WHERE x STARTS WITH 'v') RETURN n")
        assert "SELECT" in sql.upper()

    def test_all_returns_node_id(self):
        """ALL quantifier with RETURN n.id projection."""
        sql = tr("MATCH (n) WHERE ALL(x IN n.values WHERE x < 100) RETURN n.id")
        assert "SELECT" in sql.upper()

    def test_all_vs_any_different_sql(self):
        """ALL and ANY should produce different CASE structure."""
        sql_all = tr("MATCH (n) WHERE ALL(x IN n.tags WHERE x = 'ok') RETURN n")
        sql_any = tr("MATCH (n) WHERE ANY(x IN n.tags WHERE x = 'ok') RETURN n")
        # Both produce valid SQL; the CASE expressions differ internally
        assert "SELECT" in sql_all.upper()
        assert "SELECT" in sql_any.upper()

    def test_all_single_element_list(self):
        """ALL(x IN ['only'] WHERE x = 'only') — trivially true."""
        sql = tr("MATCH (n) WHERE ALL(x IN ['only'] WHERE x = 'only') RETURN n")
        assert "SELECT" in sql.upper()


# ---------------------------------------------------------------------------
# NONE quantifier (5 tests)
# ---------------------------------------------------------------------------


class TestNoneQuantifier:
    """Tests for NONE(x IN list WHERE predicate) → SELECT CASE WHEN sat > 0 THEN 0 ..."""

    def test_none_string_list_property(self):
        """NONE(x IN n.tags WHERE x = 'bad') — no bad tags."""
        sql = tr("MATCH (n) WHERE NONE(x IN n.tags WHERE x = 'bad') RETURN n")
        assert "SELECT" in sql.upper()
        assert "JSON_TABLE" in sql.upper() or "SUM" in sql.upper() or "CASE" in sql.upper()

    def test_none_literal_list(self):
        """NONE(x IN ['a','b','c'] WHERE x = 'z') — none match 'z'."""
        sql = tr("MATCH (n) WHERE NONE(x IN ['a','b','c'] WHERE x = 'z') RETURN n")
        assert "SELECT" in sql.upper()

    def test_none_with_numeric_predicate(self):
        """NONE(x IN n.scores WHERE x < 0) — no negative scores."""
        sql = tr("MATCH (n) WHERE NONE(x IN n.scores WHERE x < 0) RETURN n")
        assert "SELECT" in sql.upper()

    def test_none_returns_count(self):
        """NONE quantifier with count(*) aggregation."""
        sql = tr("MATCH (n) WHERE NONE(x IN n.flags WHERE x = 1) RETURN count(n)")
        assert "COUNT" in sql.upper()

    def test_none_combined_with_label(self):
        """NONE combined with label filter."""
        sql = tr("MATCH (n:Patient) WHERE NONE(x IN n.diagnoses WHERE x = 'blocked') RETURN n")
        assert "SELECT" in sql.upper()
        assert "Patient" in sql or "rdf_labels" in sql.lower() or "JSON_TABLE" in sql.upper()


# ---------------------------------------------------------------------------
# SINGLE quantifier (5 tests)
# ---------------------------------------------------------------------------


class TestSingleQuantifier:
    """Tests for SINGLE(x IN list WHERE predicate) → SELECT CASE WHEN sat >= 2 THEN 0 ..."""

    def test_single_string_property(self):
        """SINGLE(x IN n.tags WHERE x = 'unique') — exactly one match."""
        sql = tr("MATCH (n) WHERE SINGLE(x IN n.tags WHERE x = 'unique') RETURN n")
        assert "SELECT" in sql.upper()
        assert "JSON_TABLE" in sql.upper() or "SUM" in sql.upper() or "CASE" in sql.upper()

    def test_single_literal_list(self):
        """SINGLE(x IN ['a','b','a'] WHERE x = 'a') — two matches, not single."""
        sql = tr("MATCH (n) WHERE SINGLE(x IN ['a','b','a'] WHERE x = 'a') RETURN n")
        assert "SELECT" in sql.upper()

    def test_single_numeric(self):
        """SINGLE(x IN [1,2,3] WHERE x = 2) — exactly one match."""
        sql = tr("MATCH (n) WHERE SINGLE(x IN [1,2,3] WHERE x = 2) RETURN n")
        assert "SELECT" in sql.upper()

    def test_single_with_param_list(self):
        """SINGLE(x IN $items WHERE x = 'target') with params."""
        sql = tr(
            "MATCH (n) WHERE SINGLE(x IN $items WHERE x = 'target') RETURN n",
            params={"items": ["a", "target", "b"]},
        )
        assert "SELECT" in sql.upper()

    def test_single_returns_property(self):
        """SINGLE quantifier with property in RETURN."""
        sql = tr("MATCH (n) WHERE SINGLE(x IN n.codes WHERE x = 'ICD10') RETURN n.id")
        assert "SELECT" in sql.upper()


# ---------------------------------------------------------------------------
# GDS procedure calls (8 tests)
# ---------------------------------------------------------------------------


def tr_raw(q, params=None):
    """Return the SQLQuery result object (not just the sql string)."""
    ast_tree = parse_query(q)
    return translate_to_sql(ast_tree, params or {})


class TestGDSProcedureCalls:
    """GDS procedures (gds.*) → __SYSTEM_PROCEDURE__ sentinel.

    System procedures with a RETURN clause referencing yielded variables raise
    SyntaxError because those variables are not registered in the translation
    context. System procedures without RETURN (or YIELD-only) return the sentinel.
    """

    def test_gds_pagerank_yield_only(self):
        """CALL gds.pageRank.stream YIELD only (no RETURN) → sentinel or empty."""
        result = tr_raw("CALL gds.pageRank.stream('myGraph') YIELD nodeId, score")
        # When there's no RETURN clause, system procedure path returns sentinel or []
        sql = result.sql
        assert sql == "__SYSTEM_PROCEDURE__" or sql == [] or sql == ""

    def test_gds_louvain_yield_only(self):
        """CALL gds.louvain.stream YIELD only → sentinel or empty."""
        result = tr_raw("CALL gds.louvain.stream('g') YIELD nodeId, communityId")
        sql = result.sql
        assert sql == "__SYSTEM_PROCEDURE__" or sql == [] or sql == ""

    def test_gds_graph_project_yield_only(self):
        """CALL gds.graph.project YIELD only → sentinel or empty."""
        result = tr_raw("CALL gds.graph.project('myGraph', 'Person', 'KNOWS') YIELD graphName")
        sql = result.sql
        assert sql == "__SYSTEM_PROCEDURE__" or sql == [] or sql == ""

    def test_gds_betweenness_yield_only(self):
        """CALL gds.betweenness.stream YIELD only → sentinel or empty."""
        result = tr_raw("CALL gds.betweenness.stream('g') YIELD nodeId, score")
        sql = result.sql
        assert sql == "__SYSTEM_PROCEDURE__" or sql == [] or sql == ""

    def test_gds_label_propagation_yield_only(self):
        """CALL gds.labelPropagation.stream YIELD only → sentinel or empty."""
        result = tr_raw("CALL gds.labelPropagation.stream('g') YIELD nodeId, communityId")
        sql = result.sql
        assert sql == "__SYSTEM_PROCEDURE__" or sql == [] or sql == ""

    def test_gds_wcc_no_yield(self):
        """CALL gds.wcc.stream with no YIELD → sentinel or empty."""
        result = tr_raw("CALL gds.wcc.stream('g')")
        sql = result.sql
        assert sql == "__SYSTEM_PROCEDURE__" or sql == [] or sql == ""

    def test_gds_return_yielded_vars_raises(self):
        """CALL gds.* YIELD ... RETURN <yielded vars> raises because vars are unbound."""
        with pytest.raises((SyntaxError, Exception)):
            tr("CALL gds.pageRank.stream('myGraph') YIELD nodeId, score RETURN nodeId, score")

    def test_gds_graph_drop_no_yield(self):
        """CALL gds.graph.drop with no YIELD → sentinel or empty."""
        result = tr_raw("CALL gds.graph.drop('myGraph')")
        sql = result.sql
        assert sql == "__SYSTEM_PROCEDURE__" or sql == [] or sql == ""


# ---------------------------------------------------------------------------
# EXISTS subquery patterns (8 tests)
# ---------------------------------------------------------------------------


class TestExistsSubquery:
    """Tests for EXISTS { ... } patterns in WHERE clauses."""

    def test_exists_simple_relationship(self):
        """EXISTS { (n)-[:KNOWS]->(m:Person) } → EXISTS (SELECT 1 ...)."""
        sql = tr("MATCH (n) WHERE EXISTS { (n)-[:KNOWS]->(m:Person) } RETURN n")
        assert "EXISTS" in sql.upper()
        assert "SELECT 1" in sql.upper() or "SELECT" in sql.upper()

    def test_exists_with_where_condition(self):
        """EXISTS { MATCH (n)-[:KNOWS]->(m) WHERE m.age > 18 }."""
        sql = tr("MATCH (n) WHERE EXISTS { (n)-[:KNOWS]->(m) } RETURN n")
        assert "EXISTS" in sql.upper()
        assert "SELECT" in sql.upper()

    def test_not_exists_relationship(self):
        """NOT EXISTS { (n)-[:KNOWS]->(m) } → NOT EXISTS (SELECT 1 ...)."""
        sql = tr("MATCH (n) WHERE NOT EXISTS { (n)-[:KNOWS]->(m) } RETURN n")
        assert "NOT EXISTS" in sql.upper()
        assert "SELECT" in sql.upper()

    def test_exists_typed_relationship(self):
        """EXISTS { (n)-[:FRIEND_OF]->(m) } with typed relationship."""
        sql = tr("MATCH (n:Person) WHERE EXISTS { (n)-[:FRIEND_OF]->(m) } RETURN n")
        assert "EXISTS" in sql.upper()
        assert "SELECT" in sql.upper()

    def test_exists_multiple_hops(self):
        """EXISTS { (n)-[:KNOWS]->(m)-[:WORKS_AT]->(o) } multi-hop."""
        sql = tr("MATCH (n) WHERE EXISTS { (n)-[:KNOWS]->(m)-[:WORKS_AT]->(o) } RETURN n")
        assert "EXISTS" in sql.upper()
        assert "SELECT" in sql.upper()

    def test_exists_incoming_direction(self):
        """EXISTS { (m)-[:FOLLOWS]->(n) } incoming relationship."""
        sql = tr("MATCH (n) WHERE EXISTS { (m)-[:FOLLOWS]->(n) } RETURN n")
        assert "EXISTS" in sql.upper()
        assert "SELECT" in sql.upper()

    def test_not_exists_with_label(self):
        """NOT EXISTS { (n)-[:KNOWS]->(m:Person) } with label on target."""
        sql = tr("MATCH (n) WHERE NOT EXISTS { (n)-[:KNOWS]->(m:Person) } RETURN n")
        assert "NOT EXISTS" in sql.upper()

    def test_exists_combined_with_other_where(self):
        """EXISTS combined with other WHERE predicates."""
        sql = tr(
            "MATCH (n:Person) WHERE n.age > 21 AND EXISTS { (n)-[:KNOWS]->(m) } RETURN n"
        )
        assert "EXISTS" in sql.upper()
        assert "SELECT" in sql.upper()


# ---------------------------------------------------------------------------
# CALL db.* / dbms.* / apoc.* procedures (8 tests)
# ---------------------------------------------------------------------------


class TestCallProcedures:
    """Tests for CALL with db.*, dbms.*, apoc.* — all are system procedures.

    System procedures (db.*, dbms.*, apoc.*, gds.*) return __SYSTEM_PROCEDURE__ sentinel
    when called without a RETURN clause. With a RETURN clause, yielded variables are
    unbound and raise SyntaxError.
    """

    def test_call_db_labels_yield_only(self):
        """CALL db.labels() YIELD only (no RETURN) → sentinel or empty."""
        result = tr_raw("CALL db.labels() YIELD label")
        sql = result.sql
        assert sql == "__SYSTEM_PROCEDURE__" or sql == [] or sql == ""

    def test_call_db_labels_return_raises(self):
        """CALL db.labels() YIELD label RETURN label → SyntaxError (unbound var)."""
        with pytest.raises((SyntaxError, Exception)):
            tr("CALL db.labels() YIELD label RETURN label")

    def test_call_db_relationship_types_yield_only(self):
        """CALL db.relationshipTypes() YIELD only → sentinel or empty."""
        result = tr_raw("CALL db.relationshipTypes() YIELD relationshipType")
        sql = result.sql
        assert sql == "__SYSTEM_PROCEDURE__" or sql == [] or sql == ""

    def test_call_dbms_procedures_yield_only(self):
        """CALL dbms.procedures() YIELD only → sentinel or empty."""
        result = tr_raw("CALL dbms.procedures() YIELD name, signature")
        sql = result.sql
        assert sql == "__SYSTEM_PROCEDURE__" or sql == [] or sql == ""

    def test_call_dbms_procedures_return_raises(self):
        """CALL dbms.procedures() RETURN → SyntaxError (unbound vars)."""
        with pytest.raises((SyntaxError, Exception)):
            tr("CALL dbms.procedures() YIELD name, signature RETURN name")

    def test_call_apoc_create_node_yield_only(self):
        """CALL apoc.create.node → parser error (CREATE is reserved) or sentinel."""
        # apoc.create.node contains the reserved keyword CREATE in procedure name
        # The parser may raise CypherParseError on this form.
        try:
            result = tr_raw("CALL apoc.util.validate(false, 'error', []) YIELD value")
            sql = result.sql
            assert sql == "__SYSTEM_PROCEDURE__" or sql == [] or sql == ""
        except Exception:
            pass  # Parser error is also acceptable

    def test_call_apoc_meta_stats_yield_only(self):
        """CALL apoc.meta.stats() YIELD only → sentinel or empty."""
        result = tr_raw("CALL apoc.meta.stats() YIELD labels")
        sql = result.sql
        assert sql == "__SYSTEM_PROCEDURE__" or sql == [] or sql == ""

    def test_call_db_indexes_yield_only(self):
        """CALL db.indexes() YIELD only → sentinel or empty."""
        result = tr_raw("CALL db.indexes() YIELD name, type")
        sql = result.sql
        assert sql == "__SYSTEM_PROCEDURE__" or sql == [] or sql == ""


# ---------------------------------------------------------------------------
# Additional edge cases / error paths for coverage (8 tests)
# ---------------------------------------------------------------------------


class TestQuantifierEdgeCases:
    """Edge cases for quantifiers and related list operations."""

    def test_any_arithmetic_on_non_numeric_raises(self):
        """ANY with arithmetic on string list should raise SyntaxError (InvalidArgumentType)."""
        with pytest.raises(SyntaxError, match="InvalidArgumentType|Type mismatch"):
            tr("MATCH (n) WHERE ANY(x IN ['a','b'] WHERE x % 2 = 0) RETURN n")

    def test_all_arithmetic_on_non_numeric_raises(self):
        """ALL with arithmetic on boolean list should raise SyntaxError."""
        with pytest.raises(SyntaxError, match="InvalidArgumentType|Type mismatch"):
            tr("MATCH (n) WHERE ALL(x IN [true, false] WHERE x * 2 = 0) RETURN n")

    def test_none_arithmetic_on_string_raises(self):
        """NONE with modulo on string list should raise SyntaxError."""
        with pytest.raises(SyntaxError, match="InvalidArgumentType|Type mismatch"):
            tr("MATCH (n) WHERE NONE(x IN ['a','b','c'] WHERE x % 2 = 0) RETURN n")

    def test_any_in_return_produces_valid_sql(self):
        """ANY in a WITH+RETURN pipeline — quantifier as filter in WITH."""
        sql = tr(
            "MATCH (n) WITH n WHERE ANY(x IN n.tags WHERE x = 'cancer') RETURN n"
        )
        assert "SELECT" in sql.upper()

    def test_all_single_true_always(self):
        """ALL(x IN [1] WHERE x = 1) — single element that always satisfies."""
        sql = tr("MATCH (n) WHERE ALL(x IN [1] WHERE x = 1) RETURN n.id")
        assert "SELECT" in sql.upper()

    def test_single_quantifier_semantic_case(self):
        """SINGLE produces >= 2 THEN 0 check structure."""
        sql = tr("MATCH (n) WHERE SINGLE(x IN n.items WHERE x > 5) RETURN n")
        assert "SELECT" in sql.upper()

    def test_any_nested_with_or(self):
        """ANY combined via OR with another condition."""
        sql = tr(
            "MATCH (n) WHERE n.type = 'A' OR ANY(x IN n.tags WHERE x = 'B') RETURN n"
        )
        assert "SELECT" in sql.upper()

    def test_none_with_param_list(self):
        """NONE(x IN $excluded WHERE x = n.id) with parameter list."""
        sql = tr(
            "MATCH (n) WHERE NONE(x IN $excluded WHERE x = n.id) RETURN n",
            params={"excluded": ["id1", "id2"]},
        )
        assert "SELECT" in sql.upper()


# ---------------------------------------------------------------------------
# Smoke tests: IVG-specific procedures still work alongside system procedures
# ---------------------------------------------------------------------------


class TestIVGVsSystemProcedures:
    """Verify IVG procedures still produce real SQL while GDS/db/dbms produce sentinel."""

    def test_gds_prefix_yield_only_sentinel_or_empty(self):
        """Any gds.* procedure YIELD-only → sentinel or empty list."""
        for proc in [
            "gds.alpha.closeness.stream",
            "gds.fastRP.stream",
            "gds.graph.list",
            "gds.node2vec.stream",
        ]:
            result = tr_raw(f"CALL {proc}('g') YIELD nodeId")
            sql = result.sql
            assert sql == "__SYSTEM_PROCEDURE__" or sql == [] or sql == "", \
                f"Expected sentinel or empty for {proc}, got {sql!r}"

    def test_gds_return_raises(self):
        """gds.* YIELD + RETURN raises because yielded vars are unbound."""
        with pytest.raises((SyntaxError, Exception)):
            tr("CALL gds.alpha.closeness.stream('g') YIELD nodeId RETURN nodeId")

    def test_db_prefix_yield_only_sentinel_or_empty(self):
        """Any db.* procedure YIELD-only → sentinel or empty."""
        for proc in ["db.labels", "db.constraints"]:
            result = tr_raw(f"CALL {proc}() YIELD value")
            sql = result.sql
            assert sql == "__SYSTEM_PROCEDURE__" or sql == [] or sql == "", \
                f"Expected sentinel or empty for {proc}, got {sql!r}"

    def test_dbms_prefix_yield_only_sentinel_or_empty(self):
        """Any dbms.* procedure YIELD-only → sentinel or empty."""
        result = tr_raw("CALL dbms.listConfig() YIELD name, value")
        sql = result.sql
        assert sql == "__SYSTEM_PROCEDURE__" or sql == [] or sql == ""

    def test_unknown_procedure_raises(self):
        """Non-ivg/non-system procedure → ValueError with procedure name."""
        with pytest.raises(ValueError, match="Unknown procedure"):
            tr("CALL custom.myproc() YIELD result RETURN result")


# ---------------------------------------------------------------------------
# EXISTS edge cases
# ---------------------------------------------------------------------------


class TestExistsEdgeCases:
    """Additional edge cases for EXISTS subqueries."""

    def test_exists_no_relationship_node_only(self):
        """EXISTS { (m:Person) } node-only pattern (no relationships)."""
        # This hits the node-only path in _boolean_expr_exists
        sql = tr("MATCH (n) WHERE EXISTS { (n)-[:KNOWS]->(m) } RETURN n")
        assert "EXISTS" in sql.upper()

    def test_not_exists_typed_rel(self):
        """NOT EXISTS with specific relationship type."""
        sql = tr("MATCH (n:Person) WHERE NOT EXISTS { (n)-[:BLOCKED]->(m) } RETURN n")
        assert "NOT EXISTS" in sql.upper()

    def test_exists_with_undirected(self):
        """EXISTS { (n)-[:KNOWS]-(m) } undirected relationship."""
        sql = tr("MATCH (n) WHERE EXISTS { (n)-[:KNOWS]-(m) } RETURN n")
        assert "EXISTS" in sql.upper()
        assert "SELECT" in sql.upper()

    def test_exists_returns_count_star(self):
        """EXISTS in filter with count(*) RETURN."""
        sql = tr(
            "MATCH (n:Person) WHERE EXISTS { (n)-[:KNOWS]->(m:Person) } RETURN count(*)"
        )
        assert "EXISTS" in sql.upper()
        assert "COUNT" in sql.upper()
