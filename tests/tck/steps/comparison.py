"""TCK result comparison: value parsing and table diff."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any


@dataclass
class TCKValue:
    raw: str
    python: Any

    @staticmethod
    def parse(cell: str) -> "TCKValue":
        cell = cell.strip()
        python = _parse_tck_value(cell)
        return TCKValue(raw=cell, python=python)


def _parse_tck_value(s: str) -> Any:
    s = s.strip()
    if s == "null":
        return None
    if s == "true":
        return True
    if s == "false":
        return False
    if s.startswith("'") and s.endswith("'"):
        return s[1:-1]
    if s.startswith("[") and s.endswith("]"):
        inner = s[1:-1].strip()
        if not inner:
            return []
        return [_parse_tck_value(item) for item in _split_tck_list(inner)]
    if s.startswith("{") and s.endswith("}"):
        inner = s[1:-1].strip()
        if not inner:
            return {}
        result = {}
        for pair in _split_tck_list(inner):
            k, _, v = pair.partition(":")
            result[k.strip()] = _parse_tck_value(v.strip())
        return result
    # numeric
    try:
        if "." in s:
            return float(s)
        return int(s)
    except ValueError:
        return s


def _split_tck_list(s: str) -> list[str]:
    """Split a comma-separated TCK list respecting nested brackets and quotes."""
    items = []
    depth = 0
    in_quote = False
    buf = []
    for ch in s:
        if ch == "'" and not in_quote:
            in_quote = True
            buf.append(ch)
        elif ch == "'" and in_quote:
            in_quote = False
            buf.append(ch)
        elif in_quote:
            buf.append(ch)
        elif ch in ("[", "{"):
            depth += 1
            buf.append(ch)
        elif ch in ("]", "}"):
            depth -= 1
            buf.append(ch)
        elif ch == "," and depth == 0:
            items.append("".join(buf).strip())
            buf = []
        else:
            buf.append(ch)
    if buf:
        items.append("".join(buf).strip())
    return items


@dataclass
class TCKResultTable:
    columns: list[str]
    rows: list[list[TCKValue]]
    ordered: bool
    list_unordered: bool

    def compare(self, actual_rows: list[dict], actual_columns: list[str]) -> str | None:
        """Return diff string on mismatch, None on match."""
        expected_rows = [
            {col: cell.python for col, cell in zip(self.columns, row)}
            for row in self.rows
        ]

        # normalise actual values against expected types
        norm_actual = [
            _normalise_row(row, expected_rows[i] if i < len(expected_rows) else {}, self.columns)
            for i, row in enumerate(actual_rows)
        ]

        if len(norm_actual) != len(expected_rows):
            return (
                f"Row count mismatch: expected {len(expected_rows)}, got {len(norm_actual)}\n"
                f"Expected: {expected_rows}\n"
                f"Actual:   {norm_actual}"
            )

        if self.ordered:
            for i, (exp, act) in enumerate(zip(expected_rows, norm_actual)):
                if not _rows_equal(exp, act, self.columns, self.list_unordered):
                    return f"Row {i} mismatch:\n  expected: {exp}\n  actual:   {act}"
            return None
        else:
            # unordered: sort both sets
            exp_sorted = _sort_rows(expected_rows, self.columns)
            act_sorted = _sort_rows(norm_actual, self.columns)
            for i, (exp, act) in enumerate(zip(exp_sorted, act_sorted)):
                if not _rows_equal(exp, act, self.columns, self.list_unordered):
                    return (
                        f"Unordered comparison failed at position {i} after sorting:\n"
                        f"  expected: {exp}\n"
                        f"  actual:   {act}\n"
                        f"Full expected (sorted): {exp_sorted}\n"
                        f"Full actual (sorted):   {act_sorted}"
                    )
            return None


def _normalise_row(actual: dict, expected: dict, columns: list[str]) -> dict:
    result = {}
    for col in columns:
        aval = actual.get(col)
        eval_ = expected.get(col)
        result[col] = normalise_iris_value(aval, eval_)
    return result


def normalise_iris_value(iris_val: Any, expected_tck_val: Any) -> Any:
    """Cast IRIS value to match expected TCK type."""
    if iris_val is None:
        return None
    if isinstance(iris_val, Decimal):
        if isinstance(expected_tck_val, int) and not isinstance(expected_tck_val, bool):
            return int(iris_val)
        return float(iris_val)
    if isinstance(expected_tck_val, bool):
        if isinstance(iris_val, str):
            return iris_val.lower() == "true"
        return bool(iris_val)
    if isinstance(expected_tck_val, int) and not isinstance(expected_tck_val, bool):
        if isinstance(iris_val, str):
            try:
                return int(iris_val)
            except ValueError:
                pass
        if isinstance(iris_val, float):
            return int(iris_val)
    if isinstance(expected_tck_val, float):
        if isinstance(iris_val, str):
            try:
                return float(iris_val)
            except ValueError:
                pass
    if isinstance(expected_tck_val, list) and isinstance(iris_val, (list, tuple)):
        return list(iris_val)
    return iris_val


def _rows_equal(exp: dict, act: dict, columns: list[str], list_unordered: bool) -> bool:
    for col in columns:
        ev = exp.get(col)
        av = act.get(col)
        if list_unordered and isinstance(ev, list) and isinstance(av, list):
            if sorted(str(x) for x in ev) != sorted(str(x) for x in av):
                return False
        else:
            if ev != av:
                return False
    return True


def _sort_key(row: dict, columns: list[str]) -> tuple:
    return tuple(str(row.get(c, "")) for c in columns)


def _sort_rows(rows: list[dict], columns: list[str]) -> list[dict]:
    return sorted(rows, key=lambda r: _sort_key(r, columns))
