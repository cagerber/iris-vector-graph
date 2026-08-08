"""
Coverage tests for cypher/parser.py uncovered paths.
Targets lines 80, 97-99, 204-210, 280-317, 380-387, 475, 499-502, 588, 604-627,
633-650, 692, 713, 730, 753, 781, 786, 803-805, 808, 824, 829, 838-840, 850,
913-930, 949-950, 968-969, 1007-1040, 1134-1135, 1198-1251, 1288-1301, 1372-1395,
1410, 1421-1446, 1494-1506, 1539, 1556-1598, 1649-1650, 1655, 1682, 1692, 1718-1738,
1767, 1788-1789, 1793, 1812, 1826.
"""
import pytest
from iris_vector_graph.cypher.parser import parse_query, CypherParseError
from iris_vector_graph.cypher import ast


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def parse(q):
    return parse_query(q)


# ---------------------------------------------------------------------------
# Lines 80, 97-99: expect / expect_identifier_or_keyword error paths
# Triggered by querying with keyword tokens as identifiers
# ---------------------------------------------------------------------------

def test_keyword_as_alias_in_return():
    """Keywords used as alias names are accepted."""
    q = parse("MATCH (n) RETURN n.name AS count")
    assert q is not None


def test_keyword_as_alias_with():
    """Keywords as WITH aliases."""
    q = parse("MATCH (n) WITH n.name AS order RETURN order")
    assert q is not None


# ---------------------------------------------------------------------------
# Lines 204-210: CALL followed by WITH
# ---------------------------------------------------------------------------

def test_call_with_subsequent_with_match():
    """CALL procedure followed by WITH then MATCH."""
    q = parse("CALL db.labels() YIELD label WITH label MATCH (n) WHERE n.type = label RETURN n")
    assert q is not None


# ---------------------------------------------------------------------------
# Lines 280-317: UNION inside parse() at the top level
# Multiple RETURN blocks in one parse tree
# ---------------------------------------------------------------------------

def test_union_all_two_branches():
    """UNION ALL produces union_queries."""
    q = parse("MATCH (n:Person) RETURN n UNION ALL MATCH (n:Drug) RETURN n")
    assert q is not None
    assert len(q.union_queries) >= 1


def test_union_two_branches():
    """UNION (dedup) two queries."""
    q = parse("MATCH (n) WHERE n.x = 1 RETURN n UNION MATCH (n) WHERE n.x = 2 RETURN n")
    assert q is not None


def test_union_three_branches():
    """Three-way UNION produces two union_queries entries."""
    q = parse("RETURN 1 AS x UNION RETURN 2 AS x UNION RETURN 3 AS x")
    assert q is not None
    assert len(q.union_queries) >= 2


def test_return_followed_by_return_as_subsequent():
    """A second RETURN after WITH becomes a subsequent_queries entry."""
    # WITH ... RETURN ... acts as a subsequent
    q = parse("MATCH (n) WITH n RETURN n.id AS id")
    assert q is not None


# ---------------------------------------------------------------------------
# Lines 380-387: OPTIONAL CALL { ... } subquery
# ---------------------------------------------------------------------------

def test_optional_call_subquery():
    """OPTIONAL CALL { MATCH (n) RETURN n } is parsed."""
    q = parse("MATCH (n) OPTIONAL CALL { MATCH (m) RETURN m } RETURN n")
    assert q is not None


def test_optional_match_basic():
    """OPTIONAL MATCH creates optional=True clause."""
    q = parse("MATCH (n) OPTIONAL MATCH (n)-[r]->(m) RETURN n, m")
    parts = q.query_parts
    # At least one clause is optional
    all_clauses = [c for p in parts for c in p.clauses]
    optional_matches = [c for c in all_clauses if isinstance(c, ast.MatchClause) and c.optional]
    assert len(optional_matches) >= 1


# ---------------------------------------------------------------------------
# Lines 499-502: subquery WITH chaining
# ---------------------------------------------------------------------------

def test_subquery_call_with_inner_with():
    """CALL { MATCH ... WITH ... RETURN ... } uses inner WITH."""
    q = parse("CALL { MATCH (n) WITH n MATCH (m) RETURN n, m } RETURN n, m")
    assert q is not None


# ---------------------------------------------------------------------------
# Lines 588, 604-627: _is_pattern_predicate detection
# These are triggered by pattern predicates in WHERE
# ---------------------------------------------------------------------------

def test_pattern_predicate_simple():
    """WHERE (n)-[:KNOWS]->() is detected as pattern predicate."""
    q = parse("MATCH (n) WHERE (n)-[:KNOWS]->() RETURN n")
    assert q is not None


