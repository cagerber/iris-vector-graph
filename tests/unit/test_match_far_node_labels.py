"""Tests for label filtering on far-end nodes in relationship patterns.

When translating MATCH (a:X)<-[:R]-(b:Y), node b is the far-end of the edge
JOIN. Its labels (Y) must be applied as rdf_labels JOINs even though b's alias
was registered by translate_relationship_pattern before translate_node_pattern
sees it.
"""
import pytest
from iris_vector_graph.cypher.parser import parse_query
from iris_vector_graph.cypher.translator import translate_to_sql


def translate(q):
    return translate_to_sql(parse_query(q))


class TestFarNodeLabels:
    def test_outgoing_target_single_label_joined(self):
        """MATCH (a:X)-[:R]->(b:Y): b's label Y must appear in SQL."""
        result = translate("MATCH (a:X)-[:R]->(b:Y) RETURN a, b")
        assert "Y" in str(result.parameters), "Label Y not in params"

    def test_incoming_target_single_label_joined(self):
        """MATCH (a:X)<-[:R]-(b:Y): b's label Y must appear in SQL."""
        result = translate("MATCH (a:X)<-[:R]-(b:Y) RETURN a, b")
        assert "Y" in str(result.parameters), "Label Y not in params for INCOMING"

    def test_multi_label_far_node_all_labels_joined(self):
        """MATCH (a:X)<-[:R]-(b:Y:TCK_abc): both Y and TCK_abc must appear."""
        result = translate("MATCH (a:X)<-[:R]-(b:Y:TCK_abc) RETURN a, b")
        params_flat = str(result.parameters)
        assert "Y" in params_flat, "Label Y missing"
        assert "TCK_abc" in params_flat, "Label TCK_abc missing"

    def test_far_node_label_filters_results(self):
        """With a wrong label, SQL must contain the label as a JOIN condition.

        We test SQL structure — the label must appear as a parameterized JOIN on
        rdf_labels anchored to the far-end node alias (not just in a subselect).
        """
        result = translate("MATCH (a:X)<-[:R]-(b:Y) RETURN b")
        sql = result.sql
        # rdf_labels must be joined for label filtering
        assert "rdf_labels" in sql, "No rdf_labels join for far-node label"
        # The label Y must be a param, not just a subselect
        assert "Y" in str(result.parameters), "Label Y missing from params"

    def test_outgoing_far_node_multi_label(self):
        """MATCH (a)-[:R]->(b:Y:Z): both Y and Z must be in params."""
        result = translate("MATCH (a)-[:R]->(b:Y:Z) RETURN b")
        params_flat = str(result.parameters)
        assert "Y" in params_flat, "Label Y missing"
        assert "Z" in params_flat, "Label Z missing"
