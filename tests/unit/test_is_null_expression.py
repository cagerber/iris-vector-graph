"""Tests for IS NULL / IS NOT NULL property expressions.

When n.missing IS NULL is translated, the structural-guard EXISTS clause must
NOT be added to WHERE — doing so would filter out the row entirely.
"""
import pytest
from iris_vector_graph.cypher.parser import parse_query
from iris_vector_graph.cypher.translator import translate_to_sql


def translate(q):
    return translate_to_sql(parse_query(q))


class TestIsNullTranslation:
    def test_is_null_no_structural_guard(self):
        """RETURN n.missing IS NULL: structural guard must NOT appear in SQL."""
        result = translate("MATCH (n:X) RETURN n.missing IS NULL")
        # The structural guard looks like: EXISTS (SELECT 1 FROM rdf_props ... key = 'missing')
        assert "missing" not in result.sql or "EXISTS" not in result.sql or (
            "missing" in result.sql and "EXISTS" not in result.sql.split("missing")[0]
        ), "Structural guard EXISTS block found before IS NULL check"
        # More direct: no structural guard for IS NULL
        assert f"'missing'" not in result.sql.split("IS NULL")[0] or \
               "LEFT JOIN" in result.sql, "Missing LEFT JOIN for IS NULL check"

    def test_is_null_uses_left_join(self):
        """n.missing IS NULL must use LEFT JOIN rdf_props (not INNER/structural JOIN)."""
        result = translate("MATCH (n:X) RETURN n.missing IS NULL")
        assert "LEFT JOIN" in result.sql or "LEFT OUTER JOIN" in result.sql

    def test_is_null_no_exists_guard(self):
        """IS NULL check must NOT have an EXISTS structural guard in WHERE."""
        result = translate("MATCH (n:X) RETURN n.p IS NULL")
        # The query must not have WHERE EXISTS for the property
        sql_upper = result.sql.upper()
        # structural guard = EXISTS (SELECT 1 FROM rdf_props WHERE key=p)
        # This would be: WHERE EXISTS (...) AND ...
        # Count of EXISTS: should only be from label check if any, not from prop guard
        where_part = result.sql.split("WHERE")[1] if "WHERE" in result.sql else ""
        assert "EXISTS" not in where_part or "rdf_props" not in where_part.split("EXISTS")[1][:100], \
            "Structural guard EXISTS in WHERE for IS NULL query"

    def test_is_not_null_no_structural_guard(self):
        """n.exists IS NOT NULL: same — no structural guard."""
        result = translate("MATCH (n:X) RETURN n.exists IS NOT NULL")
        assert "LEFT JOIN" in result.sql or "LEFT OUTER JOIN" in result.sql

    def test_case_when_null_in_select(self):
        """IS NULL in SELECT produces CASE WHEN p.val IS NULL THEN 1 ELSE 0 END."""
        result = translate("MATCH (n:X) RETURN n.x IS NULL")
        assert "CASE WHEN" in result.sql
        assert "IS NULL" in result.sql
