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

        # Remap IVG's expanded node columns (var_id, var_labels, var_props) → var
        remapped_rows = [_remap_node_columns(row, self.columns, actual_columns) for row in actual_rows]

        # normalise actual values against expected types
        norm_actual = [
            _normalise_row_with_nodes(row, expected_rows[i] if i < len(expected_rows) else {}, self.columns)
            for i, row in enumerate(remapped_rows)
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
            # unordered: bipartite matching — each expected row must match a distinct
            # actual row.  Simple sort-then-compare fails when expected uses node
            # pattern strings ("(:A)") and actual uses node dicts (sort keys differ).
            # Greedy matching fails when a less-specific pattern consumes a node that
            # a more-specific pattern needed, so we use backtracking.
            def _can_match(exp_rows, avail_indices):
                if not exp_rows:
                    return True
                exp = exp_rows[0]
                for i in avail_indices:
                    if _rows_equal(exp, norm_actual[i], self.columns, self.list_unordered):
                        remaining = [j for j in avail_indices if j != i]
                        if _can_match(exp_rows[1:], remaining):
                            return True
                return False

            avail = list(range(len(norm_actual)))
            if not _can_match(expected_rows, avail):
                return (
                    f"Unordered comparison: no perfect matching found\n"
                    f"Expected: {expected_rows}\n"
                    f"Actual:   {norm_actual}"
                )
            return None


def _remap_node_columns(actual_row: dict, tck_columns: list[str], actual_columns: list[str]) -> dict:
    """Collapse IVG's var_id/var_labels/var_props triplets into a single 'var' key.

    IVG expands RETURN n into n_id, n_labels, n_props columns.
    TCK expects a single 'n' column with node pattern notation.
    This function detects the expansion and collapses it back to a NodeData dict.
    """
    result = dict(actual_row)
    for col in tck_columns:
        id_key = f"{col}_id"
        labels_key = f"{col}_labels"
        props_key = f"{col}_props"
        if id_key in actual_columns and labels_key in actual_columns and props_key in actual_columns:
            node_id = actual_row.get(id_key)
            node_labels = actual_row.get(labels_key)
            node_props = actual_row.get(props_key)
            # All-None triplet = null node (from OPTIONAL MATCH with no results)
            if node_id is None and node_labels is None and node_props is None:
                result[col] = None
            else:
                result[col] = {
                    "_id": node_id,
                    "_labels": node_labels,
                    "_props": node_props,
                }
    return result


def _parse_node_pattern(s: str) -> dict | None:
    """Parse TCK node pattern like (:A), (:B {name: 'x'}) into a dict.

    Returns None if s is not a node pattern.
    """
    s = s.strip()
    if not (s.startswith("(") and s.endswith(")")):
        return None
    inner = s[1:-1].strip()
    # inner may be empty, :Label, :Label {props}, var:Label, var {props}
    labels: list[str] = []
    props: dict = {}
    # extract variable name (no colon at start)
    if inner and not inner.startswith(":"):
        # variable name up to ':' or ' ' or '{'
        end = 0
        while end < len(inner) and inner[end] not in (":", " ", "{"):
            end += 1
        inner = inner[end:].lstrip()
    # extract labels (:A:B...)
    while inner.startswith(":"):
        inner = inner[1:]
        end = 0
        while end < len(inner) and inner[end] not in (":", " ", "{"):
            end += 1
        labels.append(inner[:end])
        inner = inner[end:].lstrip()
    # extract props {k: v}
    if inner.startswith("{") and inner.endswith("}"):
        props = _parse_tck_value(inner)
    return {"labels": labels, "props": props}


def _node_matches(node_data: dict, pattern: dict, isolation_label: str | None = None) -> bool:
    """Check if an IVG node data dict matches a TCK node pattern dict."""
    raw_labels = node_data.get("_labels", "[]")
    if isinstance(raw_labels, str):
        import json
        try:
            actual_labels_list = json.loads(raw_labels)
        except (json.JSONDecodeError, ValueError):
            actual_labels_list = []
    else:
        actual_labels_list = list(raw_labels) if raw_labels else []

    # strip isolation label(s) — any TCK_* label
    actual_labels = {lbl for lbl in actual_labels_list if not lbl.startswith("TCK_")}

    expected_labels = set(pattern["labels"])
    if not expected_labels.issubset(actual_labels):
        return False

    expected_props = pattern.get("props") or {}
    raw_props = node_data.get("_props", "[]")
    if isinstance(raw_props, str):
        import json
        try:
            props_list = json.loads(raw_props)
        except (json.JSONDecodeError, ValueError):
            props_list = []
    else:
        props_list = list(raw_props) if raw_props else []

    # props_list is a list of {key, value} dicts OR JSON-encoded strings of same
    actual_props: dict = {}
    if isinstance(props_list, list):
        for item in props_list:
            if isinstance(item, str):
                import json as _j
                try:
                    item = _j.loads(item)
                except (json.JSONDecodeError, ValueError):
                    pass
            if isinstance(item, dict) and "key" in item:
                actual_props[item["key"]] = item.get("value")
    elif isinstance(props_list, dict):
        actual_props = props_list

    for k, v in expected_props.items():
        if actual_props.get(k) != v:
            # try string/int normalisation
            av = actual_props.get(k)
            if isinstance(v, int) and isinstance(av, str):
                try:
                    if int(av) == v:
                        continue
                except ValueError:
                    pass
            if isinstance(v, str) and isinstance(av, (int, float)):
                if str(av) == v:
                    continue
            return False
    return True


def _normalise_row_with_nodes(actual: dict, expected: dict, columns: list[str]) -> dict:
    """Normalise actual row values, with special handling for node-format TCK cells."""
    result = {}
    for col in columns:
        aval = actual.get(col)
        eval_ = expected.get(col)
        if isinstance(eval_, str):
            pattern = _parse_node_pattern(eval_)
            if pattern is not None and isinstance(aval, dict) and "_labels" in aval:
                # Keep node data as-is; comparison is done in _rows_equal
                result[col] = aval
                continue
        result[col] = normalise_iris_value(aval, eval_)
    return result


def _normalise_row(actual: dict, expected: dict, columns: list[str]) -> dict:
    result = {}
    for col in columns:
        aval = actual.get(col)
        eval_ = expected.get(col)
        result[col] = normalise_iris_value(aval, eval_)
    return result


def normalise_iris_value(iris_val: Any, expected_tck_val: Any) -> Any:
    """Cast IRIS value to match expected TCK type."""
    # IRIS stores '' as NULL — coerce back when expected is empty string
    if iris_val is None and expected_tck_val == '':
        return ''
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
        # Node pattern comparison: TCK expects "(:A)" or "(:B {name: 'x'})"
        if isinstance(ev, str):
            pattern = _parse_node_pattern(ev)
            if pattern is not None:
                if isinstance(av, dict) and "_labels" in av:
                    if not _node_matches(av, pattern):
                        return False
                    continue
                # av is not a node dict — mismatch
                return False
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
