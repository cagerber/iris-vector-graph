"""Unit tests for CREATE clause validation — VariableAlreadyBound and syntax checks."""
import pytest
from iris_vector_graph.cypher.parser import parse_query
from iris_vector_graph.cypher.translator import translate_to_sql

def translate(query_str, params=None):
    return translate_to_sql(parse_query(query_str), params)


class TestVariableAlreadyBound:
    def test_node_rebind_raises_syntax_error(self):
        """MATCH (n) CREATE (n) must raise SyntaxError."""
        with pytest.raises(SyntaxError, match="VariableAlreadyBound"):
            translate("MATCH (n) CREATE (n)")

    def test_node_rebind_with_props_raises_syntax_error(self):
        """MATCH (n) CREATE (n {p: 1}) must raise SyntaxError."""
        with pytest.raises(SyntaxError, match="VariableAlreadyBound"):
            translate("MATCH (n) CREATE (n {p: 1})")

    def test_node_rebind_with_label_raises_syntax_error(self):
        """MATCH (n) CREATE (n:A) must raise SyntaxError."""
        with pytest.raises(SyntaxError, match="VariableAlreadyBound"):
            translate("MATCH (n) CREATE (n:A)")

    def test_relationship_rebind_raises_syntax_error(self):
        """MATCH ()-[r]->() CREATE ()-[r]->() must raise SyntaxError."""
        with pytest.raises(SyntaxError, match="VariableAlreadyBound"):
            translate("MATCH ()-[r]->() CREATE ()-[r]->()")

    def test_new_variable_in_create_ok(self):
        """CREATE (n) with fresh variable must not raise."""
        result = translate("CREATE (n:A)")
        assert result is not None

    def test_create_after_match_new_variable_ok(self):
        """MATCH (x) CREATE (y:B) — new variable y must not raise."""
        result = translate("MATCH (x:A) CREATE (y:B)")
        assert result is not None


class TestCreateSyntaxValidation:
    def test_no_rel_type_raises_syntax_error(self):
        """CREATE ()-->() with no type must raise SyntaxError."""
        with pytest.raises(SyntaxError, match="NoSingleRelationshipType"):
            translate("CREATE ()-[r]->()")

    def test_no_direction_raises_syntax_error(self):
        """CREATE (a)-[:FOO]-(b) undirected must raise SyntaxError."""
        with pytest.raises(SyntaxError, match="RequiresDirectedRelationship"):
            translate("CREATE (a)-[:FOO]-(b)")

    def test_multi_type_raises_syntax_error(self):
        """CREATE ()-[:A|:B]->() multi-type must raise SyntaxError."""
        with pytest.raises(SyntaxError, match="NoSingleRelationshipType"):
            translate("CREATE ()-[:A|B]->()")

    def test_variable_length_raises_syntax_error(self):
        """CREATE ()-[:FOO*2]->() variable-length must raise SyntaxError."""
        with pytest.raises(SyntaxError, match="CreatingVarLength"):
            translate("CREATE ()-[:FOO*2]->()")

    def test_valid_create_relationship_ok(self):
        """CREATE ()-[:R]->() valid relationship must not raise."""
        result = translate("CREATE ()-[:R]->()")
        assert result is not None

    def test_reverse_direction_create_ok(self):
        """CREATE (:A)<-[:R]-(:B) reverse direction must not raise."""
        result = translate("CREATE (:A)<-[:R]-(:B)")
        assert result is not None


class TestCreateReverseDirection:
    def test_reverse_direction_swaps_source_target(self):
        """CREATE (:A)<-[:R]-(:B): edge inserted as B (source) -> A (target)."""
        result = translate("CREATE (:A)<-[:R]-(:B)")
        assert result.is_transactional
        stmts = result.sql
        params = result.parameters
        edge_idx = next((i for i, s in enumerate(stmts) if "rdf_edges" in s), None)
        assert edge_idx is not None, "Expected rdf_edges INSERT"
        edge_p = params[edge_idx]  # [source_id, 'R', target_id]
        assert edge_p[1] == 'R', f"Expected predicate 'R', got {edge_p[1]}"
        # A is first node in pattern, B is second
        a_node_uuid = params[0][0]  # First nodes INSERT
        b_node_uuid = params[2][0]  # Third INSERT (second node)
        assert edge_p[0] == b_node_uuid, "B should be edge source for INCOMING direction"
        assert edge_p[2] == a_node_uuid, "A should be edge target for INCOMING direction"


class TestCreateIntegerIdProperty:
    def test_integer_id_not_used_as_node_identifier(self):
        """CREATE (n {id: 12}) must generate a UUID, not use 12 as node_id."""
        result = translate("CREATE (n {id: 12})")
        assert result.is_transactional
        node_inserts = [(s, p) for s, p in zip(result.sql, result.parameters)
                        if "nodes" in s and "INSERT" in s]
        assert node_inserts, "Expected nodes INSERT"
        first_param = node_inserts[0][1][0]
        assert first_param != 12, "Integer id should not be used as node_id"
        assert len(str(first_param)) == 36, f"Expected UUID node_id, got: {first_param}"