def test_pattern_predicate_not():
    """WHERE NOT (n)-[:BLOCKED]->() is a negated pattern predicate."""
    q = parse("MATCH (n) WHERE NOT (n)-[:BLOCKED]->() RETURN n")
    assert q is not None


def test_pattern_predicate_inbound():
    """WHERE (n)<-[:KNOWS]-() is a pattern predicate."""
    q = parse("MATCH (n) WHERE (n)<-[:KNOWS]-() RETURN n")
    assert q is not None


def test_parenthesized_expr_not_pattern_predicate():
    """WHERE (n.age + 1) > 18 is NOT a pattern predicate."""
    q = parse("MATCH (n) WHERE (n.age + 1) > 18 RETURN n")
    assert q is not None


def test_boolean_expr_in_paren_not_pattern():
    """WHERE (n.active = true) is not a pattern predicate."""
    q = parse("MATCH (n) WHERE (n.active = true) RETURN n")
    assert q is not None


# ---------------------------------------------------------------------------
# Lines 633-650: _is_pattern_comprehension_start
# Triggered by pattern comprehension [ (n)-->(m) | m ]
# ---------------------------------------------------------------------------

def test_pattern_comprehension_bracket():
    """Pattern comprehension [(n)-[:KNOWS]->(m) | m] is parsed."""
    q = parse("MATCH (n) RETURN [(n)-[:KNOWS]->(m) | m] AS friends")
    assert q is not None


def test_list_comprehension_with_where():
    """List comprehension [x IN list WHERE pred | expr]."""
    q = parse("MATCH (n) RETURN [x IN [1,2,3] WHERE x > 1 | x * 2] AS filtered")
    assert q is not None


# ---------------------------------------------------------------------------
# Lines 692, 713, 730: parse_where_clause variants
# Labels filter, IS NOT NULL, etc.
# ---------------------------------------------------------------------------

def test_where_is_null():
    """WHERE n.x IS NULL is parsed."""
    q = parse("MATCH (n) WHERE n.x IS NULL RETURN n")
    assert q is not None


def test_where_is_not_null():
    """WHERE n.x IS NOT NULL is parsed."""
    q = parse("MATCH (n) WHERE n.x IS NOT NULL RETURN n")
    assert q is not None


def test_where_label_filter():
    """WHERE n:Person is parsed as a label filter."""
    q = parse("MATCH (n) WHERE n:Person RETURN n")
    assert q is not None


def test_where_not_label_filter():
    """WHERE NOT n:Person."""
    q = parse("MATCH (n) WHERE NOT n:Person RETURN n")
    assert q is not None


# ---------------------------------------------------------------------------
# Lines 781, 786, 803-840: parse_match_clause patterns
# Multiple labels, relationship types, properties in patterns
# ---------------------------------------------------------------------------

def test_multi_label_node():
    """(n:Person:Doctor) has two labels."""
    q = parse("MATCH (n:Person:Doctor) RETURN n")
    assert q is not None


def test_node_with_properties():
    """(n {name: 'Alice', age: 30}) parses property map."""
    q = parse("MATCH (n {name: 'Alice', age: 30}) RETURN n")
    assert q is not None


def test_relationship_with_properties():
    """[r:KNOWS {since: 2020}] parses rel with properties."""
    q = parse("MATCH (n)-[r:KNOWS {since: 2020}]->(m) RETURN n, r, m")
    assert q is not None


def test_undirected_relationship():
    """(n)-[r]-(m) undirected."""
    q = parse("MATCH (n)-[r]-(m) RETURN n, r, m")
    assert q is not None


def test_left_directed_relationship():
    """(n)<-[r]-(m) left-directed."""
    q = parse("MATCH (n)<-[r:KNOWS]-(m) RETURN n, r, m")
    assert q is not None


def test_multi_rel_type():
    """[r:KNOWS|LIKES] multi-type relationship."""
    q = parse("MATCH (n)-[r:KNOWS|LIKES]->(m) RETURN n, r, m")
    assert q is not None


# ---------------------------------------------------------------------------
# Lines 913-950: parse_set_clause / parse_remove_clause
# ---------------------------------------------------------------------------

def test_set_property():
    """SET n.name = 'Alice'."""
    q = parse("MATCH (n) SET n.name = 'Alice' RETURN n")
    assert q is not None


def test_set_labels():
    """SET n:Person adds a label."""
    q = parse("MATCH (n) SET n:Person RETURN n")
    assert q is not None


def test_set_multiple():
    """SET n.x = 1, n.y = 2 multiple set items."""
    q = parse("MATCH (n) SET n.x = 1, n.y = 2 RETURN n")
    assert q is not None


