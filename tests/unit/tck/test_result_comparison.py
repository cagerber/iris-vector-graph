"""Unit tests for TCKResultTable.compare() — must fail before implementation."""
import pytest
from tests.tck.steps.comparison import TCKValue, TCKResultTable


def _row(*cells):
    return [TCKValue.parse(c) for c in cells]


class TestTCKResultTableCompare:
    def test_ordered_match(self):
        table = TCKResultTable(
            columns=["name"],
            rows=[_row("'Alice'"), _row("'Bob'")],
            ordered=True,
            list_unordered=False,
        )
        actual_rows = [{"name": "Alice"}, {"name": "Bob"}]
        assert table.compare(actual_rows, ["name"]) is None

    def test_ordered_mismatch_order(self):
        table = TCKResultTable(
            columns=["name"],
            rows=[_row("'Alice'"), _row("'Bob'")],
            ordered=True,
            list_unordered=False,
        )
        actual_rows = [{"name": "Bob"}, {"name": "Alice"}]
        diff = table.compare(actual_rows, ["name"])
        assert diff is not None

    def test_unordered_match_different_order(self):
        table = TCKResultTable(
            columns=["name"],
            rows=[_row("'Alice'"), _row("'Bob'")],
            ordered=False,
            list_unordered=False,
        )
        actual_rows = [{"name": "Bob"}, {"name": "Alice"}]
        assert table.compare(actual_rows, ["name"]) is None

    def test_null_equality(self):
        table = TCKResultTable(
            columns=["x"],
            rows=[_row("null")],
            ordered=False,
            list_unordered=False,
        )
        actual_rows = [{"x": None}]
        assert table.compare(actual_rows, ["x"]) is None

    def test_integer_match(self):
        table = TCKResultTable(
            columns=["n"],
            rows=[_row("42")],
            ordered=False,
            list_unordered=False,
        )
        actual_rows = [{"n": 42}]
        assert table.compare(actual_rows, ["n"]) is None

    def test_row_count_mismatch(self):
        table = TCKResultTable(
            columns=["name"],
            rows=[_row("'Alice'")],
            ordered=False,
            list_unordered=False,
        )
        actual_rows = [{"name": "Alice"}, {"name": "Bob"}]
        diff = table.compare(actual_rows, ["name"])
        assert diff is not None

    def test_empty_result(self):
        table = TCKResultTable(
            columns=["name"],
            rows=[],
            ordered=False,
            list_unordered=False,
        )
        actual_rows = []
        assert table.compare(actual_rows, ["name"]) is None

    def test_list_unordered_variant(self):
        table = TCKResultTable(
            columns=["items"],
            rows=[_row("[1, 2, 3]")],
            ordered=False,
            list_unordered=True,
        )
        actual_rows = [{"items": [3, 1, 2]}]
        assert table.compare(actual_rows, ["items"]) is None

    def test_list_unordered_mismatch(self):
        table = TCKResultTable(
            columns=["items"],
            rows=[_row("[1, 2, 3]")],
            ordered=False,
            list_unordered=True,
        )
        actual_rows = [{"items": [1, 2, 4]}]
        diff = table.compare(actual_rows, ["items"])
        assert diff is not None
