"""Unit tests for TCKValue.parse() — must fail before comparison.py is implemented."""
import pytest
from tests.tck.steps.comparison import TCKValue


class TestTCKValueParse:
    def test_string_single_quoted(self):
        v = TCKValue.parse("'Alice'")
        assert v.python == "Alice"

    def test_string_empty(self):
        v = TCKValue.parse("''")
        assert v.python == ""

    def test_integer(self):
        v = TCKValue.parse("42")
        assert v.python == 42
        assert isinstance(v.python, int)

    def test_negative_integer(self):
        v = TCKValue.parse("-7")
        assert v.python == -7

    def test_float(self):
        v = TCKValue.parse("3.14")
        assert v.python == pytest.approx(3.14)
        assert isinstance(v.python, float)

    def test_null(self):
        v = TCKValue.parse("null")
        assert v.python is None

    def test_true(self):
        v = TCKValue.parse("true")
        assert v.python is True

    def test_false(self):
        v = TCKValue.parse("false")
        assert v.python is False

    def test_list_integers(self):
        v = TCKValue.parse("[1, 2, 3]")
        assert v.python == [1, 2, 3]

    def test_list_strings(self):
        v = TCKValue.parse("['a', 'b']")
        assert v.python == ["a", "b"]

    def test_list_empty(self):
        v = TCKValue.parse("[]")
        assert v.python == []

    def test_list_mixed(self):
        v = TCKValue.parse("[1, 'two', null]")
        assert v.python == [1, "two", None]

    def test_dict(self):
        v = TCKValue.parse("{name: 'Alice', age: 30}")
        assert v.python == {"name": "Alice", "age": 30}

    def test_dict_empty(self):
        v = TCKValue.parse("{}")
        assert v.python == {}

    def test_raw_preserved(self):
        raw = "'hello'"
        v = TCKValue.parse(raw)
        assert v.raw == raw