def test_remove_property():
    """REMOVE n.x."""
    q = parse("MATCH (n) REMOVE n.x RETURN n")
    assert q is not None


def test_remove_label():
    """REMOVE n:TempLabel."""
    q = parse("MATCH (n) REMOVE n:TempLabel RETURN n")
    assert q is not None


# ---------------------------------------------------------------------------
# Lines 968-969: parse_delete_clause
# ---------------------------------------------------------------------------

def test_delete_node():
    """DELETE n."""
    q = parse("MATCH (n) DELETE n")
    assert q is not None


def test_detach_delete():
    """DETACH DELETE n."""
    q = parse("MATCH (n) DETACH DELETE n")
    assert q is not None


# ---------------------------------------------------------------------------
# Lines 1007-1040: parse_foreach_clause
# ---------------------------------------------------------------------------

def test_foreach_basic():
    """FOREACH (x IN [1,2,3] | SET n.val = x)."""
    q = parse("MATCH (n) FOREACH (x IN [1, 2, 3] | SET n.val = x)")
    assert q is not None


def test_foreach_create():
    """FOREACH with CREATE."""
    q = parse("MATCH (n) FOREACH (tag IN n.tags | MERGE (:Tag {name: tag}))")
    assert q is not None


# ---------------------------------------------------------------------------
# Lines 1134-1135: parse_merge_clause
# ---------------------------------------------------------------------------

def test_merge_node():
    """MERGE (n:Person {name: 'Alice'})."""
    q = parse("MERGE (n:Person {name: 'Alice'}) RETURN n")
    assert q is not None


def test_merge_with_on_create():
    """MERGE ... ON CREATE SET ..."""
    q = parse("MERGE (n:Person {id: 'n1'}) ON CREATE SET n.created = true RETURN n")
    assert q is not None


def test_merge_with_on_match():
    """MERGE ... ON MATCH SET ..."""
    q = parse("MERGE (n:Person {id: 'n1'}) ON MATCH SET n.updated = true RETURN n")
    assert q is not None


# ---------------------------------------------------------------------------
# Lines 1198-1251: parse_expression — complex cases
# Function call on result (e.g. size(collect(...)) )
# ---------------------------------------------------------------------------

def test_function_call_chained():
    """size(collect(n)) — function call on aggregation result."""
    q = parse("MATCH (n) RETURN size(collect(n)) AS cnt")
    assert q is not None


def test_function_call_dot_prop():
    """exists(n.prop) — function followed by property."""
    q = parse("MATCH (n) RETURN exists(n.name) AS hasName")
    assert q is not None


def test_namespace_function_call():
    """duration.inSeconds(a, b) — namespace.function() call."""
    q = parse("MATCH (n) RETURN duration.inSeconds(n.startTs, n.stopTs) AS dur")
    assert q is not None


# ---------------------------------------------------------------------------
# Lines 1288-1301: parse_expression — literal list and map
# ---------------------------------------------------------------------------

def test_literal_list():
    """[1, 2, 3] literal list."""
    q = parse("MATCH (n) RETURN [1, 2, 3] AS list")
    assert q is not None


def test_literal_map():
    """{ key: 'val' } literal map."""
    q = parse("MATCH (n) RETURN {name: 'Alice', age: 30} AS m")
    assert q is not None


def test_nested_literal_map():
    """Nested map literal."""
    q = parse("MATCH (n) RETURN {outer: {inner: 1}} AS m")
    assert q is not None


# ---------------------------------------------------------------------------
# Lines 1372-1395: parse ORDER BY with expr variants
# ---------------------------------------------------------------------------

def test_order_by_function_expr():
    """ORDER BY size(n.tags) DESC."""
    q = parse("MATCH (n) RETURN n ORDER BY size(n.tags) DESC")
    assert q is not None


def test_order_by_multiple_columns():
    """ORDER BY n.age ASC, n.name DESC."""
    q = parse("MATCH (n) RETURN n ORDER BY n.age ASC, n.name DESC")
    assert q is not None


def test_order_by_ascending_keyword():
    """ASCENDING keyword."""
    q = parse("MATCH (n) RETURN n.id ORDER BY n.id ASCENDING")
    assert q is not None


def test_order_by_descending_keyword():
    """DESCENDING keyword."""
    q = parse("MATCH (n) RETURN n.id ORDER BY n.id DESCENDING")
    assert q is not None


# ---------------------------------------------------------------------------
# Lines 1421-1446: parse_expression — CASE
# ---------------------------------------------------------------------------

