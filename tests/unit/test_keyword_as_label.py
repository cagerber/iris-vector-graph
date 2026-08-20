"""Tests for keyword tokens used as node labels in Cypher.

Cypher keywords like END, ELSE, NULL, etc. are contextual — they must be
usable as node labels in patterns even though they're lexer keywords.
"""
import pytest
from iris_vector_graph.cypher.parser import parse_query, CypherParseError
from iris_vector_graph.cypher.translator import translate_to_sql


class TestKeywordAsLabel:
    def test_end_as_node_label_in_match(self):
        """MATCH (n:End) must not raise CypherParseError."""
        result = parse_query("MATCH (n:End) RETURN n")
        assert result is not None

    def test_end_as_node_label_in_create(self):
        """CREATE (n:End) must not raise."""
        result = parse_query("CREATE (n:End)")
        assert result is not None

    def test_end_as_node_label_in_relationship_target(self):
        """MATCH (a:Begin)-[:TYPE]->(b:End) must parse correctly and label b as End."""
        result = parse_query("MATCH (a:Begin)-[:TYPE]->(b:End) RETURN a, b")
        assert result is not None
        # Verify End ends up in the SQL params (translation test is sufficient)
        sql_result = translate_to_sql(result)
        assert "End" in str(sql_result.parameters)

    def test_end_as_label_translates(self):
        """MATCH (a:Begin)-[:TYPE]->(b:End): End must appear in SQL params."""
        result = translate_to_sql(parse_query("MATCH (a:Begin)-[:TYPE]->(b:End) RETURN a, b"))
        assert "End" in str(result.parameters)
        assert "Begin" in str(result.parameters)

    def test_case_end_not_broken(self):
        """CASE ... END still parses correctly after expect_label change."""
        q = "MATCH (n) RETURN CASE n.x WHEN 1 THEN 'a' ELSE 'b' END AS v"
        result = parse_query(q)
        assert result is not None

    def test_null_as_label(self):
        """MATCH (n:NULL) — NULL as a label must parse (contextual keyword)."""
        result = parse_query("MATCH (n:NULL) RETURN n")
        assert result is not None
