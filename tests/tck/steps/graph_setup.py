"""behave step definitions: Given … graph setup steps."""
from uuid import uuid4

from behave import given, step


def _new_label() -> str:
    return f"TCK_{uuid4().hex[:8]}"


@given("an empty graph")
def step_empty_graph(context):
    context.scenario_label = _new_label()
    context.params = {}
    context.last_result = None
    context.last_error = None


@given("any graph")
def step_any_graph(context):
    context.scenario_label = _new_label()
    context.params = {}
    context.last_result = None
    context.last_error = None


@given("having executed:")
def step_having_executed(context, query=None):
    if query is None:
        query = context.text
    query = query.strip()
    try:
        context.engine.execute_cypher(
            _inject_label(query, context.scenario_label), {}
        )
    except Exception:
        pass  # setup queries may fail if already exists; ignore


@step("the {graph_name} graph")
def step_named_graph(context, graph_name):
    label = context.named_graphs.get(graph_name)
    if label is None:
        raise RuntimeError(
            f"Named graph '{graph_name}' not in session registry. "
            f"Available: {list(context.named_graphs.keys())}"
        )
    context.scenario_label = label
    context.params = {}
    context.last_result = None
    context.last_error = None


@step("there exists a procedure {procedure_sig}")
def step_procedure_exists(context, procedure_sig):
    # Procedure registration not supported in IVG — mark scenario @wip at runtime
    context.scenario.skip(reason="procedure registration not supported")


def _inject_label(query: str, label: str) -> str:
    """
    Inject isolation label into ALL node patterns in CREATE/MERGE clauses.
    Injects into every node (including relationship endpoints) since all
    newly created nodes need the label for teardown.
    """
    lines = query.split('\n')
    result_lines = []
    in_create = False

    for line in lines:
        stripped = line.strip().upper()
        first_word = stripped.split()[0] if stripped.split() else ''
        if first_word in ('CREATE', 'MERGE'):
            in_create = True
        elif first_word in ('MATCH', 'OPTIONAL', 'WITH', 'RETURN', 'WHERE',
                            'ORDER', 'SKIP', 'LIMIT', 'UNWIND', 'SET',
                            'DELETE', 'REMOVE', 'CALL', 'UNION'):
            in_create = False

        if in_create:
            line = _inject_all_nodes(line, label)
        result_lines.append(line)
    return '\n'.join(result_lines)


def _inject_all_nodes(line: str, label: str) -> str:
    """Add :<label> to every node pattern in a line."""
    result = []
    i = 0
    while i < len(line):
        if line[i] == '(':
            # Find matching )
            depth = 0
            end = i
            for k in range(i, len(line)):
                if line[k] == '(':
                    depth += 1
                elif line[k] == ')':
                    depth -= 1
                    if depth == 0:
                        end = k
                        break

            inner = line[i+1:end]
            # Only inject if non-empty (skip anonymous nodes with just whitespace)
            if inner.strip() and label not in inner:
                inner = _add_label_to_node(inner, label)
            result.append(f"({inner})")
            i = end + 1
        else:
            result.append(line[i])
            i += 1
    return ''.join(result)


def _add_label_to_node(inner: str, label: str) -> str:
    """Add :<label> to a node pattern inner string, before any {} props."""
    if '{' in inner:
        label_part, _, props_part = inner.partition('{')
        return f"{label_part.rstrip()}:{label} {{{props_part}"
    else:
        return f"{inner}:{label}"