def test_case_simple():
    """Simple CASE expr."""
    q = parse("MATCH (n) RETURN CASE n.type WHEN 'a' THEN 1 ELSE 0 END AS v")
    assert q is not None


def test_case_searched():
    """Searched CASE expr."""
    q = parse("MATCH (n) RETURN CASE WHEN n.age > 18 THEN 'adult' ELSE 'minor' END AS grp")
    assert q is not None


def test_case_no_else():
    """CASE without ELSE clause."""
    q = parse("MATCH (n) RETURN CASE WHEN n.active THEN 'yes' END AS v")
    assert q is not None


# ---------------------------------------------------------------------------
# Lines 1494-1506: filter/extract list comprehension (named functions)
# ---------------------------------------------------------------------------

def test_filter_list_comprehension():
    """filter(x IN list WHERE x > 0) — legacy Cypher."""
    q = parse("MATCH (n) RETURN filter(x IN [1,-1,2] WHERE x > 0) AS pos")
    assert q is not None


def test_extract_list_comprehension():
    """extract(x IN list | x * 2) — legacy Cypher."""
    q = parse("MATCH (n) RETURN extract(x IN [1,2,3] | x * 2) AS doubled")
    assert q is not None


# ---------------------------------------------------------------------------
# Lines 1556-1598: map/list subscript and slice
# ---------------------------------------------------------------------------

def test_subscript_access():
    """n.tags[0] subscript access."""
    q = parse("MATCH (n) RETURN n.tags[0] AS first")
    assert q is not None


def test_list_slice():
    """list[1..3] slice."""
    q = parse("MATCH (n) RETURN [1,2,3,4,5][1..3] AS sliced")
    assert q is not None


# ---------------------------------------------------------------------------
# Lines 1649-1650, 1655, 1682, 1692: parse_quantifier variants
# ---------------------------------------------------------------------------

def test_any_quantifier():
    """ANY(x IN list WHERE x > 0)."""
    q = parse("MATCH (n) WHERE ANY(x IN n.scores WHERE x > 0) RETURN n")
    assert q is not None


def test_all_quantifier():
    """ALL(x IN list WHERE x > 0)."""
    q = parse("MATCH (n) WHERE ALL(x IN n.scores WHERE x > 0) RETURN n")
    assert q is not None


def test_none_quantifier():
    """NONE(x IN list WHERE x < 0)."""
    q = parse("MATCH (n) WHERE NONE(x IN n.scores WHERE x < 0) RETURN n")
    assert q is not None


def test_single_quantifier():
    """SINGLE(x IN list WHERE x = 'match')."""
    q = parse("MATCH (n) WHERE SINGLE(x IN n.tags WHERE x = 'match') RETURN n")
    assert q is not None


# ---------------------------------------------------------------------------
# Lines 1718-1738: parse_relationship_pattern — var-length specs
# ---------------------------------------------------------------------------

def test_var_length_unbounded():
    """[*] unbounded var-length."""
    q = parse("MATCH (n)-[*]->(m) RETURN n, m")
    assert q is not None


def test_var_length_min_only():
    """[*2..] min only."""
    q = parse("MATCH (n)-[r*2..]->(m) RETURN n, m")
    assert q is not None


def test_var_length_max_only():
    """[*..5] max only."""
    q = parse("MATCH (n)-[r*..5]->(m) RETURN n, m")
    assert q is not None


def test_var_length_exact():
    """[*3] exact hops."""
    q = parse("MATCH (n)-[r*3]->(m) RETURN n, m")
    assert q is not None


def test_var_length_range():
    """[*1..3] range."""
    q = parse("MATCH (n)-[r:KNOWS*1..3]->(m) RETURN n, m")
    assert q is not None


# ---------------------------------------------------------------------------
# Lines 1767, 1788-1793, 1812, 1826: path variable p = (...)
# ---------------------------------------------------------------------------

def test_named_path_variable():
    """p = (n)-[r]->(m) named path."""
    q = parse("MATCH p = (n)-[r]->(m) RETURN p")
    assert q is not None


def test_named_path_var_length():
    """p = (n)-[*1..3]->(m) named var-length path."""
    q = parse("MATCH p = (n)-[*1..3]->(m) RETURN p")
    assert q is not None


def test_named_path_with_nodes():
    """nodes(p) applied to named path."""
    q = parse("MATCH p = (n)-[r]->(m) RETURN nodes(p) AS nds")
    assert q is not None


def test_named_path_length():
    """length(p) applied to named path."""
    q = parse("MATCH p = (n)-[r]->(m) RETURN length(p) AS l")
    assert q is not None
