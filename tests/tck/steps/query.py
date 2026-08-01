"""behave step definitions: When … query execution steps."""
from behave import when, step

from tests.tck.steps.comparison import TCKValue
from tests.tck.steps.graph_setup import _inject_label


@when("executing query:")
def step_executing_query(context, query=None):
    if query is None:
        query = context.text
    query = query.strip()
    injected = _inject_match_scope(
        _inject_label(query, context.scenario_label),
        context.scenario_label,
    )
    params = getattr(context, "params", {}) or {}
    context.last_error = None
    try:
        context.last_result = context.engine.execute_cypher(injected, params)
    except Exception as exc:
        context.last_result = None
        context.last_error = exc
    context.params = {}


@when("executing control query:")
def step_control_query(context, query=None):
    if query is None:
        query = context.text
    query = query.strip()
    injected = _inject_match_scope(
        _inject_label(query, context.scenario_label),
        context.scenario_label,
    )
    params = getattr(context, "params", {}) or {}
    context.last_error = None
    try:
        context.last_result = context.engine.execute_cypher(injected, params)
    except Exception as exc:
        context.last_result = None
        context.last_error = exc
    context.params = {}


@step("parameters are:")
def step_parameters(context, table=None):
    if table is None:
        table = context.table
    params = {}
    for row in table.rows:
        values = list(row)
        for i, heading in enumerate(table.headings):
            params[heading] = TCKValue.parse(values[i]).python
    context.params = params


def _inject_match_scope(query: str, label: str) -> str:
    """
    In MATCH clauses, add label filter so reads see only this scenario's nodes.
    Skips anonymous nodes () and already-labelled nodes.
    """
    import re

    lines = query.split('\n')
    result_lines = []
    in_match = False

    for line in lines:
        stripped = line.strip().upper()
        first_word = stripped.split()[0] if stripped.split() else ''
        if first_word in ('MATCH', 'OPTIONAL'):
            in_match = True
        elif first_word in ('WHERE', 'WITH', 'RETURN', 'CREATE', 'MERGE',
                            'SET', 'DELETE', 'REMOVE', 'UNWIND', 'CALL',
                            'UNION', 'ORDER', 'SKIP', 'LIMIT'):
            in_match = False

        if in_match:
            line = _inject_match_label_in_line(line, label)
        result_lines.append(line)

    return '\n'.join(result_lines)


def _inject_match_label_in_line(line: str, label: str) -> str:
    """Add :<label> to named node patterns in MATCH lines."""
    import re

    def replacer(m):
        inner = m.group(1)
        # skip empty nodes ()
        if not inner.strip():
            return m.group(0)
        # skip already labelled
        if label in inner:
            return m.group(0)
        return f"({inner}:{label})"

    return re.sub(r'\(([^()]*)\)', replacer, line)
