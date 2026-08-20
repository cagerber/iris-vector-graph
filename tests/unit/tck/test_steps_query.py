"""Unit tests for query step definitions."""
from unittest.mock import MagicMock
import pytest


class TestQuerySteps:
    def test_executing_query_stores_result(self):
        """When executing query: result stored in context.last_result."""
        from tests.tck.steps.query import step_executing_query

        ctx = MagicMock()
        ctx.scenario_label = "TCK_abc123"
        ctx.params = {}
        mock_result = MagicMock()
        mock_result.rows = [{"n": 1}]
        ctx.engine.execute_cypher = MagicMock(return_value=mock_result)

        step_executing_query(ctx, "MATCH (n) RETURN n")
        assert ctx.last_result == mock_result

    def test_executing_query_uses_params(self):
        """When executing query: params from context.params are passed to engine."""
        from tests.tck.steps.query import step_executing_query

        ctx = MagicMock()
        ctx.scenario_label = "TCK_abc123"
        ctx.params = {"name": "Alice"}
        ctx.engine.execute_cypher = MagicMock(return_value=MagicMock(rows=[]))

        step_executing_query(ctx, "MATCH (n {name: $name}) RETURN n")
        call_args = ctx.engine.execute_cypher.call_args
        # params dict passed as second positional or keyword arg
        assert "Alice" in str(call_args)

    def test_control_query_stores_result(self):
        """When executing control query: result stored so subsequent Then steps can check it."""
        from tests.tck.steps.query import step_control_query

        ctx = MagicMock()
        ctx.scenario_label = "TCK_abc123"
        ctx.params = {}
        mock_result = MagicMock()
        mock_result.rows = [{"x": 1}]
        ctx.engine.execute_cypher = MagicMock(return_value=mock_result)

        step_control_query(ctx, "MATCH (n) RETURN n")
        assert ctx.last_result == mock_result

    def test_parameters_step_stores_params(self):
        """And parameters are: stores dict in context.params.

        TCK table format: 2-column | name | value |.
        behave treats first row as headings, so headings = [name0, val0],
        subsequent rows = additional pairs.
        """
        from tests.tck.steps.query import step_parameters

        ctx = MagicMock()
        ctx.params = {}
        # TCK table: | name | 'Alice' |  (first pair is the heading row)
        table = MagicMock()
        table.headings = ["name", "'Alice'"]
        table.rows = []

        step_parameters(ctx, table)
        assert ctx.params.get("name") == "Alice"
