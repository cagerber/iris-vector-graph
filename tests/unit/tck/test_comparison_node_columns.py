"""Unit tests for node-column remapping and node-pattern comparison."""
import pytest
from tests.tck.steps.comparison import (
    TCKValue,
    TCKResultTable,
    _parse_node_pattern,
    _node_matches,
    _remap_node_columns,
)


class TestParseNodePattern:
    def test_label_only(self):
        p = _parse_node_pattern("(:A)")
        assert p == {"labels": ["A"], "props": {}}

    def test_multiple_labels(self):
        p = _parse_node_pattern("(:A:B)")
        assert p["labels"] == ["A", "B"]

    def test_with_props(self):
        p = _parse_node_pattern("(:B {name: 'x'})")
        assert p["labels"] == ["B"]
        assert p["props"] == {"name": "x"}

    def test_empty_node(self):
        p = _parse_node_pattern("()")
        assert p == {"labels": [], "props": {}}

    def test_not_a_node(self):
        assert _parse_node_pattern("'Alice'") is None
        assert _parse_node_pattern("42") is None
        assert _parse_node_pattern("null") is None

    def test_with_variable(self):
        p = _parse_node_pattern("(n:A)")
        assert p["labels"] == ["A"]


class TestNodeMatches:
    def _node(self, labels, props=None):
        import json
        props_list = [{"key": k, "value": v} for k, v in (props or {}).items()]
        return {
            "_id": "test-id",
            "_labels": json.dumps(labels),
            "_props": json.dumps(props_list),
        }

    def test_label_match(self):
        node = self._node(["A", "TCK_abc123"])
        assert _node_matches(node, {"labels": ["A"], "props": {}})

    def test_label_mismatch(self):
        node = self._node(["B"])
        assert not _node_matches(node, {"labels": ["A"], "props": {}})

    def test_prop_match(self):
        node = self._node(["B"], {"name": "x"})
        assert _node_matches(node, {"labels": ["B"], "props": {"name": "x"}})

    def test_prop_mismatch(self):
        node = self._node(["B"], {"name": "y"})
        assert not _node_matches(node, {"labels": ["B"], "props": {"name": "x"}})

    def test_isolation_label_stripped(self):
        node = self._node(["A", "TCK_deadbeef"])
        assert _node_matches(node, {"labels": ["A"], "props": {}})


class TestRemapNodeColumns:
    def test_remaps_triplet(self):
        tck_cols = ["a", "b"]
        actual_cols = ["a_id", "a_labels", "a_props", "b_id", "b_labels", "b_props"]
        row = {
            "a_id": "id1", "a_labels": '["A"]', "a_props": "[]",
            "b_id": "id2", "b_labels": '["B"]', "b_props": "[]",
        }
        result = _remap_node_columns(row, tck_cols, actual_cols)
        assert "_labels" in result["a"]
        assert "_labels" in result["b"]

    def test_no_remap_scalar(self):
        tck_cols = ["name"]
        actual_cols = ["name"]
        row = {"name": "Alice"}
        result = _remap_node_columns(row, tck_cols, actual_cols)
        assert result["name"] == "Alice"


class TestNodeCompareIntegration:
    def _node_row(self, var, labels, props=None):
        import json
        props_list = [{"key": k, "value": v} for k, v in (props or {}).items()]
        return {
            f"{var}_id": "test-id",
            f"{var}_labels": json.dumps(labels),
            f"{var}_props": json.dumps(props_list),
        }

    def test_create2_style_match(self):
        """RETURN a, b where a=(:A) b=(:B)"""
        table = TCKResultTable(
            columns=["a", "b"],
            rows=[[TCKValue.parse("(:A)"), TCKValue.parse("(:B)")]],
            ordered=False,
            list_unordered=False,
        )
        actual_cols = ["a_id", "a_labels", "a_props", "b_id", "b_labels", "b_props"]
        row = {**self._node_row("a", ["A", "TCK_abc"]), **self._node_row("b", ["B", "TCK_abc"])}
        actual_rows = [{col: row[col] for col in actual_cols}]
        diff = table.compare(actual_rows, actual_cols)
        assert diff is None, f"Expected match, got: {diff}"

    def test_node_label_mismatch(self):
        table = TCKResultTable(
            columns=["a"],
            rows=[[TCKValue.parse("(:A)")]],
            ordered=False,
            list_unordered=False,
        )
        actual_cols = ["a_id", "a_labels", "a_props"]
        row = self._node_row("a", ["C"])
        actual_rows = [{col: row[col] for col in actual_cols}]
        diff = table.compare(actual_rows, actual_cols)
        assert diff is not None
