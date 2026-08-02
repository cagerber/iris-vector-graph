"""
Cypher-to-SQL Translation Artifacts

Classes for managing SQL generation from Cypher AST.
Supports multi-stage queries via Common Table Expressions (CTEs).
"""

from dataclasses import dataclass, field
from typing import List, Any, Dict, Optional, Union
import logging
import json
from pydantic import BaseModel, Field
from . import ast
from .parser import CypherParseError
from iris_vector_graph.security import (
    validate_table_name,
    VALID_GRAPH_TABLES,
    sanitize_identifier,
)

logger = logging.getLogger(__name__)

# Module-level schema prefix configuration
# Set to "Graph_KG" to use Graph_KG.nodes, Graph_KG.rdf_labels, etc.
# Set to "" (empty string) for unqualified table names
_schema_prefix: str = ""

# Procedure CTE aliases (VecSearch, BM25, DegCent, etc.) whose columns must be
# referenced UNQUALIFIED in SELECT/ORDER BY. IRIS does not register a
# JSON_TABLE-backed CTE name as a referenceable label, so `DegCent.score`
# raises SQLCODE -23; bare `score` resolves correctly.
_PROC_CTE_ALIASES = frozenset({
    "VecSearch", "BM25", "PPR", "IVF_SEARCH", "Retrieve", "Neighbors", "WS",
    "DegCent", "Betweenness", "Closeness", "Eigenvector",
    "Leiden", "TriangleCount", "SCC", "KCore",
})

# Allowed map-parameter keys for centrality procedures (Spec 162 FR-029).
# The procedure-call validator rejects unknown keys to prevent silent typos
# and reserves keys (e.g. `weighted`) for future Phase 2 extensions.
CENTRALITY_ALLOWED_KEYS: Dict[str, set] = {
    "ivg.degreeCentrality": {"direction", "predicate", "topK"},
    "ivg.betweenness":      {"sampleSize", "direction", "maxHops", "topK", "memBudgetMB"},
    "ivg.closeness":        {"formula", "direction", "maxHops", "topK"},
    "ivg.eigenvector":      {"maxIter", "tol", "topK"},
}

# Allowed map-parameter keys for community-detection procedures (Spec 163 FR-015).
# Same forward-compat semantics as CENTRALITY_ALLOWED_KEYS — `weighted` is reserved
# for Phase 2 weighted Leiden / weighted Triangle / etc.
COMMUNITY_ALLOWED_KEYS: Dict[str, set] = {
    "ivg.leiden":         {"maxLevels", "gamma", "tol", "topK", "memBudgetMB", "randomSeed"},
    "ivg.triangleCount":  {"topK"},
    "ivg.scc":            {"topK"},
    "ivg.kcore":          {"topK"},
}


def _validate_centrality_proc_map(proc_name: str, map_keys) -> None:
    """Reject unknown map-parameter keys for centrality procedures.

    Raises ValueError with a clear message listing both the unknown key(s)
    and the allowed set. Used by `_translate_degree_centrality`,
    `_translate_betweenness`, `_translate_closeness`, `_translate_eigenvector`.
    """
    allowed = CENTRALITY_ALLOWED_KEYS.get(proc_name, set())
    unknown = set(map_keys) - allowed
    if unknown:
        raise ValueError(
            f"Unknown parameters for {proc_name}: {sorted(unknown)}. "
            f"Allowed: {sorted(allowed)}"
        )


def _validate_community_proc_map(proc_name: str, map_keys) -> None:
    """Reject unknown map-parameter keys for community-detection procedures (Spec 163 FR-015).

    Same forward-compat semantics as `_validate_centrality_proc_map` — `weighted`
    is reserved for Phase 2. Used by `_translate_leiden`, `_translate_triangle_count`,
    `_translate_scc`, `_translate_kcore`.
    """
    allowed = COMMUNITY_ALLOWED_KEYS.get(proc_name, set())
    unknown = set(map_keys) - allowed
    if unknown:
        raise ValueError(
            f"Unknown parameters for {proc_name}: {sorted(unknown)}. "
            f"Allowed: {sorted(allowed)}"
        )


def set_schema_prefix(prefix: str) -> None:
    """Set the schema prefix for all table references in generated SQL.

    Args:
        prefix: Schema name (e.g., "Graph_KG") or empty string for unqualified names
    """
    global _schema_prefix
    _schema_prefix = prefix


def get_schema_prefix() -> str:
    """Get the current schema prefix."""
    return _schema_prefix


def _table(name: str, prefix: Optional[str] = None) -> str:
    """Return fully qualified table name with schema prefix if configured.

    Security: Validates name against VALID_GRAPH_TABLES allowlist to prevent
    SQL injection via table name manipulation.

    Args:
        name: Table name (must be in VALID_GRAPH_TABLES)
        prefix: Override the module-level prefix. Pass engine._schema_prefix
                to get per-instance isolation instead of the process global.

    Returns:
        Schema-qualified table name (e.g., "Graph_KG.nodes")

    Raises:
        ValueError: If name is not in the allowlist
    """
    validate_table_name(name)
    p = prefix if prefix is not None else _schema_prefix
    if p:
        return f"{p}.{name}"
    return name


def labels_subquery(node_expr: str) -> str:
    return f"COALESCE((SELECT JSON_ARRAYAGG(label) FROM {_table('rdf_labels')} WHERE s = {node_expr}), CAST('[]' AS VARCHAR(256)))"


def properties_subquery(node_expr: str) -> str:
    # Stable string-based JSON aggregation.
    # We avoid native JSON_OBJECT in subqueries as it triggers an IRIS optimizer bug
    # (looking for %QPAR in the local schema) in some versions (e.g. 2025.1).
    # We use minimal REPLACE calls for performance while ensuring valid JSON escaping.
    return (
        "(SELECT JSON_ARRAYAGG("
        "'{\"key\":\"' || REPLACE(REPLACE(\"key\", '\\', '\\\\'), '\"', '\\\"') || "
        "'\",\"value\":\"' || REPLACE(REPLACE(val, '\\', '\\\\'), '\"', '\\\"') || '\"}') "
        f"FROM {_table('rdf_props')} WHERE s = {node_expr})"
    )


class QueryMetadata(BaseModel):
    estimated_rows: Optional[int] = None
    index_usage: List[str] = Field(default_factory=list)
    optimization_applied: List[str] = Field(default_factory=list)
    complexity_score: Optional[float] = None
    warnings: List[str] = Field(default_factory=list)

    model_config = {"arbitrary_types_allowed": True}


@dataclass
class TemporalBound:
    ts_start: Any
    ts_end: Any
    rel_variable: str
    predicate: Optional[str]
    direction: str


class TemporalQueryRequiresEngine(ValueError):
    pass


class SQLQuery(BaseModel):
    sql: Union[str, List[str]]
    parameters: List[List[Any]] = Field(default_factory=list)
    query_metadata: QueryMetadata = Field(default_factory=QueryMetadata)
    is_transactional: bool = False
    var_length_paths: Optional[List[dict]] = None
    # Mapping from SQL-safe column alias → desired Cypher column name, for renaming after execution.
    column_name_map: Dict[str, str] = Field(default_factory=dict)
    # Parallel list to the result columns: each entry is "scalar", "node", or "relationship".
    # Consumed by the Bolt server to emit correct PackStream struct tags (TAG_NODE / TAG_RELATIONSHIP).
    bolt_column_types: List[str] = Field(default_factory=list)

    model_config = {"arbitrary_types_allowed": True}


class TranslationContext:
    """Stateful context for SQL generation across multiple query stages."""

    def __init__(self, parent: Optional["TranslationContext"] = None):
        self.variable_aliases: Dict[str, str] = {}
        if parent is not None:
            self.variable_aliases = parent.variable_aliases.copy()

        # Variables that are scalar (not graph nodes) — skip node expansion in RETURN
        self.scalar_variables: set = (
            set() if parent is None else parent.scalar_variables.copy()
        )

        self.graph_context: Optional[str] = (
            None if parent is None else parent.graph_context
        )

        # Named path registry: path variable → AST NamedPath + SQL aliases
        self.named_paths: Dict[str, ast.NamedPath] = (
            {} if parent is None else parent.named_paths.copy()
        )
        self.path_node_aliases: Dict[str, List[str]] = (
            {} if parent is None else parent.path_node_aliases.copy()
        )
        self.path_edge_aliases: Dict[str, List[str]] = (
            {} if parent is None else parent.path_edge_aliases.copy()
        )
        # Maps (pattern_id, relationship_index) → SQL alias for capturing anon rels in named paths
        self.pattern_rel_aliases: Dict[tuple, str] = (
            {} if parent is None else parent.pattern_rel_aliases.copy()
        )
        # Maps relationship object id() → SQL alias for anonymous relationship lookup
        self.rel_obj_aliases: Dict[int, str] = (
            {} if parent is None else parent.rel_obj_aliases.copy()
        )
        # Maps anonymous node object id() → SQL alias (for chained anonymous node reuse)
        self.node_obj_aliases: Dict[int, str] = (
            {} if parent is None else parent.node_obj_aliases.copy()
        )
        self.var_length_paths: List[dict] = (
            [] if parent is None else parent.var_length_paths
        )

        self.select_items: List[str] = []
        self.from_clauses: List[str] = []
        self.join_clauses: List[str] = []
        self.where_conditions: List[str] = []
        self.having_conditions: List[str] = []
        self.group_by_items: List[str] = []
        self._undirected_aliases: set = set()
        self._edgescan_aliases: set = set()

        self.select_params: List[Any] = []
        self.join_params: List[Any] = []
        self.where_params: List[Any] = []

        self.dml_statements: List[tuple[str, List[Any]]] = []

        self.all_stage_params: List[Any] = (
            [] if parent is None else parent.all_stage_params
        )
        self._alias_counter: int = 0 if parent is None else parent._alias_counter
        self.stages: List[str] = [] if parent is None else parent.stages
        self.input_params: Dict[str, Any] = (
            {} if parent is None else parent.input_params
        )
        self.temporal_rel_ctes: Dict[str, str] = (
            {} if parent is None else parent.temporal_rel_ctes.copy()
        )
        self.temporal_derived: Dict[str, str] = (
            {} if parent is None else parent.temporal_derived.copy()
        )
        # Variables bound to relationship patterns in MATCH clauses.
        # Used by translate_to_sql() to tag Bolt column types as "relationship".
        self.rel_variables: set = (
            set() if parent is None else parent.rel_variables.copy()
        )
        self.system_procedure_call: Optional[Any] = None
        self.pending_where = None
        self.mapped_node_aliases: Dict[str, dict] = (
            {} if parent is None else parent.mapped_node_aliases.copy()
        )
        # Maps SQL-safe column alias → Cypher expression text for post-execution column renaming.
        self.column_name_map: Dict[str, str] = (
            {} if parent is None else parent.column_name_map
        )
        # OPTIONAL MATCH null-row fallback: when set, the generated SQL gains a
        # UNION ALL branch that emits one null row when the label has no nodes.
        # List of (label_value, param_placeholder) tuples — one per optional label constraint.
        self.optional_null_row_labels: List[tuple] = []
        # Parallel list of SQL values for the null row, one per select item.
        self.optional_null_row_items: List[str] = []
        # OPTIONAL MATCH intermediate-node null-gating: maps node_alias → edge_alias.
        # When the gating edge is null (second-hop path failed), the intermediate node
        # should appear as null in SELECT even though it was joined via the first hop.
        # Set by _trp_directed_edge when multi-hop optional with bound end node detected.
        self.opt_intermediate_nulled: Dict[str, str] = {}
        # Variable type tracking for semantic validation.
        # Maps variable name → "node" | "relationship" | "scalar"
        # Used to enforce type consistency and detect VariableTypeConflict/VariableAlreadyBound errors.
        self.variable_types: Dict[str, str] = (
            {} if parent is None else parent.variable_types.copy()
        )

    def next_alias(self, prefix: str = "t") -> str:
        alias = f"{prefix}{self._alias_counter}"
        self._alias_counter += 1
        return alias

    def register_variable(self, variable: str, prefix: str = "n") -> str:
        if variable not in self.variable_aliases:
            self.variable_aliases[variable] = self.next_alias(prefix)
        return self.variable_aliases[variable]

    def bind_variable_type(self, variable: str, var_type: str) -> None:
        """Track variable type and validate no type conflicts.

        Args:
            variable: Cypher variable name
            var_type: One of "node", "relationship", or "scalar"

        Raises:
            CypherParseError: If variable is rebound to a different type
        """
        if not variable:
            return
        if variable in self.variable_types:
            existing_type = self.variable_types[variable]
            if existing_type != var_type:
                raise CypherParseError(
                    f"VariableTypeConflict: variable '{variable}' is bound as "
                    f"{existing_type!r}, cannot rebind as {var_type!r}"
                )
        else:
            self.variable_types[variable] = var_type

    def add_select_param(self, value: Any) -> str:
        self.select_params.append(value)
        return "?"

    def add_join_param(self, value: Any) -> str:
        self.join_params.append(value)
        return "?"

    def add_where_param(self, value: Any) -> str:
        self.where_params.append(value)
        return "?"

    @staticmethod
    def _predicate_cost(cond: str) -> int:
        """Heuristic cost for SQL WHERE condition ordering (Morrison OPT-4).
        EXISTS structural guards cheapest; string scans most expensive."""
        if "EXISTS" in cond:
            return 0
        if " = " in cond or " IS " in cond:
            return 1
        if " > " in cond or " < " in cond or " >= " in cond or " <= " in cond:
            return 2
        if "LIKE" in cond or "%CONTAINS" in cond or "LOWER(" in cond:
            return 3
        return 4

    @staticmethod
    def _structural_guard_sql(node_alias: str, prop_name: str) -> str:
        """EXISTS guard confirming a property key exists before JOIN fetches value (OPT-3)."""
        props_tbl = _table("rdf_props")
        safe_key = prop_name.replace("'", "''")
        return (
            f"EXISTS (SELECT 1 FROM {props_tbl} _sg{node_alias} "
            f"WHERE _sg{node_alias}.s = {node_alias}.node_id "
            f"AND _sg{node_alias}.\"key\" = '{safe_key}')"
        )

    def build_stage_sql(
        self, distinct: bool = False, select_override: Optional[str] = None
    ) -> tuple[str, List[Any]]:
        select = (
            select_override
            if select_override
            else f"SELECT {'DISTINCT ' if distinct else ''}{', '.join(self.select_items)}"
        )
        parts = [select]
        if self.from_clauses:
            expanded = []
            for fc in self.from_clauses:
                if fc in self.temporal_derived:
                    expanded.append(f"({self.temporal_derived[fc]}) {fc}")
                else:
                    expanded.append(fc)
            parts.append(f"FROM {', '.join(expanded)}")
        expanded_joins = []
        for jc in self.join_clauses:
            for tname, tsql in self.temporal_derived.items():
                if f"JOIN {tname} " in jc or f"JOIN {tname}\n" in jc:
                    jc = jc.replace(f"JOIN {tname} ", f"JOIN ({tsql}) {tname} ")
                    jc = jc.replace(f"JOIN {tname}\n", f"JOIN ({tsql}) {tname}\n")
            expanded_joins.append(jc)
        if expanded_joins:
            parts.extend(expanded_joins)
        if self.where_conditions:
            # Pair each condition with its param slice so sorting keeps params aligned.
            wp = list(self.where_params)
            offset = 0
            paired = []
            for cond in self.where_conditions:
                n = cond.count("?")
                paired.append((cond, wp[offset : offset + n]))
                offset += n
            paired.sort(key=lambda x: self._predicate_cost(x[0]))
            ordered_conds = [p[0] for p in paired]
            ordered_where_params = [v for p in paired for v in p[1]]
            parts.append(f"WHERE {' AND '.join(ordered_conds)}")
        else:
            ordered_where_params = list(self.where_params)
        if self.group_by_items:
            parts.append(f"GROUP BY {', '.join(self.group_by_items)}")
        if self.having_conditions:
            parts.append(f"HAVING {' AND '.join(self.having_conditions)}")
        sql = "\n".join(parts)
        params = (
            (self.select_params if not select_override else [])
            + self.join_params
            + ordered_where_params
        )
        return sql, params

    def add_dml(self, sql: str, params: List[Any]):
        self.dml_statements.append((sql, params))

    def build_dml_subquery(self, select_override: str) -> tuple[str, str, List[Any]]:
        """Build a SELECT subquery for use in DML, returning (cte_prefix, select_sql, params).

        When variable_aliases reference StageN CTEs (set after a WITH clause),
        cte_prefix is a 'WITH ...' string that must precede the DML verb.
        Callers assemble as: f"{cte_prefix}{dml_verb} {target} {select_sql}"
        For DELETE WHERE IN, use: f"{cte_prefix}DELETE FROM t WHERE c IN ({select_sql})"
        When no stages exist, cte_prefix is empty string.
        """
        sql, params = self.build_stage_sql(select_override=select_override)
        all_ctes = [
            c
            for c in getattr(self, "cte_clauses", [])
            if not any(td in c for td in self.temporal_derived)
        ] + self.stages
        if all_ctes:
            cte_prefix = "WITH " + ",\n".join(all_ctes) + "\n"
            params = list(self.all_stage_params) + list(params)
        else:
            cte_prefix = ""
        return cte_prefix, sql, params


def translate_procedure_call(
    proc: ast.CypherProcedureCall, context: TranslationContext
) -> None:
    """Translate a CALL procedure into a CTE prepended to context.stages.

    Supported:
    - ivg.*       — IVG-specific procedures (vector search, BFS, BM25, etc.)
    - db.*        — Neo4j built-in procedures (forwarded to engine)
    - dbms.*      — Neo4j system procedures (forwarded to engine)
    - apoc.*      — APOC procedures (forwarded to engine)
    """
    _SYSTEM_PROC_PREFIXES = ("db.", "dbms.", "apoc.", "gds.")
    name = proc.procedure_name
    if any(name.lower().startswith(p) for p in _SYSTEM_PROC_PREFIXES):
        context.system_procedure_call = proc
        return
    if name == "ivg.vector.search":
        _translate_vector_search(proc, context)
    elif name == "ivg.neighbors":
        _translate_neighbors(proc, context)
    elif name == "ivg.ppr":
        _translate_ppr(proc, context)
    elif name == "ivg.bm25.search":
        _translate_bm25_search(proc, context)
    elif name == "ivg.ivf.search":
        _translate_ivf_search(proc, context)
    elif name == "ivg.retrieve":
        _translate_retrieve(proc, context)
    elif name == "ivg.shortestpath.weighted" or name == "ivg.shortestPath.weighted":
        _translate_weighted_shortest_path(proc, context)
    elif name == "ivg.degreeCentrality":
        _translate_degree_centrality(proc, context)
    elif name == "ivg.betweenness":
        _translate_betweenness(proc, context)
    elif name == "ivg.closeness":
        _translate_closeness(proc, context)
    elif name == "ivg.eigenvector":
        _translate_eigenvector(proc, context)
    elif name == "ivg.leiden":
        _translate_leiden(proc, context)
    elif name == "ivg.triangleCount":
        _translate_triangle_count(proc, context)
    elif name == "ivg.scc":
        _translate_scc(proc, context)
    elif name == "ivg.kcore":
        _translate_kcore(proc, context)
    else:
        raise ValueError(
            f"Unknown procedure: {name!r}. Supported: ivg.retrieve, ivg.vector.search, ivg.neighbors, ivg.ppr, ivg.bm25.search, ivg.ivf.search, ivg.shortestPath.weighted, ivg.degreeCentrality, ivg.betweenness, ivg.closeness, ivg.eigenvector, ivg.leiden, ivg.triangleCount, ivg.scc, ivg.kcore"
        )


def _resolve_arg(arg, context: TranslationContext, name: str, expected_type=None):
    """Resolve a procedure argument (literal, variable/parameter, or list)."""
    if isinstance(arg, ast.Literal):
        val = arg.value
        if isinstance(val, list):
            return [
                item.value if isinstance(item, ast.Literal) else item for item in val
            ]
        return val
    elif isinstance(arg, ast.Variable):
        if arg.name in context.input_params:
            return context.input_params[arg.name]
        raise ValueError(f"{name}: parameter '${arg.name}' not found in params")
    raise ValueError(f"{name}: argument must be a literal or parameter")


def _vs_resolve_query_input(query_input_arg, context):
    if isinstance(query_input_arg, ast.Literal):
        raw = query_input_arg.value
        if isinstance(raw, list):
            return [item.value if isinstance(item, ast.Literal) else item for item in raw]
        return raw
    if isinstance(query_input_arg, ast.Variable):
        var_name = query_input_arg.name
        if var_name in context.input_params:
            return context.input_params[var_name]
        raise ValueError(f"ivg.vector.search: parameter '${var_name}' not found in params")
    raise ValueError(
        "ivg.vector.search: third argument (query_input) must be a literal or parameter"
    )


def _vs_resolve_limit(limit_arg, context):
    if isinstance(limit_arg, ast.Literal):
        limit_val = limit_arg.value
    elif isinstance(limit_arg, ast.Variable):
        var_name = limit_arg.name
        if var_name in context.input_params:
            limit_val = context.input_params[var_name]
        else:
            raise ValueError(f"ivg.vector.search: parameter '${var_name}' not found in params")
    else:
        raise ValueError(
            "ivg.vector.search: fourth argument (limit) must be an integer literal or parameter"
        )
    try:
        limit_int = int(limit_val)
    except (TypeError, ValueError):
        raise ValueError(f"ivg.vector.search: limit must be an integer, got {limit_val!r}")
    if limit_int <= 0:
        raise ValueError(f"ivg.vector.search: limit must be > 0, got {limit_int}")
    return limit_int


def _vs_build_similarity(query_input, vector_fn, label, options, emb_table):
    if isinstance(query_input, list):
        vec_json = json.dumps(query_input)
        return f"{vector_fn}(e.emb, TO_VECTOR(?, DOUBLE))", [vec_json, label], False
    if isinstance(query_input, str):
        embedding_config = options.get("embedding_config")
        if embedding_config:
            return (
                f"{vector_fn}(e.emb, EMBEDDING(?, ?))",
                [query_input, embedding_config, label],
                False,
            )
        return (
            f"{vector_fn}(e.emb, (SELECT e2.emb FROM {emb_table} e2 WHERE e2.id = ?))",
            [query_input, label],
            True,
        )
    raise ValueError(
        f"ivg.vector.search: query_input must be a list[float] or str, got {type(query_input).__name__}"
    )


def _translate_vector_search(
    proc: ast.CypherProcedureCall, context: TranslationContext
) -> None:
    args = proc.arguments
    if len(args) < 4:
        raise ValueError(
            f"ivg.vector.search requires at least 4 arguments "
            f"(label, property, query_input, limit), got {len(args)}"
        )

    label_arg = args[0]
    if not isinstance(label_arg, ast.Literal) or not isinstance(label_arg.value, str):
        raise ValueError(
            "ivg.vector.search: first argument (label) must be a string literal"
        )
    label = label_arg.value
    validate_table_name("rdf_labels")

    prop_arg = args[1]
    if not isinstance(prop_arg, ast.Literal) or not isinstance(prop_arg.value, str):
        raise ValueError(
            "ivg.vector.search: second argument (property) must be a string literal"
        )

    query_input = _vs_resolve_query_input(args[2], context)
    limit_int = _vs_resolve_limit(args[3], context)

    raw_options = proc.options or {}
    options: Dict[str, Any] = {}
    for k, v in raw_options.items():
        options[k] = v.value if isinstance(v, ast.Literal) else v

    similarity = options.get("similarity", "cosine")
    if similarity not in ("cosine", "dot_product"):
        raise ValueError(
            f"ivg.vector.search: similarity must be 'cosine' or 'dot_product', got {similarity!r}"
        )

    vector_fn = "VECTOR_COSINE" if similarity == "cosine" else "VECTOR_DOT_PRODUCT"
    emb_table = _table("kg_NodeEmbeddings")
    labels_tbl = _table("rdf_labels")

    similarity_expr, ordered_params, exclude_self = _vs_build_similarity(
        query_input, vector_fn, label, options, emb_table
    )

    cte_sql = (
        f"SELECT TOP {limit_int} e.id AS node, {similarity_expr} AS score\n"
        f"FROM {emb_table} e\n"
        f"JOIN {labels_tbl} lbl ON lbl.s = e.id AND lbl.label = ?\n"
    )
    if exclude_self:
        cte_sql += f"WHERE e.id != ?\n"
        ordered_params.append(query_input)
    cte_sql += f"ORDER BY score DESC"

    context.all_stage_params.extend(ordered_params)
    context.stages.insert(0, f"VecSearch AS (\n{cte_sql}\n)")

    for item in proc.yield_items:
        context.variable_aliases[item] = "VecSearch"
    if "score" in proc.yield_items:
        context.scalar_variables.add("score")


def _translate_neighbors(
    proc: ast.CypherProcedureCall, context: TranslationContext
) -> None:
    """CALL ivg.neighbors($sources, 'MENTIONS', 'out') YIELD neighbor

    Args: source (str or list[str]), predicate (str, optional), direction ('out'/'in'/'both', default 'out')
    Yields: neighbor (node ID)
    """
    args = proc.arguments
    if len(args) < 1:
        raise ValueError("ivg.neighbors requires at least 1 argument (source_ids)")

    sources = _resolve_arg(args[0], context, "ivg.neighbors")
    if isinstance(sources, str):
        sources = [sources]
    if not isinstance(sources, list):
        raise ValueError(
            f"ivg.neighbors: source must be a string or list, got {type(sources).__name__}"
        )

    predicate = (
        _resolve_arg(args[1], context, "ivg.neighbors") if len(args) > 1 else None
    )
    direction = (
        _resolve_arg(args[2], context, "ivg.neighbors") if len(args) > 2 else "out"
    )
    if direction not in ("out", "in", "both"):
        raise ValueError(
            f"ivg.neighbors: direction must be 'out', 'in', or 'both', got {direction!r}"
        )

    edges_tbl = _table("rdf_edges")
    ph = ", ".join(["?"] * len(sources))
    parts = []

    if direction in ("out", "both"):
        sql = (
            f"SELECT DISTINCT e.o_id AS neighbor FROM {edges_tbl} e WHERE e.s IN ({ph})"
        )
        p = list(sources)
        if predicate:
            sql += " AND e.p = ?"
            p.append(predicate)
        parts.append((sql, p))

    if direction in ("in", "both"):
        sql = (
            f"SELECT DISTINCT e.s AS neighbor FROM {edges_tbl} e WHERE e.o_id IN ({ph})"
        )
        p = list(sources)
        if predicate:
            sql += " AND e.p = ?"
            p.append(predicate)
        parts.append((sql, p))

    if len(parts) == 1:
        cte_sql, cte_params = parts[0]
    else:
        cte_sql = " UNION ".join(sql for sql, _ in parts)
        cte_params = []
        for _, p in parts:
            cte_params.extend(p)

    context.all_stage_params.extend(cte_params)
    context.stages.insert(0, f"Neighbors AS (\n{cte_sql}\n)")

    for item in proc.yield_items:
        context.variable_aliases[item] = "Neighbors"


def _translate_ppr(proc: ast.CypherProcedureCall, context: TranslationContext) -> None:
    """CALL ivg.ppr($seeds, 0.85, 20) YIELD node, score

    Generates SQL: SELECT Graph_KG.kg_PPR(?, ?, ?, 0, 1.0)
    Then wraps in JSON_TABLE to produce rows of (node, score).
    """
    args = proc.arguments
    if len(args) < 1:
        raise ValueError("ivg.ppr requires at least 1 argument (seed_ids)")

    seeds = _resolve_arg(args[0], context, "ivg.ppr")
    if isinstance(seeds, str):
        seeds = [seeds]
    if not isinstance(seeds, list):
        raise ValueError(
            f"ivg.ppr: seeds must be a string or list, got {type(seeds).__name__}"
        )

    alpha = float(_resolve_arg(args[1], context, "ivg.ppr")) if len(args) > 1 else 0.85
    max_iter = int(_resolve_arg(args[2], context, "ivg.ppr")) if len(args) > 2 else 20

    seed_json = json.dumps(seeds)
    ppr_fn = f"{_schema_prefix}.kg_PPR" if _schema_prefix else "kg_PPR"

    cte_sql = (
        f"SELECT j.node_id AS node, j.score\n"
        f"FROM JSON_TABLE(\n"
        f"  {ppr_fn}(?, ?, ?, 0, 1.0),\n"
        f"  '$[*]' COLUMNS(\n"
        f"    node_id VARCHAR(256) PATH '$.id',\n"
        f"    score DOUBLE PATH '$.score'\n"
        f"  )\n"
        f") j"
    )
    context.all_stage_params.extend([seed_json, alpha, max_iter])
    context.stages.insert(0, f"PPR AS (\n{cte_sql}\n)")

    for item in proc.yield_items:
        context.variable_aliases[item] = "PPR"
    if "score" in proc.yield_items:
        context.scalar_variables.add("score")


def _translate_bm25_search(
    proc: ast.CypherProcedureCall, context: TranslationContext
) -> None:
    args = proc.arguments
    if len(args) < 3:
        raise ValueError("ivg.bm25.search requires 3 arguments: name, query, k")

    idx_name = _resolve_arg(args[0], context, "ivg.bm25.search")
    if not isinstance(idx_name, str):
        raise ValueError(
            "ivg.bm25.search: first argument (name) must be a string literal"
        )

    query = _resolve_arg(args[1], context, "ivg.bm25.search")
    k_val = _resolve_arg(args[2], context, "ivg.bm25.search")
    try:
        k_int = int(k_val)
    except (TypeError, ValueError):
        raise ValueError(
            f"ivg.bm25.search: third argument (k) must be an integer, got {k_val!r}"
        )

    bm25_fn = f"{_schema_prefix}.kg_BM25" if _schema_prefix else "kg_BM25"
    # Bind idx_name and query as parameters (? placeholders) rather than
    # interpolating them inline.  k_int is an integer cast — safe as inline literal.
    context.all_stage_params.extend([str(idx_name), str(query)])
    cte_sql = (
        f"SELECT j.node_id AS node, j.score\n"
        f"FROM JSON_TABLE(\n"
        f"  {bm25_fn}(?, ?, {k_int}),\n"
        f"  '$[*]' COLUMNS(\n"
        f"    node_id VARCHAR(256) PATH '$.id',\n"
        f"    score DOUBLE PATH '$.score'\n"
        f"  )\n"
        f") j"
    )
    context.stages.insert(0, f"BM25 AS (\n{cte_sql}\n)")

    for item in proc.yield_items:
        context.variable_aliases[item] = "BM25"
    if "score" in proc.yield_items:
        context.scalar_variables.add("score")


def _translate_retrieve(
    proc: ast.CypherProcedureCall, context: TranslationContext
) -> None:
    args = proc.arguments
    if not args:
        raise ValueError(
            "ivg.retrieve requires: (query_text, limit, bm25_name='default', vec_label='*', rrf_k=60, embedding_config='')"
        )

    query = _resolve_arg(args[0], context, "ivg.retrieve")
    limit = int(_resolve_arg(args[1], context, "ivg.retrieve")) if len(args) > 1 else 10
    bm25_name = str(_resolve_arg(args[2], context, "ivg.retrieve")) if len(args) > 2 else "default"
    vec_label = str(_resolve_arg(args[3], context, "ivg.retrieve")) if len(args) > 3 else "*"
    rrf_k = int(_resolve_arg(args[4], context, "ivg.retrieve")) if len(args) > 4 else 60
    embedding_config = str(_resolve_arg(args[5], context, "ivg.retrieve")) if len(args) > 5 else ""

    if not query:
        raise ValueError("ivg.retrieve: query text cannot be empty")

    vec_limit = limit * 2
    bm25_limit = limit * 2
    emb_table = f"{_schema_prefix}.kg_NodeEmbeddings" if _schema_prefix else "Graph_KG.kg_NodeEmbeddings"
    bm25_fn = f"{_schema_prefix}.kg_BM25" if _schema_prefix else "Graph_KG.kg_BM25"

    # Bind all string user inputs as ? parameters; integer args stay inline (safe).
    # Order: bm25_name, query (for BM25 CTE), then query again (for Vec EMBEDDING()),
    # then embedding_config, then vec_label filter (if not wildcard).
    context.all_stage_params.extend([str(bm25_name), str(query)])

    bm25_cte = (
        f"SELECT j.node_id AS node, j.score\n"
        f"FROM JSON_TABLE(\n"
        f"  {bm25_fn}(?, ?, {bm25_limit}),\n"
        f"  '$[*]' COLUMNS(\n"
        f"    node_id VARCHAR(256) PATH '$.id',\n"
        f"    score DOUBLE PATH '$.score'\n"
        f"  )\n"
        f") j"
    )

    context.all_stage_params.append(str(query))   # for EMBEDDING(?, ...)
    context.all_stage_params.append(str(embedding_config))

    if vec_label == "*":
        vec_where = ""
    else:
        vec_where = " WHERE n.label = ?"
        context.all_stage_params.append(str(vec_label))

    vec_cte = (
        f"SELECT TOP {vec_limit} e.id AS node, VECTOR_COSINE(e.emb, EMBEDDING(?, ?)) AS score\n"
        f"FROM {emb_table} e{vec_where}\n"
        f"ORDER BY score DESC"
    )

    rrf_cte = (
        f"SELECT node, SUM(rrf_score) AS rrf_score\n"
        f"FROM (\n"
        f"  SELECT node, 1.0 / ({rrf_k} + ROW_NUMBER() OVER (ORDER BY score DESC)) AS rrf_score\n"
        f"  FROM BM25_Retrieve\n"
        f"  UNION ALL\n"
        f"  SELECT node, 1.0 / ({rrf_k} + ROW_NUMBER() OVER (ORDER BY score DESC)) AS rrf_score\n"
        f"  FROM Vec_Retrieve\n"
        f") ranked\n"
        f"GROUP BY node\n"
        f"ORDER BY rrf_score DESC\n"
        f"FETCH FIRST {limit} ROWS ONLY"
    )

    context.stages.insert(0, f"Retrieve AS (\n{rrf_cte}\n)")
    context.stages.insert(0, f"Vec_Retrieve AS (\n{vec_cte}\n)")
    context.stages.insert(0, f"BM25_Retrieve AS (\n{bm25_cte}\n)")

    for item in proc.yield_items:
        context.variable_aliases[item] = "Retrieve"
    if "score" in proc.yield_items:
        context.scalar_variables.add("score")


def _translate_ivf_search(
    proc: ast.CypherProcedureCall, context: TranslationContext
) -> None:
    args = proc.arguments
    if len(args) < 4:
        raise ValueError(
            "ivg.ivf.search requires 4 arguments: name, query_vec, k, nprobe"
        )

    idx_name = _resolve_arg(args[0], context, "ivg.ivf.search")
    if not isinstance(idx_name, str):
        raise ValueError(
            "ivg.ivf.search: first argument (name) must be a string literal"
        )

    query_vec = _resolve_arg(args[1], context, "ivg.ivf.search")
    if not isinstance(query_vec, list):
        raise ValueError(
            "ivg.ivf.search: second argument (query_vec) must be a list of floats"
        )
    floats = [float(v) for v in query_vec]
    import json as _json

    query_json = _json.dumps(floats).replace("'", "''")

    k_val = _resolve_arg(args[2], context, "ivg.ivf.search")
    try:
        k_int = int(k_val)
    except (TypeError, ValueError):
        raise ValueError(
            f"ivg.ivf.search: third argument (k) must be an integer, got {k_val!r}"
        )

    nprobe_val = _resolve_arg(args[3], context, "ivg.ivf.search")
    try:
        nprobe_int = int(nprobe_val)
    except (TypeError, ValueError):
        raise ValueError(
            f"ivg.ivf.search: fourth argument (nprobe) must be an integer, got {nprobe_val!r}"
        )

    ivf_fn = f"{_schema_prefix}.kg_IVF" if _schema_prefix else "kg_IVF"
    # Bind idx_name and query_json as ? parameters; k_int/nprobe_int are safe int literals.
    context.all_stage_params.extend([str(idx_name), _json.dumps(floats)])

    cte_sql = (
        f"SELECT j.node, j.score\n"
        f"FROM JSON_TABLE(\n"
        f"  {ivf_fn}(?, ?, {k_int}, {nprobe_int}),\n"
        f"  '$[*]' COLUMNS(\n"
        f"    node VARCHAR(256) PATH '$.id',\n"
        f"    score DOUBLE PATH '$.score'\n"
        f"  )\n"
        f") j"
    )

    # IRIS can't resolve CTEs over JSON_TABLE(stored_proc(...)) — use inline derived table
    context.temporal_derived["IVF_SEARCH"] = cte_sql
    context.from_clauses.append("IVF_SEARCH")

    for item in proc.yield_items:
        context.variable_aliases[item] = "IVF_SEARCH"
    if "score" in proc.yield_items:
        context.scalar_variables.add("score")


def _translate_weighted_shortest_path(
    proc: ast.CypherProcedureCall, context: TranslationContext
) -> None:
    args = proc.arguments
    if len(args) < 2:
        raise ValueError(
            "ivg.shortestPath.weighted requires at least 2 arguments: from, to"
        )

    from_id = _resolve_arg(args[0], context, "ivg.shortestPath.weighted")
    to_id = _resolve_arg(args[1], context, "ivg.shortestPath.weighted")
    weight_prop = (
        str(_resolve_arg(args[2], context, "ivg.shortestPath.weighted"))
        if len(args) > 2
        else "weight"
    )
    max_cost = (
        float(_resolve_arg(args[3], context, "ivg.shortestPath.weighted"))
        if len(args) > 3
        else 9999.0
    )
    max_hops = (
        int(_resolve_arg(args[4], context, "ivg.shortestPath.weighted"))
        if len(args) > 4
        else 10
    )
    direction = (
        str(_resolve_arg(args[5], context, "ivg.shortestPath.weighted"))
        if len(args) > 5
        else "out"
    )

    if not isinstance(from_id, str) or not isinstance(to_id, str):
        raise ValueError(
            "ivg.shortestPath.weighted: from and to must be string literals or $param"
        )

    context.var_length_paths.append(
        {
            "weighted": True,
            "src_id_param": from_id
            if not isinstance(from_id, str) or from_id.startswith("$")
            else f"'{from_id}'",
            "dst_id_param": to_id
            if not isinstance(to_id, str) or to_id.startswith("$")
            else f"'{to_id}'",
            "weight_prop": weight_prop,
            "max_cost": max_cost,
            "max_hops": max_hops,
            "direction": direction,
            "return_path_funcs": list(proc.yield_items),
        }
    )

    for item in proc.yield_items:
        if item in ("path", "totalCost", "totalcost", "node"):
            context.variable_aliases[item] = "WS"
            context.scalar_variables.add(item)


_TEMPORAL_TS_OPS = {
    ast.BooleanOperator.GREATER_THAN_OR_EQUAL,
    ast.BooleanOperator.LESS_THAN_OR_EQUAL,
    ast.BooleanOperator.GREATER_THAN,
    ast.BooleanOperator.LESS_THAN,
    ast.BooleanOperator.EQUALS,
}


def _extract_temporal_bounds(where_expr, rel_var: str, params: dict):
    if where_expr is None:
        return None
    return _walk_for_temporal(where_expr, rel_var, params)


def _resolve_ts_value(expr, params: dict):
    if isinstance(expr, ast.Literal):
        return expr.value
    if hasattr(ast, "Parameter") and isinstance(expr, ast.Parameter):
        return params.get(expr.name)
    if isinstance(expr, ast.Variable):
        return params.get(expr.name)
    return None


def _walk_for_temporal(expr, rel_var: str, params: dict):
    if not isinstance(expr, ast.BooleanExpression):
        return None

    op = expr.operator

    if op == ast.BooleanOperator.OR:
        for operand in expr.operands:
            if isinstance(operand, ast.BooleanExpression) and operand.operands:
                left = operand.operands[0]
                if (
                    isinstance(left, ast.PropertyReference)
                    and left.variable == rel_var
                    and left.property_name == "ts"
                ):
                    raise ValueError(
                        f"Temporal r.ts OR conditions are not supported. "
                        f"Use AND to combine timestamp bounds."
                    )
        return None

    if op == ast.BooleanOperator.AND:
        ts_start = None
        ts_end = None
        found = False
        for operand in expr.operands:
            result = _walk_for_temporal(operand, rel_var, params)
            if result is not None:
                found = True
                if result.ts_start is not None and ts_start is None:
                    ts_start = result.ts_start
                if result.ts_end is not None and ts_end is None:
                    ts_end = result.ts_end
        if found:
            return TemporalBound(
                ts_start=ts_start,
                ts_end=ts_end,
                rel_variable=rel_var,
                predicate=None,
                direction="out",
            )
        return None

    if op in _TEMPORAL_TS_OPS and len(expr.operands) >= 2:
        left, right = expr.operands[0], expr.operands[1]
        if (
            isinstance(left, ast.PropertyReference)
            and left.variable == rel_var
            and left.property_name == "ts"
        ):
            val = _resolve_ts_value(right, params)
            if op in (
                ast.BooleanOperator.GREATER_THAN_OR_EQUAL,
                ast.BooleanOperator.GREATER_THAN,
            ):
                return TemporalBound(
                    ts_start=val,
                    ts_end=None,
                    rel_variable=rel_var,
                    predicate=None,
                    direction="out",
                )
            if op in (
                ast.BooleanOperator.LESS_THAN_OR_EQUAL,
                ast.BooleanOperator.LESS_THAN,
            ):
                return TemporalBound(
                    ts_start=None,
                    ts_end=val,
                    rel_variable=rel_var,
                    predicate=None,
                    direction="out",
                )
            if op == ast.BooleanOperator.EQUALS:
                return TemporalBound(
                    ts_start=val,
                    ts_end=val,
                    rel_variable=rel_var,
                    predicate=None,
                    direction="out",
                )

    return None


def _build_temporal_cte(edges: list, cte_name: str, metadata) -> str:
    _LIMIT = 10_000
    if not edges:
        return "SELECT NULL AS s, NULL AS p, NULL AS o, NULL AS ts, NULL AS weight FROM (SELECT 1) __empty WHERE 1=0"
    if len(edges) > _LIMIT:
        metadata.warnings.append(
            f"temporal result truncated to {_LIMIT:,} edges — "
            f"narrow the time window or use get_edges_in_window()"
        )
        edges = edges[:_LIMIT]
    rows = []
    for e in edges:
        s = str(e.get("s", e.get("source", ""))).replace("'", "''")
        p = str(e.get("p", e.get("predicate", ""))).replace("'", "''")
        o = str(e.get("o", e.get("target", ""))).replace("'", "''")
        ts = int(e.get("ts", e.get("timestamp", 0)))
        w = float(e.get("w", e.get("weight", 1.0)))
        rows.append(
            f"SELECT '{s}' AS s, '{p}' AS p, '{o}' AS o, {ts} AS ts, {w} AS weight"
        )
    return " UNION ALL ".join(rows)


def _remove_ts_conditions_from_where(context, rel_var: str):
    kept = []
    for cond in context.where_conditions:
        if f".ts" in cond and rel_var in cond:
            continue
        kept.append(cond)
    context.where_conditions = kept


def _maybe_split_deep_joins(sql: str, params: list, context) -> str:
    JOIN_THRESHOLD = 20
    join_count = sql.count(" JOIN ")
    if join_count <= JOIN_THRESHOLD:
        return sql
    import re as _re
    # Capture an optional `TOP n` in the prefix (the FETCH-FIRST-+-JOIN workaround emits
    # SELECT [DISTINCT] TOP n) so it stays attached to the SELECT keyword and is not
    # swept into the column list.
    select_m = _re.match(r'(SELECT\s+(?:DISTINCT\s+)?(?:TOP\s+\d+\s+)?)(.*?)(\nFROM\s)', sql, _re.DOTALL)
    if not select_m:
        return sql
    # select_prefix carries any DISTINCT and TOP n; both propagate to the outer wrapper
    # (line below), so the CTE wrap preserves the row cap from the TOP workaround.
    select_prefix = select_m.group(1)
    select_cols = select_m.group(2).strip()
    has_agg = bool(_re.search(r'\b(AVG|SUM|COUNT|MIN|MAX|STDEV|JSON_ARRAYAGG)\s*\(', select_cols))
    has_group = 'GROUP BY' in sql
    if has_agg and not has_group:
        return sql
    inner_from_onwards = sql[select_m.start(3):]
    inner_sql = f"SELECT {select_cols}{inner_from_onwards}"
    _SQL_TYPES = frozenset({
        'INTEGER','INT','DOUBLE','FLOAT','REAL','VARCHAR','CHAR','BIGINT','SMALLINT',
        'DECIMAL','NUMERIC','BOOLEAN','DATE','TIME','TIMESTAMP','VARBINARY','BINARY',
    })
    alias_re = _re.compile(r'\)\s+AS\s+("?[a-z_][a-z0-9_"]*"?)\s*(?:,|\Z)', _re.IGNORECASE | _re.DOTALL)
    top_as_re = _re.compile(r'(?:^|,)\s*(?:[^,]+?)\s+AS\s+("?[a-z_][a-z0-9_"]*"?)\s*(?=,|$)', _re.IGNORECASE)
    seen = {}
    for m in _re.finditer(r'(?:^|(?<=,))\s*([^,]+?)\s+AS\s+("?[a-z_][a-z0-9_"]*"?)\s*(?=,|$)', select_cols, _re.DOTALL):
        alias = m.group(2).strip('"')
        if alias.upper() not in _SQL_TYPES:
            seen[alias] = alias
    outer_cols = ', '.join(seen.keys())
    if not outer_cols:
        return sql
    outer_sql = f"WITH _MR AS (\n{inner_sql}\n)\n{select_prefix}{outer_cols}\nFROM _MR"
    order_m = _re.search(r'\nORDER BY .+', sql, _re.DOTALL)
    limit_m = _re.search(r'\nFETCH FIRST (\d+) ROWS ONLY', sql)
    offset_m = _re.search(r'\nOFFSET \d+', sql)
    suffix = ""
    if order_m:
        start = order_m.start()
        end = limit_m.start() if limit_m and limit_m.start() > start else len(sql)
        suffix += sql[start:end]
    if limit_m:
        suffix += f"\nFETCH FIRST {limit_m.group(1)} ROWS ONLY"
    if offset_m:
        suffix += f"\nOFFSET {offset_m.group(0).split()[1]}"
    outer_sql += suffix
    return outer_sql


def _demote_agg_stages_to_subqueries(sql: str, ctes: list) -> tuple:
    remaining_ctes = []
    for cte in ctes:
        name_end = cte.index(" AS (")
        cte_name = cte[:name_end].strip()
        body_start = cte.index(" AS (") + 5
        body_end = cte.rindex(")")
        body = cte[body_start:body_end].strip()

        if "GROUP BY" in body.upper() and f"FROM {cte_name}" in sql:
            sql = sql.replace(f"FROM {cte_name}", f"FROM ({body}) {cte_name}", 1)
        else:
            remaining_ctes.append(cte)
    return sql, remaining_ctes


def _to_sql_init_part_from(context: TranslationContext, cypher_query: ast.CypherQuery, i: int) -> None:
    if i > 0:
        context.from_clauses.append(f"Stage{i}")
    elif getattr(context, "_ivf_derived", None):
        context.from_clauses.append(context._ivf_derived)
    elif cypher_query.procedure_call is not None:
        if context.temporal_derived:
            for td_name in context.temporal_derived:
                context.from_clauses.append(td_name)
        elif context.stages:
            cte_name = context.stages[0].split(" AS ")[0].strip()
            context.from_clauses.append(cte_name)
        else:
            context.from_clauses.append("VecSearch")
    elif context.stages and not context.from_clauses:
        cte_name = context.stages[0].split(" AS ")[0].strip()
        context.from_clauses.append(cte_name)


def _to_sql_handle_foreach(clause, context: TranslationContext, metadata) -> bool:
    if isinstance(clause.source, ast.Literal) and isinstance(clause.source.value, list):
        for item in clause.source.value:
            orig_aliases = dict(context.variable_aliases)
            context.variable_aliases[clause.variable] = "__foreach_literal__"
            context.foreach_literals = getattr(context, "foreach_literals", {})
            context.foreach_literals[clause.variable] = (
                item.value if isinstance(item, ast.Literal) else item
            )
            for uc in clause.update_clauses:
                if isinstance(uc, ast.UpdatingClause):
                    translate_updating_clause(uc, context, metadata)
            context.variable_aliases = orig_aliases
            if hasattr(context, "foreach_literals"):
                context.foreach_literals.pop(clause.variable, None)
    else:
        for uc in clause.update_clauses:
            if isinstance(uc, ast.UpdatingClause):
                translate_updating_clause(uc, context, metadata)
    return True


def _to_sql_handle_with(part, context: TranslationContext, i: int, cypher_query=None) -> None:
    translate_with_clause(part.with_clause, context)

    # Preprocess ORDER BY items for the WITH clause (before build_stage_sql so joins are included).
    # For alias-based ORDER BY (e.g. WITH n.name AS prop ORDER BY prop), the alias is not yet
    # resolvable as a variable — emit it as a bare column name for the subquery wrapper to resolve.
    order_by_items = []
    with_aliases = {
        (item.alias or (item.expression.name if isinstance(item.expression, ast.Variable) else None))
        for item in part.with_clause.items
    } - {None}
    # Map (variable, property_name) -> alias for PropertyReference WITH projections.
    # ORDER BY a.name after WITH DISTINCT a.name AS name should use alias 'name', not add a new JOIN.
    prop_alias_map: dict = {}
    for wi in part.with_clause.items:
        if wi.alias and isinstance(wi.expression, ast.PropertyReference):
            prop_alias_map[(wi.expression.variable, wi.expression.property_name)] = wi.alias
    # sort_projections: list of (alias, sql_expr) for complex ORDER BY expressions that
    # need to be projected into the inner SELECT so the outer ORDER BY can reference them.
    sort_projections: list = []
    if part.with_clause.order_by_clause:
        for item in part.with_clause.order_by_clause.items:
            direction = "ASC" if item.ascending else "DESC"
            # If ORDER BY expression is a PropertyReference projected as a WITH alias, use the alias.
            # This avoids adding a second JOIN that would break DISTINCT semantics.
            if isinstance(item.expression, ast.PropertyReference):
                _pk = (item.expression.variable, item.expression.property_name)
                if _pk in prop_alias_map:
                    col = _safe_alias(prop_alias_map[_pk])
                    order_by_items.append(
                        f"CASE WHEN ISNUMERIC({col}) = 1 THEN CAST({col} AS DOUBLE) END {direction}, {col} {direction}"
                    )
                    continue
            # If the expression is a variable that matches a WITH alias, emit as bare column name
            # (but quote it if it's a SQL reserved word, same as the SELECT alias)
            if isinstance(item.expression, ast.Variable) and item.expression.name in with_aliases:
                col = _safe_alias(item.expression.name)
                # Emit numeric-aware sort: numeric values sort by DOUBLE, strings by VARCHAR.
                # CASE WHEN ISNUMERIC(x)=1 THEN CAST(x AS DOUBLE) END returns NULL for strings
                # (NULLs sort last in ASC, first in DESC — both correct for mixed-type data).
                order_by_items.append(
                    f"CASE WHEN ISNUMERIC({col}) = 1 THEN CAST({col} AS DOUBLE) END {direction}, {col} {direction}"
                )
            else:
                try:
                    # Use segment="inline" so numeric literals become inline constants.
                    # Property references add JOINs to context (join_params) as needed.
                    expr = translate_expression(item.expression, context, segment="inline")
                    # If the expression references JOIN aliases (p\d+.val), it cannot be used
                    # directly in ORDER BY on the outer subquery — project it as a sort column.
                    import re as _re_ob
                    if _re_ob.search(r'\bp\d+\.val\b', expr):
                        sort_alias = f"__sort{len(sort_projections)}"
                        sort_projections.append((sort_alias, expr))
                        order_by_items.append(f"{sort_alias} {direction}")
                    else:
                        order_by_items.append(f"{expr} {direction}")
                except Exception:
                    pass

    sql, stage_params = context.build_stage_sql(part.with_clause.distinct)

    # Inject sort projection columns into the inner SELECT if any complex ORDER BY expressions exist.
    if sort_projections:
        # The sql is "SELECT col1, col2, ... FROM ..." — inject sort columns after SELECT list.
        # Find the first FROM (not inside a subquery) to insert sort columns before it.
        for sort_alias, sort_expr in sort_projections:
            # Inject into SELECT: "SELECT ..., (sort_expr) AS sort_alias\nFROM ..."
            # Match the first \nFROM or " FROM " at the top level
            _from_pat = _re_ob.search(r'\nFROM ', sql)
            if _from_pat:
                insert_at = _from_pat.start()
                sql = sql[:insert_at] + f", ({sort_expr}) AS {sort_alias}" + sql[insert_at:]
            else:
                # Fallback: append to SELECT line
                sql = sql + f", ({sort_expr}) AS {sort_alias}"

    # Apply ORDER BY, SKIP, LIMIT from the WITH clause (if present).
    # IRIS does not allow ORDER BY directly in a CTE body — it must be inside a subquery wrapper.
    limit = _resolve_pagination_value(part.with_clause.limit, context)
    skip = _resolve_pagination_value(part.with_clause.skip, context)

    if order_by_items or limit is not None or skip is not None:
        has_join = "\nJOIN " in sql or " JOIN " in sql

        if limit is not None and skip is not None:
            # SKIP+LIMIT: ROW_NUMBER subquery (no FETCH FIRST, no ORDER BY in CTE body)
            if order_by_items:
                inner = f"SELECT * FROM ({sql}) __ob ORDER BY {', '.join(order_by_items)}"
            else:
                inner = sql
            sql = (
                f"SELECT * FROM (\n"
                f"SELECT ROW_NUMBER() OVER() AS __rn, __q.* FROM ({inner}) __q\n"
                f") __paged WHERE __rn > {skip} AND __rn <= {skip + limit}"
            )
        elif limit is not None:
            if order_by_items:
                # Need subquery to hold ORDER BY + TOP together
                inner = f"SELECT * FROM ({sql}) __ob ORDER BY {', '.join(order_by_items)}"
                head, sep, rest = inner.partition("SELECT ")
                if rest[:9].upper().startswith("DISTINCT "):
                    rest = "DISTINCT " + f"TOP {limit} " + rest[9:]
                else:
                    rest = f"TOP {limit} " + rest
                sql = head + sep + rest
            elif has_join:
                # JOIN CTE: inject TOP to avoid qaqpre FETCH FIRST crash
                head, sep, rest = sql.partition("SELECT ")
                if rest[:9].upper().startswith("DISTINCT "):
                    rest = "DISTINCT " + f"TOP {limit} " + rest[9:]
                else:
                    rest = f"TOP {limit} " + rest
                sql = head + sep + rest
            else:
                sql += f"\nFETCH FIRST {limit} ROWS ONLY"
        elif skip is not None:
            # SKIP only
            if order_by_items:
                inner = f"SELECT * FROM ({sql}) __ob ORDER BY {', '.join(order_by_items)}"
                sql = (
                    f"SELECT * FROM (\n"
                    f"SELECT ROW_NUMBER() OVER() AS __rn, __q.* FROM ({inner}) __q\n"
                    f") __paged WHERE __rn > {skip}"
                )
            elif has_join:
                # ROW_NUMBER on JOIN query to avoid OFFSET in CTE
                sql = (
                    f"SELECT * FROM (\n"
                    f"SELECT ROW_NUMBER() OVER() AS __rn, __q.* FROM ({sql}) __q\n"
                    f") __paged WHERE __rn > {skip}"
                )
            else:
                sql += f"\nOFFSET {skip} ROWS"
        elif order_by_items:
            # ORDER BY only (no LIMIT/SKIP): wrap to keep ORDER BY out of CTE body
            sql = f"SELECT * FROM ({sql}) __ob ORDER BY {', '.join(order_by_items)}"

    context.all_stage_params.extend(stage_params)
    context.stages.append(f"Stage{i + 1} AS (\n{sql}\n)")
    context.having_conditions = []
    context.where_params = []
    new_stage = f"Stage{i + 1}"
    if part.with_clause.star:
        new_aliases = {var: new_stage for var in context.variable_aliases}
    else:
        new_aliases = {}
        for item in part.with_clause.items:
            alias = item.alias or (
                item.expression.name
                if isinstance(item.expression, ast.Variable)
                else None
            )
            if alias:
                new_aliases[alias] = new_stage
                # Track the type of the variable in WITH clause.
                # If it's a reference to an existing variable, preserve its type.
                # Otherwise, it's a scalar (function result, literal, etc.)
                if isinstance(item.expression, ast.Variable):
                    # Passthrough: preserve the bound variable's type
                    # (from MATCH, previous WITH, etc.)
                    existing_type = context.variable_types.get(item.expression.name)
                    if existing_type:
                        context.bind_variable_type(alias, existing_type)
                    else:
                        # If not yet typed, assume node (safest for graph ops)
                        context.bind_variable_type(alias, "node")
                else:
                    # Everything else is scalar: aggregation, function call, literal, etc.
                    context.bind_variable_type(alias, "scalar")
            if isinstance(item.expression, ast.AggregationFunction) and alias:
                context.scalar_variables.add(alias)
            elif alias and not isinstance(item.expression, ast.Variable):
                context.scalar_variables.add(alias)
    context.variable_aliases = new_aliases



def _tts_union_branches(cypher_query, params):
    """Handle UNION/UNION ALL. Returns SQLQuery or None."""
    if not getattr(cypher_query, "union_queries", None):
        return None
    branches = [cypher_query] + [uq["query"] for uq in cypher_query.union_queries]
    all_flags = [False] + [uq["all"] for uq in cypher_query.union_queries]
    sqls = []
    all_params = []
    for branch in branches:
        branch_copy = ast.CypherQuery(
            query_parts=branch.query_parts,
            return_clause=branch.return_clause,
            order_by_clause=branch.order_by_clause,
            skip=branch.skip,
            limit=branch.limit,
            procedure_call=branch.procedure_call,
        )
        branch_copy.union_queries = []
        r = translate_to_sql(branch_copy, params)
        sqls.append(r.sql if isinstance(r.sql, str) else "\n".join(r.sql))
        all_params.extend(r.parameters)
    sep = " UNION ALL " if any(all_flags[1:]) else " UNION "
    def _ensure_from(s: str) -> str:
        if "\nFROM " not in s and "\nfrom " not in s:
            return s.rstrip() + "\nFROM (SELECT 1) __dual"
        return s
    combined = sep.join(f"({_ensure_from(s)})" for s in sqls)
    flat_params = []
    for p_list in all_params:
        flat_params.extend(p_list)
    return SQLQuery(sql=combined, parameters=[flat_params])


def _tts_process_parts(cypher_query, context, metadata):
    """Handle procedure_call + iterate query_parts. Returns is_transactional."""
    is_transactional = False
    if cypher_query.procedure_call is not None:
        translate_procedure_call(cypher_query.procedure_call, context)
        if context.system_procedure_call is not None:
            return SQLQuery(
                sql="__SYSTEM_PROCEDURE__",
                parameters=[[]],
                query_metadata=metadata,
                var_length_paths=None,
            )
        if not cypher_query.query_parts:
            if context.temporal_derived:
                for td_name in context.temporal_derived:
                    if td_name not in context.from_clauses:
                        context.from_clauses.append(td_name)
            else:
                cte_name = (
                    context.stages[0].split(" AS ")[0].strip()
                    if context.stages
                    else "VecSearch"
                )
                context.from_clauses.append(cte_name)

    for i, part in enumerate(cypher_query.query_parts):
        context.select_items, context.from_clauses, context.join_clauses = [], [], []
        context.where_conditions, context.group_by_items = [], []
        context.select_params, context.join_params, context.where_params = [], [], []
        _to_sql_init_part_from(context, cypher_query, i)
        for clause in part.clauses:
            if isinstance(clause, ast.WhereClause):
                context.pending_where = clause.expression
                break
        # Check for UNWIND+UPDATE pattern: when a literal-list UNWIND feeds updating clauses,
        # expand Python-side (like FOREACH) so each list element gets its own DML set.
        unwind_clause = next(
            (c for c in part.clauses if isinstance(c, ast.UnwindClause)), None
        )
        has_updating = any(isinstance(c, ast.UpdatingClause) for c in part.clauses)
        unwind_literals = None
        if (
            unwind_clause is not None
            and has_updating
            and isinstance(unwind_clause.expression, ast.Literal)
            and isinstance(unwind_clause.expression.value, list)
        ):
            unwind_literals = unwind_clause.expression.value
        elif (
            unwind_clause is not None
            and has_updating
            and isinstance(unwind_clause.expression, ast.Variable)
            and unwind_clause.expression.name in context.input_params
            and isinstance(context.input_params[unwind_clause.expression.name], list)
        ):
            unwind_literals = [
                ast.Literal(v) if not isinstance(v, ast.Literal) else v
                for v in context.input_params[unwind_clause.expression.name]
            ]

        if unwind_literals is not None:
            # UNWIND literal list + updating clauses → expand Python-side, one DML set per element
            is_transactional = True
            aliases_before = dict(context.variable_aliases)
            last_iter_aliases = dict(context.variable_aliases)
            # Accumulate created node IDs per variable for use in RETURN
            if not hasattr(context, "_unwind_create_node_ids"):
                context._unwind_create_node_ids = {}  # var_name → [uuid, ...]
            for item in unwind_literals:
                item_val = item.value if isinstance(item, ast.Literal) else item
                # Start each iteration from the pre-loop state so new vars don't accumulate
                context.variable_aliases = dict(aliases_before)
                context.variable_aliases[unwind_clause.alias] = "__foreach_literal__"
                context.foreach_literals = getattr(context, "foreach_literals", {})
                context.foreach_literals[unwind_clause.alias] = item_val
                for clause in part.clauses:
                    if isinstance(clause, ast.UnwindClause):
                        continue  # handled by foreach expansion above
                    elif isinstance(clause, ast.UpdatingClause):
                        translate_updating_clause(clause, context, metadata)
                    elif isinstance(clause, ast.WhereClause):
                        translate_where_clause(clause, context)
                # Collect node IDs created in this iteration
                for var_name in set(context.variable_aliases) - set(aliases_before):
                    nid = context.input_params.get(f"__create_id_{var_name}")
                    if nid:
                        context._unwind_create_node_ids.setdefault(var_name, []).append(nid)
                # Collect relationship identities created in this iteration
                if not hasattr(context, "_unwind_create_rel_ids"):
                    context._unwind_create_rel_ids = {}  # var_name → [(s,p,o), ...]
                for var_name in set(context.variable_aliases) - set(aliases_before):
                    edge_key = context.input_params.get(f"__create_edge_{var_name}")
                    if edge_key:
                        context._unwind_create_rel_ids.setdefault(var_name, []).append(edge_key)
                last_iter_aliases = dict(context.variable_aliases)
            if hasattr(context, "foreach_literals"):
                context.foreach_literals.pop(unwind_clause.alias, None)
            # After loop: keep vars created by updating clauses (from last iteration)
            context.variable_aliases = last_iter_aliases
            context.variable_aliases.pop(unwind_clause.alias, None)
            # Still need to add the UNWIND to context for RETURN clause access
            translate_unwind_clause(unwind_clause, context)
        else:
            for clause in part.clauses:
                if isinstance(clause, ast.MatchClause):
                    translate_match_clause(clause, context, metadata)
                elif isinstance(clause, ast.UnwindClause):
                    translate_unwind_clause(clause, context)
                elif isinstance(clause, ast.SubqueryCall):
                    translate_subquery_call(clause, context, metadata)
                elif isinstance(clause, ast.ForeachClause):
                    is_transactional = _to_sql_handle_foreach(clause, context, metadata) or is_transactional
                elif isinstance(clause, ast.UpdatingClause):
                    is_transactional = True
                    translate_updating_clause(clause, context, metadata)
                elif isinstance(clause, ast.WhereClause):
                    translate_where_clause(clause, context)
        if part.procedure_call is not None:
            translate_procedure_call(part.procedure_call, context)
        if part.with_clause:
            # If UNWIND+CREATE relationship expansion ran, reset context to correct single-table
            # structure before building the WITH stage (avoids 5-JOIN spurious structure).
            _apply_unwind_create_context_reset(context)
            _to_sql_handle_with(part, context, i)
    return is_transactional


def _apply_unwind_create_context_reset(context):
    """Reset FROM/JOIN/WHERE to correct single-table structure after UNWIND+CREATE expansion.

    Called before translating a WITH clause or RETURN that follows UNWIND+CREATE.
    Prevents the accumulated per-iteration JOIN structure from leaking into CTEs.
    """
    unwind_node_ids = getattr(context, "_unwind_create_node_ids", {})
    if unwind_node_ids:
        context.select_items, context.select_params = [], []
        context.join_clauses, context.join_params = [], []
        context.where_conditions, context.where_params = [], []
        first_node_alias = None
        for var_name, ids in unwind_node_ids.items():
            if not ids:
                continue
            node_alias = context.next_alias("n")
            if first_node_alias is None:
                first_node_alias = node_alias
            context.variable_aliases[var_name] = node_alias
            placeholders = ",".join(["?"] * len(ids))
            context.where_conditions.append(f"{node_alias}.node_id IN ({placeholders})")
            context.where_params.extend(ids)
        if first_node_alias is not None:
            context.from_clauses = [f"{_table('nodes')} {first_node_alias}"]
            for var_name, ids in list(unwind_node_ids.items())[1:]:
                if not ids:
                    continue
                na = context.variable_aliases[var_name]
                context.from_clauses.append(f"{_table('nodes')} {na}")
        return

    unwind_rel_ids = getattr(context, "_unwind_create_rel_ids", {})
    if unwind_rel_ids:
        context.select_items, context.select_params = [], []
        context.join_clauses, context.join_params = [], []
        context.where_conditions, context.where_params = [], []
        first_edge_alias = None
        for var_name, triples in unwind_rel_ids.items():
            if not triples:
                continue
            e_alias = context.next_alias("e")
            if first_edge_alias is None:
                first_edge_alias = e_alias
            context.variable_aliases[var_name] = e_alias
            conds = []
            for s_id, p_val, o_id in triples:
                conds.append(
                    f"({e_alias}.s = {context.add_where_param(s_id)}"
                    f" AND {e_alias}.p = {context.add_where_param(p_val)}"
                    f" AND {e_alias}.o_id = {context.add_where_param(o_id)})"
                )
            context.where_conditions.append("(" + " OR ".join(conds) + ")")
        if first_edge_alias is not None:
            context.from_clauses = [f"{_table('rdf_edges')} {first_edge_alias}"]
            for var_name in list(unwind_rel_ids.keys())[1:]:
                if not unwind_rel_ids[var_name]:
                    continue
                ea = context.variable_aliases[var_name]
                context.from_clauses.append(f"{_table('rdf_edges')} {ea}")


def _tts_finalize_context(cypher_query, context):
    """Apply last-part WITH, translate RETURN, compute order_by + graph_context. Returns order_by_items."""
    # 2. Final stage (RETURN)
    # If the last QueryPart had a WITH clause, we must select from that CTE stage.
    # Otherwise, we continue with the context of the last QueryPart (e.g. current MATCH joins).
    last_part_had_with = (
        cypher_query.query_parts[-1].with_clause is not None
        if cypher_query.query_parts
        else False
    )
    # A WITH clause in any query part (not just the last) may have produced stages.
    any_part_had_with = any(
        qp.with_clause is not None for qp in cypher_query.query_parts
    ) if cypher_query.query_parts else False
    if context.stages and last_part_had_with:
        # The last query part ended with a WITH clause, which just created Stage{N}.
        # RETURN must select directly from that stage with no additional JOINs.
        # Preserve UNWIND CROSS JOIN JSON_TABLE clauses before reset — they were
        # added by translate_unwind_clause and must survive the stage reset.
        unwind_joins = [
            j for j in context.join_clauses
            if "JSON_TABLE" in j and j.strip().startswith("CROSS JOIN")
        ]
        context.select_items, context.select_params = [], []
        context.from_clauses, context.join_clauses, context.join_params = (
            [f"Stage{len(context.stages)}"],
            list(unwind_joins),
            [],
        )
        context.where_conditions, context.where_params = [], []
    elif context.stages and any_part_had_with and not last_part_had_with:
        # A prior part had WITH (produced stages), but the last part is a plain MATCH/RETURN.
        # _to_sql_init_part_from already set from_clauses to Stage{N} and the MATCH clauses
        # added join_clauses correctly. Do NOT reset join_clauses or where_conditions —
        # they capture the post-WITH MATCH + WHERE filters (e.g. MATCH (b) WHERE a = b).
        # Only clear select_items so translate_return_clause can rebuild them.
        context.select_items, context.select_params = [], []

    # UNWIND+CREATE+RETURN: foreach expansion created nodes/relationships and tracked their IDs.
    # Reset context to a fresh single-table scan filtered to the collected IDs.
    # Skip when any query part had a WITH — the Stage CTE already handles scoping.
    if cypher_query.return_clause and not any_part_had_with:
        _apply_unwind_create_context_reset(context)

    if cypher_query.return_clause:
         translate_return_clause(cypher_query.return_clause, context)

    order_by_items = preprocess_order_by(cypher_query, context)

    if cypher_query.graph_context:
        safe_graph = cypher_query.graph_context.replace("'", "''")
        edge_aliases = [
            v
            for v in context.variable_aliases.values()
            if v and v.startswith("e") and not v.startswith("ES_")
        ]
        for ea in edge_aliases:
            context.where_conditions.append(f"{ea}.graph_id = '{safe_graph}'")
        context.where_conditions.append(f"1=1")
        graph_filter = f"'{safe_graph}'"
        for ea in list(context.variable_aliases.values()):
            if (
                ea
                and not ea.startswith("n")
                and not ea.startswith("l")
                and not ea.startswith("Stage")
            ):
                context.where_conditions.append(f"{ea}.graph_id = {graph_filter}")
                break

    return order_by_items


def _tts_transactional_result(cypher_query, context, metadata, order_by_items):
    """Assemble SQLQuery for transactional (DML) queries."""
    stmts, all_params = [], []
    for s, p in context.dml_statements:
        stmts.append(s)
        all_params.append(p)
    sql = None
    if cypher_query.return_clause:
        sql, p = context.build_stage_sql(cypher_query.return_clause.distinct)
        sql = apply_pagination(sql, cypher_query, context, order_by_items)
    # OPTIONAL MATCH null-row fallback for RETURN clause in transactional queries.
    optional_union_sql = ""
    optional_extra_params: List[Any] = []
    if sql is not None and context.optional_null_row_labels and context.optional_null_row_items:
        null_items = list(context.optional_null_row_items)
        while len(null_items) < len(context.select_items):
            null_items.append("NULL")
        null_select = ", ".join(null_items[:len(context.select_items)])
        not_exists_parts = []
        for label in context.optional_null_row_labels:
            not_exists_parts.append(
                f"NOT EXISTS (SELECT 1 FROM {_table('rdf_labels')} WHERE label = ?)"
            )
            optional_extra_params.append(label)
        where_clause = " AND ".join(not_exists_parts)
        optional_union_sql = f"\nUNION ALL\nSELECT {null_select} WHERE {where_clause}"

    all_ctes = [
        c
        for c in getattr(context, "cte_clauses", [])
        if not any(td in c for td in context.temporal_derived)
    ] + context.stages
    if all_ctes and sql is not None:
        sql, all_ctes = _demote_agg_stages_to_subqueries(sql, all_ctes)
        if all_ctes:
            sql = "WITH " + ",\n".join(all_ctes) + "\n" + sql
        if optional_union_sql:
            sql += optional_union_sql
        all_params.append(context.all_stage_params + p + optional_extra_params)
    elif sql is not None:
        if optional_union_sql:
            sql += optional_union_sql
        all_params.append(p + optional_extra_params)
    if sql is not None:
        stmts.append(sql)
    return SQLQuery(
        sql=stmts,
        parameters=all_params,
        query_metadata=metadata,
        is_transactional=True,
        column_name_map=dict(context.column_name_map),
    )


def _tts_collect_path_funcs(cypher_query, vl):
    """Collect RETURN path functions for shortest-path queries. Mutates vl[0]."""
    if not (vl and (vl[0].get("shortest") or vl[0].get("all_shortest")) and cypher_query.return_clause):
        return

    path_funcs = []
    path_var = vl[0].get("target_var") or vl[0].get("source_var")
    named_path_vars = {
        np.variable
        for np in (
            cypher_query.query_parts[0].clauses[0].named_paths
            if cypher_query.query_parts
            else []
        )
    }
    for item in cypher_query.return_clause.items:
        expr = item.expression
        if isinstance(expr, ast.Variable) and expr.name in named_path_vars:
            path_funcs.append("path")
        elif isinstance(
            expr, ast.FunctionCall
        ) and expr.function_name.lower() in (
            "length",
            "nodes",
            "relationships",
        ):
            if expr.arguments and isinstance(expr.arguments[0], ast.Variable):
                if expr.arguments[0].name in named_path_vars:
                    path_funcs.append(expr.function_name.lower())
    if path_funcs:
        vl[0]["return_path_funcs"] = path_funcs


def _build_bolt_column_types(cypher_query, context) -> List[str]:
    """Return a list of Bolt column type tags parallel to the RETURN clause columns.

    Tags: "relationship" for variables bound in MATCH relationship patterns,
    "node" for node variables, "scalar" for everything else.
    """
    if not cypher_query or not cypher_query.return_clause:
        return []
    types = []
    node_vars = set(context.variable_aliases.keys()) - context.rel_variables
    for item in cypher_query.return_clause.items:
        expr = item.expression
        if isinstance(expr, ast.Variable):
            if expr.name in context.rel_variables:
                types.append("relationship")
            elif expr.name in node_vars:
                types.append("node")
            else:
                types.append("scalar")
        else:
            types.append("scalar")
    return types


def _tts_select_result(cypher_query, context, metadata, order_by_items):
    """Assemble SQLQuery for SELECT queries."""
    sql, p = context.build_stage_sql(
        cypher_query.return_clause.distinct if cypher_query.return_clause else False
    )
    if not context.select_items and context.stages and context.from_clauses:
        stage_name = context.from_clauses[-1]
        if stage_name in [s.split(" AS ")[0].strip() for s in context.stages]:
            sql = sql.replace("SELECT \nFROM", f"SELECT *\nFROM", 1)
            sql = sql.replace("SELECT DISTINCT \nFROM", f"SELECT DISTINCT *\nFROM", 1)
    if hasattr(context, '_percentile_queries') and context._percentile_queries:
        import re as _re
        from_match = _re.search(r'\nFROM\s+(.*?)(?:\nWHERE|\nORDER|\nFETCH|\nGROUP|\nHAVING|$)', sql, _re.DOTALL)
        if from_match and len(context._percentile_queries) == 1:
            from_clause = from_match.group(0).strip()
            val_expr, pct_val, fn_name, var_name, alias = context._percentile_queries[0]
            col_alias = _re.search(r'AS\s+(\w+)\s*$', sql.split('\n')[0])
            out_alias = col_alias.group(1) if col_alias else "result"
            proc = "PCONT" if fn_name == "percentilecont" else "PDISC"
            inner_col = val_expr.split('.')[-1] if '.' in val_expr else val_expr
            sql = (
                f"SELECT IVG.Percentile_{proc}("
                f"(SELECT JSON_ARRAYAGG(CAST({val_expr} AS DOUBLE)) "
                f"\n{from_clause}), {pct_val}) AS {out_alias}"
            )
            p = []
    sql = apply_pagination(sql, cypher_query, context, order_by_items)
    vl = context.var_length_paths or None

    _tts_collect_path_funcs(cypher_query, vl)

    # OPTIONAL MATCH null-row fallback: append UNION ALL SELECT <nulls> WHERE NOT EXISTS
    # This handles the Cypher semantics: if the optional pattern matches 0 rows, yield
    # one null row instead of 0 rows.
    optional_union_sql = ""
    optional_extra_params: List[Any] = []
    if context.optional_null_row_labels and context.optional_null_row_items:
        null_items = context.optional_null_row_items
        # Build the null-row SELECT: pad with NULLs if fewer items than select columns
        while len(null_items) < len(context.select_items):
            null_items.append("NULL")
        null_select = ", ".join(null_items[:len(context.select_items)])
        # NOT EXISTS check: all label constraints must have 0 matching nodes
        not_exists_parts = []
        for label in context.optional_null_row_labels:
            not_exists_parts.append(
                f"NOT EXISTS (SELECT 1 FROM {_table('rdf_labels')} WHERE label = ?)"
            )
            optional_extra_params.append(label)
        where_clause = " AND ".join(not_exists_parts)
        optional_union_sql = f"\nUNION ALL\nSELECT {null_select} WHERE {where_clause}"

    all_ctes = [
        c
        for c in getattr(context, "cte_clauses", [])
        if not any(td in c for td in context.temporal_derived)
    ] + context.stages
    if all_ctes:
        sql, all_ctes = _demote_agg_stages_to_subqueries(sql, all_ctes)
        if all_ctes:
            sql = "WITH " + ",\n".join(all_ctes) + "\n" + sql
        if optional_union_sql:
            sql += optional_union_sql
        return SQLQuery(
            sql=sql,
            parameters=[context.all_stage_params + p + optional_extra_params],
            query_metadata=metadata,
            var_length_paths=vl,
            bolt_column_types=_build_bolt_column_types(cypher_query, context),
            column_name_map=dict(context.column_name_map),
        )

    sql = _maybe_split_deep_joins(sql, p, context)
    if optional_union_sql:
        sql += optional_union_sql

    return SQLQuery(
        sql=sql, parameters=[p + optional_extra_params], query_metadata=metadata,
        var_length_paths=vl,
        bolt_column_types=_build_bolt_column_types(cypher_query, context),
        column_name_map=dict(context.column_name_map),
    )


def translate_to_sql(
    cypher_query: ast.CypherQuery, params: Optional[Dict[str, Any]] = None, engine=None
) -> SQLQuery:
    result = _tts_union_branches(cypher_query, params)
    if result is not None:
        return result

    context = TranslationContext()
    context.input_params = params or {}
    context._engine = engine
    context.graph_context = getattr(cypher_query, "graph_context", None)
    metadata = QueryMetadata()
    context._metadata = metadata
    is_transactional = _tts_process_parts(cypher_query, context, metadata)
    order_by_items = _tts_finalize_context(cypher_query, context)
    if is_transactional:
        return _tts_transactional_result(cypher_query, context, metadata, order_by_items)
    return _tts_select_result(cypher_query, context, metadata, order_by_items)


def preprocess_order_by(query: ast.CypherQuery, context: TranslationContext) -> list:
    if not query.order_by_clause:
        return []
    items = []
    alias_to_sql: dict = {}
    if query.return_clause:
        for ret_item in query.return_clause.items:
            if ret_item.alias:
                saved_select = list(context.select_params)
                saved_where = list(context.where_params)
                saved_join = list(context.join_params)
                saved_join_clauses = list(context.join_clauses)
                try:
                    sql_expr = translate_expression(ret_item.expression, context, segment="select")
                    alias_to_sql[ret_item.alias] = sql_expr
                except Exception:
                    pass
                finally:
                    context.select_params = saved_select
                    context.where_params = saved_where
                    context.join_params = saved_join
                    context.join_clauses = saved_join_clauses
    import re as _re
    _proc_prefix_re = _re.compile(r'^(?:Stage\d+|' + '|'.join(_PROC_CTE_ALIASES) + r')\.')
    for item in query.order_by_clause.items:
        try:
            if (isinstance(item.expression, ast.Variable)
                    and item.expression.name in alias_to_sql):
                expr = _proc_prefix_re.sub('', alias_to_sql[item.expression.name])
            else:
                expr = translate_expression(item.expression, context, segment="where")
                expr = _proc_prefix_re.sub('', expr)
        except ValueError:
            if (isinstance(item.expression, ast.Variable)
                    and item.expression.name in alias_to_sql):
                expr = _proc_prefix_re.sub('', alias_to_sql[item.expression.name])
            else:
                raise
        items.append(f"{expr} {'ASC' if item.ascending else 'DESC'}")
    return items


def _resolve_pagination_value(value, context: TranslationContext) -> Optional[int]:
    """Resolve a SKIP/LIMIT value that may be an integer literal or a parameter variable."""
    if value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, ast.Variable):
        resolved = context.input_params.get(value.name)
        if resolved is None:
            raise ValueError(
                f"Parameter '${value.name}' used in SKIP/LIMIT but not provided in params dict"
            )
        return int(resolved)
    return int(value)


def apply_pagination(
    sql: str,
    query: ast.CypherQuery,
    context: TranslationContext,
    order_by_items: list = None,
) -> str:
    if order_by_items:
        sql += f"\nORDER BY {', '.join(order_by_items)}"
    limit = _resolve_pagination_value(query.limit, context)
    skip = _resolve_pagination_value(query.skip, context)
    if limit is not None or skip is not None:
        if "\nFROM " not in sql and "FROM " not in sql.split("\n")[0]:
            sql = sql.rstrip() + "\nFROM (SELECT 1) __dual"
    # Build-106 workaround: IRIS 2026.3.0AI build 106 SIGSEGVs in %qaqpre when a
    # multi-table JOIN is combined with `FETCH FIRST n ROWS ONLY` on VARCHAR-keyed
    # tables (the ivg schema). `SELECT TOP n` does NOT crash for LIMIT-only queries.
    # For SKIP+LIMIT, wrap in a ROW_NUMBER subquery (also avoids FETCH FIRST).
    engine = getattr(context, "_engine", None)
    fetch_first_unsafe = bool(getattr(engine, "_fetch_first_unsafe", False))
    if fetch_first_unsafe and sql.lstrip().upper().startswith("SELECT "):
        if limit is not None and skip is not None:
            # ROW_NUMBER subquery handles SKIP+LIMIT without FETCH FIRST/OFFSET
            sql = (
                f"SELECT * FROM (\n"
                f"SELECT ROW_NUMBER() OVER() AS __rn, __q.* FROM ({sql}) __q\n"
                f") __paged WHERE __rn > {skip} AND __rn <= {skip + limit}"
            )
            return sql
        if limit is not None and " TOP " not in sql.split("\n", 1)[0].upper():
            # LIMIT only: inject TOP (no OFFSET needed)
            head, sep, rest = sql.partition("SELECT ")
            if rest[:9].upper().startswith("DISTINCT "):
                rest = "DISTINCT " + f"TOP {limit} " + rest[9:]
            else:
                rest = f"TOP {limit} " + rest
            return head + sep + rest
        if skip is not None:
            # SKIP only with unsafe FETCH FIRST: use ROW_NUMBER
            sql = (
                f"SELECT * FROM (\n"
                f"SELECT ROW_NUMBER() OVER() AS __rn, __q.* FROM ({sql}) __q\n"
                f") __paged WHERE __rn > {skip}"
            )
            return sql
    if limit is not None:
        if limit == 0 and sql.lstrip().upper().startswith("SELECT "):
            # FETCH FIRST 0 ROWS ONLY hangs on IRIS 2026.x builds; use TOP 0 instead
            head, sep, rest = sql.partition("SELECT ")
            if rest[:9].upper().startswith("DISTINCT "):
                rest = "DISTINCT TOP 0 " + rest[9:]
            else:
                rest = "TOP 0 " + rest
            sql = head + sep + rest
        else:
            sql += f"\nFETCH FIRST {limit} ROWS ONLY"
    if skip is not None:
        sql += f"\nOFFSET {skip}"
    return sql


def translate_updating_clause(upd, context, metadata):
    if isinstance(upd, ast.CreateClause):
        translate_create_clause(upd, context, metadata)
    elif isinstance(upd, ast.DeleteClause):
        translate_delete_clause(upd, context, metadata)
    elif isinstance(upd, ast.MergeClause):
        translate_merge_clause(upd, context, metadata)
    elif isinstance(upd, ast.SetClause):
        translate_set_clause(upd, context, metadata)
    elif isinstance(upd, ast.RemoveClause):
        translate_remove_clause(upd, context, metadata)


def translate_unwind_clause(unwind, context):
    expr = translate_expression(unwind.expression, context, segment="join")
    if (
        isinstance(unwind.expression, ast.Variable)
        and unwind.expression.name in context.input_params
    ):
        val = context.input_params[unwind.expression.name]
        if isinstance(val, list):
            context.join_params[-1] = json.dumps(val)
    alias = context.register_variable(unwind.alias, prefix="u")
    context.scalar_variables.add(unwind.alias)
    # UNWIND binds a scalar variable (array element)
    context.bind_variable_type(unwind.alias, "scalar")
    json_table_sql = f"JSON_TABLE({expr}, '$[*]' COLUMNS ({unwind.alias} VARCHAR(1000) PATH '$')) {alias}"
    if context.from_clauses:
        context.join_clauses.append(f"CROSS JOIN {json_table_sql}")
    else:
        context.from_clauses.append(json_table_sql)


def _extract_literal_value(v):
    """Recursively extract Python values from Literal/list/dict structures.

    Handles nested Literals within lists and dicts.
    """
    if isinstance(v, ast.Literal):
        return _extract_literal_value(v.value)
    elif isinstance(v, list):
        return [_extract_literal_value(item) for item in v]
    elif isinstance(v, dict):
        return {k: _extract_literal_value(val) for k, val in v.items()}
    else:
        return v


_TEMPORAL_CREATE_FNS = frozenset({"date", "time", "localtime", "localdatetime", "datetime", "duration"})


def _create_resolve_prop_value(v, context):
    if isinstance(v, ast.Literal):
        val = _extract_literal_value(v)
        # JSON-encode lists and dicts for storage in rdf_props
        if isinstance(val, (list, dict)):
            return json.dumps(val)
        return val
    if isinstance(v, ast.Variable) and v.name in context.input_params:
        val = context.input_params[v.name]
        # JSON-encode lists and dicts for storage in rdf_props
        if isinstance(val, (list, dict)):
            return json.dumps(val)
        return val
    if isinstance(v, ast.Variable) and getattr(context, "foreach_literals", {}).get(v.name) is not None:
        raw = context.foreach_literals[v.name]
        val = _extract_literal_value(raw)
        # JSON-encode lists and dicts for storage in rdf_props
        if isinstance(val, (list, dict)):
            return json.dumps(val)
        return val
    if isinstance(v, ast.Variable) and v.name not in context.variable_aliases:
        raise SyntaxError(f"Undefined variable: {v.name}")
    # Temporal constructors (date(), time(), datetime(), etc.) in property expressions:
    # translate to their ISO string value for storage in rdf_props.
    if isinstance(v, ast.FunctionCall) and v.function_name.lower() in _TEMPORAL_CREATE_FNS:
        try:
            sql_val = translate_expression(v, context, segment="select")
            # sql_val is a SQL string literal like '1910-05-06' — strip the quotes
            if isinstance(sql_val, str) and sql_val.startswith("'") and sql_val.endswith("'"):
                return sql_val[1:-1]
        except Exception:
            pass
    return v


def _create_node_literal(node, node_id_expr, context):
    node_id = node_id_expr.value if isinstance(node_id_expr, ast.Literal) else node_id_expr
    context.add_dml(
        f"INSERT INTO {_table('nodes')} (node_id) SELECT ? WHERE NOT EXISTS (SELECT 1 FROM {_table('nodes')} WHERE node_id = ?)",
        [node_id, node_id],
    )
    for label in node.labels:
        context.add_dml(
            f"INSERT INTO {_table('rdf_labels')} (s, label) SELECT ?, ? WHERE NOT EXISTS (SELECT 1 FROM {_table('rdf_labels')} WHERE s = ? AND label = ?)",
            [node_id, label, node_id, label],
        )
    for k, v in node.properties.items():
        val = _create_resolve_prop_value(v, context)
        if isinstance(val, ast.Variable):
            # Property value is a bound stage variable — use SELECT-based INSERT
            var_alias = context.variable_aliases[val.name]
            col_expr = f"{var_alias}.{val.name}"
            cte, sql, p = context.build_dml_subquery(
                select_override=f"SELECT ?, ?, CAST({col_expr} AS VARCHAR)"
            )
            context.add_dml(
                f'{cte}INSERT INTO {_table("rdf_props")} (s, "key", val) {sql} WHERE NOT EXISTS (SELECT 1 FROM {_table("rdf_props")} WHERE s = ? AND "key" = ?)',
                [node_id, k] + p + [node_id, k],
            )
        else:
            context.add_dml(
                f'INSERT INTO {_table("rdf_props")} (s, "key", val) SELECT ?, ?, ? WHERE NOT EXISTS (SELECT 1 FROM {_table("rdf_props")} WHERE s = ? AND "key" = ?)',
                [node_id, k, val, node_id, k],
            )


def _create_node_from_alias(node, node_id_expr, var_alias, context):
    cte, sql, p = context.build_dml_subquery(
        select_override=f"SELECT {var_alias}.{node_id_expr.name} AS node_id"
    )
    context.add_dml(
        f"{cte}INSERT INTO {_table('nodes')} (node_id) SELECT t.node_id FROM ({sql}) AS t WHERE NOT EXISTS (SELECT 1 FROM {_table('nodes')} WHERE node_id = t.node_id)",
        p,
    )
    for label in node.labels:
        context.add_dml(
            f"{cte}INSERT INTO {_table('rdf_labels')} (s, label) SELECT t.node_id, ? FROM ({sql}) AS t WHERE NOT EXISTS (SELECT 1 FROM {_table('rdf_labels')} WHERE s = t.node_id AND label = ?)",
            [label] + p + [label],
        )


def _create_clause_node_entry(node, context):
    if node.variable and node.variable in context.variable_aliases:
        return
    _raw_id = node.properties.get("id") or node.properties.get("node_id")
    # Only use the id/node_id property as the node identifier when it resolves to a string.
    # Integer literals are user-defined property values, not IVG node identifiers.
    node_id_expr = None
    _id_is_user_property = False
    if _raw_id is not None:
        if isinstance(_raw_id, ast.Literal) and isinstance(_raw_id.value, str):
            node_id_expr = _raw_id
        elif isinstance(_raw_id, ast.Variable):
            node_id_expr = _raw_id
        else:
            # Integer or other non-string literal: treat as a regular user property
            _id_is_user_property = True
    if node_id_expr is None:
        import uuid as _uuid
        generated_id = str(_uuid.uuid4())
        node_id_expr = ast.Literal(generated_id)
        key = f"__create_id_{node.variable}" if node.variable else f"__create_id_anon_{id(node)}"
        context.input_params[key] = generated_id
        if not hasattr(context, '_anon_node_keys'):
            context._anon_node_keys = {}
        context._anon_node_keys[id(node)] = generated_id

    var_alias = None
    if isinstance(node_id_expr, ast.Variable):
        var_alias = context.variable_aliases.get(node_id_expr.name)
        if not var_alias and node_id_expr.name in context.input_params:
            node_id_expr = ast.Literal(context.input_params[node_id_expr.name])
        elif not var_alias:
            raise SyntaxError(f"Undefined variable: {node_id_expr.name}")

    if isinstance(node_id_expr, ast.Variable) and var_alias:
        _create_node_from_alias(node, node_id_expr, var_alias, context)
    else:
        _create_node_literal(node, node_id_expr, context)
    if node.variable:
        context.register_variable(node.variable)
        if _id_is_user_property:
            # Track that this variable uses 'id' as a regular rdf_props entry, not node_id
            if not hasattr(context, '_id_as_property_vars'):
                context._id_as_property_vars = set()
            context._id_as_property_vars.add(node.variable)
        if not context.from_clauses and isinstance(node_id_expr, ast.Literal):
            node_id_val = node_id_expr.value
            alias = context.variable_aliases[node.variable]
            context.from_clauses.append(f"{_table('nodes')} {alias}")
            context.where_conditions.append(f"{alias}.node_id = {context.add_where_param(node_id_val)}")


def _create_clause_resolve_node_id(id_expr, node, context):
    if id_expr is None:
        if node.variable:
            stored = context.input_params.get(f"__create_id_{node.variable}")
            if stored:
                return stored
            if node.variable in context.input_params:
                return context.input_params[node.variable]
        anon_id = getattr(context, '_anon_node_keys', {}).get(id(node))
        if anon_id:
            return anon_id
        return None
    if isinstance(id_expr, ast.Literal):
        return id_expr.value
    if isinstance(id_expr, ast.Variable) and id_expr.name in context.input_params:
        return context.input_params[id_expr.name]
    if not isinstance(id_expr, ast.Variable):
        return id_expr
    return None


def _create_clause_relationship_entry(rel, i, pat, context):
    left_node, right_node = pat.nodes[i], pat.nodes[i + 1]
    # For INCOMING direction ((:A)<-[:R]-(:B)), the right node is the edge source.
    if rel.direction == ast.Direction.INCOMING:
        source_node, target_node = right_node, left_node
    else:
        source_node, target_node = left_node, right_node

    s_id_expr = source_node.properties.get("id") or source_node.properties.get("node_id")
    t_id_expr = target_node.properties.get("id") or target_node.properties.get("node_id")
    if s_id_expr is not None and isinstance(s_id_expr, ast.Literal) and not isinstance(s_id_expr.value, str):
        s_id_expr = None
    if t_id_expr is not None and isinstance(t_id_expr, ast.Literal) and not isinstance(t_id_expr.value, str):
        t_id_expr = None

    s_id = _create_clause_resolve_node_id(s_id_expr, source_node, context)
    t_id = _create_clause_resolve_node_id(t_id_expr, target_node, context)
    if s_id and t_id:
        for rt in rel.types:
            rel_props_raw = {
                k: (v.value if isinstance(v, ast.Literal) else
                    context.foreach_literals.get(v.name)
                    if isinstance(v, ast.Variable) and hasattr(context, "foreach_literals")
                    else None)
                for k, v in rel.properties.items()
            }
            # Exclude null values (null props are not stored per openCypher semantics)
            rel_props = {k: v for k, v in rel_props_raw.items() if v is not None}
            if rel_props:
                import json as _json
                # Store all values as strings — JSON_VALUE returns VARCHAR; ints stored
                # as JSON numbers are returned as NULL by IRIS SQLUser.JSON_VALUE.
                qualifiers_json = _json.dumps({k: str(v) for k, v in rel_props.items()})
                context.add_dml(
                    f"INSERT INTO {_table('rdf_edges')} (s, p, o_id, qualifiers) VALUES (?, ?, ?, ?)",
                    [s_id, rt, t_id, qualifiers_json],
                )
            else:
                context.add_dml(
                    f"INSERT INTO {_table('rdf_edges')} (s, p, o_id) VALUES (?, ?, ?)",
                    [s_id, rt, t_id],
                )
    else:
        s_alias = (
            context.variable_aliases.get(source_node.variable)
            if source_node.variable
            else None
        )
        t_alias = (
            context.variable_aliases.get(target_node.variable)
            if target_node.variable
            else None
        )
        s_expr, s_p = (
            ("?", [s_id])
            if s_id
            else (
                f"{s_alias}.{source_node.variable}"
                if s_alias and s_alias.startswith("Stage")
                else f"{s_alias}.node_id",
                [],
            )
        )
        t_expr, t_p = (
            ("?", [t_id])
            if t_id
            else (
                f"{t_alias}.{target_node.variable}"
                if t_alias and t_alias.startswith("Stage")
                else f"{t_alias}.node_id",
                [],
            )
        )
        for rt in rel.types:
            cte, sql, p = context.build_dml_subquery(
                select_override=f"SELECT {s_expr}, ?, {t_expr}"
            )
            context.add_dml(
                f"{cte}INSERT INTO {_table('rdf_edges')} (s, p, o_id) {sql}",
                s_p + [rt] + t_p + p,
            )


def translate_create_clause(create, context, metadata):
    for pat in create.patterns:
        # Validate before any DML: VariableAlreadyBound, syntax errors
        is_relationship_pattern = bool(pat.relationships)
        for node in pat.nodes:
            if node.variable and node.variable in context.variable_aliases:
                # VariableAlreadyBound: re-binding a known variable in CREATE is an error
                # if it adds new labels/props, or if it appears as a standalone CREATE (no rel).
                if node.labels or node.properties or not is_relationship_pattern:
                    raise SyntaxError(
                        f"VariableAlreadyBound: variable '{node.variable}' already bound"
                    )
        for rel in pat.relationships:
            if rel.variable and rel.variable in context.variable_aliases:
                raise SyntaxError(
                    f"VariableAlreadyBound: variable '{rel.variable}' already bound"
                )
            if not rel.types:
                raise SyntaxError("NoSingleRelationshipType: CREATE relationship must have exactly one type")
            if len(rel.types) > 1:
                raise SyntaxError("NoSingleRelationshipType: CREATE relationship must have exactly one type")
            if rel.direction == ast.Direction.BOTH:
                raise SyntaxError("RequiresDirectedRelationship: CREATE relationship must be directed")
            if rel.variable_length is not None:
                raise SyntaxError("CreatingVarLength: variable-length relationships cannot be used in CREATE")
        for node in pat.nodes:
            _create_clause_node_entry(node, context)
        for i, rel in enumerate(pat.relationships):
            _create_clause_relationship_entry(rel, i, pat, context)
            if rel.variable:
                _register_created_relationship(rel, i, pat, context)


def _register_created_relationship(rel, i, pat, context):
    """Register a named relationship created by CREATE so RETURN r works."""
    left_node, right_node = pat.nodes[i], pat.nodes[i + 1]
    if rel.direction == ast.Direction.INCOMING:
        source_node, target_node = right_node, left_node
    else:
        source_node, target_node = left_node, right_node

    def _node_id(node):
        if node.variable:
            nid = context.input_params.get(f"__create_id_{node.variable}")
            if nid:
                return nid
            return context.input_params.get(node.variable)
        return context.input_params.get(f"__create_id_anon_{id(node)}")

    s_id = _node_id(source_node)
    t_id = _node_id(target_node)
    rel_type = rel.types[0] if rel.types else None
    e_alias = context.register_variable(rel.variable, prefix="e")
    if s_id and t_id and rel_type:
        # Store identity for UNWIND+CREATE relationship tracking in _tts_finalize_context
        if rel.variable:
            context.input_params[f"__create_edge_{rel.variable}"] = (s_id, rel_type, t_id)
        if not context.from_clauses:
            context.from_clauses.append(f"{_table('rdf_edges')} {e_alias}")
        else:
            context.join_clauses.append(
                f"JOIN {_table('rdf_edges')} {e_alias} ON "
                f"{e_alias}.s = {context.add_join_param(s_id)}"
                f" AND {e_alias}.p = {context.add_join_param(rel_type)}"
                f" AND {e_alias}.o_id = {context.add_join_param(t_id)}"
            )
            return
        context.where_conditions.append(
            f"{e_alias}.s = {context.add_where_param(s_id)}"
            f" AND {e_alias}.p = {context.add_where_param(rel_type)}"
            f" AND {e_alias}.o_id = {context.add_where_param(t_id)}"
        )


def translate_delete_clause(delete, context, metadata):
    for var in delete.expressions:
        alias = context.variable_aliases.get(var.name)
        if not alias:
            raise SyntaxError(f"Undefined variable: {var.name}")

        # Detect whether the variable is a relationship (edge), taking into account
        # variables promoted to a CTE stage via WITH.
        is_edge_var = (
            alias.startswith("e")
            or var.name in getattr(context, "edge_stage_variables", set())
        )
        # When alias is a CTE stage (e.g. "Stage1"), the node_id column is named
        # after the variable (e.g. "n"), not "node_id".
        stage_names = {s.split(" AS ")[0].strip() for s in getattr(context, "stages", [])}
        is_stage_alias = alias in stage_names

        if is_edge_var and is_stage_alias:
            # Relationship variable promoted through WITH into a CTE stage.
            # The Stage SELECT now includes __edge_<var>_s/p/o identity columns;
            # use them to reconstruct the edge identity for deletion.
            s_col = f"__edge_{var.name}_s"
            p_col = f"__edge_{var.name}_p"
            o_col = f"__edge_{var.name}_o"
            cte_s, subquery_s, subparams_s = context.build_dml_subquery(
                select_override=f"SELECT {alias}.{s_col}"
            )
            _, subquery_p, _ = context.build_dml_subquery(
                select_override=f"SELECT {alias}.{p_col}"
            )
            _, subquery_o, _ = context.build_dml_subquery(
                select_override=f"SELECT {alias}.{o_col}"
            )
            # All three calls return the same CTE and params (same Stage1 binding).
            # The CTE appears once in the SQL; params are bound once.
            context.add_dml(
                f"{cte_s}DELETE FROM {_table('rdf_edges')} WHERE "
                f"s IN ({subquery_s}) AND p IN ({subquery_p}) AND o_id IN ({subquery_o})",
                subparams_s,
            )
            return

        node_col = var.name if is_stage_alias else "node_id"
        cte, subquery, subparams = context.build_dml_subquery(
            select_override=f"SELECT {alias}.{node_col}"
        )
        # When a CTE is present, the subquery is a bare reference (no ?); params are CTE-only
        # and used once. When no CTE, the subquery has its own ? for each IN clause.
        dual_params = subparams if cte else subparams + subparams
        if delete.detach:
            context.add_dml(
                f"{cte}DELETE FROM {_table('rdf_edges')} WHERE s IN ({subquery}) OR o_id IN ({subquery})",
                dual_params,
            )
        elif not is_edge_var:
            # Non-DETACH DELETE: guard against connected nodes (Cypher constraint).
            # Stored as a sentinel SQL so execute_transaction can raise the right error.
            context.add_dml(
                f"__constraint_check_delete_connected__ {cte}SELECT COUNT(*) FROM {_table('rdf_edges')} WHERE s IN ({subquery}) OR o_id IN ({subquery})",
                dual_params,
            )
        if not is_edge_var:
            context.add_dml(
                f"{cte}DELETE FROM {_table('rdf_labels')} WHERE s IN ({subquery})", subparams
            )
            context.add_dml(
                f"{cte}DELETE FROM {_table('rdf_props')} WHERE s IN ({subquery})", subparams
            )
            context.add_dml(
                f"{cte}DELETE FROM {_table('kg_NodeEmbeddings')} WHERE id IN ({subquery})",
                subparams,
            )
            context.add_dml(
                f"{cte}DELETE FROM {_table('nodes')} WHERE node_id IN ({subquery})",
                subparams,
            )
        else:
            is_undirected = alias in getattr(context, "_undirected_aliases", set())
            s_col = "_src" if is_undirected else "s"
            p_col = "_p" if is_undirected else "p"
            o_col = "_dst" if is_undirected else "o_id"
            cte_s, subquery_s, subparams_s = context.build_dml_subquery(
                select_override=f"SELECT {alias}.{s_col}"
            )
            cte_p, subquery_p, subparams_p = context.build_dml_subquery(
                select_override=f"SELECT {alias}.{p_col}"
            )
            cte_o, subquery_o, subparams_o = context.build_dml_subquery(
                select_override=f"SELECT {alias}.{o_col}"
            )
            context.add_dml(
                f"{cte_s}DELETE FROM {_table('rdf_edges')} WHERE "
                f"s IN ({subquery_s}) AND p IN ({subquery_p}) AND o_id IN ({subquery_o})",
                subparams_s + subparams_p + subparams_o,
            )


def _merge_pattern_existence_sql(merge_node):
    """Build the NOT EXISTS sub-SELECT that checks whether any node already matches
    the MERGE pattern (labels + properties).  Returns (sql_fragment, params_list).

    The fragment is suitable for use in:
        INSERT INTO nodes (node_id) SELECT ? WHERE NOT EXISTS (<fragment>)
    """
    labels = merge_node.labels if merge_node else []
    props = merge_node.properties if merge_node else {}

    if not labels and not props:
        # No constraints — any node in the graph matches; check by a sentinel always-true.
        return f"SELECT 1 FROM {_table('nodes')} WHERE 1=1", []

    joins = []
    params: list = []
    for i, label in enumerate(labels):
        alias = f"_ml{i}"
        if i == 0:
            joins.append(f"{_table('nodes')} {alias}")
        else:
            # Additional label: re-join rdf_labels on same node
            first_alias = "_ml0"
            l_alias = f"_ml{i}"
            joins.append(
                f"JOIN {_table('rdf_labels')} {l_alias} ON "
                f"{l_alias}.s = _ml0.node_id AND {l_alias}.label = ?"
            )
            params.append(label)

    if labels:
        # Primary label checked via rdf_labels
        primary_label = labels[0]
        lbl0_join = (
            f"SELECT 1 FROM {_table('nodes')} _ml0 "
            f"JOIN {_table('rdf_labels')} _lbl0 ON _lbl0.s = _ml0.node_id AND _lbl0.label = ?"
        )
        params_prefix = [primary_label]
        extra_label_joins = ""
        for i, label in enumerate(labels[1:], start=1):
            l_alias = f"_ml{i}"
            extra_label_joins += (
                f" JOIN {_table('rdf_labels')} {l_alias} ON "
                f"{l_alias}.s = _ml0.node_id AND {l_alias}.label = ?"
            )
            params_prefix.append(label)
        prop_joins = ""
        prop_params: list = []
        for ki, (k, v) in enumerate(props.items()):
            val = v.value if isinstance(v, ast.Literal) else v
            p_alias = f"_mp{ki}"
            prop_joins += (
                f' JOIN {_table("rdf_props")} {p_alias} ON '
                f'{p_alias}.s = _ml0.node_id AND {p_alias}."key" = ? AND {p_alias}.val = ?'
            )
            prop_params.extend([k, str(val)])
        return (
            lbl0_join + extra_label_joins + prop_joins,
            params_prefix + prop_params,
        )

    # No labels, only properties
    prop_joins_parts = []
    prop_params = []
    for ki, (k, v) in enumerate(props.items()):
        val = v.value if isinstance(v, ast.Literal) else v
        p_alias = f"_mp{ki}"
        if ki == 0:
            prop_joins_parts.append(
                f"SELECT 1 FROM {_table('nodes')} _ml0 "
                f'JOIN {_table("rdf_props")} {p_alias} ON '
                f'{p_alias}.s = _ml0.node_id AND {p_alias}."key" = ? AND {p_alias}.val = ?'
            )
        else:
            prop_joins_parts.append(
                f'JOIN {_table("rdf_props")} {p_alias} ON '
                f'{p_alias}.s = _ml0.node_id AND {p_alias}."key" = ? AND {p_alias}.val = ?'
            )
        prop_params.extend([k, str(val)])
    return " ".join(prop_joins_parts), prop_params


def translate_merge_clause(merge, context, metadata):
    # Snapshot context state before translate_create_clause so we can replace the
    # UUID-based DMLs and WHERE with label/property-based equivalents.
    _pre_dml_len = len(context.dml_statements)
    _pre_from_len = len(context.from_clauses)
    _pre_where_len = len(context.where_conditions)
    _pre_where_params_len = len(context.where_params)

    translate_create_clause(ast.CreateClause(patterns=[merge.pattern]), context, metadata)

    # --- Rewrite DML + SELECT for single-node MERGE patterns ---
    # translate_create_clause generates INSERT ... WHERE NOT EXISTS (node_id = <uuid>).
    # For MERGE we need INSERT ... WHERE NOT EXISTS (<label/prop pattern match>)
    # so that a pre-existing node prevents a new node from being created.
    merge_node = merge.pattern.nodes[0] if merge.pattern.nodes else None
    if merge_node is not None and not merge.pattern.relationships:
        var_name = merge_node.variable
        node_alias = context.variable_aliases.get(var_name) if var_name else None
        generated_uuid = (
            context.input_params.get(f"__create_id_{var_name}")
            if var_name else None
        )
        if generated_uuid is None and hasattr(context, '_anon_node_keys'):
            generated_uuid = context._anon_node_keys.get(id(merge_node))

        # Check whether translate_create_clause added UUID-based from/where entries.
        added_froms = context.from_clauses[_pre_from_len:]
        added_wheres = context.where_conditions[_pre_where_len:]
        _has_uuid_from = (
            len(added_froms) == 1
            and node_alias
            and f"{_table('nodes')} {node_alias}" in added_froms[0]
        )
        _has_uuid_where = (
            len(added_wheres) == 1
            and node_alias
            and f"{node_alias}.node_id = ?" in added_wheres[0]
        )

        exist_sql, exist_params = _merge_pattern_existence_sql(merge_node)
        new_uuid = generated_uuid

        if new_uuid and (_has_uuid_from or _has_uuid_where or True):
            # Replace UUID-based node DML statements with label/prop-aware equivalents.
            # We only touch the DML added by translate_create_clause for THIS merge node.
            added_dmls = context.dml_statements[_pre_dml_len:]
            new_dmls = []
            for sql, params in added_dmls:
                if "INSERT INTO " + _table("nodes") in sql and new_uuid in str(params):
                    # Replace "WHERE NOT EXISTS (SELECT 1 FROM nodes WHERE node_id = ?)"
                    # with a pattern-based check so existing matching nodes block the INSERT.
                    new_dmls.append((
                        f"INSERT INTO {_table('nodes')} (node_id) SELECT ? "
                        f"WHERE NOT EXISTS ({exist_sql})",
                        [new_uuid] + exist_params,
                    ))
                elif "INSERT INTO " + _table("rdf_labels") in sql and new_uuid in str(params):
                    # The rdf_labels insert must also be guarded by pattern existence.
                    # Extract the label from the original params (second param).
                    label_val = params[1] if len(params) > 1 else None
                    if label_val is not None:
                        new_dmls.append((
                            f"INSERT INTO {_table('rdf_labels')} (s, label) SELECT ?, ? "
                            f"WHERE NOT EXISTS ({exist_sql})",
                            [new_uuid, label_val] + exist_params,
                        ))
                    else:
                        new_dmls.append((sql, params))
                else:
                    new_dmls.append((sql, params))

            # Swap out the DML statements.
            del context.dml_statements[_pre_dml_len:]
            context.dml_statements.extend(new_dmls)

        # --- Fix SELECT query to find the node by label/property, not by the new UUID ---
        if _has_uuid_from and _has_uuid_where:
            del context.from_clauses[_pre_from_len:]
            del context.where_conditions[_pre_where_len:]
            del context.where_params[_pre_where_params_len:]

            # Re-add FROM nodes + label JOINs + property JOINs so the SELECT finds the
            # matching node whether it was just created or already existed.
            context.from_clauses.append(f"{_table('nodes')} {node_alias}")
            for label in merge_node.labels:
                l_alias = context.next_alias("l")
                context.join_clauses.append(
                    f"JOIN {_table('rdf_labels')} {l_alias} ON "
                    f"{l_alias}.s = {node_alias}.node_id AND "
                    f"{l_alias}.label = {context.add_join_param(label)}"
                )
            for k, v in merge_node.properties.items():
                val = v.value if isinstance(v, ast.Literal) else (
                    context.input_params.get(v.name) if isinstance(v, ast.Variable) else v
                )
                p_alias = context.next_alias("p")
                context.join_clauses.append(
                    f'JOIN {_table("rdf_props")} {p_alias} ON '
                    f'{p_alias}.s = {node_alias}.node_id AND '
                    f'{p_alias}."key" = {context.add_join_param(k)} AND '
                    f'{p_alias}.val = {context.add_join_param(str(val))}'
                )

    var = merge.pattern.nodes[0].variable if merge.pattern.nodes else None
    for action, is_create in [(merge.on_create, True), (merge.on_match, False)]:
        if action:
            for item in action.items:
                if isinstance(item, ast.SetItem) and isinstance(
                    item.expression, ast.PropertyReference
                ):
                    var_name = item.expression.variable
                    sql_alias = context.variable_aliases.get(var_name, "")
                    actual_id = (
                        context.input_params.get(f"__create_id_{var_name}")
                        or context.input_params.get(var_name)
                    )
                    k, v = item.expression.property_name, item.value
                    val = v.value if isinstance(v, ast.Literal) else v
                    if is_create:
                        if actual_id:
                            context.add_dml(
                                f'INSERT INTO {_table("rdf_props")} (s, "key", val) SELECT ?, ?, ? WHERE NOT EXISTS (SELECT 1 FROM {_table("rdf_props")} WHERE s = ? AND "key" = ?)',
                                [actual_id, k, val, actual_id, k],
                            )
                        else:
                            context.add_dml(
                                f'INSERT INTO {_table("rdf_props")} (s, "key", val) SELECT node_id, ?, ? FROM {_table("nodes")} WHERE node_id = ? AND NOT EXISTS (SELECT 1 FROM {_table("rdf_props")} WHERE s = ? AND "key" = ?)',
                                [k, val, sql_alias, sql_alias, k],
                            )
                    else:
                        if actual_id:
                            context.add_dml(
                                f'UPDATE {_table("rdf_props")} SET val = ? WHERE s = ? AND "key" = ?',
                                [val, actual_id, k],
                            )
                        else:
                            context.add_dml(
                                f'UPDATE {_table("rdf_props")} SET val = ? WHERE s IN (SELECT node_id FROM {_table("nodes")} WHERE node_id = ?) AND "key" = ?',
                                [val, sql_alias, k],
                            )


def translate_set_clause(set_cl, context, metadata):
    for item in set_cl.items:
        if isinstance(item.expression, ast.Variable) and getattr(item, "merge", False):
            alias = context.variable_aliases.get(item.expression.name)
            cte, subquery, subparams = context.build_dml_subquery(
                select_override=f"SELECT {alias}.node_id"
            )
            val_expr = item.value
            if isinstance(val_expr, ast.Variable) and val_expr.name in context.input_params:
                map_val = context.input_params[val_expr.name]
                if isinstance(map_val, dict):
                    for k, v in map_val.items():
                        context.add_dml(
                            f'{cte}UPDATE {_table("rdf_props")} SET val = ? WHERE s IN ({subquery}) AND "key" = ?',
                            [v] + subparams + [k],
                        )
                        context.add_dml(
                            f'{cte}INSERT INTO {_table("rdf_props")} (s, "key", val) SELECT node_id, ?, ? FROM {_table("nodes")} WHERE node_id IN ({subquery}) AND NOT EXISTS (SELECT 1 FROM {_table("rdf_props")} WHERE s = {_table("nodes")}.node_id AND "key" = ?)',
                            [k, v] + subparams + [k],
                        )
            elif isinstance(val_expr, ast.MapLiteral):
                for k, v in val_expr.entries.items():
                    val = v.value if isinstance(v, ast.Literal) else context.input_params.get(v.name) if isinstance(v, ast.Variable) else v
                    context.add_dml(
                        f'{cte}UPDATE {_table("rdf_props")} SET val = ? WHERE s IN ({subquery}) AND "key" = ?',
                        [val] + subparams + [k],
                    )
                    context.add_dml(
                        f'{cte}INSERT INTO {_table("rdf_props")} (s, "key", val) SELECT node_id, ?, ? FROM {_table("nodes")} WHERE node_id IN ({subquery}) AND NOT EXISTS (SELECT 1 FROM {_table("rdf_props")} WHERE s = {_table("nodes")}.node_id AND "key" = ?)',
                        [k, val] + subparams + [k],
                    )
        elif isinstance(item.expression, ast.PropertyReference):
            alias, k, v = (
                context.variable_aliases.get(item.expression.variable),
                item.expression.property_name,
                item.value,
            )
            val = v.value if isinstance(v, ast.Literal) else v
            cte, subquery, subparams = context.build_dml_subquery(
                select_override=f"SELECT {alias}.node_id"
            )
            context.add_dml(
                f'{cte}UPDATE {_table("rdf_props")} SET val = ? WHERE s IN ({subquery}) AND "key" = ?',
                [val] + subparams + [k],
            )
            context.add_dml(
                f'{cte}INSERT INTO {_table("rdf_props")} (s, "key", val) SELECT node_id, ?, ? FROM {_table("nodes")} WHERE node_id IN ({subquery}) AND NOT EXISTS (SELECT 1 FROM {_table("rdf_props")} WHERE s = {_table("nodes")}.node_id AND "key" = ?)',
                [k, val] + subparams + [k],
            )
        elif isinstance(item.expression, ast.Variable):
            alias, label = (
                context.variable_aliases.get(item.expression.name),
                str(
                    item.value.value
                    if isinstance(item.value, ast.Literal)
                    else item.value
                ),
            )
            cte, subquery, subparams = context.build_dml_subquery(
                select_override=f"SELECT {alias}.node_id"
            )
            context.add_dml(
                f"{cte}INSERT INTO {_table('rdf_labels')} (s, label) SELECT node_id, ? FROM {_table('nodes')} WHERE node_id IN ({subquery}) AND NOT EXISTS (SELECT 1 FROM {_table('rdf_labels')} WHERE s = {_table('nodes')}.node_id AND label = ?)",
                [label] + subparams + [label],
            )


def translate_remove_clause(remove, context, metadata):
    for item in remove.items:
        if isinstance(item.expression, ast.Variable) and item.label:
            alias = context.variable_aliases.get(item.expression.name)
            cte, subquery, subparams = context.build_dml_subquery(
                select_override=f"SELECT {alias}.node_id"
            )
            context.add_dml(
                f"{cte}DELETE FROM {_table('rdf_labels')} WHERE s IN ({subquery}) AND label = ?",
                subparams + [item.label],
            )
        elif isinstance(item.expression, ast.PropertyReference):
            alias, k = (
                context.variable_aliases.get(item.expression.variable),
                item.expression.property_name,
            )
            cte, subquery, subparams = context.build_dml_subquery(
                select_override=f"SELECT {alias}.node_id"
            )
            context.add_dml(
                f'{cte}DELETE FROM {_table("rdf_props")} WHERE s IN ({subquery}) AND "key" = ?',
                subparams + [k],
            )


def translate_match_clause(match_clause, context, metadata):
    # For OPTIONAL MATCH: snapshot the set of alias strings already bound before
    # this clause starts.  _trp_directed_edge uses this to choose the correct null
    # guard: "edge IS NULL" when the source was bound before the optional started
    # (the source can be legitimately non-null while the edge is absent), vs
    # "source IS NULL" when the source was introduced inside this optional (meaning
    # the source only exists if the full optional path was found).
    if match_clause.optional:
        context.optional_prebound_aliases = set(context.variable_aliases.values())
    else:
        context.optional_prebound_aliases = set()
    # Validate no duplicate variables within the same MATCH clause (across all patterns)
    vars_in_match = set()
    for pattern in match_clause.patterns:
        if not pattern.nodes:
            continue
        # Check for node back-references within a pattern chain (e.g. (n)-->(n) self-loop).
        # These are VALID in Cypher — they constrain start/end to the same node.
        # Track them so we can add self-loop WHERE constraints below.
        node_vars_in_pattern: dict = {}  # var -> first index
        self_loop_vars: set = set()  # vars that appear twice (back-reference)
        for idx_n, node in enumerate(pattern.nodes):
            if node.variable:
                if node.variable in node_vars_in_pattern:
                    # Back-reference (self-loop) — valid in Cypher
                    self_loop_vars.add(node.variable)
                else:
                    node_vars_in_pattern[node.variable] = idx_n
                # Also check if this variable was already seen in this MATCH clause
                if node.variable in vars_in_match and node.variable not in self_loop_vars:
                    raise CypherParseError(
                        f"VariableAlreadyBound: variable '{node.variable}' is already bound "
                        f"in this MATCH clause"
                    )
                vars_in_match.add(node.variable)
        # Track rel vars in a separate set (rel vars can't be duplicated)
        rel_vars_in_pattern: set = set()
        for rel in pattern.relationships:
            if rel.variable:
                if rel.variable in rel_vars_in_pattern:
                    raise CypherParseError(
                        f"VariableAlreadyBound: variable '{rel.variable}' appears twice "
                        f"in the same pattern"
                    )
                rel_vars_in_pattern.add(rel.variable)
                # Also check if this variable was already seen in this MATCH clause
                if rel.variable in vars_in_match:
                    raise CypherParseError(
                        f"VariableAlreadyBound: variable '{rel.variable}' is already bound "
                        f"in this MATCH clause"
                    )
                vars_in_match.add(rel.variable)
        first_node = pattern.nodes[0]
        # Skip upfront node join when the first node is unbound but the pattern's
        # last node IS already bound.  translate_relationship_pattern will anchor
        # the edge on the bound target and join this node from the edge (direction-
        # symmetry fix).  Without this guard, translate_node_pattern emits a CROSS
        # JOIN that produces wrong results for (t)-[:R]->(f_bound).
        first_is_unbound = (
            first_node.variable is not None
            and first_node.variable not in context.variable_aliases
        )
        last_node_bound = (
            pattern.nodes
            and pattern.nodes[-1].variable
            and pattern.nodes[-1].variable in context.variable_aliases
        )
        skip_first_node_join = (
            first_is_unbound
            and last_node_bound
            and bool(pattern.relationships)
        )
        has_rels = bool(pattern.relationships)
        if not skip_first_node_join:
            if first_node.variable:
                translate_node_pattern(
                    first_node, context, metadata, optional=match_clause.optional
                )
            elif first_node.labels or first_node.properties:
                if not has_rels:
                    # Standalone anonymous labeled/propertied node — translate normally.
                    translate_node_pattern(
                        first_node, context, metadata, optional=match_clause.optional
                    )
                else:
                    # Anonymous labeled source in a relationship — edge join handles it.
                    # Adding a standalone nodes JOIN here would create a Cartesian product.
                    _ = context.next_alias("n")
            else:
                _ = context.next_alias("n")
        # Track (edge_alias, is_undirected) for each hop in this pattern — used
        # after each hop to add isomorphic-edge-exclusion WHERE conditions.
        # Cypher guarantees the same physical edge cannot be traversed twice in
        # a single path pattern.
        _pattern_edge_aliases: list = []  # list of (alias, is_undirected)

        for i, rel in enumerate(pattern.relationships):
            src_node = pattern.nodes[i]
            tgt_node = pattern.nodes[i + 1]
            translate_relationship_pattern(
                rel,
                src_node,
                tgt_node,
                context,
                metadata,
                optional=match_clause.optional,
            )
            last_node = tgt_node
            is_back_ref = (
                src_node.variable
                and tgt_node.variable
                and src_node.variable == tgt_node.variable
            )
            if is_back_ref:
                # Self-loop: both ends are the same node — add edge self-loop constraint.
                src_alias = context.variable_aliases.get(src_node.variable)
                edge_alias = context.rel_obj_aliases.get(id(rel))
                if src_alias and edge_alias:
                    if rel.direction == ast.Direction.BOTH:
                        # Undirected edge: _src and _dst are the same node
                        context.where_conditions.append(
                            f"{edge_alias}._src = {edge_alias}._dst"
                        )
                    else:
                        # Directed edge: s and o_id are the same node
                        context.where_conditions.append(
                            f"{edge_alias}.s = {edge_alias}.o_id"
                        )
            elif last_node.variable:
                translate_node_pattern(
                    last_node, context, metadata, optional=match_clause.optional
                )
            elif not (last_node.labels or last_node.properties):
                pass  # truly anonymous target — edge join covers it
            # else: anonymous labeled/propertied target — _trp_directed_edge already
            # added label JOINs against the edge-joined alias; no standalone JOIN needed.

            # Isomorphic edge exclusion: this hop's physical edge must differ from
            # every previous hop's physical edge in this pattern.
            new_ea = context.rel_obj_aliases.get(id(rel))
            if new_ea and _pattern_edge_aliases:
                new_is_und = new_ea in context._undirected_aliases
                # Physical identity columns for the new edge:
                if new_is_und:
                    # Undirected: CTE exposes _os/_oo as physical s/o_id
                    new_s = f"{new_ea}._os"
                    new_p = f"{new_ea}._p"
                    new_o = f"{new_ea}._oo"
                else:
                    new_s = f"{new_ea}.s"
                    new_p = f"{new_ea}.p"
                    new_o = f"{new_ea}.o_id"
                for prev_ea, prev_is_und in _pattern_edge_aliases:
                    if prev_is_und:
                        prev_s = f"{prev_ea}._os"
                        prev_p = f"{prev_ea}._p"
                        prev_o = f"{prev_ea}._oo"
                    else:
                        prev_s = f"{prev_ea}.s"
                        prev_p = f"{prev_ea}.p"
                        prev_o = f"{prev_ea}.o_id"
                    # Isomorphic-edge exclusion: the same physical edge cannot be
                    # traversed twice.  When in an OPTIONAL MATCH, either edge may be
                    # null (LEFT OUTER JOIN returned no row).  NULL comparisons evaluate
                    # to NULL rather than FALSE, which would incorrectly filter null rows.
                    # Guard with IS NULL checks so null hops always pass the constraint.
                    is_opt = match_clause.optional
                    excl = (
                        f"NOT ({new_s} = {prev_s} AND {new_p} = {prev_p} AND {new_o} = {prev_o})"
                    )
                    if is_opt:
                        excl = f"({excl} OR {new_s} IS NULL OR {prev_s} IS NULL)"
                    context.where_conditions.append(excl)
            if new_ea:
                new_is_und = new_ea in context._undirected_aliases
                _pattern_edge_aliases.append((new_ea, new_is_und))

    for np in match_clause.named_paths:
        context.named_paths[np.variable] = np
        # Track path variable type for semantic validation
        context.bind_variable_type(np.variable, "path")
        node_aliases = [
            context.variable_aliases.get(n.variable, f"n{i}")
            for i, n in enumerate(np.pattern.nodes)
        ]
        # For relationships: first try the variable alias, then look up by object id
        edge_aliases = []
        for i, r in enumerate(np.pattern.relationships):
            if r.variable:
                # Named relationship: use its registered alias
                alias = context.variable_aliases.get(r.variable, f"e{i}")
            else:
                # Anonymous relationship: look up by object id from _trp_setup_aliases tracking
                alias = context.rel_obj_aliases.get(id(r), f"e{i}")
            edge_aliases.append(alias)
        context.path_node_aliases[np.variable] = node_aliases
        context.path_edge_aliases[np.variable] = edge_aliases


def _subquery_correlated_scalar(subquery, inner, child_ctx, context):
    ret_item = inner.return_clause.items[0]
    alias = ret_item.alias or "sub_result"
    inner_expr = translate_expression(ret_item.expression, child_ctx, segment="select")
    inner_sql_parts = [f"SELECT {inner_expr}"]
    if child_ctx.from_clauses:
        inner_sql_parts.append(f"FROM {', '.join(child_ctx.from_clauses)}")
        if child_ctx.join_clauses:
            inner_sql_parts.extend(child_ctx.join_clauses)
    elif child_ctx.join_clauses:
        first_join = (
            child_ctx.join_clauses[0]
            .replace("JOIN ", "", 1)
            .replace("CROSS JOIN ", "", 1)
        )
        on_idx = first_join.find(" ON ")
        if on_idx > 0:
            from_part = first_join[:on_idx]
            on_part = first_join[on_idx + 4 :]
            inner_sql_parts.append(f"FROM {from_part}")
            if child_ctx.join_clauses[1:]:
                inner_sql_parts.extend(child_ctx.join_clauses[1:])
            if child_ctx.where_conditions:
                child_ctx.where_conditions.insert(0, on_part)
            else:
                child_ctx.where_conditions.append(on_part)
        else:
            inner_sql_parts.append(f"FROM {first_join}")
            if child_ctx.join_clauses[1:]:
                inner_sql_parts.extend(child_ctx.join_clauses[1:])
    if child_ctx.where_conditions:
        inner_sql_parts.append(f"WHERE {' AND '.join(child_ctx.where_conditions)}")
    scalar_sql = "\n".join(inner_sql_parts)
    all_params = child_ctx.select_params + child_ctx.join_params + child_ctx.where_params
    for p in all_params:
        context.select_params.append(p)
    context.select_items.append(f"COALESCE(({scalar_sql}), 0) AS {alias}")
    context.scalar_variables.add(alias)
    context.variable_aliases[alias] = "scalar"


def _subquery_lateral_inline_param(val):
    if isinstance(val, str):
        return f"'{val.replace(chr(39), chr(39) + chr(39))}'"
    if isinstance(val, bool):
        return "1" if val else "0"
    if val is None:
        return "NULL"
    return str(val)


def _subquery_correlated_lateral(subquery, inner, context, metadata):
    child_ctx_lateral = TranslationContext()
    child_ctx_lateral.input_params = context.input_params
    child_ctx_lateral._alias_counter = context._alias_counter
    for var in subquery.import_variables:
        child_ctx_lateral.variable_aliases[var] = context.variable_aliases[var]

    child_ctx_lateral.add_join_param = _subquery_lateral_inline_param
    child_ctx_lateral.add_where_param = _subquery_lateral_inline_param
    child_ctx_lateral.add_select_param = _subquery_lateral_inline_param

    for part in inner.query_parts:
        for clause in part.clauses:
            if isinstance(clause, ast.MatchClause):
                translate_match_clause(clause, child_ctx_lateral, metadata)
            elif isinstance(clause, ast.WhereClause):
                translate_where_clause(clause, child_ctx_lateral)
    translate_return_clause(inner.return_clause, child_ctx_lateral)
    if not child_ctx_lateral.from_clauses and child_ctx_lateral.join_clauses:
        first_jc = child_ctx_lateral.join_clauses[0]
        for prefix in ("CROSS JOIN ", "LEFT OUTER JOIN ", "JOIN "):
            if first_jc.startswith(prefix):
                rest = first_jc[len(prefix):]
                on_idx = rest.find(" ON ")
                if on_idx > 0:
                    table_part = rest[:on_idx]
                    cond_part = rest[on_idx + 4:]
                    child_ctx_lateral.from_clauses.append(table_part)
                    if cond_part.strip() and cond_part.strip() != "1=1":
                        child_ctx_lateral.where_conditions.insert(0, cond_part)
                    child_ctx_lateral.join_clauses = child_ctx_lateral.join_clauses[1:]
                else:
                    child_ctx_lateral.from_clauses.append(rest)
                    child_ctx_lateral.join_clauses = child_ctx_lateral.join_clauses[1:]
                break
    inner_sql_parts_lat = [
        f"SELECT {'DISTINCT ' if inner.return_clause.distinct else ''}{', '.join(child_ctx_lateral.select_items)}"
    ]
    if child_ctx_lateral.from_clauses:
        inner_sql_parts_lat.append(f"FROM {', '.join(child_ctx_lateral.from_clauses)}")
    if child_ctx_lateral.join_clauses:
        inner_sql_parts_lat.extend(child_ctx_lateral.join_clauses)
    if child_ctx_lateral.where_conditions:
        inner_sql_parts_lat.append(f"WHERE {' AND '.join(child_ctx_lateral.where_conditions)}")
    inner_sql = "\n".join(inner_sql_parts_lat)
    lat_alias = context.next_alias("lat")
    context.join_clauses.append(f"CROSS JOIN LATERAL (\n{inner_sql}\n) {lat_alias}")
    for item in inner.return_clause.items:
        col_alias = item.alias
        if col_alias is None:
            if isinstance(item.expression, ast.Variable):
                col_alias = item.expression.name
            elif isinstance(item.expression, ast.PropertyReference):
                col_alias = f"{item.expression.variable}_{item.expression.property_name}"
            else:
                col_alias = f"col_{len(context.scalar_variables)}"
        if col_alias:
            context.variable_aliases[col_alias] = lat_alias
            context.scalar_variables.add(col_alias)


def _subquery_correlated(subquery, inner, context, metadata):
    if not inner.return_clause:
        raise ValueError("Correlated subquery requires a RETURN clause")

    child_ctx = TranslationContext()
    child_ctx.input_params = context.input_params
    child_ctx._alias_counter = context._alias_counter

    for var in subquery.import_variables:
        if var not in context.variable_aliases:
            raise ValueError(f"Imported variable '{var}' is not defined in outer scope")
        child_ctx.variable_aliases[var] = context.variable_aliases[var]

    for part in inner.query_parts:
        for clause in part.clauses:
            if isinstance(clause, ast.MatchClause):
                translate_match_clause(clause, child_ctx, metadata)
            elif isinstance(clause, ast.WhereClause):
                translate_where_clause(clause, child_ctx)

    num_return_cols = len(inner.return_clause.items)
    is_single_scalar = num_return_cols == 1 and isinstance(
        inner.return_clause.items[0].expression, (ast.AggregationFunction,)
    )

    if is_single_scalar:
        _subquery_correlated_scalar(subquery, inner, child_ctx, context)
    else:
        _subquery_correlated_lateral(subquery, inner, context, metadata)


def _subquery_uncorrelated(subquery, inner, context, metadata):
    child_ctx = TranslationContext()
    child_ctx.input_params = context.input_params

    for part in inner.query_parts:
        for clause in part.clauses:
            if isinstance(clause, ast.MatchClause):
                translate_match_clause(clause, child_ctx, metadata)
            elif isinstance(clause, ast.WhereClause):
                translate_where_clause(clause, child_ctx)
            elif isinstance(clause, ast.UnwindClause):
                translate_unwind_clause(clause, child_ctx)

    if inner.return_clause:
        translate_return_clause(inner.return_clause, child_ctx)

    inner_sql, inner_params = child_ctx.build_stage_sql(
        inner.return_clause.distinct if inner.return_clause else False
    )

    cte_name = f"SubQuery{len(context.stages)}"
    context.all_stage_params.extend(inner_params)
    context.stages.append(f"{cte_name} AS (\n{inner_sql}\n)")

    if not context.from_clauses:
        context.from_clauses.append(cte_name)
    else:
        context.join_clauses.append(f"CROSS JOIN {cte_name}")

    if inner.return_clause:
        for item in inner.return_clause.items:
            alias = item.alias
            if alias is None:
                if isinstance(item.expression, ast.Variable):
                    alias = item.expression.name
                elif isinstance(item.expression, ast.PropertyReference):
                    alias = f"{item.expression.variable}_{item.expression.property_name}"
                elif isinstance(item.expression, (ast.AggregationFunction, ast.FunctionCall)):
                    alias = f"{item.expression.function_name}_res"
            if alias:
                context.variable_aliases[alias] = cte_name
                context.scalar_variables.add(alias)


def translate_subquery_call(
    subquery: ast.SubqueryCall, context: TranslationContext, metadata
):
    inner = subquery.inner_query
    is_correlated = len(subquery.import_variables) > 0
    if is_correlated:
        _subquery_correlated(subquery, inner, context, metadata)
    else:
        _subquery_uncorrelated(subquery, inner, context, metadata)


def translate_node_pattern(node, context, metadata, optional=False):
    if node.variable and node.variable in context.variable_aliases:
        # Validate type consistency: if variable was previously bound to a different type, error
        context.bind_variable_type(node.variable, "node")
        # Node already registered (e.g. as far-end of a relationship JOIN), but
        # labels and properties declared on this node pattern still need to be
        # applied as filter JOINs against the already-registered alias.
        if node.labels or node.properties:
            alias = context.variable_aliases[node.variable]
            jt = "LEFT OUTER JOIN" if optional else "JOIN"
            for label in node.labels:
                l_alias = context.next_alias("l")
                context.join_clauses.append(
                    f"{jt} {_table('rdf_labels')} {l_alias} ON {l_alias}.s = {alias}.node_id AND {l_alias}.label = {context.add_join_param(label)}"
                )
                if not optional:
                    context.where_conditions.append(f"{l_alias}.s IS NOT NULL")
            for k, v in node.properties.items():
                val_sql = translate_expression(v, context, segment="where")
                if k == "node_id":
                    context.where_conditions.append(f"{alias}.node_id = {val_sql}")
                else:
                    if not optional:
                        context.where_conditions.append(
                            TranslationContext._structural_guard_sql(alias, k)
                        )
                    p_alias = context.next_alias("p")
                    context.join_clauses.append(
                        f"{jt} {_table('rdf_props')} {p_alias} "
                        f'ON {p_alias}.s = {alias}.node_id AND {p_alias}."key" = {context.add_join_param(k)}'
                    )
                    if optional:
                        context.where_conditions.append(
                            f"({p_alias}.s IS NULL OR {p_alias}.val = {val_sql})"
                        )
                    else:
                        context.where_conditions.append(f"{p_alias}.val = {val_sql}")
        return
    alias = (
        context.register_variable(node.variable)
        if node.variable
        else context.next_alias("n")
    )
    # Track node type for semantic validation
    if node.variable:
        context.bind_variable_type(node.variable, "node")
    jt = "LEFT OUTER JOIN" if optional else "JOIN"

    engine = getattr(context, "_engine", None)
    if engine and node.labels:
        for label in node.labels:
            mapping = engine.get_table_mapping(label)
            if mapping:
                sql_table = sanitize_identifier(mapping["sql_table"])
                context.mapped_node_aliases[alias] = mapping
                if not context.from_clauses:
                    context.from_clauses.append(f"{sql_table} {alias}")
                elif not any(alias in fc for fc in context.from_clauses):
                    context.join_clauses.append(f"{jt} {sql_table} {alias} ON 1=1")
                for k, v in node.properties.items():
                    val_sql = translate_expression(v, context, segment="where")
                    context.where_conditions.append(
                        f"{alias}.{sanitize_identifier(k)} = {val_sql}"
                    )
                return

    nodes_tbl = _table("nodes")
    # For OPTIONAL MATCH, the "optional" semantics mean: if the whole pattern matches
    # nothing, produce one null row. The label/property constraints on the anchor node
    # (the first node in the query, when from_clauses is still empty) are still
    # restrictive — use INNER JOIN so only nodes carrying the label are returned.
    # LEFT OUTER JOIN is reserved for extending an already-bound variable (e.g. the
    # target node in MATCH (a) OPTIONAL MATCH (a)-->(b)).
    is_anchor_optional = optional and not context.from_clauses
    effective_jt = "JOIN" if is_anchor_optional else jt
    if not context.from_clauses:
        context.from_clauses.append(f"{nodes_tbl} {alias}")
    elif f"{nodes_tbl} {alias}" not in context.from_clauses and not any(
        alias in j for j in context.join_clauses
    ):
        context.join_clauses.append(f"CROSS JOIN {nodes_tbl} {alias}")
    if node.labels:
        if getattr(node, 'labels_or', False) and len(node.labels) > 1:
            l_alias = context.next_alias("l")
            labels_inlined = ", ".join(f"'{lab}'" for lab in node.labels)
            context.join_clauses.append(
                f"{effective_jt} {_table('rdf_labels')} {l_alias} ON {l_alias}.s = {alias}.node_id AND {l_alias}.label IN ({labels_inlined})"
            )
            if not optional or is_anchor_optional:
                context.where_conditions.append(f"{l_alias}.s IS NOT NULL")
        else:
            for label in node.labels:
                l_alias = context.next_alias("l")
                label_param = context.add_join_param(label)
                context.join_clauses.append(
                    f"{effective_jt} {_table('rdf_labels')} {l_alias} ON {l_alias}.s = {alias}.node_id AND {l_alias}.label = {label_param}"
                )
                if not optional or is_anchor_optional:
                    context.where_conditions.append(f"{l_alias}.s IS NOT NULL")
                if is_anchor_optional:
                    context.optional_null_row_labels.append(label)
    for k, v in node.properties.items():
        val_sql = translate_expression(v, context, segment="where")
        if k == "node_id":
            context.where_conditions.append(f"{alias}.node_id = {val_sql}")
        else:
            if not optional:
                context.where_conditions.append(
                    TranslationContext._structural_guard_sql(alias, k)
                )
            p_alias = context.next_alias("p")
            context.join_clauses.append(
                f"{jt} {_table('rdf_props')} {p_alias} "
                f'ON {p_alias}.s = {alias}.node_id AND {p_alias}."key" = {context.add_join_param(k)}'
            )
            if optional:
                context.where_conditions.append(
                    f"({p_alias}.s IS NULL OR {p_alias}.val = {val_sql})"
                )
            else:
                context.where_conditions.append(f"{p_alias}.val = {val_sql}")



def _trp_variable_length(rel, source_node, target_node, context, metadata):
    """Handle variable-length path patterns. Writes to context.var_length_paths."""
    if rel.variable_length is not None:
        source_alias = context.variable_aliases.get(source_node.variable, "")
        target_alias = context.register_variable(target_node.variable)

        def _resolve_id_param(node):
            id_val = node.properties.get("id")
            if id_val is None:
                if node.variable and node.variable in context.input_params:
                    val = context.input_params[node.variable]
                    return f"${node.variable}" if isinstance(val, str) else None
                return None
            if isinstance(id_val, ast.Variable):
                return f"${id_val.name}"
            if isinstance(id_val, ast.Literal):
                return str(id_val.value)
            if isinstance(id_val, str):
                return id_val
            return str(id_val)

        src_id_param = _resolve_id_param(source_node)
        dst_id_param = _resolve_id_param(target_node)

        direction_str = "both" if rel.direction == ast.Direction.BOTH else ("in" if rel.direction == ast.Direction.INCOMING else "out")

        context.var_length_paths.append(
            {
                "source_var": source_node.variable,
                "source_alias": source_alias,
                "target_var": target_node.variable,
                "target_alias": target_alias,
                "types": rel.types or [],
                "direction": direction_str,
                "min_hops": rel.variable_length.min_hops,
                "max_hops": rel.variable_length.max_hops,
                "shortest": rel.variable_length.shortest,
                "all_shortest": rel.variable_length.all_shortest,
                "src_id_param": src_id_param,
                "dst_id_param": dst_id_param,
                "return_path_funcs": [],
                "properties": {
                    k: (v.value if isinstance(v, ast.Literal) else v)
                    for k, v in rel.properties.items()
                } if rel.properties else {},
                "source_labels": list(source_node.labels) if source_node.labels else [],
                "target_labels": list(target_node.labels) if target_node.labels else [],
            }
        )
        if not context.from_clauses:
            context.from_clauses.append(f"{_table('nodes')} {target_alias}")
        else:
            context.join_clauses.append(f"JOIN {_table('nodes')} {target_alias} ON 1=1")
        return


def _trp_setup_aliases(rel, source_node, target_node, context):
    """Register aliases. Returns (src, tgt, edge, is_anon, is_new, is_unbound_src).

    is_unbound_src: source has a variable name but was not yet bound when this
    pattern was entered.  The caller must NOT pre-join it as a CROSS JOIN; instead
    the edge join must be anchored on the (already-bound) target and the source
    node joined from the edge.  This fixes the direction-symmetry bug: patterns
    (t)-[:R]->(f) and (f)<-[:R]-(t) must produce identical SQL when f is bound.
    """
    is_anon_source = source_node.variable is None
    is_unbound_src = False
    if is_anon_source:
        # Reuse existing alias if this anonymous node object was already seen
        # (e.g. as target of previous hop in a chain like ()-[]-(x)-[]-()).
        node_id_key = id(source_node)
        if node_id_key in context.node_obj_aliases:
            source_alias = context.node_obj_aliases[node_id_key]
            is_anon_source = False  # treat as bound — it has a backing JOIN
        else:
            source_alias = context.next_alias("n")
    else:
        existing = context.variable_aliases.get(source_node.variable)
        if existing is None:
            # Source variable exists in the query but has not been bound yet.
            # Register it now so downstream code has an alias, but flag it so
            # translate_relationship_pattern anchors the edge on the target side.
            source_alias = context.register_variable(source_node.variable)
            is_unbound_src = True
        else:
            source_alias = existing
    if target_node.variable is None:
        # Anonymous target: use object-id keyed alias to avoid sharing None key
        # across multiple anonymous nodes in a chain.
        node_key = id(target_node)
        if node_key in context.node_obj_aliases:
            target_alias = context.node_obj_aliases[node_key]
            is_new_target = False
        else:
            target_alias = context.next_alias("n")
            context.node_obj_aliases[node_key] = target_alias
            is_new_target = True
    else:
        is_new_target = target_node.variable not in context.variable_aliases
        target_alias = context.register_variable(target_node.variable)
    edge_alias = (
        context.register_variable(rel.variable, prefix="e")
        if rel.variable
        else context.next_alias("e")
    )
    # Track relationship object → SQL alias for named path lookup
    context.rel_obj_aliases[id(rel)] = edge_alias
    return source_alias, target_alias, edge_alias, is_anon_source, is_new_target, is_unbound_src


def _trp_temporal_rewrite_from_joins(context, source_alias, cte_name):
    new_from = []
    for fc in context.from_clauses:
        if source_alias in fc and _table("nodes") in fc:
            new_from.append(cte_name)
        else:
            new_from.append(fc)
    if not new_from or cte_name not in new_from:
        new_from = [cte_name] + [f for f in new_from if f != cte_name]
    context.from_clauses = new_from

    new_joins = []
    for jc in context.join_clauses:
        if (
            f"{source_alias}.node_id" in jc
            or f"{_table('nodes')} {source_alias}" in jc
        ):
            continue
        new_joins.append(jc)
    context.join_clauses = new_joins


def _trp_temporal_edge(rel, source_node, target_node, context, source_alias, edge_alias, direction):
    if rel.variable is None or context.pending_where is None:
        return False
    tb = _extract_temporal_bounds(
        context.pending_where, rel.variable, context.input_params
    )
    if tb is None:
        return False
    engine = getattr(context, "_engine", None)
    if engine is None:
        raise TemporalQueryRequiresEngine(
            f"Temporal WHERE {rel.variable}.ts filter detected but no engine was provided. "
            f"Pass engine=self when calling translate_to_sql() from execute_cypher()."
        )
    tb.direction = direction
    predicate_filter = rel.types[0] if rel.types and len(rel.types) == 1 else ""
    src_node_id = None
    if source_alias and not source_alias.startswith("Stage"):
        bound_src = source_node.variable
        if bound_src:
            src_val = context.input_params.get(bound_src)
            if src_val:
                src_node_id = src_val
    source_filter = src_node_id or ""
    ts_start = tb.ts_start if tb.ts_start is not None else 0
    ts_end = tb.ts_end if tb.ts_end is not None else 9_999_999_999
    edges = engine.get_edges_in_window(
        source_filter,
        predicate_filter,
        ts_start,
        ts_end,
        direction=tb.direction,
    )
    cte_name = f"tc{edge_alias}"
    cte_sql = _build_temporal_cte(edges, cte_name, getattr(context, "_metadata", None))
    if not hasattr(context, "cte_clauses"):
        context.cte_clauses = []
    context.cte_clauses.append(
        f"{cte_name}(s, p, o, ts, weight) AS ({cte_sql})"
    )
    context.temporal_rel_ctes[rel.variable] = cte_name
    context.temporal_derived[cte_name] = cte_sql
    context.temporal_rel_ctes[rel.variable] = cte_name

    if not hasattr(context, "temporal_node_col"):
        context.temporal_node_col = {}

    if direction == "out":
        src_col_in_cte, tgt_col_in_cte = "s", "o"
    else:
        src_col_in_cte, tgt_col_in_cte = "o", "s"

    context.temporal_node_col[source_node.variable] = src_col_in_cte
    context.temporal_node_col[target_node.variable] = tgt_col_in_cte
    context.variable_aliases[source_node.variable] = cte_name
    context.variable_aliases[target_node.variable] = cte_name

    _trp_temporal_rewrite_from_joins(context, source_alias, cte_name)

    _remove_ts_conditions_from_where(context, rel.variable)
    return True


def _trp_mapped_relation(rel, source_node, target_node, context, source_alias, target_alias, optional):
    """Handle SQL-table-bridge mapped relations. Returns True if handled."""
    if not (rel.types and len(rel.types) == 1):
        return False
    engine = getattr(context, "_engine", None)
    src_label = (
        next((lbl for lbl in source_node.labels), None)
        if source_node.labels
        else None
    )
    tgt_label = (
        next((lbl for lbl in target_node.labels), None)
        if target_node.labels
        else None
    )
    if engine and src_label and tgt_label:
        rel_map = engine.get_rel_mapping(src_label, rel.types[0], tgt_label)
        if rel_map:
            src_mapping = engine.get_table_mapping(src_label)
            tgt_mapping = engine.get_table_mapping(tgt_label)
            if src_mapping and tgt_mapping:
                jt = "LEFT OUTER JOIN" if optional else "JOIN"
                tgt_tbl = sanitize_identifier(tgt_mapping["sql_table"])
                tgt_id_col = tgt_mapping["id_column"]
                src_id_col = src_mapping["id_column"]
                if rel_map.get("target_fk"):
                    tfk = sanitize_identifier(rel_map["target_fk"])
                    context.join_clauses.append(
                        f"{jt} {tgt_tbl} {target_alias} ON {target_alias}.{tfk} = {source_alias}.{src_id_col}"
                    )
                elif rel_map.get("via_table"):
                    via_tbl = sanitize_identifier(rel_map["via_table"])
                    vs = sanitize_identifier(rel_map["via_source"])
                    vt = sanitize_identifier(rel_map["via_target"])
                    via_alias = context.next_alias("vj")
                    context.join_clauses.append(
                        f"{jt} {via_tbl} {via_alias} ON {via_alias}.{vs} = {source_alias}.{src_id_col}"
                    )
                    context.join_clauses.append(
                        f"{jt} {tgt_tbl} {target_alias} ON {target_alias}.{tgt_id_col} = {via_alias}.{vt}"
                    )
                context.mapped_node_aliases[target_alias] = tgt_mapping
                return True


def _trp_undirected_edge(
    rel, source_node, target_node, context,
    source_alias, target_alias, edge_alias, s_col, t_col, jt, is_new_target,
    is_anon_source=False,
):
    """Handle undirected (BOTH direction) patterns via CTE-based UNION ALL.

    Using a CTE (rather than an inline derived table) avoids an IRIS 2026.x
    UNDEFINED crash that occurs when a UNION ALL subquery appears inside a
    multi-table JOIN chain.  The CTE also exposes _os/_oo columns (the physical
    (s, o_id) pair regardless of traversal direction) so that callers can add
    isomorphic-edge-exclusion WHERE conditions to prevent the same physical edge
    from being matched twice in one pattern.
    """
    pred_filter = ""
    if rel.types:
        if len(rel.types) == 1:
            safe_p = rel.types[0].replace("'", "''")
            pred_filter = f" AND p = '{safe_p}'"
        else:
            safe_ps = ", ".join(f"'{t.replace(chr(39), chr(39)+chr(39))}'" for t in rel.types)
            pred_filter = f" AND p IN ({safe_ps})"
    edges_tbl = _table("rdf_edges")

    # Build an unfiltered (or predicate-filtered) UNION ALL CTE.
    # Forward rows:  s  -> o_id  (all edges including self-loops)
    # Backward rows: o_id -> s   (self-loops excluded to avoid double-counting)
    # _os/_oo carry the physical edge identity so isomorphic-edge exclusion
    # WHERE conditions can be added by translate_match_clause.
    if pred_filter:
        where_fwd = f"WHERE 1=1{pred_filter}"
        where_rev = f"WHERE s != o_id{pred_filter}"
        cte_body = (
            f"  SELECT s AS _src, p AS _p, o_id AS _dst, s AS _os, o_id AS _oo\n"
            f"  FROM {edges_tbl}\n"
            f"  {where_fwd}\n"
            f"  UNION ALL\n"
            f"  SELECT o_id AS _src, p AS _p, s AS _dst, s AS _os, o_id AS _oo\n"
            f"  FROM {edges_tbl}\n"
            f"  {where_rev}"
        )
    else:
        cte_body = (
            f"  SELECT s AS _src, p AS _p, o_id AS _dst, s AS _os, o_id AS _oo\n"
            f"  FROM {edges_tbl}\n"
            f"  UNION ALL\n"
            f"  SELECT o_id AS _src, p AS _p, s AS _dst, s AS _os, o_id AS _oo\n"
            f"  FROM {edges_tbl} WHERE s != o_id"
        )

    cte_name = f"_u{edge_alias}"
    if not hasattr(context, "cte_clauses"):
        context.cte_clauses = []
    context.cte_clauses.append(f"{cte_name}(_src, _p, _dst, _os, _oo) AS (\n{cte_body}\n)")

    # Join the CTE as the edge alias.
    target_on = f"{target_alias}.{t_col} = {edge_alias}._dst"
    if is_anon_source:
        # No bound source node — the first hop; use as FROM or cross-JOIN.
        if not context.from_clauses:
            context.from_clauses.append(f"{cte_name} {edge_alias}")
        else:
            context.join_clauses.append(f"{jt} {cte_name} {edge_alias} ON 1=1")
        # Apply source node labels via _src column (anonymous source has no node table)
        for label in (source_node.labels or []):
            l_alias = context.next_alias("l")
            context.join_clauses.append(
                f"{jt} {_table('rdf_labels')} {l_alias} "
                f"ON {l_alias}.s = {edge_alias}._src AND {l_alias}.label = {context.add_join_param(label)}"
            )
    else:
        # Bound source: filter by source node id in the JOIN condition.
        src_filter = f"{edge_alias}._src = {source_alias}.{s_col}"
        context.join_clauses.append(f"{jt} {cte_name} {edge_alias} ON {src_filter}")

    context._undirected_aliases.add(edge_alias)
    if is_new_target and not target_alias.startswith("Stage"):
        context.join_clauses.append(
            f"{jt} {_table('nodes')} {target_alias} ON {target_on}"
        )
    else:
        context.where_conditions.append(target_on)
    context.variable_aliases[rel.variable or edge_alias] = edge_alias
    for prop_node, prop_alias in (
        (source_node, source_alias),
        (target_node, target_alias),
    ):
        if prop_node:
            for k, v in (prop_node.properties or {}).items():
                if k in ("id", "node_id"):
                    id_col = f"{prop_alias}.node_id"
                    context.where_conditions.append(
                        f"{id_col} = {context.add_where_param(v.value if isinstance(v, ast.Literal) else str(v))}"
                    )
                else:
                    p_alias = context.next_alias("p")
                    context.join_clauses.append(
                        f"JOIN {_table('rdf_props')} {p_alias} ON {p_alias}.s = {prop_alias}.node_id AND {p_alias}.\"key\" = {context.add_join_param(k)}"
                    )
                    context.where_conditions.append(
                        f"{p_alias}.val = {context.add_where_param(v.value if isinstance(v, ast.Literal) else str(v))}"
                    )


def _trp_resolve_src_id_sql(source_node, context):
    src_id_val = source_node.properties.get("id") if source_node else None
    if src_id_val is None:
        return None
    if isinstance(src_id_val, ast.Literal):
        return f"'{str(src_id_val.value)}'"
    if isinstance(src_id_val, ast.Variable):
        resolved = context.input_params.get(src_id_val.name) if context.input_params else None
        return f"'{resolved}'" if resolved else None
    return None


def _trp_apply_inline_props(source_node, source_alias, target_node, target_alias, context, jt):
    for prop_node, prop_alias in (
        (source_node, source_alias),
        (target_node, target_alias),
    ):
        if prop_node is None or not prop_node.properties:
            continue
        for k, v in prop_node.properties.items():
            val_sql = translate_expression(v, context, segment="where")
            if k == "node_id":
                context.where_conditions.append(f"{prop_alias}.node_id = {val_sql}")
            else:
                p_alias = context.next_alias("p")
                context.join_clauses.append(
                    f"{jt} {_table('rdf_props')} {p_alias} "
                    f'ON {p_alias}.s = {prop_alias}.node_id AND {p_alias}."key" = {context.add_join_param(k)}'
                )
                context.where_conditions.append(f"{p_alias}.val = {val_sql}")


def _trp_directed_edge_join(
    rel, source_node, context, source_alias, edge_alias, edge_cond, jt, is_anon_source
):
    use_edgescan = (
        source_alias is not None
        and not source_alias.startswith("tc")
        and not source_alias.startswith("Stage")
        and not source_alias.startswith("BM25")
        and not source_alias.startswith("IVF_SEARCH")
        and not source_alias.startswith("IVF")
        and not source_alias.startswith("VecSearch")
    )

    if use_edgescan and not is_anon_source:
        pred_sql = f"'{rel.types[0]}'" if len(rel.types) == 1 else "NULL"
        src_id_sql = _trp_resolve_src_id_sql(source_node, context)
        if src_id_sql is not None and not context.graph_context:
            derived = (
                f"(\n"
                f"SELECT j.s, j.p, j.o_id, j.w\n"
                f"FROM JSON_TABLE(\n"
                f"  Graph_KG.MatchEdges({src_id_sql}, {pred_sql}, 0),\n"
                f"  '$[*]' COLUMNS(\n"
                f"    s VARCHAR(256) PATH '$.s',\n"
                f"    p VARCHAR(256) PATH '$.p',\n"
                f"    o_id VARCHAR(256) PATH '$.o',\n"
                f"    w DOUBLE PATH '$.w'\n"
                f"  )\n"
                f") j\n"
                f") {edge_alias}"
            )
            context.join_clauses.append(f"{jt} {derived} ON {edge_cond}")
            context._edgescan_aliases.add(edge_alias)
        else:
            context.join_clauses.append(
                f"{jt} {_table('rdf_edges')} {edge_alias} ON {edge_cond}"
            )
    else:
        if is_anon_source:
            actual_cond = edge_cond.lstrip("1=1").lstrip(" AND ").strip() if edge_cond.startswith("1=1") else edge_cond
            if not context.from_clauses:
                context.from_clauses.append(f"{_table('rdf_edges')} {edge_alias}")
                if actual_cond:
                    # Edge predicate goes into WHERE — params for rel.types were added as
                    # join_params but appear AFTER join-clause params in the SQL.  Move
                    # them to where_params so positional ? order matches SQL order.
                    n_type_params = actual_cond.count("?")
                    if n_type_params > 0:
                        moved = context.join_params[-n_type_params:]
                        del context.join_params[-n_type_params:]
                        context.where_params.extend(moved)
                    context.where_conditions.append(actual_cond)
            else:
                full_cond = actual_cond if actual_cond else "1=1"
                context.join_clauses.append(
                    f"{jt} {_table('rdf_edges')} {edge_alias} ON {full_cond}"
                )
        else:
            context.join_clauses.append(
                f"{jt} {_table('rdf_edges')} {edge_alias} ON {edge_cond}"
            )


def _trp_apply_anon_source_constraints(source_node, edge_alias, src_col, context, jt):
    """Apply label/property constraints for an anonymous source node via the edge column.

    When source has no variable there is no nodes JOIN for it — the edge table
    provides the source id via edge_alias.<src_col> ('s' for OUTGOING, 'o_id' for INCOMING).
    """
    src_ref = f"{edge_alias}.{src_col}"
    for label in (source_node.labels or []):
        l_alias = context.next_alias("l")
        context.join_clauses.append(
            f"{jt} {_table('rdf_labels')} {l_alias} "
            f"ON {l_alias}.s = {src_ref} AND {l_alias}.label = {context.add_join_param(label)}"
        )
    for k, v in (source_node.properties or {}).items():
        val_sql = translate_expression(v, context, segment="where")
        if k == "node_id":
            context.where_conditions.append(f"{src_ref} = {val_sql}")
        else:
            p_alias = context.next_alias("p")
            context.join_clauses.append(
                f"{jt} {_table('rdf_props')} {p_alias} "
                f'ON {p_alias}.s = {src_ref} AND {p_alias}."key" = {context.add_join_param(k)}'
            )
            context.where_conditions.append(f"{p_alias}.val = {val_sql}")


def _trp_move_target_cond_to_edge_join(context, edge_alias, target_on, source_alias):
    """Move a bound-target equality condition from WHERE into the edge JOIN ON clause.

    Used for multi-hop OPTIONAL MATCH where source was introduced within the same
    optional pattern.  Adding the target condition to the edge ON means the edge is
    null when the path fails, while the intermediate source node (null-gated via
    opt_intermediate_nulled) is nulled in SELECT when the edge is null.

    Also registers source_alias → edge_alias in opt_intermediate_nulled so that
    translate_return_clause can emit a CASE expression rather than a bare node_id.
    """
    # Find the edge join clause and append the target condition to its ON clause
    for i, jc in enumerate(context.join_clauses):
        # Match the clause containing this edge alias (e.g. "LEFT OUTER JOIN rdf_edges e9 ON ...")
        # or the derived JSON_TABLE variant ("LEFT OUTER JOIN (...) e9 ON ...")
        if f") {edge_alias} ON " in jc or f"rdf_edges {edge_alias} ON " in jc:
            context.join_clauses[i] = jc + f" AND {target_on}"
            break
    else:
        # Fallback: edge JOIN not found (e.g. edgescan with derived table) — add WHERE guard
        if f"{edge_alias}.o_id" in target_on:
            null_guard = f"{edge_alias}.o_id IS NULL"
        elif f"{edge_alias}.s" in target_on:
            null_guard = f"{edge_alias}.s IS NULL"
        else:
            null_guard = None
        if null_guard:
            context.where_conditions.append(f"({target_on} OR {null_guard})")
        else:
            context.where_conditions.append(target_on)
        return  # Don't register null-gating if we fell back to WHERE
    # Register null-gating: source node is null when edge is null
    context.opt_intermediate_nulled[source_alias] = edge_alias


def _trp_directed_edge(
    rel, source_node, target_node, context,
    source_alias, target_alias, edge_alias, s_col, t_col,
    edge_cond, target_on, jt, is_anon_source, is_new_target,
):
    optional = jt == "LEFT OUTER JOIN"
    if rel.types:
        if len(rel.types) == 1:
            edge_cond += f" AND {edge_alias}.p = {context.add_join_param(rel.types[0])}"
        else:
            edge_cond += f" AND {edge_alias}.p IN ({', '.join([context.add_join_param(t) for t in rel.types])})"

    _trp_directed_edge_join(
        rel, source_node, context, source_alias, edge_alias, edge_cond, jt, is_anon_source
    )

    if is_new_target and not target_alias.startswith("Stage"):
        context.join_clauses.append(
            f"{jt} {_table('nodes')} {target_alias} ON {target_on}"
        )
    elif optional:
        # For OPTIONAL MATCH with an already-bound target, choose null guard:
        # - If source was introduced WITHIN this optional match (not pre-bound):
        #   The bound-target equality goes into the edge JOIN ON clause (not WHERE),
        #   and the source node is null-gated by that edge (opt_intermediate_nulled).
        #   This correctly nulls out intermediate nodes when the full path fails,
        #   e.g. OPTIONAL MATCH (a)-->(b)-->(c_bound): b=null when c unreachable.
        # - If source was bound BEFORE this optional match (pre-bound):
        #   Use "edge IS NULL" guard in WHERE: the source legitimately has a value
        #   even when the edge doesn't exist, e.g. OPTIONAL MATCH (x)-[r]->(b_bound)
        #   where x was found by a prior OPTIONAL MATCH.
        prebound = getattr(context, "optional_prebound_aliases", set())
        if (
            not is_anon_source
            and source_alias
            and not source_alias.startswith("Stage")
        ):
            if source_alias in prebound:
                # Source was bound before this OPTIONAL — use edge null guard in WHERE
                if f"{edge_alias}.o_id" in target_on:
                    null_guard = f"{edge_alias}.o_id IS NULL"
                elif f"{edge_alias}.s" in target_on:
                    null_guard = f"{edge_alias}.s IS NULL"
                else:
                    null_guard = None
                if null_guard:
                    context.where_conditions.append(f"({target_on} OR {null_guard})")
                else:
                    context.where_conditions.append(target_on)
            else:
                # Source was introduced within this OPTIONAL — move target equality
                # into the edge JOIN ON (no WHERE), and null-gate source via this edge.
                # This avoids filtering the base row when the full path fails.
                _trp_move_target_cond_to_edge_join(
                    context, edge_alias, target_on, source_alias
                )
        else:
            context.where_conditions.append(target_on)
    else:
        context.where_conditions.append(target_on)

    if is_anon_source and (source_node.labels or source_node.properties):
        # Source node has constraints but no variable — filter via edge column.
        # OUTGOING: target is on edge.o_id → source is edge.s
        # INCOMING: target is on edge.s → source is edge.o_id
        anon_src_col = "s" if target_on.endswith(f"{edge_alias}.o_id") else "o_id"
        _trp_apply_anon_source_constraints(source_node, edge_alias, anon_src_col, context, jt)
        _trp_apply_inline_props(None, None, target_node, target_alias, context, jt)
    else:
        _trp_apply_inline_props(source_node, source_alias, target_node, target_alias, context, jt)

    # Apply label constraints for anonymous target nodes inline.
    if target_node.variable is None and target_node.labels and not target_alias.startswith("Stage"):
        for label in target_node.labels:
            l_alias = context.next_alias("l")
            context.join_clauses.append(
                f"{jt} {_table('rdf_labels')} {l_alias} "
                f"ON {l_alias}.s = {target_alias}.node_id AND {l_alias}.label = {context.add_join_param(label)}"
            )


def translate_relationship_pattern(
    rel, source_node, target_node, context, metadata, optional=False
):
    if rel.variable_length is not None:
        _trp_variable_length(rel, source_node, target_node, context, metadata)
        return
    source_alias, target_alias, edge_alias, is_anon_source, is_new_target, is_unbound_src = (
        _trp_setup_aliases(rel, source_node, target_node, context)
    )
    # Track named relationship variables for Bolt column-type tagging.
    if rel.variable:
        context.rel_variables.add(rel.variable)
        # Track relationship type for semantic validation
        context.bind_variable_type(rel.variable, "relationship")
    def _node_col(variable, alias):
        if alias.startswith("Stage") or alias == "VecSearch":
            return variable
        return "node_id"
    direction = "in" if rel.direction == ast.Direction.INCOMING else "out"
    s_col = _node_col(source_node.variable, source_alias)
    t_col = _node_col(target_node.variable, target_alias)
    jt = "LEFT OUTER JOIN" if optional else "JOIN"

    # Stage-bound relationship: when the edge variable is already promoted to a CTE
    # stage (alias = "StageN"), its edge identity is stored as __edge_<var>_s/p/o columns.
    # Use those directly instead of re-joining rdf_edges with the wrong alias.
    stage_names = {s.split(" AS (")[0].strip() for s in getattr(context, "stages", [])}
    if rel.variable and edge_alias in stage_names and edge_alias.startswith("Stage"):
        var_name = rel.variable
        stage = edge_alias
        s_col_stage = f"__edge_{var_name}_s"
        o_col_stage = f"__edge_{var_name}_o"
        # For OUTGOING (src)-[r]->(tgt): rdf_edges.s=src, rdf_edges.o_id=tgt
        # For INCOMING (src)<-[r]-(tgt): rdf_edges.s=tgt, rdf_edges.o_id=src
        # source_node is the LEFT node in the Cypher pattern.
        if rel.direction == ast.Direction.OUTGOING:
            # (source)-[r]->(target): source->s_col_stage, target->o_col_stage
            src_edge_col, tgt_edge_col = s_col_stage, o_col_stage
        else:
            # (source)<-[r]-(target): source is on o_id side, target is on s side
            src_edge_col, tgt_edge_col = o_col_stage, s_col_stage
        # Build direction-check condition (must be satisfied for the edge to match).
        # For OPTIONAL patterns this goes into the LEFT OUTER JOIN ON clause so that
        # mismatch yields NULL rather than filtering out the whole row.
        dir_checks = []
        if not is_anon_source and not is_unbound_src:
            src_id = (
                f"{source_alias}.{source_node.variable}"
                if source_alias.startswith("Stage")
                else f"{source_alias}.node_id"
            )
            dir_checks.append(f"{src_id} = {stage}.{src_edge_col}")
        if not is_new_target and target_node.variable:
            tgt_id = (
                f"{target_alias}.{target_node.variable}"
                if target_alias.startswith("Stage")
                else f"{target_alias}.node_id"
            )
            dir_checks.append(f"{tgt_id} = {stage}.{tgt_edge_col}")
        dir_cond = " AND ".join(dir_checks) if dir_checks else "1=1"
        # When optional and source was pre-registered by translate_node_pattern (is_unbound_src
        # is False but source was not stage-bound), it got a CROSS JOIN.  Upgrade it to a LEFT
        # OUTER JOIN anchored on the edge column so OPTIONAL semantics are preserved.
        if optional and not is_unbound_src and not is_anon_source and not source_alias.startswith("Stage"):
            nodes_tbl = _table("nodes")
            cross_clause = f"CROSS JOIN {nodes_tbl} {source_alias}"
            new_join_clauses = []
            for jc in context.join_clauses:
                if jc.strip() == cross_clause:
                    new_join_clauses.append(
                        f"LEFT OUTER JOIN {nodes_tbl} {source_alias} ON {source_alias}.node_id = {stage}.{src_edge_col}"
                    )
                else:
                    new_join_clauses.append(jc)
            context.join_clauses = new_join_clauses
            # Remove the spurious WHERE condition that would nullify the LEFT OUTER JOIN
            context.where_conditions = [
                w for w in context.where_conditions
                if not (f"{source_alias}.node_id = {stage}.{src_edge_col}" in w)
            ]
            # Label JOINs for the source were added (without WHERE) by translate_node_pattern.
            # For a Stage-bound OPTIONAL pattern the label is still a strict filter:
            # if the edge's source node doesn't carry the required label the pattern doesn't
            # match and the whole OPTIONAL should yield NULL.  Enforce with WHERE IS NOT NULL.
            for jc in context.join_clauses:
                if (jc.startswith("LEFT OUTER JOIN") and
                        _table("rdf_labels") in jc and
                        f"{source_alias}.node_id" in jc):
                    # Extract the label alias (first token after rdf_labels keyword)
                    parts = jc.split()
                    rdf_idx = next((i for i, p in enumerate(parts) if "rdf_labels" in p), None)
                    if rdf_idx is not None and rdf_idx + 1 < len(parts):
                        l_alias_found = parts[rdf_idx + 1]
                        context.where_conditions.append(
                            f"({l_alias_found}.s IS NOT NULL OR {source_alias}.node_id IS NULL)"
                        )
        # Register new target node if it is unbound — join via edge column.
        # Include direction check in the ON clause so OPTIONAL semantics work.
        if is_new_target and target_node.variable:
            target_alias_fresh = context.next_alias("n")
            context.variable_aliases[target_node.variable] = target_alias_fresh
            on_cond = (
                f"{target_alias_fresh}.node_id = {stage}.{tgt_edge_col}"
                + (f" AND {dir_cond}" if dir_cond != "1=1" else "")
            )
            context.join_clauses.append(
                f"{jt} {_table('nodes')} {target_alias_fresh} ON {on_cond}"
            )
            for label in (target_node.labels or []):
                l_alias = context.next_alias("l")
                context.join_clauses.append(
                    f"{jt} {_table('rdf_labels')} {l_alias} "
                    f"ON {l_alias}.s = {target_alias_fresh}.node_id AND {l_alias}.label = {context.add_join_param(label)}"
                )
                if not optional:
                    context.where_conditions.append(f"{l_alias}.s IS NOT NULL")
        elif dir_checks and optional:
            # Target already bound and pattern is optional: use WHERE (filtering is OK here
            # since a fully-bound pattern either matches or returns nothing / null-union handles it).
            for chk in dir_checks:
                context.where_conditions.append(chk)
        elif dir_checks:
            for chk in dir_checks:
                context.where_conditions.append(chk)
        # Register new source node if it is unbound.
        if is_unbound_src and source_node.variable:
            source_alias_fresh = context.next_alias("n")
            context.variable_aliases[source_node.variable] = source_alias_fresh
            on_cond = (
                f"{source_alias_fresh}.node_id = {stage}.{src_edge_col}"
                + (f" AND {dir_cond}" if dir_cond != "1=1" and not (is_new_target and target_node.variable) else "")
            )
            context.join_clauses.append(
                f"{jt} {_table('nodes')} {source_alias_fresh} ON {on_cond}"
            )
            for label in (source_node.labels or []):
                l_alias = context.next_alias("l")
                context.join_clauses.append(
                    f"{jt} {_table('rdf_labels')} {l_alias} "
                    f"ON {l_alias}.s = {source_alias_fresh}.node_id AND {l_alias}.label = {context.add_join_param(label)}"
                )
                if not optional:
                    context.where_conditions.append(f"{l_alias}.s IS NOT NULL")
        return
    if _trp_temporal_edge(rel, source_node, target_node, context, source_alias, edge_alias, direction):
        return
    if _trp_mapped_relation(rel, source_node, target_node, context, source_alias, target_alias, optional):
        return
    if rel.direction == ast.Direction.BOTH:
        _trp_undirected_edge(rel, source_node, target_node, context,
                              source_alias, target_alias, edge_alias, s_col, t_col, jt, is_new_target,
                              is_anon_source=is_anon_source)
        return

    # Direction-symmetry fix: when source is unbound but target is already bound,
    # anchor the edge join on the target and join the source node from the edge.
    # This makes (t)-[:R]->(f) and (f)<-[:R]-(t) with f pre-bound produce identical SQL.
    if is_unbound_src and not is_new_target:
        if rel.direction == ast.Direction.OUTGOING:
            # (t)-[:R]->(f_bound): anchor edge on f, join t from edge.s
            edge_cond = f"{edge_alias}.o_id = {target_alias}.{t_col}"
            src_on = f"{source_alias}.{s_col} = {edge_alias}.s"
        else:
            # (t)<-[:R]-(f_bound): anchor edge on f, join t from edge.o_id
            edge_cond = f"{edge_alias}.s = {target_alias}.{t_col}"
            src_on = f"{source_alias}.{s_col} = {edge_alias}.o_id"
        if rel.types:
            if len(rel.types) == 1:
                edge_cond += f" AND {edge_alias}.p = {context.add_join_param(rel.types[0])}"
            else:
                edge_cond += f" AND {edge_alias}.p IN ({', '.join([context.add_join_param(t) for t in rel.types])})"
        context.join_clauses.append(f"{jt} {_table('rdf_edges')} {edge_alias} ON {edge_cond}")
        context.join_clauses.append(f"{jt} {_table('nodes')} {source_alias} ON {src_on}")
        # Apply source node labels — skip_first_node_join bypassed translate_node_pattern,
        # so labels must be joined here to avoid missing filter constraints.
        for label in (source_node.labels or []):
            l_alias = context.next_alias("l")
            context.join_clauses.append(
                f"{jt} {_table('rdf_labels')} {l_alias} "
                f"ON {l_alias}.s = {source_alias}.node_id AND {l_alias}.label = {context.add_join_param(label)}"
            )
            if not optional:
                context.where_conditions.append(f"{l_alias}.s IS NOT NULL")
        _trp_apply_inline_props(source_node, source_alias, target_node, target_alias, context, jt)
        return

    if rel.direction == ast.Direction.OUTGOING:
        if is_anon_source:
            edge_cond = "1=1"
            target_on = f"{target_alias}.{t_col} = {edge_alias}.o_id"
        else:
            edge_cond = f"{edge_alias}.s = {source_alias}.{s_col}"
            target_on = f"{target_alias}.{t_col} = {edge_alias}.o_id"
    else:
        if is_anon_source:
            edge_cond = "1=1"
            target_on = f"{target_alias}.{t_col} = {edge_alias}.s"
        else:
            edge_cond = f"{edge_alias}.o_id = {source_alias}.{s_col}"
            target_on = f"{target_alias}.{t_col} = {edge_alias}.s"
    _trp_directed_edge(rel, source_node, target_node, context,
                       source_alias, target_alias, edge_alias, s_col, t_col,
                       edge_cond, target_on, jt, is_anon_source, is_new_target)


def translate_where_clause(where, context):
    context.where_conditions.append(
        translate_boolean_expression(where.expression, context)
    )


def _is_temporal_ts_condition(expr, context) -> bool:
    if not isinstance(expr, ast.BooleanExpression):
        return False
    if expr.operator not in _TEMPORAL_TS_OPS:
        return False
    if not expr.operands:
        return False
    left = expr.operands[0]
    return (
        isinstance(left, ast.PropertyReference)
        and left.property_name == "ts"
        and left.variable in context.temporal_rel_ctes
    )


def _boolean_expr_exists(expr, context) -> Optional[str]:
    pat = expr.pattern
    if pat.relationships:
        rel = pat.relationships[0]
        src_node = pat.nodes[0]
        tgt_node = pat.nodes[1] if len(pat.nodes) > 1 else None
        src_bound = (
            src_node.variable and src_node.variable in context.variable_aliases
        )
        tgt_bound = (
            tgt_node
            and tgt_node.variable
            and tgt_node.variable in context.variable_aliases
        )
        edge_alias = f"_ex{len(context.variable_aliases) + 1}"
        child_ctx = TranslationContext()
        child_ctx.input_params = context.input_params
        child_ctx._alias_counter = context._alias_counter
        child_ctx.variable_aliases = dict(context.variable_aliases)
        if tgt_node and tgt_node.variable and not tgt_bound:
            tgt_alias = child_ctx.next_alias("n")
            child_ctx.variable_aliases[tgt_node.variable] = tgt_alias
        if tgt_bound:
            tgt_ref = context.variable_aliases[tgt_node.variable]
            cond = f"{edge_alias}.o_id = {tgt_ref}.node_id"
        elif src_bound:
            src_ref = context.variable_aliases[src_node.variable]
            cond = f"{edge_alias}.s = {src_ref}.node_id"
        else:
            cond = "1=1"
        if rel.types:
            cond += f" AND {edge_alias}.p = '{rel.types[0]}'"
        sub_froms = [f"{_table('rdf_edges')} {edge_alias}"]
        sub_wheres = [cond]
        if tgt_node and tgt_node.variable and not tgt_bound:
            tgt_alias = child_ctx.variable_aliases.get(tgt_node.variable, "_tn")
            sub_froms.append(f"{_table('nodes')} {tgt_alias}")
            sub_wheres.append(f"{tgt_alias}.node_id = {edge_alias}.o_id")
            if tgt_node.labels:
                for lbl in tgt_node.labels:
                    lbl_alias = child_ctx.next_alias("l")
                    sub_froms.append(f"{_table('rdf_labels')} {lbl_alias}")
                    sub_wheres.append(f"{lbl_alias}.s = {tgt_alias}.node_id AND {lbl_alias}.label = '{lbl}'")
        if expr.where_condition:
            where_sql = translate_boolean_expression(expr.where_condition, child_ctx)
            for p in child_ctx.where_params:
                context.where_params.append(p)
            for p in child_ctx.join_params:
                context.join_params.append(p)
            # Any JOINs generated by the WHERE condition (e.g. rdf_props for n.prop = m.prop)
            # must be included in the subquery's FROM clause.
            for jc in child_ctx.join_clauses:
                # Strip leading JOIN/LEFT JOIN keyword — use comma-join for simple existence check
                jc_stripped = jc.strip()
                if jc_stripped.upper().startswith("JOIN ") or jc_stripped.upper().startswith("LEFT JOIN "):
                    # Convert to FROM clause entry by stripping JOIN keyword and ON condition
                    # For simple cases (JOIN rdf_props p1 ON ...) split into table and condition
                    parts = jc_stripped.split(" ON ", 1)
                    if len(parts) == 2:
                        tbl_part = parts[0].split(None, 1)[1] if parts[0].upper().startswith(("JOIN", "LEFT")) else parts[0]
                        # Remove "JOIN" / "LEFT JOIN" prefix
                        for kw in ("LEFT JOIN ", "JOIN "):
                            if tbl_part.upper().startswith(kw):
                                tbl_part = tbl_part[len(kw):]
                                break
                        sub_froms.append(tbl_part)
                        sub_wheres.append(parts[1].strip())
                    else:
                        sub_froms.append(jc_stripped)
            # Additional WHERE conditions collected by child context
            for wc in child_ctx.where_conditions:
                sub_wheres.append(wc)
            sub_wheres.append(where_sql)
        sub = f"SELECT 1 FROM {', '.join(sub_froms)} WHERE {' AND '.join(sub_wheres)}"
        prefix = "NOT " if expr.negated else ""
        return f"{prefix}EXISTS ({sub})"
    return None


def _boolean_expr_comparison_ops(op, left, left_expr, right, right_expr) -> Optional[str]:
    if op == ast.BooleanOperator.EQUALS:
        return f"{left} = {right}"
    if op == ast.BooleanOperator.NOT_EQUALS:
        return f"{left} <> {right}"
    if op == ast.BooleanOperator.LESS_THAN:
        return f"{left} < {right}"
    if op == ast.BooleanOperator.LESS_THAN_OR_EQUAL:
        return f"{left} <= {right}"
    if op == ast.BooleanOperator.GREATER_THAN:
        return f"{left} > {right}"
    if op == ast.BooleanOperator.GREATER_THAN_OR_EQUAL:
        return f"{left} >= {right}"
    if op == ast.BooleanOperator.STARTS_WITH:
        return f"{left} LIKE ({right} || '%')"
    if op == ast.BooleanOperator.ENDS_WITH:
        return f"{left} LIKE ('%' || {right})"
    if op == ast.BooleanOperator.CONTAINS:
        return f"{left} LIKE ('%' || {right} || '%')"
    if op == ast.BooleanOperator.REGEX_MATCH:
        return f"SQLUser.REGEX_MATCH({left}, {right}) = 1"
    if op == ast.BooleanOperator.IN:
        return f"{left} IN {right}"
    return None


def _get_non_boolean_operand(expr):
    """Check if expression has non-boolean literal operands and return it."""
    for operand in expr.operands:
        if isinstance(operand, ast.Literal):
            v = operand.value
            # Boolean and None (null) are valid for AND/OR/XOR/NOT
            if not (isinstance(v, bool) or v is None):
                return operand
        elif isinstance(operand, (ast.MapLiteral, ast.Literal)):
            # Map literals are not boolean
            if isinstance(operand, ast.MapLiteral):
                return operand
    return None

def _format_invalid_type(operand):
    """Format error message for invalid operand type."""
    if isinstance(operand, ast.Literal):
        v = operand.value
        type_name = type(v).__name__
        return f"{type_name}: {v!r}"
    elif isinstance(operand, ast.MapLiteral):
        return "map"
    elif isinstance(operand, (ast.Literal, ast.Variable)):
        if isinstance(operand.value, list):
            return f"list: {operand.value!r}"
    # Fallback
    return str(operand)



def _coerce_varchar_boolean_if_needed(operand, translated_sql, context) -> str:
    if isinstance(operand, ast.Variable) and operand.name in context.scalar_variables:
        return f"(({translated_sql} = '1' OR {translated_sql} = 'true'))"
    return translated_sql


def _boolean_expr_logical(op, expr, context):
    if op == ast.BooleanOperator.AND:
        # Type validation: AND requires boolean operands
        bad_operand = _get_non_boolean_operand(expr)
        if bad_operand is not None:
            raise CypherParseError(
                f"InvalidArgumentType: AND requires boolean operands, "
                f"got {_format_invalid_type(bad_operand)}"
            )
        parts = []
        has_null = False
        _sp0 = len(context.select_params)
        _wp0 = len(context.where_params)
        _jp0 = len(context.join_params)
        for o in expr.operands:
            if _is_temporal_ts_condition(o, context):
                continue
            p = translate_boolean_expression(o, context)
            p = _coerce_varchar_boolean_if_needed(o, p, context)
            if p == "NULL":
                has_null = True
            else:
                parts.append(p)
        if not parts:
            # All operands were null or temporal
            return "NULL" if has_null else "1=1"
        # Three-value AND: if any operand is definitively false, result is false.
        # Roll back any params added by discarded operands.
        if "(1=0)" in parts:
            del context.select_params[_sp0:]
            del context.where_params[_wp0:]
            del context.join_params[_jp0:]
            return "(1=0)"
        # Unwrap nested nullable CASE WHEN parts (produced by inner 3VL AND/OR):
        # "CASE WHEN NOT (cond) THEN (1=0) ELSE NULL END" means: false if NOT cond, else NULL.
        # For AND, we need cond to hold (it's already nullable → mark has_null).
        # "CASE WHEN (cond) THEN (1=1) ELSE NULL END" means: true if cond, else NULL.
        import re as _re_and
        unwrapped = []
        for p in parts:
            m_not = _re_and.match(r'^CASE WHEN NOT \((.+)\) THEN \(1=0\) ELSE NULL END$', p)
            if m_not:
                has_null = True
                unwrapped.append(m_not.group(1))
            else:
                unwrapped.append(p)
        parts = unwrapped
        # Simplify: filter out always-true sentinels (they don't affect AND result)
        non_trivial = [p for p in parts if p != "(1=1)"]
        if not non_trivial:
            # All parts are literal true
            if has_null:
                return "NULL"
            return "(1=1)"
        combined = "(" + " AND ".join(non_trivial) + ")"
        if has_null:
            return f"CASE WHEN NOT ({combined}) THEN (1=0) ELSE NULL END"
        return combined
    if op == ast.BooleanOperator.OR:
        # Type validation: OR requires boolean operands
        bad_operand = _get_non_boolean_operand(expr)
        if bad_operand is not None:
            raise CypherParseError(
                f"InvalidArgumentType: OR requires boolean operands, "
                f"got {_format_invalid_type(bad_operand)}"
            )
        parts_or = []
        has_null_or = False
        _sp0_or = len(context.select_params)
        _wp0_or = len(context.where_params)
        _jp0_or = len(context.join_params)
        for o in expr.operands:
            p = translate_boolean_expression(o, context)
            p = _coerce_varchar_boolean_if_needed(o, p, context)
            if p == "NULL":
                has_null_or = True
            else:
                parts_or.append(p)
        if not parts_or:
            return "NULL" if has_null_or else "(1=0)"
        # Three-value OR: if any operand is definitively true, result is true.
        # Roll back params added by discarded operands.
        if "(1=1)" in parts_or:
            del context.select_params[_sp0_or:]
            del context.where_params[_wp0_or:]
            del context.join_params[_jp0_or:]
            return "(1=1)"
        # Unwrap nested nullable CASE WHEN parts from inner 3VL AND/OR:
        import re as _re_or
        unwrapped_or = []
        for p in parts_or:
            m_or = _re_or.match(r'^CASE WHEN \((.+)\) THEN \(1=1\) ELSE NULL END$', p)
            if m_or:
                has_null_or = True
                unwrapped_or.append(m_or.group(1))
            else:
                unwrapped_or.append(p)
        parts_or = unwrapped_or
        # Simplify: filter out always-false sentinels (they don't affect OR result)
        non_trivial_or = [p for p in parts_or if p != "(1=0)"]
        if not non_trivial_or:
            # All parts are literal false
            if has_null_or:
                return "NULL"
            return "(1=0)"
        combined_or = "(" + " OR ".join(non_trivial_or) + ")"
        if has_null_or:
            return f"CASE WHEN ({combined_or}) THEN (1=1) ELSE NULL END"
        return combined_or
    if op == ast.BooleanOperator.XOR:
        # Type validation: XOR requires boolean operands
        bad_operand = _get_non_boolean_operand(expr)
        if bad_operand is not None:
            raise CypherParseError(
                f"InvalidArgumentType: XOR requires boolean operands, "
                f"got {_format_invalid_type(bad_operand)}"
            )
        a, b = expr.operands[0], expr.operands[1]
        sa = translate_boolean_expression(a, context)
        sa = _coerce_varchar_boolean_if_needed(a, sa, context)
        sb = translate_boolean_expression(b, context)
        sb = _coerce_varchar_boolean_if_needed(b, sb, context)
        if sa == "NULL" or sb == "NULL":
            return "NULL"
        return f"(({sa} AND NOT ({sb})) OR (NOT ({sa}) AND {sb}))"
    if op == ast.BooleanOperator.NOT:
        # Type validation: NOT requires boolean operand
        bad_operand = _get_non_boolean_operand(expr)
        if bad_operand is not None:
            raise CypherParseError(
                f"InvalidArgumentType: NOT requires boolean operand, "
                f"got {_format_invalid_type(bad_operand)}"
            )
        operand = expr.operands[0]
        # NOT null = null (three-valued logic)
        if isinstance(operand, ast.Literal) and operand.value is None:
            return "NULL"
        # Fold NOT NOT: double negation cancels (IRIS SQL rejects NOT NOT syntax)
        if (isinstance(operand, ast.BooleanExpression)
                and operand.operator == ast.BooleanOperator.NOT
                and len(operand.operands) == 1):
            return translate_boolean_expression(operand.operands[0], context)
        # NOT (x IS NULL) → x IS NOT NULL (IRIS parses NOT x IS NULL as (NOT x) IS NULL)
        if (isinstance(operand, ast.BooleanExpression)
                and operand.operator == ast.BooleanOperator.IS_NULL):
            left = translate_expression(operand.operands[0], context, segment="where")
            return f"{left} IS NOT NULL"
        # NOT (x IS NOT NULL) → x IS NULL
        if (isinstance(operand, ast.BooleanExpression)
                and operand.operator == ast.BooleanOperator.IS_NOT_NULL):
            left = translate_expression(operand.operands[0], context, segment="where")
            return f"{left} IS NULL"
        operand_sql = translate_boolean_expression(operand, context)
        operand_sql = _coerce_varchar_boolean_if_needed(operand, operand_sql, context)
        return f"NOT ({operand_sql})"
    return None


def _boolean_expr_in(left, right_expr, context):
    if isinstance(right_expr, ast.SubscriptExpression):
        inner_sql = translate_expression(right_expr.expression, context, segment="where")
        idx = right_expr.index
        if isinstance(idx, ast.Literal) and isinstance(idx.value, int):
            i = idx.value
            ij_alias = context.next_alias("ij")
            sub_arr_sql = f"(SELECT __sa FROM JSON_TABLE({inner_sql}, '$[{i}]' COLUMNS(__sa VARCHAR(4096) PATH '$')) __jt_sa)"
            return f"{left} IN (SELECT __iv FROM JSON_TABLE({sub_arr_sql}, '$[*]' COLUMNS(__iv VARCHAR(1000) PATH '$')) {ij_alias})"
        idx_sql = translate_expression(idx, context, segment="where")
        sub_arr_sql = f"SQLUser.JSON_VALUE({inner_sql}, '$[' || CAST(({idx_sql}) AS VARCHAR) || ']')"
        ij_alias = context.next_alias("ij")
        return f"{left} IN (SELECT __iv FROM JSON_TABLE({sub_arr_sql}, '$[*]' COLUMNS(__iv VARCHAR(1000) PATH '$')) {ij_alias})"
    if isinstance(right_expr, ast.SliceExpression):
        slice_sql = translate_expression(right_expr, context, segment="where")
        ij_alias = context.next_alias("ij")
        return f"{left} IN (SELECT __iv FROM JSON_TABLE({slice_sql}, '$[*]' COLUMNS(__iv VARCHAR(1000) PATH '$')) {ij_alias})"
    if isinstance(right_expr, ast.Literal) and isinstance(right_expr.value, list):
        items = right_expr.value
        # Separate null items from non-null items for 3VL: x IN [a, null, b]
        # = x IN (a, b) OR NULL (unknown if no exact match but list has nulls)
        null_items = [i for i in items if isinstance(i, ast.Literal) and i.value is None]
        non_null_items = [i for i in items if not (isinstance(i, ast.Literal) and i.value is None)]
        if not non_null_items:
            # All null: x IN [null] = null (handled by caller null check for left=null, else null)
            return "NULL"
        placeholders = ", ".join(
            context.add_where_param(item.value if isinstance(item, ast.Literal) else item)
            for item in non_null_items
        )
        in_expr = f"{left} IN ({placeholders})"
        if null_items:
            # 3VL: if x matches → true; if x doesn't match and list has null → null
            return f"CASE WHEN {in_expr} THEN 1 ELSE NULL END"
        return in_expr
    if isinstance(right_expr, ast.Variable) and right_expr.name in context.input_params:
        val = context.input_params[right_expr.name]
        if isinstance(val, list):
            null_vals = [v for v in val if v is None]
            non_null_vals = [v for v in val if v is not None]
            if not non_null_vals:
                return "NULL"
            placeholders = ", ".join(context.add_where_param(v) for v in non_null_vals)
            in_expr = f"{left} IN ({placeholders})"
            if null_vals:
                return f"CASE WHEN {in_expr} THEN 1 ELSE NULL END"
            return in_expr
    return None


def _rel_identity_comparison(op, left_expr, right_expr, context) -> Optional[str]:
    """Generate relationship identity comparison (s, p, o triple match) for a = b / a <> b.

    Returns SQL condition string if both operands are relationship variables, else None.
    Handles three cases:
      1. stage-edge vs current-edge: __edge_a_s = e.s AND __edge_a_p = e.p AND __edge_a_o = e.o_id
      2. current-edge vs current-edge: e1.s = e2.s AND e1.p = e2.p AND e1.o_id = e2.o_id
      3. current-edge vs stage-edge: same as case 1, reversed
    """
    def _get_edge_info(expr_var):
        """Return (kind, alias, var_name) for a Variable that is an edge variable.
        kind: 'stage' or 'current' or None
        """
        if not isinstance(expr_var, ast.Variable):
            return None
        var_name = expr_var.name
        alias = context.variable_aliases.get(var_name)
        if alias is None:
            return None
        edge_stage_vars = getattr(context, "edge_stage_variables", set())
        if alias.startswith("Stage") and var_name in edge_stage_vars:
            return ('stage', alias, var_name)
        if alias.startswith("e") and not alias.startswith("Stage"):
            is_undirected = alias in getattr(context, "_undirected_aliases", set())
            return ('current', alias, var_name, is_undirected)
        return None

    left_info = _get_edge_info(left_expr)
    right_info = _get_edge_info(right_expr)
    if left_info is None or right_info is None:
        return None

    # Both are edge variables — generate triple comparison
    op_str = "=" if op == ast.BooleanOperator.EQUALS else "<>"
    join_str = " AND " if op == ast.BooleanOperator.EQUALS else " OR "

    def _stage_cols(var_name):
        return (
            f"__edge_{var_name}_s",
            f"__edge_{var_name}_p",
            f"__edge_{var_name}_o",
        )

    def _current_cols(alias, is_undirected=False):
        if is_undirected:
            return (f"{alias}._src", f"{alias}._p", f"{alias}._dst")
        return (f"{alias}.s", f"{alias}.p", f"{alias}.o_id")

    if left_info[0] == 'stage':
        ls, lp, lo = _stage_cols(left_info[2])
    else:
        ls, lp, lo = _current_cols(left_info[1], left_info[3] if len(left_info) > 3 else False)

    if right_info[0] == 'stage':
        rs, rp, ro = _stage_cols(right_info[2])
    else:
        rs, rp, ro = _current_cols(right_info[1], right_info[3] if len(right_info) > 3 else False)

    parts = [
        f"{ls} {op_str} {rs}",
        f"{lp} {op_str} {rp}",
        f"{lo} {op_str} {ro}",
    ]
    if op == ast.BooleanOperator.EQUALS:
        return "(" + " AND ".join(parts) + ")"
    else:
        # NOT EQUALS: at least one component differs
        return "(" + " OR ".join(parts) + ")"


def translate_boolean_expression(expr, context) -> str:
    if isinstance(expr, ast.ExistsExpression):
        result = _boolean_expr_exists(expr, context)
        if result is not None:
            return result
        return "1=1"
    if isinstance(expr, ast.LabelPredicate):
        alias = context.variable_aliases.get(expr.variable)
        node_col = f"{alias}.node_id" if alias else "node_id"
        labels_tbl = _table("rdf_labels")
        safe_label = context.add_where_param(expr.label)
        return (
            f"EXISTS (SELECT 1 FROM {labels_tbl} _lp WHERE _lp.s = {node_col}"
            f" AND _lp.label = {safe_label})"
        )
    if not isinstance(expr, ast.BooleanExpression):
        if isinstance(expr, ast.Literal):
            if expr.value is True:
                return "(1=1)"
            if expr.value is False:
                return "(1=0)"
        # When a PropertyReference is used directly in a boolean context,
        # convert it to a proper boolean comparison. IVG stores booleans as '1'/'0'.
        if isinstance(expr, ast.PropertyReference):
            prop_expr = translate_expression(expr, context, segment="where")
            # Convert VARCHAR '1'/'0' to boolean: property = '1'
            return f"({prop_expr} = '1')"
        # Quantifier expressions (any/all/none/single) return a CASE WHEN 0/1/NULL
        # expression. When used as a standalone boolean predicate in WHERE, wrap with
        # = 1 so IRIS treats it as a proper predicate.  When used as an operand in a
        # comparison (e.g. none(...) = (NOT any(...))), translate_expression is called
        # directly (not via here), so it gets the raw CASE expression — valid as a
        # scalar in a comparison.
        if isinstance(expr, ast.ListPredicateExpression):
            case_sql = translate_expression(expr, context, segment="where")
            return f"({case_sql} = 1)"
        return translate_expression(expr, context, segment="where")
    op = expr.operator
    logical = _boolean_expr_logical(op, expr, context)
    if logical is not None:
        return logical
    left_expr = expr.operands[0]
    right_expr = expr.operands[1] if len(expr.operands) > 1 else None
    if op in (ast.BooleanOperator.IS_NULL, ast.BooleanOperator.IS_NOT_NULL):
        # Use segment="select" to avoid the structural-guard EXISTS clause —
        # IS NULL explicitly handles the missing-property case via LEFT JOIN.
        left = translate_expression(left_expr, context, segment="select")
        if op == ast.BooleanOperator.IS_NULL:
            return f"{left} IS NULL"
        return f"{left} IS NOT NULL"
    # Cypher three-valued logic: any comparison involving NULL yields NULL (unknown).
    # This includes null = null, null <> null, null < x, x IN [null], null IN [...], etc.
    _left_is_null = isinstance(left_expr, ast.Literal) and left_expr.value is None
    _right_is_null = right_expr is not None and isinstance(right_expr, ast.Literal) and right_expr.value is None
    # Also check parameter variables whose resolved value is null
    if not _left_is_null and isinstance(left_expr, ast.Variable):
        _left_val = context.input_params.get(left_expr.name)
        _left_is_null = _left_val is None and left_expr.name in context.input_params
    if not _right_is_null and right_expr is not None and isinstance(right_expr, ast.Variable):
        _right_val = context.input_params.get(right_expr.name)
        _right_is_null = _right_val is None and right_expr.name in context.input_params
    if (_left_is_null or _right_is_null) and op not in (
        ast.BooleanOperator.IS_NULL, ast.BooleanOperator.IS_NOT_NULL
    ):
        # Special case: null IN [] = false (empty list, no unknowns possible)
        if op == ast.BooleanOperator.IN and _left_is_null:
            if isinstance(right_expr, ast.Literal) and right_expr.value == []:
                return "(1=0)"  # false
            if isinstance(right_expr, ast.Variable):
                _rcoll = context.input_params.get(right_expr.name)
                if isinstance(_rcoll, list) and len(_rcoll) == 0:
                    return "(1=0)"  # false
        return "NULL"
    # Relationship identity comparison: a = b / a <> b where a and/or b are
    # relationship variables. Relationship equality means same (s, p, o) triple.
    # Stage edge variables store identity as __edge_{var}_s/p/o columns.
    if op in (ast.BooleanOperator.EQUALS, ast.BooleanOperator.NOT_EQUALS):
        rel_id_cond = _rel_identity_comparison(op, left_expr, right_expr, context)
        if rel_id_cond is not None:
            return rel_id_cond
        # Constant folding: both sides are fully literal lists/maps — evaluate in Python
        # (SQL string comparison can't produce NULL for Cypher three-valued list equality)
        is_list_or_map = lambda e: (
            (isinstance(e, ast.Literal) and isinstance(e.value, list))
            or isinstance(e, ast.MapLiteral)
        )
        if right_expr is not None and is_list_or_map(left_expr) and is_list_or_map(right_expr):
            if _is_fully_literal(left_expr) and _is_fully_literal(right_expr):
                lv = _literal_to_python(left_expr)
                rv = _literal_to_python(right_expr)
                result = _cypher_eq(lv, rv)
                if result is None:
                    return "NULL"
                bool_val = result if op == ast.BooleanOperator.EQUALS else not result
                return "(1=1)" if bool_val else "(1=0)"
        # Scalar literal type-mismatch: Cypher is strongly typed, string != number
        if right_expr is not None and isinstance(left_expr, ast.Literal) and isinstance(right_expr, ast.Literal):
            lv, rv = left_expr.value, right_expr.value
            if lv is not None and rv is not None and not isinstance(lv, bool) and not isinstance(rv, bool):
                # string vs numeric: always false in Cypher (no implicit coercion)
                lv_str = isinstance(lv, str)
                rv_str = isinstance(rv, str)
                lv_num = isinstance(lv, (int, float))
                rv_num = isinstance(rv, (int, float))
                if (lv_str and rv_num) or (lv_num and rv_str):
                    is_eq = False
                    bool_val = is_eq if op == ast.BooleanOperator.EQUALS else not is_eq
                    return "(1=1)" if bool_val else "(1=0)"

    left_inlined = _inline_literal(left_expr)
    left = left_inlined if left_inlined is not None else translate_expression(left_expr, context, segment="where")
    # Wrap CASE WHEN expressions in parens — IRIS SQLCODE -25 if bare CASE ends before =
    if left.startswith("CASE WHEN ") and " END" in left:
        left = f"({left})"
    if op == ast.BooleanOperator.IN:
        in_sql = _boolean_expr_in(left, right_expr, context)
        if in_sql is not None:
            return in_sql
    right_inlined = _inline_literal(right_expr)
    right = right_inlined if right_inlined is not None else translate_expression(right_expr, context, segment="where")
    if right.startswith("CASE WHEN ") and " END" in right:
        right = f"({right})"
    if op in (
        ast.BooleanOperator.LESS_THAN,
        ast.BooleanOperator.LESS_THAN_OR_EQUAL,
        ast.BooleanOperator.GREATER_THAN,
        ast.BooleanOperator.GREATER_THAN_OR_EQUAL,
    ):
        if isinstance(left_expr, ast.PropertyReference):
            left = f"CAST({left} AS DOUBLE)"
        if isinstance(right_expr, ast.PropertyReference):
            right = f"CAST({right} AS DOUBLE)"
    result = _boolean_expr_comparison_ops(op, left, left_expr, right, right_expr)
    if result is not None:
        return result
    raise ValueError(f"Unsupported operator: {op}")


def _cypher_eq(a, b):
    """Three-valued Cypher equality. Returns True, False, or None (null)."""
    if a is None or b is None:
        return None
    if isinstance(a, list) and isinstance(b, list):
        if len(a) != len(b):
            return False
        result = True
        for x, y in zip(a, b):
            eq = _cypher_eq(x, y)
            if eq is False:
                return False
            if eq is None:
                result = None  # might still be false from later items
        return result
    if isinstance(a, dict) and isinstance(b, dict):
        if set(a.keys()) != set(b.keys()):
            return False
        result = True
        for k in a:
            eq = _cypher_eq(a[k], b[k])
            if eq is False:
                return False
            if eq is None:
                result = None
        return result
    return a == b


def _inline_literal(expr) -> Optional[str]:
    if expr is None:
        return None
    if isinstance(expr, ast.Literal):
        v = expr.value
        if v is None:
            return "NULL"
        if isinstance(v, bool):
            return "1" if v else "0"
        if isinstance(v, (int, float)):
            return str(v)
        if isinstance(v, list):
            # List literals need full translate_expression (json.dumps path)
            return None
        return f"'{str(v)}'"
    return None


def _sql_arg(v) -> str:
    if isinstance(v, bool):
        return "1" if v else "0"
    if isinstance(v, (int, float)):
        return str(v)
    return "'" + str(v).replace("'", "''") + "'"


def _expr_pattern_comprehension(expr, context, segment):
    pat = expr.pattern
    src_node = pat.nodes[0] if pat.nodes else None
    tgt_node = pat.nodes[1] if len(pat.nodes) > 1 else None
    rel = pat.relationships[0] if pat.relationships else None

    e_alias = context.next_alias("pce")
    t_alias = context.next_alias("pct")

    pred_type = ""
    if rel and rel.types:
        safe_type = rel.types[0].replace("'", "''")
        pred_type = f" AND {e_alias}.p = '{safe_type}'"

    src_bind = ""
    if (
        src_node
        and src_node.variable
        and src_node.variable in context.variable_aliases
    ):
        src_id = f"{context.variable_aliases[src_node.variable]}.node_id"
        src_bind = f" AND {e_alias}.s = {src_id}"

    tgt_var = tgt_node.variable if tgt_node else None

    if (
        expr.projection
        and isinstance(expr.projection, ast.PropertyReference)
        and expr.projection.variable == tgt_var
    ):
        if expr.projection.property_name == "node_id":
            proj_sql = f"{t_alias}.node_id"
        else:
            safe_key = expr.projection.property_name.replace("'", "''")
            proj_sql = (
                f"(SELECT val FROM {_table('rdf_props')} "
                f"WHERE s = {t_alias}.node_id AND \"key\" = '{safe_key}')"
            )
    elif expr.projection:
        if tgt_var:
            context.variable_aliases[tgt_var] = t_alias
        if rel and rel.variable:
            context.variable_aliases[rel.variable] = e_alias
        proj_sql = translate_expression(expr.projection, context, segment="select")
        if tgt_var and tgt_var in context.variable_aliases:
            del context.variable_aliases[tgt_var]
        if rel and rel.variable and rel.variable in context.variable_aliases:
            del context.variable_aliases[rel.variable]
    else:
        proj_sql = f"{t_alias}.node_id"

    return (
        f"(SELECT JSON_ARRAYAGG({proj_sql}) FROM "
        f"{_table('rdf_edges')} {e_alias} "
        f"JOIN {_table('nodes')} {t_alias} ON {t_alias}.node_id = {e_alias}.o_id "
        f"WHERE 1=1{pred_type}{src_bind})"
    )


def _expr_prop(expr, context, segment):
    inner_expr = expr.arguments[0]
    prop = str(expr.arguments[1].value) if isinstance(expr.arguments[1], ast.Literal) else "node_id"
    if prop == "id":
        prop = "node_id"
    inner_fn = inner_expr.function_name.lower() if isinstance(inner_expr, ast.FunctionCall) else ""
    if inner_fn in ("startnode", "endnode"):
        return translate_expression(inner_expr, context, segment=segment)
    inner = translate_expression(inner_expr, context, segment=segment)
    return f"{inner}.{prop}"


def _expr_arith(expr, context, segment):
    op = expr.function_name[len("__arith_") :]
    left = translate_expression(expr.arguments[0], context, segment=segment)
    right = translate_expression(expr.arguments[1], context, segment=segment)
    if op == "%":
        return f"MOD({left}, {right})"
    if op == "^":
        return f"POWER({left}, {right})"
    if op == "+":
        def _is_str(arg):
            return (isinstance(arg, ast.Literal) and isinstance(arg.value, str)) or \
                isinstance(arg, ast.FunctionCall) and arg.function_name.startswith("__arith_+")
        left_str = _is_str(expr.arguments[0])
        right_str = _is_str(expr.arguments[1])
        if left_str or right_str:
            return f"(CAST({left} AS VARCHAR(4096)) || CAST({right} AS VARCHAR(4096)))"
    return f"({left} {op} {right})"


def _expr_list_predicate(expr, context, segment):
    source_sql = translate_expression(expr.source, context, segment=segment)
    var = sanitize_identifier(expr.variable)
    alias = context.next_alias("lp")
    # Always use VARCHAR(1000) — IRIS JSON_TABLE BIGINT/DOUBLE columns fail to
    # match bind-param floats/ints due to IRIS internal type precision.
    # VARCHAR comparisons work when params are stringified (done below).
    col_type = "VARCHAR(1000)"
    context.variable_aliases[expr.variable] = f"{alias}"
    context.scalar_variables.add(expr.variable)
    # Snapshot param lists so we can stringify newly added numeric params.
    _sp_len = len(context.select_params)
    _wp_len = len(context.where_params)
    # Use translate_boolean_expression for proper SQL predicates — IRIS requires
    # comparison predicates in WHERE, not CASE WHEN boolean expressions.
    if isinstance(expr.predicate, ast.BooleanExpression):
        pred_sql = translate_boolean_expression(expr.predicate, context)
    else:
        pred_sql = translate_expression(expr.predicate, context, segment="where")
    # Convert newly added int/float params to str so VARCHAR column comparison works.
    for _lst in (context.select_params, context.where_params):
        _snap = _sp_len if _lst is context.select_params else _wp_len
        for _i in range(_snap, len(_lst)):
            if isinstance(_lst[_i], float):
                _lst[_i] = str(_lst[_i])
            elif isinstance(_lst[_i], int) and not isinstance(_lst[_i], bool):
                _lst[_i] = str(_lst[_i])
    # Also replace inline CAST('...' AS DOUBLE) with string literal for VARCHAR column comparison.
    import re as _re_qp
    def _cast_double_to_str(m):
        return f"'{m.group(1)}'"
    pred_sql = _re_qp.sub(r"CAST\('([^']+)' AS DOUBLE\)", _cast_double_to_str, pred_sql)
    del context.variable_aliases[expr.variable]
    context.scalar_variables.discard(expr.variable)
    pred_with_alias = pred_sql
    for col in ("node_id", "p", "val", "label"):
        pred_with_alias = pred_with_alias.replace(
            f"{alias}.{col}", f"{alias}.{var}"
        )
    # IRIS WHERE clause needs a comparison predicate, not a bare boolean expression.
    # Coerce bare 1/0 and bare column references to proper predicates.
    where_pred = pred_with_alias
    if where_pred in ("1", "1=1", "TRUE"):
        where_pred = "1 = 1"
    elif where_pred in ("0", "1=0", "FALSE"):
        where_pred = "1 = 0"
    elif where_pred.startswith("CASE WHEN ") and where_pred.endswith(" END"):
        # Nested quantifier: a CASE WHEN 0/1/NULL expression used as a WHERE predicate.
        # IRIS requires a comparison predicate, not a bare CASE WHEN. Add = 1 to coerce.
        where_pred = f"({where_pred} = 1)"
    elif where_pred and not any(op in where_pred for op in ("=", "<", ">", " IN ", " IS ", " LIKE ", " NOT ")):
        # Bare column reference (e.g. lp0.x) — treat as truth test
        where_pred = f"{where_pred} = 1"
    null_alias = context.next_alias("lpn")
    non_null_alias = context.next_alias("lpnn")
    satisfy_sql = (
        f"SELECT COUNT(*) FROM JSON_TABLE({source_sql}, '$[*]' COLUMNS({var} {col_type} PATH '$')) {alias}"
        f" WHERE {where_pred}"
    )
    null_count_sql = f"SELECT COUNT(*) FROM JSON_TABLE({source_sql}, '$[*]' COLUMNS({var} {col_type} PATH '$')) {null_alias} WHERE {null_alias}.{var} IS NULL"
    # 3VL (three-valued logic) semantics for quantifiers with null-containing lists:
    # - any:    true  if any non-null element satisfies pred;
    #           null  if none satisfy but nulls exist (unknown if null would satisfy);
    #           false otherwise
    # - none:   false if any non-null element satisfies pred;
    #           null  if none satisfy but nulls exist;
    #           true  otherwise (no elements satisfy and no nulls)
    # - all:    false if any non-null element fails pred;
    #           null  if all non-null satisfy but nulls exist;
    #           true  otherwise
    # - single: true  if exactly one non-null satisfies pred (ignore nulls for now);
    #           null  if zero satisfy and nulls exist;
    #           false otherwise
    if expr.quantifier == "all":
        # false if any non-null element fails; null if all pass but nulls present; true otherwise
        non_null_count_sql = f"SELECT COUNT(*) FROM JSON_TABLE({source_sql}, '$[*]' COLUMNS({var} {col_type} PATH '$')) {non_null_alias} WHERE {non_null_alias}.{var} IS NOT NULL"
        return (
            f"CASE WHEN (({satisfy_sql}) < ({non_null_count_sql})) THEN 0"
            f" WHEN (({null_count_sql}) > 0) THEN NULL"
            f" ELSE 1 END"
        )
    elif expr.quantifier == "none":
        return (
            f"CASE WHEN (({satisfy_sql}) > 0) THEN 0"
            f" WHEN (({null_count_sql}) > 0) THEN NULL"
            f" ELSE 1 END"
        )
    elif expr.quantifier == "single":
        return (
            f"CASE WHEN (({satisfy_sql}) = 1) THEN 1"
            f" WHEN (({satisfy_sql}) = 0) AND (({null_count_sql}) > 0) THEN NULL"
            f" ELSE 0 END"
        )
    else:  # any
        return (
            f"CASE WHEN (({satisfy_sql}) > 0) THEN 1"
            f" WHEN (({null_count_sql}) > 0) THEN NULL"
            f" ELSE 0 END"
        )


def _expr_list_comprehension(expr, context, segment):
    source_sql = translate_expression(expr.source, context, segment="inline")
    var = sanitize_identifier(expr.variable)
    alias = context.next_alias("lc")
    context.variable_aliases[expr.variable] = alias
    where_clause = ""
    if expr.predicate:
        if isinstance(expr.predicate, ast.BooleanExpression):
            pred_sql = translate_boolean_expression(expr.predicate, context)
        else:
            pred_sql = translate_expression(expr.predicate, context, segment="inline")
        for col in ("node_id", "p", "val", "label"):
            pred_sql = pred_sql.replace(f"{alias}.{col}", f"{alias}.{var}")
        where_clause = f" WHERE {pred_sql}"
    select_expr = f"{alias}.{var}"
    if expr.projection:
        proj_sql = translate_expression(expr.projection, context, segment="inline")
        for col in ("node_id", "p", "val", "label"):
            proj_sql = proj_sql.replace(f"{alias}.{col}", f"{alias}.{var}")
        select_expr = proj_sql
    del context.variable_aliases[expr.variable]
    return (
        f"(SELECT JSON_ARRAYAGG({select_expr}) FROM "
        f"JSON_TABLE({source_sql}, '$[*]' COLUMNS({var} INTEGER PATH '$')) {alias}"
        f"{where_clause})"
    )


def _expr_reduce(expr, context, segment):
    var = sanitize_identifier(expr.variable)
    acc = expr.accumulator

    try:
        init_val = float(expr.init.value) if hasattr(expr.init, "value") else 0.0
    except Exception:
        init_val = 0.0

    if (
        isinstance(expr.source, ast.AggregationFunction)
        and expr.source.function_name.lower() == "collect"
        and expr.source.argument is not None
    ):
        collect_arg = expr.source.argument
        collect_sql = translate_expression(collect_arg, context, segment=segment)
        return f"({init_val} + SUM(CAST({collect_sql} AS DOUBLE)))"

    source_sql = translate_expression(expr.source, context, segment=segment)
    alias = context.next_alias("re")
    context.variable_aliases[expr.variable] = alias
    context.variable_aliases[acc] = "__acc__"
    body_sql = translate_expression(expr.body, context, segment=segment)
    for col in ("node_id", "p", "val", "label"):
        body_sql = body_sql.replace(f"{alias}.{col}", f"{alias}.{var}")
    body_sql = body_sql.replace("__acc__.node_id", "0").replace("__acc__", "0")
    del context.variable_aliases[expr.variable]
    del context.variable_aliases[acc]
    init_sql = translate_expression(expr.init, context, segment=segment)
    return (
        f"({init_sql} + (SELECT SUM({body_sql}) FROM "
        f"JSON_TABLE({source_sql}, '$[*]' COLUMNS({var} DOUBLE PATH '$')) {alias}))"
    )


def _expr_case(expr, context, segment):
    parts = ["CASE"]
    if expr.test_expression is not None:
        parts.append(translate_expression(expr.test_expression, context, segment))
    for wc in expr.when_clauses:
        if isinstance(wc.condition, ast.BooleanExpression):
            cond = translate_boolean_expression(wc.condition, context)
        else:
            cond = translate_expression(wc.condition, context, segment)
        res = _inline_literal(wc.result)
        if res is None:
            res = translate_expression(wc.result, context, segment)
        parts.append(f"WHEN {cond} THEN {res}")
    else_res = (
        _inline_literal(expr.else_result) if expr.else_result is not None else None
    )
    if else_res is None and expr.else_result is not None:
        else_res = translate_expression(expr.else_result, context, segment)
    if else_res is not None:
        parts.append(f"ELSE {else_res}")
    parts.append("END")
    return " ".join(parts)


def _expr_propref_temporal(expr, context, alias):
    cte_alias = context.temporal_rel_ctes.get(expr.variable)
    if cte_alias is not None:
        if expr.property_name == "ts":
            return f"{cte_alias}.ts"
        if expr.property_name in ("weight", "w"):
            return f"{cte_alias}.weight"
    temporal_node_col = getattr(context, "temporal_node_col", {})
    if expr.variable in temporal_node_col:
        col = temporal_node_col[expr.variable]
        cte_name = context.variable_aliases[expr.variable]
        if expr.property_name in ("id", "node_id"):
            return f"{cte_name}.{col}"
    if (
        expr.property_name in ("ts", "weight", "w")
        and expr.variable not in context.temporal_rel_ctes
    ):
        if alias and alias.startswith("e"):
            m = getattr(context, "_metadata", None)
            if m is not None:
                m.warnings.append(
                    f"{expr.variable}.{expr.property_name} in RETURN without WHERE {expr.variable}.ts filter "
                    f"— {expr.property_name} will be NULL. Add WHERE {expr.variable}.ts >= $start AND "
                    f"{expr.variable}.ts <= $end for temporal routing."
                )
            return "NULL"
    return None


def _expr_propref_edge_alias(expr, context, alias):
    is_undirected = alias in getattr(context, "_undirected_aliases", set())
    is_edgescan = alias in getattr(context, "_edgescan_aliases", set())
    if expr.property_name == "p":
        return f"{alias}.{'_p' if is_undirected else 'p'}"
    if expr.property_name == "s":
        return f"{alias}.{'_src' if is_undirected else 's'}"
    if expr.property_name == "o_id":
        return f"{alias}.{'_dst' if is_undirected else 'o_id'}"
    if is_undirected or is_edgescan:
        return "NULL"
    return f"CASE WHEN {alias}.qualifiers IS NULL THEN NULL ELSE SQLUser.JSON_VALUE({alias}.qualifiers, '$.{expr.property_name}') END"


def _expr_property_reference(expr, context, segment):
    alias = context.variable_aliases.get(expr.variable)
    if not alias:
        raise SyntaxError(f"Undefined variable: {expr.variable}")
    temporal = _expr_propref_temporal(expr, context, alias)
    if temporal is not None:
        return temporal
    if alias in context.mapped_node_aliases:
        mapping = context.mapped_node_aliases[alias]
        if expr.property_name in ("id", "node_id"):
            return f"{alias}.{sanitize_identifier(mapping['id_column'])}"
        return f"{alias}.{sanitize_identifier(expr.property_name)}"
    if alias.startswith("Stage"):
        if expr.property_name in ("node_id", "id"):
            return _safe_alias(expr.variable)
        # Edge-qualifiers variables: use JSON_VALUE on the column value (Stage column = qualifiers JSON)
        edge_stage_vars = getattr(context, "edge_stage_variables", set())
        if expr.variable in context.scalar_variables or expr.variable in edge_stage_vars:
            return f"CASE WHEN {_safe_alias(expr.variable)} IS NULL THEN NULL ELSE SQLUser.JSON_VALUE({_safe_alias(expr.variable)}, '$.{expr.property_name}') END"
        # Node variables from Stage: JOIN rdf_props to get the property value
        # The Stage column contains the node ID, so use it directly
        p_alias = context.next_alias("p")
        stage_col = _safe_alias(expr.variable)
        context.join_clauses.append(
            f'LEFT JOIN {_table("rdf_props")} {p_alias} ON {p_alias}.s = {stage_col} AND {p_alias}."key" = {context.add_join_param(expr.property_name)}'
        )
        return f"{p_alias}.val"
    if alias.startswith("e") and not alias.startswith("ES_"):
        return _expr_propref_edge_alias(expr, context, alias)
    # Scalar variable from JSON_TABLE (list predicate / list comprehension): use JSON_VALUE
    # not rdf_props join.  The column holds a JSON-serialised value, not a graph node id.
    # Guard: only call JSON_VALUE when the value is a JSON object (starts with '{').
    # JSON_VALUE raises SQLCODE=-400 on non-JSON or non-matching path.
    if expr.variable in context.scalar_variables:
        col_ref = f"{alias}.{sanitize_identifier(expr.variable)}"
        prop = expr.property_name.replace("'", "''")
        return (
            f"CASE WHEN ({col_ref}) IS NULL OR SUBSTRING({col_ref}, 1, 1) <> '{{' "
            f"THEN NULL ELSE SQLUser.JSON_VALUE({col_ref}, '$.{prop}') END"
        )
    if expr.property_name == "node_id":
        return f"{alias}.node_id"
    if segment == "where":
        context.where_conditions.append(
            TranslationContext._structural_guard_sql(alias, expr.property_name)
        )
    p_alias = context.next_alias("p")
    context.join_clauses.append(
        f'LEFT JOIN {_table("rdf_props")} {p_alias} ON {p_alias}.s = {alias}.node_id AND {p_alias}."key" = {context.add_join_param(expr.property_name)}'
    )
    return f"{p_alias}.val"



def _expr_map_projection(expr, context, segment):
    alias = context.variable_aliases.get(expr.variable, "")
    parts = []
    for key_spec, _ in expr.keys:
        prop = key_spec.lstrip(".")
        p_alias = context.next_alias("p")
        context.join_clauses.append(
            f"LEFT JOIN {_table('rdf_props')} {p_alias} ON {p_alias}.s = {alias}.node_id AND {p_alias}.\"key\" = {context.add_join_param(prop)}"
        )
        safe_prop = prop.replace("'", "''")
        parts.append(f"'\"'||'{safe_prop}'||'\":'||COALESCE('\"'||{p_alias}.val||'\"','null')")
    if not parts:
        return "'{}'"
    return "('{'||" + "||','||".join(parts) + "||'}')"


def _expr_map_literal(expr, context, segment):
    if not expr.entries:
        return "'{}'"
    if _is_fully_literal(expr):
        import json as _json
        py_val = _literal_to_python(expr)
        json_str = _json.dumps(py_val)
        str_len = max(len(json_str) + 1, 256)
        escaped = json_str.replace("'", "''")
        return f"CAST('{escaped}' AS VARCHAR({str_len}))"
    parts = []
    for k, v in expr.entries.items():
        safe_k = k.replace("'", "''")
        if isinstance(v, ast.Literal) and v.value is None:
            parts.append(f"'\"'||'{safe_k}'||'\":null'")
        elif isinstance(v, ast.Literal) and isinstance(v.value, bool):
            bval = "true" if v.value else "false"
            parts.append(f"'\"'||'{safe_k}'||'\":{bval}'")
        elif isinstance(v, ast.Literal) and isinstance(v.value, (int, float)):
            parts.append(f"'\"'||'{safe_k}'||'\":'||CAST({v.value} AS VARCHAR)")
        elif isinstance(v, ast.Literal) and isinstance(v.value, str):
            safe_v = v.value.replace("\\", "\\\\").replace('"', '\\"').replace("'", "''")
            parts.append(f"'\"'||'{safe_k}'||'\":\"'||'{safe_v}'||'\"'")
        elif isinstance(v, ast.MapLiteral):
            # Nested map: the value is already a JSON object — no extra quotes
            val_sql = translate_expression(v, context, segment=segment)
            parts.append(f"'\"'||'{safe_k}'||'\":'||CAST({val_sql} AS VARCHAR)")
        elif isinstance(v, ast.Literal) and isinstance(v.value, list):
            # Nested list literal: already a JSON array — no extra quotes
            val_sql = translate_expression(v, context, segment=segment)
            parts.append(f"'\"'||'{safe_k}'||'\":'||CAST({val_sql} AS VARCHAR)")
        else:
            val_sql = translate_expression(v, context, segment=segment)
            parts.append(f"'\"'||'{safe_k}'||'\":\"'||CAST({val_sql} AS VARCHAR)||'\"'")
    inner = " || ',' || ".join(parts)
    return f"('{{'||{inner}||'}}')"


def _expr_subscript(expr, context, segment):
    base = expr.expression
    idx = expr.index
    if isinstance(base, ast.Variable):
        base_alias = context.variable_aliases.get(base.name, "")
        is_scalar = base_alias.startswith("Stage") or base.name in context.scalar_variables
        if not is_scalar:
            # base is a node variable — subscript is a property key expression
            node_alias = base_alias
            node_ref = f"{node_alias}.node_id" if node_alias else "NULL"
            p_alias = context.next_alias("dp")
            if isinstance(idx, ast.Variable):
                key_val = context.input_params.get(idx.name, idx.name)
                key_sql = context.add_join_param(key_val)
            else:
                key_sql = translate_expression(idx, context, segment="join")
            context.join_clauses.append(
                f"LEFT JOIN {_table('rdf_props')} {p_alias} ON {p_alias}.s = {node_ref} AND {p_alias}.\"key\" = {key_sql}"
            )
            return f"{p_alias}.val"
        # Scalar variable — use JSON array index or key lookup
        base_sql = translate_expression(base, context, segment=segment)
        idx_sql = translate_expression(idx, context, segment=segment)
        return f"SQLUser.JSON_VALUE({base_sql}, '$[' || CAST(({idx_sql}) AS VARCHAR) || ']')"
    base_sql = translate_expression(base, context, segment=segment)
    if isinstance(idx, ast.Literal) and isinstance(idx.value, int):
        i = idx.value
        return (
            f"(SELECT elem FROM JSON_TABLE({base_sql}, "
            f"'$[{i}]' COLUMNS (elem VARCHAR(1000) PATH '$')) __jt)"
        )
    idx_sql = translate_expression(idx, context, segment=segment)
    return f"SQLUser.JSON_VALUE({base_sql}, '$[' || CAST(({idx_sql}) AS VARCHAR) || ']')"


def _expr_slice(expr, context, segment):
    base_sql = translate_expression(expr.expression, context, segment=segment)
    start_val = expr.start.value if isinstance(expr.start, ast.Literal) else None
    end_val = expr.end.value if isinstance(expr.end, ast.Literal) else None
    jt_alias = context.next_alias("slc")
    if start_val is not None and end_val is not None:
        s = int(start_val)
        e = int(end_val)
        if e <= s:
            return _EMPTY_JSON_ARRAY
        return (
            f"(SELECT JSON_ARRAYAGG(elem) FROM "
            f"(SELECT elem, ROW_NUMBER() OVER() AS rn "
            f"FROM JSON_TABLE({base_sql}, '$[*]' COLUMNS(elem VARCHAR(1000) PATH '$')) {jt_alias}) __sliced "
            f"WHERE rn > {s} AND rn <= {e})"
        )
    start_sql = translate_expression(expr.start, context, segment=segment) if expr.start is not None else "0"
    end_sql = translate_expression(expr.end, context, segment=segment) if expr.end is not None else f"SQLUser.JSON_ARRAYLENGTH({base_sql})"
    return (
        f"(SELECT JSON_ARRAYAGG(elem) FROM "
        f"(SELECT elem, ROW_NUMBER() OVER() AS rn "
        f"FROM JSON_TABLE({base_sql}, '$[*]' COLUMNS(elem VARCHAR(1000) PATH '$')) {jt_alias}) __sliced "
        f"WHERE rn > ({start_sql}) AND rn <= ({end_sql}))"
    )


def _expr_property_access(expr, context, segment):
    base_sql = translate_expression(expr.expression, context, segment=segment)
    prop = expr.property_name.replace("'", "''")
    return f"CASE WHEN ({base_sql}) IS NULL THEN NULL ELSE SQLUser.JSON_VALUE({base_sql}, '$.{prop}') END"


def _expr_variable(expr, context, segment):
    alias = context.variable_aliases.get(expr.name)
    if alias == "__foreach_literal__":
        val = getattr(context, "foreach_literals", {}).get(expr.name)
        if val is not None:
            if isinstance(val, str):
                safe = val.replace("'", "''")
                return f"'{safe}'"
            if isinstance(val, bool):
                return "1" if val else "0"
            return str(val)
    if not alias:
        if expr.name in context.input_params:
            v = context.input_params[expr.name]
            if segment == "select":
                return context.add_select_param(v)
            if segment == "join":
                return context.add_join_param(v)
            return context.add_where_param(v)
        raise SyntaxError(f"Undefined variable: {expr.name}")
    if alias.startswith("Stage"):
        return _safe_alias(expr.name)
    if alias.startswith("e"):
        is_undirected = alias in getattr(context, "_undirected_aliases", set())
        return f"{alias}.{'_p' if is_undirected else 'p'}"
    if expr.name in context.scalar_variables:
        if alias == "scalar" or alias in _PROC_CTE_ALIASES:
            return expr.name
        return f"{alias}.{expr.name}"
    if alias in context.mapped_node_aliases:
        mapping = context.mapped_node_aliases[alias]
        return f"{alias}.{sanitize_identifier(mapping['id_column'])}"
    return f"{alias}.node_id"


def _is_fully_literal(node):
    """Return True if node is fully evaluable at translate-time (no variables/exprs)."""
    if isinstance(node, ast.Literal):
        v = node.value
        if isinstance(v, list):
            return all(_is_fully_literal(item) for item in v)
        return True  # scalar Literal
    if isinstance(node, ast.MapLiteral):
        return all(_is_fully_literal(val) for val in node.entries.values())
    return False


def _literal_to_python(node):
    """Extract Python value from a fully-literal AST node."""
    if isinstance(node, ast.Literal):
        v = node.value
        if isinstance(v, list):
            return [_literal_to_python(item) for item in v]
        if v is True: return True
        if v is False: return False
        return v
    if isinstance(node, ast.MapLiteral):
        return {k: _literal_to_python(val) for k, val in node.entries.items()}
    return None


def _expr_literal(expr, context, segment):
    import json as _json
    v = expr.value
    if v is True:
        return "1"
    if v is False:
        return "0"
    if v is None:
        return "NULL"
    if isinstance(v, list):
        # When ALL items are fully literal (including nested lists/maps), serialize
        # the whole structure as a JSON string.  This avoids IRIS embedding nested
        # arrays as VARCHAR strings (e.g. JSON_ARRAY(CAST('[1,2]' AS VARCHAR)) → ["[1,2]"]).
        if _is_fully_literal(expr):
            py_val = _literal_to_python(expr)
            json_str = _json.dumps(py_val)
            str_len = max(len(json_str) + 1, 256)
            escaped = json_str.replace("'", "''")
            return f"CAST('{escaped}' AS VARCHAR({str_len}))"
        sql_items = []
        for item in v:
            if isinstance(item, ast.Literal):
                iv = item.value
                if iv is True: sql_items.append("1")
                elif iv is False: sql_items.append("0")
                elif iv is None: sql_items.append("NULL")
                elif isinstance(iv, str): sql_items.append(f"'{iv.replace(chr(39), chr(39)+chr(39))}'")
                elif isinstance(iv, list):
                    # Nested list literal — recursively translate via the list branch
                    sql_items.append(translate_expression(item, context, segment=segment))
                else: sql_items.append(str(iv))
            else:
                sql_items.append(translate_expression(item, context, segment=segment))
        return f"JSON_ARRAY({', '.join(sql_items)})"
    if isinstance(v, float):
        import math as _math
        if _math.isinf(v) or _math.isnan(v):
            if segment == "select":
                return context.add_select_param(v)
            if segment == "join":
                return context.add_join_param(v)
            if segment == "inline":
                return repr(v)
            return context.add_where_param(v)
        float_str = repr(v)
        return f"CAST({float_str!r} AS DOUBLE)"
    if isinstance(v, str):
        escaped = v.replace("'", "''")
        str_len = max(len(v) + 1, 256)
        return f"CAST('{escaped}' AS VARCHAR({str_len}))"
    if segment == "select":
        return context.add_select_param(v)
    if segment == "join":
        return context.add_join_param(v)
    if segment == "inline":
        if isinstance(v, str): return f"'{v.replace(chr(39), chr(39)+chr(39))}'"
        return str(v)
    return context.add_where_param(v)


def _expr_aggregation(expr, context, segment):
    if expr.argument and isinstance(expr.argument, ast.Literal):
        v = expr.argument.value
        if v is True: arg = "1"
        elif v is False: arg = "0"
        elif v is None: arg = "NULL"
        elif isinstance(v, str): arg = f"'{v.replace(chr(39), chr(39)+chr(39))}'"
        elif isinstance(v, list): arg = _expr_literal(expr.argument, context, segment)
        else: arg = str(v)
    else:
        arg = (
            translate_expression(expr.argument, context, segment=segment)
            if expr.argument
            else "*"
        )
    fn = (
        "JSON_ARRAYAGG"
        if expr.function_name.upper() == "COLLECT"
        else expr.function_name.upper()
    )
    return f"{fn}({'DISTINCT ' if expr.distinct else ''}{arg})"


def _scalar_coalesce(fn, args, args_exprs):
    if fn == "coalesce":
        if len(args) >= 2 and args_exprs:
            coerced = []
            for i, (arg, arg_expr) in enumerate(zip(args, args_exprs)):
                if i == 0:
                    coerced.append(f"CAST({arg} AS VARCHAR(4096))")
                elif isinstance(arg_expr, ast.Literal) and not isinstance(arg_expr.value, str) and arg_expr.value is not None:
                    coerced.append(f"CAST({arg} AS VARCHAR(4096))")
                else:
                    coerced.append(arg)
            return f"COALESCE({', '.join(coerced)})"
        return f"COALESCE({', '.join(args)})" if args else "NULL"
    return None


def _scalar_string(fn, args, args_exprs):
    if fn == "tointeger":
        return f"CASE WHEN ISNUMERIC({args[0]}) = 1 THEN CAST({args[0]} AS INTEGER) ELSE NULL END"
    if fn == "tofloat":
        return f"CASE WHEN ISNUMERIC({args[0]}) = 1 THEN CAST({args[0]} AS DOUBLE) ELSE NULL END"
    if fn == "tostring":
        if args_exprs and isinstance(args_exprs[0], ast.Literal) and isinstance(args_exprs[0].value, bool):
            return f"'{'true' if args_exprs[0].value else 'false'}'"
        return f"CAST({args[0]} AS VARCHAR(4096))"
    if fn == "substring":
        if len(args) >= 2:
            start = f"({args[1]}) + 1"
            if len(args) >= 3:
                return f"SUBSTRING({args[0]}, {start}, {args[2]})"
            return f"SUBSTRING({args[0]}, {start})"
        return f"SUBSTRING({args[0]})"
    if fn == "reverse":
        if not args:
            return "NULL"
        arg_expr = args_exprs[0] if args_exprs else None
        is_list = (
            isinstance(arg_expr, ast.Literal) and isinstance(arg_expr.value, list)
        ) or isinstance(arg_expr, ast.ListComprehension)
        if is_list:
            return f"SQLUser.LIST_REVERSE({args[0]})"
        return f"REVERSE({args[0]})"
    if fn == "split":
        return f"SQLUser.STR_SPLIT({args[0]}, {args[1]})" if len(args) >= 2 else "NULL"
    return None


def _extract_int_from_map_entry(map_literal, key, default=0):
    if key not in map_literal.entries:
        return default
    val_expr = map_literal.entries[key]
    if isinstance(val_expr, ast.Literal) and isinstance(val_expr.value, int):
        return val_expr.value
    return default


def _extract_num_from_map_entry(map_literal, key, default=0):
    """Like _extract_int_from_map_entry but also returns floats."""
    if key not in map_literal.entries:
        return default
    val_expr = map_literal.entries[key]
    if isinstance(val_expr, ast.Literal) and isinstance(val_expr.value, (int, float)):
        return val_expr.value
    return default


def _has_map_key(map_literal, key):
    return key in map_literal.entries


def _date_from_iso_week(year, week, dow=1):
    """Compute date for ISO week date (year, week, day-of-week where Mon=1)."""
    import datetime as _dt
    # ISO week 1 is the week containing the first Thursday of the year.
    # Jan 4 is always in ISO week 1.
    jan4 = _dt.date(year, 1, 4)
    # Monday of ISO week 1
    week1_monday = jan4 - _dt.timedelta(days=jan4.isoweekday() - 1)
    return week1_monday + _dt.timedelta(weeks=week - 1, days=dow - 1)


def _date_from_ordinal_day(year, ordinal):
    """Convert year + ordinal day (1-based) to date."""
    import datetime as _dt
    return _dt.date(year, 1, 1) + _dt.timedelta(days=ordinal - 1)


def _date_from_quarter(year, quarter, day_of_quarter=1):
    """Convert year + quarter + day-of-quarter to date."""
    import datetime as _dt
    first_month = (quarter - 1) * 3 + 1
    start = _dt.date(year, first_month, 1)
    return start + _dt.timedelta(days=day_of_quarter - 1)


def _normalize_tz_str(tz):
    """Normalize a timezone suffix: compact +0100 → +01:00, -00:00 → Z, etc."""
    import re as _re
    if not tz:
        return ""
    if tz in ("Z", "z"):
        return "Z"
    # +HHMM or -HHMM (no colon, 4 digits) → +HH:MM
    m = _re.match(r'^([+-])(\d{2})(\d{2})$', tz)
    if m:
        sign, hh, mm = m.group(1), m.group(2), m.group(3)
        if (sign == "-" or sign == "+") and hh == "00" and mm == "00":
            return "Z"
        return f"{sign}{hh}:{mm}"
    # +HH:MM or -HH:MM (with colon)
    m = _re.match(r'^([+-])(\d{2}):(\d{2})$', tz)
    if m:
        sign, hh, mm = m.group(1), m.group(2), m.group(3)
        if hh == "00" and mm == "00":
            return "Z"
        return f"{sign}{hh}:{mm}"
    # +HH or -HH (hours only, 2 digits) → +HH:00
    m = _re.match(r'^([+-])(\d{2})$', tz)
    if m:
        sign, hh = m.group(1), m.group(2)
        return f"{sign}{hh}:00"
    # -HH:MM:SS → keep as-is, but strip :00 seconds
    return _normalize_tz_offset(tz)


def _iana_tz_offset(iana_name, ref_year=2015, ref_month=7, ref_day=21):
    """Return '+HH:MM' or '+HH:MM:SS' offset for IANA timezone at reference date."""
    try:
        from zoneinfo import ZoneInfo as _ZI
        import datetime as _dt2
        _zi = _ZI(iana_name)
        _aware = _dt2.datetime(ref_year, ref_month, ref_day, tzinfo=_zi)
        _off = _aware.utcoffset()
        _total_s = int(_off.total_seconds())
        _sign = "+" if _total_s >= 0 else "-"
        _abs_s = abs(_total_s)
        _hh = _abs_s // 3600
        _mm = (_abs_s % 3600) // 60
        _ss = _abs_s % 60
        if _ss:
            return f"{_sign}{_hh:02d}:{_mm:02d}:{_ss:02d}"
        return f"{_sign}{_hh:02d}:{_mm:02d}"
    except Exception:
        return ""


def _parse_time_string(s, ref_year=2015, ref_month=7, ref_day=21):
    """
    Parse ISO 8601 time string to normalized form 'HH:MM[:SS[.frac]][tz]'.
    Returns normalized string or None.
    """
    import re as _re
    s = s.strip()
    # Split off IANA bracket zone [Name]
    iana_suffix = ""
    iana_name = ""
    m_iana = _re.match(r'^(.*?)(\[([^\]]+)\])$', s)
    if m_iana:
        s = m_iana.group(1)
        iana_name = m_iana.group(3)
        iana_suffix = f"[{iana_name}]"

    # Split off timezone
    tz = ""
    m = _re.match(r'^(.*?)([Zz]|[+-]\d{2}(?::?\d{2}(?::?\d{2})?)?)$', s)
    if m:
        time_part = m.group(1)
        tz_raw = m.group(2)
        tz = _normalize_tz_str(tz_raw)
        # If no explicit offset but IANA zone given, compute the offset
        if iana_name and not tz:
            tz = _iana_tz_offset(iana_name, ref_year, ref_month, ref_day)
    else:
        time_part = s
        if iana_name:
            tz = _iana_tz_offset(iana_name, ref_year, ref_month, ref_day)

    # Extended: HH:MM[:SS[.frac]]
    m = _re.match(r'^(\d{2}):(\d{2})(?::(\d{2})(?:\.(\d+))?)?$', time_part)
    if m:
        h, mi, s_str, frac = m.group(1), m.group(2), m.group(3), m.group(4)
        if s_str:
            if frac:
                return f"{h}:{mi}:{s_str}.{frac}{tz}{iana_suffix}"
            return f"{h}:{mi}:{s_str}{tz}{iana_suffix}"
        return f"{h}:{mi}{tz}{iana_suffix}"

    # Compact: HHMMSS.frac or HHMMSS or HHMM or HH
    m = _re.match(r'^(\d{2})(?:(\d{2})(?:(\d{2})(?:\.(\d+))?)?)?$', time_part)
    if m:
        h = m.group(1)
        mi = m.group(2) or "00"
        s_str = m.group(3)
        frac = m.group(4)
        if s_str:
            if frac:
                return f"{h}:{mi}:{s_str}.{frac}{tz}{iana_suffix}"
            return f"{h}:{mi}:{s_str}{tz}{iana_suffix}"
        if m.group(2):
            return f"{h}:{mi}{tz}{iana_suffix}"
        return f"{h}:00{tz}{iana_suffix}"

    return None


def _parse_datetime_string(s):
    """
    Parse ISO 8601 datetime string (with T separator) to normalized form.
    Returns normalized string or None.
    """
    s = s.strip()
    # Split at T
    if "T" not in s and "t" not in s:
        return None
    sep_idx = s.upper().index("T")
    date_str = s[:sep_idx]
    rest = s[sep_idx + 1:]

    parsed_date = _parse_date_string(date_str)
    if not parsed_date:
        return None
    y_out, mo_out, d_out = parsed_date

    parsed_time = _parse_time_string(rest, ref_year=y_out, ref_month=mo_out, ref_day=d_out)
    if not parsed_time:
        return None

    return f"{y_out:04d}-{mo_out:02d}-{d_out:02d}T{parsed_time}"


def _parse_duration_string(s):
    """
    Parse ISO 8601 duration string into (years, months, days, hours, minutes, seconds_ns_total).
    Handles fractional components. Returns normalized ISO 8601 string or None.
    """
    import re as _re

    s = s.strip()
    # Calendar notation: P2012-02-02T14:37:21.545 → Pyyyy-mm-ddThh:mm:ss.frac
    m = _re.match(r'^P(-?\d+)-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.(\d+))?$', s)
    if m:
        yr, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        h, mi, sec = int(m.group(4)), int(m.group(5)), int(m.group(6))
        frac_str = m.group(7) or ""
        rem_ns = int(frac_str.ljust(9, '0')[:9]) if frac_str else 0
        return _format_duration(yr, mo, d, h, mi, sec, rem_ns)

    # Standard: PnYnMnWnDTnHnMnS with possible fractions on last component
    m = _re.match(
        r'^P'
        r'(?:(-?[\d.]+)Y)?'
        r'(?:(-?[\d.]+)M)?'
        r'(?:(-?[\d.]+)W)?'
        r'(?:(-?[\d.]+)D)?'
        r'(?:T'
        r'(?:(-?[\d.]+)H)?'
        r'(?:(-?[\d.]+)M)?'
        r'(?:(-?[\d.]+)S)?'
        r')?$',
        s
    )
    if not m or not any(m.groups()):
        return None

    years = float(m.group(1) or 0)
    months = float(m.group(2) or 0)
    weeks = float(m.group(3) or 0)
    days = float(m.group(4) or 0)
    hours = float(m.group(5) or 0)
    minutes = float(m.group(6) or 0)
    seconds = float(m.group(7) or 0)

    # Normalize fractional components
    mo_int = int(months)
    mo_frac = months - mo_int
    days = days + mo_frac * 30.436875
    days = days + weeks * 7

    d_int = int(days)
    d_frac = days - d_int
    hours = hours + d_frac * 24

    h_int = int(hours)
    h_frac = hours - h_int
    minutes = minutes + h_frac * 60

    m_int = int(minutes)
    m_frac = minutes - m_int
    seconds = seconds + m_frac * 60

    s_int = int(seconds)
    s_frac = seconds - s_int
    rem_ns = round(s_frac * 1_000_000_000)

    # Normalize seconds → minutes → hours → days
    extra_s = rem_ns // 1_000_000_000
    rem_ns = rem_ns % 1_000_000_000
    s_int += extra_s
    extra_m = s_int // 60
    s_int = s_int % 60
    m_int += extra_m
    extra_h = m_int // 60
    m_int = m_int % 60
    h_int += extra_h
    extra_d = h_int // 24
    h_int = h_int % 24
    d_int += extra_d

    return _format_duration(int(years), mo_int, d_int, h_int, m_int, s_int, rem_ns)


def _format_duration(yr_int, mo_int, d_int, h_int, m_int, s_int, rem_ns):
    date_part = ""
    if yr_int:
        date_part += f"{yr_int}Y"
    if mo_int:
        date_part += f"{mo_int}M"
    if d_int:
        date_part += f"{d_int}D"
    time_part = ""
    if h_int:
        time_part += f"{h_int}H"
    if m_int:
        time_part += f"{m_int}M"
    if rem_ns > 0:
        ns_str = f"{rem_ns:09d}".rstrip('0')
        time_part += f"{s_int}.{ns_str}S"
    elif s_int:
        time_part += f"{s_int}S"
    if not date_part and not time_part:
        return "PT0S"
    if time_part:
        return f"P{date_part}T{time_part}"
    return f"P{date_part}"


def _normalize_tz_offset(tz):
    """Normalize timezone offset string: strip trailing :00 seconds component."""
    import re as _re
    # +HH:MM:00 → +HH:MM, but keep +HH:MM:SS if SS != 00
    m = _re.match(r'^([+-]\d{2}:\d{2}):00$', tz)
    if m:
        return m.group(1)
    return tz


def _subsecond_frac(ns, us, ms):
    """Combine nanosecond/microsecond/millisecond into 9-digit nanosecond fraction string, strip trailing zeros."""
    total_ns = 0
    if ms >= 0:
        total_ns += ms * 1_000_000
    if us >= 0:
        total_ns += us * 1_000
    if ns >= 0:
        total_ns += ns
    if ms < 0 and us < 0 and ns < 0:
        return None
    if total_ns == 0:
        return None
    raw = f"{total_ns:09d}"
    return raw.rstrip('0')


def _parse_date_string(s):
    """Parse ISO 8601 date string to (year, month, day). Returns None on failure."""
    import datetime as _dt
    import re as _re
    s = s.strip()
    # YYYY-MM-DD or YYYYMMDD
    m = _re.match(r'^(\d{4})-(\d{2})-(\d{2})$', s)
    if m:
        return int(m.group(1)), int(m.group(2)), int(m.group(3))
    m = _re.match(r'^(\d{4})(\d{2})(\d{2})$', s)
    if m:
        return int(m.group(1)), int(m.group(2)), int(m.group(3))
    # YYYY-MM or YYYYMM → first day of month
    m = _re.match(r'^(\d{4})-(\d{2})$', s)
    if m:
        return int(m.group(1)), int(m.group(2)), 1
    m = _re.match(r'^(\d{4})(\d{2})$', s)
    if m:
        return int(m.group(1)), int(m.group(2)), 1
    # YYYY → Jan 1
    m = _re.match(r'^(\d{4})$', s)
    if m:
        return int(m.group(1)), 1, 1
    # YYYY-Www-D or YYYYWwwD
    m = _re.match(r'^(\d{4})-W(\d{2})-(\d)$', s)
    if m:
        d = _date_from_iso_week(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        return d.year, d.month, d.day
    m = _re.match(r'^(\d{4})W(\d{2})(\d)$', s)
    if m:
        d = _date_from_iso_week(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        return d.year, d.month, d.day
    # YYYY-Www or YYYYWww → Monday of that week
    m = _re.match(r'^(\d{4})-W(\d{2})$', s)
    if m:
        d = _date_from_iso_week(int(m.group(1)), int(m.group(2)), 1)
        return d.year, d.month, d.day
    m = _re.match(r'^(\d{4})W(\d{2})$', s)
    if m:
        d = _date_from_iso_week(int(m.group(1)), int(m.group(2)), 1)
        return d.year, d.month, d.day
    # YYYY-DDD or YYYYDDD (ordinal)
    m = _re.match(r'^(\d{4})-(\d{3})$', s)
    if m:
        d = _date_from_ordinal_day(int(m.group(1)), int(m.group(2)))
        return d.year, d.month, d.day
    m = _re.match(r'^(\d{4})(\d{3})$', s)
    if m:
        d = _date_from_ordinal_day(int(m.group(1)), int(m.group(2)))
        return d.year, d.month, d.day
    return None


def _build_date_from_map(m, with_time=False, with_tz=False):
    """
    Build a date/datetime string from a MapLiteral.
    Returns None if map contains non-literal (dynamic) expressions.
    """
    import datetime as _dt

    # Resolve 'date' base key (string override)
    base_year, base_month, base_day = None, None, None
    base_iso_year = None  # ISO week-year may differ from calendar year
    base_iso_week_day = None  # ISO weekday (Mon=1) of the base date
    if _has_map_key(m, "date"):
        base_expr = m.entries["date"]
        # date('YYYY-MM-DD') nested call
        if (isinstance(base_expr, ast.FunctionCall) and
                base_expr.function_name.lower() == "date" and
                base_expr.arguments and
                isinstance(base_expr.arguments[0], ast.Literal) and
                isinstance(base_expr.arguments[0].value, str)):
            parsed = _parse_date_string(base_expr.arguments[0].value)
            if parsed:
                base_year, base_month, base_day = parsed
                base_dt = _dt.date(base_year, base_month, base_day)
                iso_cal = base_dt.isocalendar()
                base_iso_year = iso_cal[0]
                base_iso_week_day = iso_cal[2]
        if base_year is None:
            return None  # dynamic base date — can't resolve at compile time

    # year (for week calculations, use ISO week-year from base date if year not explicit)
    if _has_map_key(m, "year"):
        year = _extract_int_from_map_entry(m, "year", 1970)
        iso_year = year
    elif base_year is not None:
        year = base_year
        iso_year = base_iso_year if base_iso_year is not None else base_year
    else:
        year = 1970
        iso_year = 1970

    # Determine date component based on which keys are present
    if _has_map_key(m, "week"):
        week = _extract_int_from_map_entry(m, "week", 1)
        # Default dayOfWeek: from explicit key, or base date's weekday, or 1 (Monday)
        if _has_map_key(m, "dayOfWeek"):
            dow = _extract_int_from_map_entry(m, "dayOfWeek", 1)
        elif base_iso_week_day is not None:
            dow = base_iso_week_day
        else:
            dow = 1
        try:
            d = _date_from_iso_week(iso_year, week, dow)
            y_out, mo_out, d_out = d.year, d.month, d.day
        except Exception:
            return None
    elif _has_map_key(m, "ordinalDay"):
        ordinal = _extract_int_from_map_entry(m, "ordinalDay", 1)
        try:
            d = _date_from_ordinal_day(year, ordinal)
            y_out, mo_out, d_out = d.year, d.month, d.day
        except Exception:
            return None
    elif _has_map_key(m, "quarter"):
        quarter = _extract_int_from_map_entry(m, "quarter", 1)
        doq = _extract_int_from_map_entry(m, "dayOfQuarter", 1)
        try:
            d = _date_from_quarter(year, quarter, doq)
            y_out, mo_out, d_out = d.year, d.month, d.day
        except Exception:
            return None
    else:
        mo_out = _extract_int_from_map_entry(m, "month", base_month if base_month else 1)
        d_out = _extract_int_from_map_entry(m, "day", base_day if base_day else 1)
        y_out = year

    if not with_time:
        return f"'{y_out:04d}-{mo_out:02d}-{d_out:02d}'"

    h = _extract_int_from_map_entry(m, "hour", 0)
    mi = _extract_int_from_map_entry(m, "minute", 0)
    s = _extract_int_from_map_entry(m, "second", 0)
    # sub-second precision — combine ms/us/ns per openCypher spec
    ns = _extract_int_from_map_entry(m, "nanosecond", -1)
    us = _extract_int_from_map_entry(m, "microsecond", -1)
    ms = _extract_int_from_map_entry(m, "millisecond", -1)
    frac = _subsecond_frac(ns, us, ms)

    if frac is not None or ns >= 0 or us >= 0 or ms >= 0:
        if frac:
            time_str = f"{h:02d}:{mi:02d}:{s:02d}.{frac}"
        else:
            time_str = f"{h:02d}:{mi:02d}:{s:02d}"
    else:
        time_str = f"{h:02d}:{mi:02d}"
        if s != 0:
            time_str = f"{h:02d}:{mi:02d}:{s:02d}"

    if with_tz:
        tz_str = "Z"
        if "timezone" in m.entries:
            tz_expr = m.entries["timezone"]
            if isinstance(tz_expr, ast.Literal) and isinstance(tz_expr.value, str):
                tz_name = tz_expr.value
                # Named IANA timezone: compute offset and format as +HH:MM[Name]
                if "/" in tz_name or tz_name in ("UTC", "GMT"):
                    try:
                        from zoneinfo import ZoneInfo as _ZoneInfo
                        _zi = _ZoneInfo(tz_name)
                        _aware = _dt.datetime(y_out, mo_out, d_out, tzinfo=_zi)
                        _off = _aware.utcoffset()
                        _total_s = int(_off.total_seconds())
                        _sign = "+" if _total_s >= 0 else "-"
                        _abs_s = abs(_total_s)
                        _hh = _abs_s // 3600
                        _mm = (_abs_s % 3600) // 60
                        tz_str = f"{_sign}{_hh:02d}:{_mm:02d}[{tz_name}]"
                    except Exception:
                        tz_str = tz_name
                else:
                    tz_str = _normalize_tz_offset(tz_name)
    else:
        tz_str = ""
    return f"'{y_out:04d}-{mo_out:02d}-{d_out:02d}T{time_str}{tz_str}'"


def _scalar_numeric_and_datetime(fn, args, args_exprs, context):
    if fn == "haversin":
        return f"(1 - COS({args[0]})) / 2" if args else "NULL"
    if fn == "e":
        return "EXP(1)"
    if fn == "rand":
        return "SQLUser.RAND()"
    if fn == "timestamp":
        return "CAST(DATEDIFF('ms', '1970-01-01', GETDATE()) AS BIGINT)"
    if fn == "randomuuid":
        return "SQLUser.NEWID()"
    if fn == "date":
        if not args:
            return "NULL"
        if args_exprs and isinstance(args_exprs[0], ast.MapLiteral):
            result = _build_date_from_map(args_exprs[0], with_time=False)
            if result is not None:
                return result
        # String arg: parse ISO 8601 formats
        if args_exprs and isinstance(args_exprs[0], ast.Literal) and isinstance(args_exprs[0].value, str):
            parsed = _parse_date_string(args_exprs[0].value)
            if parsed:
                return f"'{parsed[0]:04d}-{parsed[1]:02d}-{parsed[2]:02d}'"
        return args[0]
    if fn in ("localdatetime",):
        if not args:
            return "NULL"
        if args_exprs and isinstance(args_exprs[0], ast.MapLiteral):
            result = _build_date_from_map(args_exprs[0], with_time=True, with_tz=False)
            if result is not None:
                return result
        if args_exprs and isinstance(args_exprs[0], ast.Literal) and isinstance(args_exprs[0].value, str):
            parsed = _parse_datetime_string(args_exprs[0].value)
            if parsed:
                return f"'{parsed}'"
        return args[0]
    if fn in ("datetime",):
        if not args:
            return "NULL"
        if args_exprs and isinstance(args_exprs[0], ast.MapLiteral):
            result = _build_date_from_map(args_exprs[0], with_time=True, with_tz=True)
            if result is not None:
                return result
        if args_exprs and isinstance(args_exprs[0], ast.Literal) and isinstance(args_exprs[0].value, str):
            parsed = _parse_datetime_string(args_exprs[0].value)
            if parsed:
                return f"'{parsed}'"
        return args[0]
    if fn in ("localtime", "time"):
        if not args:
            return "NULL"
        if args_exprs and isinstance(args_exprs[0], ast.MapLiteral):
            m = args_exprs[0]
            h = _extract_int_from_map_entry(m, "hour", 0)
            mi = _extract_int_from_map_entry(m, "minute", 0)
            s = _extract_int_from_map_entry(m, "second", 0)
            ns = _extract_int_from_map_entry(m, "nanosecond", -1)
            us = _extract_int_from_map_entry(m, "microsecond", -1)
            ms_val = _extract_int_from_map_entry(m, "millisecond", -1)
            frac = _subsecond_frac(ns, us, ms_val)
            if frac:
                time_part = f"{h:02d}:{mi:02d}:{s:02d}.{frac}"
            elif ns >= 0 or us >= 0 or ms_val >= 0 or s != 0:
                time_part = f"{h:02d}:{mi:02d}:{s:02d}"
            else:
                time_part = f"{h:02d}:{mi:02d}"
            if fn == "time":
                tz_val = None
                if "timezone" in m.entries:
                    tz_expr = m.entries["timezone"]
                    if isinstance(tz_expr, ast.Literal) and isinstance(tz_expr.value, str):
                        tz_val = _normalize_tz_offset(tz_expr.value)
                tz_str = tz_val if tz_val else "Z"
                return f"'{time_part}{tz_str}'"
            return f"'{time_part}'"
        if args_exprs and isinstance(args_exprs[0], ast.Literal) and isinstance(args_exprs[0].value, str):
            parsed = _parse_time_string(args_exprs[0].value)
            if parsed:
                if fn == "time" and not any(c in parsed for c in "Z+-"):
                    parsed = parsed + "Z"
                return f"'{parsed}'"
        return args[0]
    if fn == "duration":
        if not args:
            return "NULL"
        if args_exprs and isinstance(args_exprs[0], ast.MapLiteral):
            m = args_exprs[0]
            # Collect all components (allow float for fractional values)
            years = _extract_num_from_map_entry(m, "years", 0)
            months = _extract_num_from_map_entry(m, "months", 0)
            weeks = _extract_num_from_map_entry(m, "weeks", 0)
            days = _extract_num_from_map_entry(m, "days", 0)
            hours = _extract_num_from_map_entry(m, "hours", 0)
            minutes = _extract_num_from_map_entry(m, "minutes", 0)
            seconds = _extract_num_from_map_entry(m, "seconds", 0)
            ms_d = _extract_num_from_map_entry(m, "milliseconds", 0)
            us_d = _extract_num_from_map_entry(m, "microseconds", 0)
            ns_d = _extract_num_from_map_entry(m, "nanoseconds", 0)

            # Normalize: fractional months → days, fractional weeks → days, etc.
            # months with fraction → convert fraction to days (avg 30.436875)
            mo_int = int(months)
            mo_frac = months - mo_int
            days = days + mo_frac * 30.436875

            # weeks → days
            days = days + weeks * 7

            # fractional days → hours
            d_int = int(days)
            d_frac = days - d_int
            hours = hours + d_frac * 24

            # fractional hours → minutes
            h_int = int(hours)
            h_frac = hours - h_int
            minutes = minutes + h_frac * 60

            # fractional minutes → seconds
            m_int = int(minutes)
            m_frac = minutes - m_int
            seconds = seconds + m_frac * 60

            # sub-second to nanoseconds
            total_ns = round(ms_d * 1_000_000 + us_d * 1_000 + ns_d)
            # Convert fractional seconds to nanoseconds
            s_int = int(seconds)
            s_frac = seconds - s_int
            total_ns += round(s_frac * 1_000_000_000)
            # Normalize nanoseconds → seconds
            extra_s = total_ns // 1_000_000_000
            rem_ns = total_ns % 1_000_000_000
            s_int += extra_s
            # Normalize seconds → minutes
            extra_m = s_int // 60
            s_int = s_int % 60
            m_int += extra_m
            # Normalize minutes → hours
            extra_h = m_int // 60
            m_int = m_int % 60
            h_int += extra_h
            # Normalize hours → days
            extra_d = h_int // 24
            h_int = h_int % 24
            d_int += extra_d

            result_str = _format_duration(int(years), mo_int, d_int, h_int, m_int, s_int, rem_ns)
            return f"'{result_str}'"
        if args_exprs and isinstance(args_exprs[0], ast.Literal) and isinstance(args_exprs[0].value, str):
            parsed = _parse_duration_string(args_exprs[0].value)
            if parsed:
                return f"'{parsed}'"
        return args[0]
    return None


def _scalar_statistical(fn, args, args_exprs, context):
    if fn in ("stdev", "stdevs"):
        return f"STDDEV({args[0]})" if args else "NULL"
    if fn in ("stdevp",):
        return f"STDDEV_POP({args[0]})" if args else "NULL"
    if fn in ("percentiledisc", "percentilecont"):
        if not args:
            return "NULL"
        val_expr = args[0]
        pct_expr = args[1] if len(args) > 1 else "0.5"
        context._percentile_queries = getattr(context, "_percentile_queries", [])
        if args_exprs and isinstance(args_exprs[0], ast.Variable):
            var_name = args_exprs[0].name
            alias = context.variable_aliases.get(var_name, "")
            pct_val = float(pct_expr) if isinstance(pct_expr, str) and pct_expr.replace('.','',1).isdigit() else 0.5
            context._percentile_queries.append((val_expr, pct_val, fn, var_name, alias))
        return f"__PERCENTILE_PLACEHOLDER_{len(context._percentile_queries)-1 if context._percentile_queries else 0}__"
    return None


def _scalar_type_conversion(fn, args, args_exprs):
    if fn == "toboolean":
        return f"CASE WHEN LOWER(CAST({args[0]} AS VARCHAR)) IN ('true','1','yes','y') THEN 1 WHEN LOWER(CAST({args[0]} AS VARCHAR)) IN ('false','0','no','n') THEN 0 ELSE NULL END"
    return None


def _expr_scalar_function(fn, sql_fn, args, args_exprs, expr, context, segment):
    result = _scalar_coalesce(fn, args, args_exprs)
    if result is not None:
        return result
    result = _scalar_string(fn, args, args_exprs)
    if result is not None:
        return result
    result = _scalar_numeric_and_datetime(fn, args, args_exprs, context)
    if result is not None:
        return result
    result = _scalar_statistical(fn, args, args_exprs, context)
    if result is not None:
        return result
    result = _scalar_type_conversion(fn, args, args_exprs)
    if result is not None:
        return result
    return None


def _expr_fn_shortestpath(fn, expr, context):
    if fn not in ("shortestpath", "allshortestpaths") or not expr.arguments:
        return None
    arg = expr.arguments[0]
    if not (isinstance(arg, ast.Literal) and isinstance(arg.value, ast.GraphPattern)):
        return None
    pattern = arg.value
    is_all = fn == "allshortestpaths"
    for rel in pattern.relationships:
        if rel.variable_length is None:
            rel.variable_length = ast.VariableLength(
                min_hops=1, max_hops=5, shortest=not is_all, all_shortest=is_all
            )
        else:
            rel.variable_length.shortest = not is_all
            rel.variable_length.all_shortest = is_all
    fake_match = ast.MatchClause(patterns=[pattern], optional=False)
    translate_match_clause(fake_match, context, {})
    return "'path'"


def _expr_fn_path_funcs(fn, expr, context):
    if fn not in ("length", "nodes", "relationships") or len(expr.arguments) != 1:
        return None
    arg = expr.arguments[0]
    if not (isinstance(arg, ast.Variable) and arg.name in context.named_paths):
        if isinstance(arg, ast.Variable) and arg.name not in context.named_paths:
            if fn in ("nodes", "relationships"):
                raise ValueError(f"'{arg.name}' is not a named path variable")
        return None
    path_var = arg.name
    if fn == "length":
        vl_names = {vl.get("path_var") for vl in (context.var_length_paths or [])}
        if path_var in vl_names:
            node_aliases = context.path_node_aliases.get(path_var, [])
            return str(max(0, len(node_aliases) - 1))
        return str(len(context.named_paths[path_var].pattern.relationships))
    elif fn == "nodes":
        aliases = context.path_node_aliases[path_var]
        return f"JSON_ARRAY({', '.join(f'{a}.node_id' for a in aliases)})"
    else:
        aliases = context.path_edge_aliases[path_var]
        undirected_aliases = getattr(context, "_undirected_aliases", set())
        rel_refs = []
        for a in aliases:
            # Use _p for bidirectional (undirected) edges, p for directed edges
            col = "_p" if a in undirected_aliases else "p"
            rel_refs.append(f"{a}.{col}")
        return f"JSON_ARRAY({', '.join(rel_refs)})"


def _expr_fn_vector_ops(fn, args_exprs, args, context):
    if fn not in ("vector_distance", "vector_similarity", "ivg.vector_distance", "ivg.vector_similarity"):
        return None
    if len(args_exprs) < 2:
        raise ValueError(f"{fn}() requires 2 arguments: (node_variable, query_vector)")
    node_arg = args_exprs[0]
    vec_arg = args_exprs[1]
    alias = context.variable_aliases.get(node_arg.name, node_arg.name) if isinstance(node_arg, ast.Variable) else args[0]
    emb_table = f"{_schema_prefix}.kg_NodeEmbeddings" if _schema_prefix else "Graph_KG.kg_NodeEmbeddings"
    if isinstance(vec_arg, ast.Variable) and vec_arg.name in context.input_params:
        vec_val = context.input_params[vec_arg.name]
        if isinstance(vec_val, list):
            vec_str = ",".join(str(x) for x in vec_val)
            placeholder = f"TO_VECTOR('{vec_str}', DOUBLE)"
        else:
            placeholder = f"TO_VECTOR(?, DOUBLE)"
            context.all_stage_params.append(vec_val)
    elif isinstance(vec_arg, ast.Literal) and isinstance(vec_arg.value, list):
        vec_str = ",".join(str(x) for x in vec_arg.value)
        placeholder = f"TO_VECTOR('{vec_str}', DOUBLE)"
    else:
        placeholder = args[1]
    if fn in ("vector_distance", "ivg.vector_distance"):
        return f"(1 - VECTOR_COSINE((SELECT emb FROM {emb_table} WHERE id = {alias}.node_id), {placeholder}))"
    else:
        return f"VECTOR_COSINE((SELECT emb FROM {emb_table} WHERE id = {alias}.node_id), {placeholder})"


def _expr_fn_node_funcs(fn, args_exprs, args, context):
    if fn == "type":
        if args_exprs and isinstance(args_exprs[0], ast.Variable):
            var_name = args_exprs[0].name
            context_alias = context.variable_aliases.get(var_name, "")
            if context_alias:
                if context_alias.startswith("Stage"):
                    return f"{context_alias}.{var_name}"
                p_col = "_p" if getattr(context, "_undirected_aliases", set()) and context_alias in context._undirected_aliases else "p"
                return f"{context_alias}.{p_col}"
        return args[0] if args else "NULL"
    if fn == "startnode":
        if args_exprs and isinstance(args_exprs[0], ast.Variable):
            var_name = args_exprs[0].name
            context_alias = context.variable_aliases.get(var_name, "")
            if context_alias:
                return f"{context_alias}.s"
        return args[0] if args else "NULL"
    if fn == "endnode":
        if args_exprs and isinstance(args_exprs[0], ast.Variable):
            var_name = args_exprs[0].name
            context_alias = context.variable_aliases.get(var_name, "")
            if context_alias:
                return f"{context_alias}.o_id"
        return args[0] if args else "NULL"
    if fn == "id":
        if args_exprs and isinstance(args_exprs[0], ast.Variable):
            var_name = args_exprs[0].name
            context_alias = context.variable_aliases.get(var_name, "")
            if context_alias:
                return f"{context_alias}.node_id"
        return args[0] if args else "NULL"
    return None


def _expr_fn_keys(args):
    if not args:
        return "JSON_ARRAY()"
    id_expr = args[0]
    return f"COALESCE((SELECT JSON_ARRAYAGG(rp.\"key\") FROM {_table('rdf_props')} rp WHERE rp.s = {id_expr}), CAST('[]' AS VARCHAR(256)))"


_EMPTY_JSON_ARRAY = "CAST('[]' AS VARCHAR(256))"


def _expr_fn_range(args_exprs):
    if len(args_exprs) < 2:
        return _EMPTY_JSON_ARRAY
    # Type-check BEFORE int() conversion: int() raises TypeError for list/map/str args,
    # which would be swallowed by 'except TypeError: pass'. Check types explicitly first.
    # Also catch non-Literal AST nodes that are clearly wrong types (MapLiteral, etc.).
    for _i, _arg in enumerate(args_exprs[:3]):
        if isinstance(_arg, ast.MapLiteral):
            raise ValueError(
                f"range() argument {_i} must be an integer, got 'Map'"
            )
        if isinstance(_arg, ast.Literal):
            _v = _arg.value
            if isinstance(_v, list):
                raise ValueError(
                    f"range() argument {_i} must be an integer, got 'List'"
                )
            if not isinstance(_v, int) or isinstance(_v, bool):
                raise ValueError(
                    f"range() argument {_i} must be an integer, got {type(_v).__name__!r}"
                )
    try:
        start = int(args_exprs[0].value) if isinstance(args_exprs[0], ast.Literal) else None
        end = int(args_exprs[1].value) if isinstance(args_exprs[1], ast.Literal) else None
        step_arg = args_exprs[2] if len(args_exprs) > 2 else None
        if step_arg is not None:
            step = int(step_arg.value)
        else:
            step = 1
        if step == 0:
            raise ValueError("range() step cannot be zero (NumberOutOfRange)")
        if start is not None and end is not None:
            vals = list(range(start, end + (1 if step > 0 else -1), step))
            if not vals:
                return _EMPTY_JSON_ARRAY
            return f"JSON_ARRAY({', '.join(str(v) for v in vals)})"
    except ValueError:
        raise
    except TypeError:
        pass
    return _EMPTY_JSON_ARRAY


def _expr_fn_list_ops(fn, args, args_exprs):
    if fn == "keys":
        # For literal maps, extract keys at compile time
        if args_exprs and isinstance(args_exprs[0], ast.MapLiteral):
            keys = list(args_exprs[0].entries.keys())
            return f"JSON_ARRAY({', '.join(repr(k) for k in keys)})"
        return _expr_fn_keys(args)
    if fn == "range":
        return _expr_fn_range(args_exprs)
    if fn == "size":
        if not args:
            return "0"
        arg_expr = args_exprs[0] if args_exprs else None
        is_list = (
            isinstance(arg_expr, ast.Literal) and isinstance(arg_expr.value, list)
        ) or isinstance(arg_expr, ast.ListComprehension)
        if is_list:
            return f"SQLUser.JSON_ARRAYLENGTH({args[0]})"
        return None
    if fn == "head":
        if not args:
            return "NULL"
        return f"SQLUser.LIST_HEAD({args[0]})"
    if fn == "tail":
        if not args:
            return "JSON_ARRAY()"
        return f"SQLUser.LIST_TAIL({args[0]})"
    if fn == "last":
        if not args:
            return "NULL"
        return f"SQLUser.LIST_LAST({args[0]})"
    if fn == "isempty":
        if not args:
            return "1"
        return f"CASE WHEN {args[0]} IS NULL OR {args[0]} = '' OR {args[0]} = '[]' OR {args[0]} = '{{}}' THEN 1 ELSE 0 END"
    if fn == "round":
        return f"CAST(ROUND({args[0] if args else '0'}, 0) AS DOUBLE)"
    return None


def _expr_function_call(expr, context, segment):
    fn = expr.function_name.lower()

    result = _expr_fn_shortestpath(fn, expr, context)
    if result is not None:
        return result

    result = _expr_fn_path_funcs(fn, expr, context)
    if result is not None:
        return result

    if fn == "toboolean" and expr.arguments and isinstance(expr.arguments[0], ast.Literal):
        v = expr.arguments[0].value
        if not isinstance(v, str):
            return "1" if v else "0"
    # toString(bool_expr): must be checked BEFORE args translation to avoid double-parameter issue
    if fn == "tostring" and expr.arguments:
        arg0 = expr.arguments[0]
        if isinstance(arg0, ast.BooleanExpression):
            cond = translate_boolean_expression(arg0, context)
            return f"CASE WHEN ({cond}) THEN 'true' ELSE 'false' END"

    def _translate_arg(a):
        if isinstance(a, ast.Literal) and not isinstance(a.value, list):
            inlined = _inline_literal(a)
            if inlined is not None:
                return inlined
        return translate_expression(a, context, segment="inline")

    args = [_translate_arg(a) for a in expr.arguments]

    result = _expr_fn_vector_ops(fn, expr.arguments, args, context)
    if result is not None:
        return result

    result = _expr_fn_node_funcs(fn, expr.arguments, args, context)
    if result is not None:
        return result

    if fn == "labels":
        return labels_subquery(args[0] if args else "NULL")
    if fn == "properties":
        if expr.arguments:
            arg0 = expr.arguments[0]
            # properties(map) — just return the map itself
            if isinstance(arg0, ast.MapLiteral):
                return args[0]
            # properties(null) — return null
            if isinstance(arg0, ast.Literal) and arg0.value is None:
                return "NULL"
        return properties_subquery(args[0] if args else "NULL")

    # size(x) where x is a scalar list-predicate variable (VARCHAR holding either a
    # plain string or a JSON-encoded list/map): dispatch at runtime by first character.
    if fn == "size" and args and expr.arguments:
        arg0 = expr.arguments[0]
        if isinstance(arg0, ast.Variable) and arg0.name in context.scalar_variables:
            col = args[0]
            return (
                f"CASE WHEN SUBSTRING({col}, 1, 1) IN ('[', '{{') "
                f"THEN SQLUser.JSON_ARRAYLENGTH({col}) "
                f"ELSE LENGTH({col}) END"
            )

    result = _expr_fn_list_ops(fn, args, expr.arguments)
    if result is not None:
        return result

    _CYPHER_FN_MAP = {
        "tolower": "LOWER",
        "toupper": "UPPER",
        "trim": "TRIM",
        "ltrim": "LTRIM",
        "rtrim": "RTRIM",
        "tostring": "CAST",
        "tointeger": "CAST",
        "tofloat": "CAST",
        "size": "LENGTH",
        "length": "LENGTH",
        "substring": "SUBSTRING",
        "left": "LEFT",
        "right": "RIGHT",
        "split": "STRTOK_TO_TABLE",
        "replace": "REPLACE",
        "reverse": "REVERSE",
        "abs": "ABS",
        "ceil": "CEILING",
        "floor": "FLOOR",
        "round": "ROUND",
        "sqrt": "SQRT",
        "sign": "SIGN",
        "coalesce": "COALESCE",
        "nullif": "NULLIF",
        "exists": "EXISTS",
        "toboolean": "CASE WHEN",
    }
    sql_fn = _CYPHER_FN_MAP.get(fn, fn.upper())
    scalar_result = _expr_scalar_function(fn, sql_fn, args, expr.arguments, expr, context, segment)
    if scalar_result is not None:
        return scalar_result
    return f"{sql_fn}({', '.join(args)})"


def _expr_boolean(expr, context, segment):
    cond = translate_boolean_expression(expr, context)
    # If the condition evaluates to SQL NULL (e.g. NOT null, null = null),
    # the result must also be NULL (Cypher three-valued logic).
    if cond == "NULL":
        return "NULL"
    # Sentinel booleans from 3VL logic — return as integer literals
    if cond == "(1=1)":
        return "1"
    if cond == "(1=0)":
        return "0"
    # If translate_boolean_expression already returned a 1/0/NULL CASE expression
    # (e.g. for IN with null list elements, or 3VL AND/OR), don't wrap it again.
    # Also handle 3VL CASE WHEN patterns with (1=0)/(1=1) that need integer normalization.
    # IMPORTANT: only skip re-wrapping when the CASE is the entire expression (ends with END).
    # e.g. "CASE WHEN (0=1) THEN 1 ELSE 0 END IS NULL" must still be wrapped because IRIS
    # rejects a bare CASE expression followed by IS NULL in a SELECT list.
    if cond.startswith("CASE WHEN ") and cond.endswith(" END"):
        # Replace (1=0) and (1=1) sentinels with integers in the CASE WHEN body
        cond = cond.replace("THEN (1=0)", "THEN 0").replace("THEN (1=1)", "THEN 1")
        if " THEN 1 ELSE NULL END" in cond or " THEN 0 ELSE NULL END" in cond or " THEN 1 ELSE 0 END" in cond:
            return cond
    return f"CASE WHEN ({cond}) THEN 1 ELSE 0 END"


def translate_expression(expr, context, segment="select") -> str:

    if isinstance(expr, ast.PatternComprehension):
        return _expr_pattern_comprehension(expr, context, segment)
    if isinstance(expr, ast.FunctionCall) and expr.function_name == "__prop__":
        return _expr_prop(expr, context, segment)
    if isinstance(expr, ast.FunctionCall) and expr.function_name.startswith("__arith_"):
        return _expr_arith(expr, context, segment)
    if isinstance(expr, ast.ListPredicateExpression):
        return _expr_list_predicate(expr, context, segment)
    if isinstance(expr, ast.ListComprehension):
        return _expr_list_comprehension(expr, context, segment)
    if isinstance(expr, ast.ReduceExpression):
        return _expr_reduce(expr, context, segment)
    if isinstance(expr, ast.CaseExpression):
        return _expr_case(expr, context, segment)
    if isinstance(expr, ast.PropertyReference):
        return _expr_property_reference(expr, context, segment)
    if isinstance(expr, ast.MapProjection):
        return _expr_map_projection(expr, context, segment)
    if isinstance(expr, ast.MapLiteral):
        return _expr_map_literal(expr, context, segment)
    if isinstance(expr, ast.SubscriptExpression):
        return _expr_subscript(expr, context, segment)
    if isinstance(expr, ast.SliceExpression):
        return _expr_slice(expr, context, segment)
    if isinstance(expr, ast.PropertyAccessExpression):
        return _expr_property_access(expr, context, segment)
    if isinstance(expr, ast.Variable):
        return _expr_variable(expr, context, segment)
    if isinstance(expr, ast.Literal):
        return _expr_literal(expr, context, segment)
    if isinstance(expr, ast.AggregationFunction):
        return _expr_aggregation(expr, context, segment)
    if isinstance(expr, ast.FunctionCall):
        return _expr_function_call(expr, context, segment)
    if isinstance(expr, ast.BooleanExpression):
        return _expr_boolean(expr, context, segment)
    if isinstance(expr, ast.LabelPredicate):
        alias = context.variable_aliases.get(expr.variable)
        # Detect relationship variables (alias starts with 'e' for rdf_edges)
        is_rel = alias and (alias.startswith("e") or expr.variable in getattr(context, "edge_stage_variables", set()))
        if is_rel:
            # For relationships, r:TYPE means edge type matches — check rdf_edges.p
            if segment in ("select", "inline", None):
                safe_lbl = context.add_select_param(expr.label)
                return f"CASE WHEN ({alias}.p = {safe_lbl}) THEN 1 ELSE 0 END"
            safe_lbl = context.add_where_param(expr.label)
            return f"({alias}.p = {safe_lbl})"
        node_col = f"{alias}.node_id" if alias else "node_id"
        labels_tbl = _table("rdf_labels")
        if segment in ("select", "inline", None):
            safe_label = context.add_select_param(expr.label)
            cond = (
                f"EXISTS (SELECT 1 FROM {labels_tbl} _lp"
                f" WHERE _lp.s = {node_col} AND _lp.label = {safe_label})"
            )
            return f"CASE WHEN ({cond}) THEN 1 ELSE 0 END"
        safe_label = context.add_where_param(expr.label)
        return (
            f"EXISTS (SELECT 1 FROM {labels_tbl} _lp"
            f" WHERE _lp.s = {node_col} AND _lp.label = {safe_label})"
        )

    return "NULL"



_IRIS_RESERVED = frozenset({
    "count","sum","avg","min","max","key","value","type","name","label",
    "order","group","index","select","from","where","join","having",
    "union","insert","update","delete","create","drop","alter","set",
    "table","schema","column","row","data","id","user","date","time",
    "result","results","null","true","false","top","exists","not","and","or",
    "input",
})


# IRIS tokenizer splits identifiers that start with certain reserved keyword tokens.
# e.g. "inputList" is tokenized as keyword INPUT + identifier List.
# Only add keywords here that are confirmed to cause IRIS tokenizer splitting.
_IRIS_RESERVED_PREFIX_MATCH = frozenset({"input"})


def _safe_alias(a: str) -> str:
    if not a:
        return a
    lower = a.lower()
    if lower in _IRIS_RESERVED:
        return f'"{a}"'
    for rw in _IRIS_RESERVED_PREFIX_MATCH:
        if lower.startswith(rw) and len(lower) > len(rw):
            return f'"{a}"'
    return a


def _expr_to_cypher_text(expr) -> str:
    """Return a Cypher-text representation of an expression for use as a column alias."""
    if isinstance(expr, ast.LabelPredicate):
        return f"({expr.variable}:{expr.label})"
    if isinstance(expr, ast.PropertyReference):
        return f"{expr.variable}.{expr.property_name}"
    if isinstance(expr, ast.PropertyAccessExpression):
        base = _expr_to_cypher_text(expr.expression)
        return f"{base}.{expr.property_name}" if base else f".{expr.property_name}"
    if isinstance(expr, ast.Variable):
        return expr.name
    if isinstance(expr, ast.Literal):
        return repr(expr.value)
    if isinstance(expr, ast.BooleanExpression):
        op = expr.operator
        if op in (ast.BooleanOperator.IS_NULL, ast.BooleanOperator.IS_NOT_NULL):
            left = _expr_to_cypher_text(expr.operands[0])
            suffix = "IS NULL" if op == ast.BooleanOperator.IS_NULL else "IS NOT NULL"
            return f"{left} {suffix}"
        if op == ast.BooleanOperator.NOT and len(expr.operands) == 1:
            return f"NOT {_expr_to_cypher_text(expr.operands[0])}"
        op_str = {
            ast.BooleanOperator.AND: "AND",
            ast.BooleanOperator.OR: "OR",
            ast.BooleanOperator.EQUALS: "=",
            ast.BooleanOperator.NOT_EQUALS: "<>",
            ast.BooleanOperator.LESS_THAN: "<",
            ast.BooleanOperator.LESS_THAN_OR_EQUAL: "<=",
            ast.BooleanOperator.GREATER_THAN: ">",
            ast.BooleanOperator.GREATER_THAN_OR_EQUAL: ">=",
            ast.BooleanOperator.IN: "IN",
            ast.BooleanOperator.CONTAINS: "CONTAINS",
            ast.BooleanOperator.STARTS_WITH: "STARTS WITH",
            ast.BooleanOperator.ENDS_WITH: "ENDS WITH",
        }.get(op, str(op))
        parts = [_expr_to_cypher_text(o) for o in expr.operands]
        return f" {op_str} ".join(parts)
    if isinstance(expr, ast.AggregationFunction):
        # count(*) may be parsed as argument=Literal("*") or argument=None
        is_count_star = (
            expr.function_name == "count"
            and (
                expr.argument is None
                or (isinstance(expr.argument, ast.Literal) and expr.argument.value == "*")
            )
        )
        if is_count_star:
            return "count(*)"
        distinct = "DISTINCT " if expr.distinct else ""
        arg_text = _expr_to_cypher_text(expr.argument) if expr.argument is not None else "*"
        return f"{expr.function_name}({distinct}{arg_text})"
    if isinstance(expr, ast.FunctionCall):
        args = ", ".join(_expr_to_cypher_text(a) for a in expr.arguments)
        return f"{expr.function_name}({args})"
    return ""


def translate_return_clause(ret, context):
    # RETURN * after a WITH stage: the star is parsed as Literal('*').
    # When we're selecting from a stage CTE, don't add any select_items — the
    # _tts_select_result fallback (line ~1814) will emit "SELECT *" instead.
    if (
        len(ret.items) == 1
        and isinstance(ret.items[0].expression, ast.Literal)
        and ret.items[0].expression.value == "*"
        and context.stages
    ):
        return
    for item in ret.items:
        if isinstance(item.expression, ast.Variable):
            var_name = item.expression.name
            if var_name in context.named_paths:
                alias = item.alias or var_name
                node_aliases = context.path_node_aliases[var_name]
                edge_aliases = context.path_edge_aliases[var_name]
                nodes_arr = ", ".join(f"{a}.node_id" for a in node_aliases)
                # Use _p for bidirectional (undirected) edges, p for directed edges
                undirected_aliases = getattr(context, "_undirected_aliases", set())
                rels_parts = []
                for a in edge_aliases:
                    col = "_p" if a in undirected_aliases else "p"
                    rels_parts.append(f"{a}.{col}")
                rels_arr = ", ".join(rels_parts)
                json_expr = f"'{{\"nodes\":' || JSON_ARRAY({nodes_arr}) || ',\"rels\":' || JSON_ARRAY({rels_arr}) || '}}'"
                context.select_items.append(f"{json_expr} AS {_safe_alias(alias)}")
                continue
            alias_name = context.variable_aliases.get(var_name)
            is_scalar = var_name in context.scalar_variables
            # Stage-promoted edge variables (e.g. WITH r promoted to Stage1 with __edge_r_s/p/o)
            # must NOT go through the node path — emit edge identity columns instead.
            edge_stage_vars = getattr(context, "edge_stage_variables", set())
            if alias_name and alias_name.startswith("Stage") and var_name in edge_stage_vars:
                prefix = item.alias or var_name
                p_col = f"__edge_{var_name}_p"
                s_col = f"__edge_{var_name}_s"
                o_col = f"__edge_{var_name}_o"
                # Emit p (type) as the relationship identifier and s/p/o for identity.
                context.select_items.append(f"{alias_name}.{p_col} AS {prefix}")
                context.optional_null_row_items.append("NULL")
                continue
            if alias_name == "scalar":
                continue
            if alias_name and not alias_name.startswith("e") and not is_scalar:
                prefix = item.alias or var_name
                if alias_name.startswith("Stage") or alias_name in _PROC_CTE_ALIASES:
                    node_expr = var_name
                else:
                    # Check if this node is null-gated by a downstream optional edge.
                    # When multi-hop OPTIONAL MATCH fails the second hop, the intermediate
                    # node (e.g. b in OPTIONAL MATCH (a)-->(b)-->(c)) must appear as null
                    # even though it was left-outer-joined via the first hop.
                    gate_edge = context.opt_intermediate_nulled.get(alias_name)
                    if gate_edge:
                        node_expr = (
                            f"CASE WHEN {gate_edge}.s IS NULL "
                            f"THEN NULL ELSE {alias_name}.node_id END"
                        )
                    else:
                        node_expr = f"{alias_name}.node_id"
                context.select_items.append(f"{node_expr} AS {prefix}_id")
                context.select_items.append(
                    f"{labels_subquery(node_expr)} AS {prefix}_labels"
                )
                context.select_items.append(
                    f"{properties_subquery(node_expr)} AS {prefix}_props"
                )
                # Null-row for OPTIONAL MATCH: node is null → 3 NULLs
                context.optional_null_row_items.extend(["NULL", "NULL", "NULL"])
                continue
        sql = translate_expression(item.expression, context, segment="select")
        alias = item.alias
        cypher_col = None  # Cypher-text column name for post-execution remapping
        if alias is None:
            if isinstance(item.expression, ast.PropertyReference):
                alias = f"{item.expression.variable}_{item.expression.property_name}"
                cypher_col = f"{item.expression.variable}.{item.expression.property_name}"
                context.column_name_map[alias] = cypher_col
            elif isinstance(item.expression, ast.Variable):
                alias = item.expression.name
            elif isinstance(
                item.expression, (ast.AggregationFunction, ast.FunctionCall)
            ):
                # For function calls (e.g., labels(a), count(*)), use cypher_text as the actual column name
                cypher_text = _expr_to_cypher_text(item.expression)
                if cypher_text:
                    import re as _re_fn
                    alias = _re_fn.sub(r'[^A-Za-z0-9_]', '_', cypher_text)
                    if alias and alias[0].isdigit():
                        alias = f"_{alias}"
                    if not alias:
                        alias = f"{item.expression.function_name}_res"
                else:
                    alias = f"{item.expression.function_name}_res"
                if cypher_text and cypher_text != alias:
                    context.column_name_map[alias] = cypher_text
            else:
                cypher_text = _expr_to_cypher_text(item.expression)
                if cypher_text:
                    import re as _re_alias
                    # Build a SQL-safe alias (replace non-identifier chars with underscores)
                    alias = _re_alias.sub(r'[^A-Za-z0-9_]', '_', cypher_text)
                    if alias and alias[0].isdigit():
                        alias = f"_{alias}"
                    # Register the mapping so the engine can rename after execution
                    if alias and cypher_text != alias:
                        context.column_name_map[alias] = cypher_text
        if alias:
            context.select_items.append(f"{sql} AS {_safe_alias(alias).replace('.', '_')}")
        else:
            context.select_items.append(sql)
        # Build null-row value for OPTIONAL MATCH fallback:
        # IS NULL → 1 (null IS NULL = true), IS NOT NULL → 0, else NULL
        if isinstance(item.expression, ast.BooleanExpression):
            op = item.expression.operator
            if op == ast.BooleanOperator.IS_NULL:
                context.optional_null_row_items.append("1")
            elif op == ast.BooleanOperator.IS_NOT_NULL:
                context.optional_null_row_items.append("0")
            else:
                context.optional_null_row_items.append("NULL")
        else:
            context.optional_null_row_items.append("NULL")


def translate_with_clause(with_clause, context):
    if with_clause.star:
        for var, alias in context.variable_aliases.items():
            if alias.startswith("e"):
                is_undirected = alias in getattr(context, "_undirected_aliases", set())
                if is_undirected:
                    context.select_items.append(f"{alias}._src AS {var}_src, {alias}._p AS {var}_p, {alias}._dst AS {var}_dst")
                else:
                    context.select_items.append(f"{alias}.s AS {var}_s, {alias}.p AS {var}_p, {alias}.o_id AS {var}_o_id")
            else:
                context.select_items.append(f"{alias}.node_id AS {var}")
        if with_clause.where_clause:
            context.where_conditions.append(
                translate_boolean_expression(with_clause.where_clause.expression, context)
            )
        return
    has_agg = any(
        isinstance(i.expression, ast.AggregationFunction) for i in with_clause.items
    )
    agg_aliases: set = set()
    for item in with_clause.items:
        sql = translate_expression(item.expression, context, segment="select")
        alias = item.alias
        if alias is None:
            if isinstance(item.expression, ast.PropertyReference):
                alias = f"{item.expression.variable}_{item.expression.property_name}"
            elif isinstance(item.expression, ast.Variable):
                alias = item.expression.name
            elif isinstance(item.expression, ast.AggregationFunction):
                alias = f"{item.expression.function_name}"
        if alias is None:
            alias = context.next_alias("v")
        # Edge variables: expose qualifiers JSON so downstream r.prop works via JSON_VALUE(r, '$.prop')
        if (isinstance(item.expression, ast.Variable)
                and context.variable_aliases.get(item.expression.name, "").startswith("e")
                and not context.variable_aliases.get(item.expression.name, "").startswith("Stage")):
            e_alias = context.variable_aliases[item.expression.name]
            sql = f"{e_alias}.qualifiers"
            if not hasattr(context, "edge_stage_variables"):
                context.edge_stage_variables = set()
            context.edge_stage_variables.add(item.expression.name)
            # Preserve edge identity columns so DELETE can find the original edge row
            # even after the relationship variable is promoted to a CTE stage.
            var_name = item.expression.name
            is_undirected = e_alias in getattr(context, "_undirected_aliases", set())
            if is_undirected:
                context.select_items.append(f"{e_alias}._src AS __edge_{var_name}_s")
                context.select_items.append(f"{e_alias}._p AS __edge_{var_name}_p")
                context.select_items.append(f"{e_alias}._dst AS __edge_{var_name}_o")
            else:
                context.select_items.append(f"{e_alias}.s AS __edge_{var_name}_s")
                context.select_items.append(f"{e_alias}.p AS __edge_{var_name}_p")
                context.select_items.append(f"{e_alias}.o_id AS __edge_{var_name}_o")
        context.select_items.append(f"{sql} AS {_safe_alias(alias).replace('.', '_')}")
        if has_agg and not isinstance(item.expression, ast.AggregationFunction):
            context.group_by_items.append(sql)
        if isinstance(item.expression, ast.AggregationFunction):
            agg_aliases.add(alias)
    agg_alias_sql: dict = {}
    for item in with_clause.items:
        if isinstance(item.expression, ast.AggregationFunction):
            alias = item.alias
            if alias is None:
                alias = item.expression.function_name
            agg_alias_sql[alias] = translate_expression(item.expression, context, segment="select")
    if with_clause.where_clause:
        expr = with_clause.where_clause.expression
        if has_agg and agg_aliases and _references_agg_alias(expr, agg_aliases):
            context.having_conditions.append(
                _translate_having_expr(expr, agg_aliases, agg_alias_sql, context)
            )
        else:
            context.where_conditions.append(
                translate_boolean_expression(expr, context)
            )


def _references_agg_alias(expr, agg_aliases: set) -> bool:
    if isinstance(expr, ast.Variable) and expr.name in agg_aliases:
        return True
    if isinstance(expr, ast.BooleanExpression):
        return any(_references_agg_alias(o, agg_aliases) for o in expr.operands)
    return False


def _translate_having_expr(expr, agg_aliases: set, agg_alias_sql: dict, context) -> str:
    if isinstance(expr, ast.Variable) and expr.name in agg_aliases:
        return agg_alias_sql.get(expr.name, expr.name)
    if isinstance(expr, ast.BooleanExpression):
        op = expr.operator
        if op == ast.BooleanOperator.AND:
            return "(" + " AND ".join(
                _translate_having_expr(o, agg_aliases, agg_alias_sql, context) for o in expr.operands
            ) + ")"
        if op == ast.BooleanOperator.OR:
            return "(" + " OR ".join(
                _translate_having_expr(o, agg_aliases, agg_alias_sql, context) for o in expr.operands
            ) + ")"
        if op == ast.BooleanOperator.NOT:
            return f"NOT ({_translate_having_expr(expr.operands[0], agg_aliases, agg_alias_sql, context)})"
        left = _translate_having_expr(expr.operands[0], agg_aliases, agg_alias_sql, context)
        right_expr = expr.operands[1] if len(expr.operands) > 1 else None
        right = translate_expression(right_expr, context, segment="where") if right_expr is not None else ""
        op_map = {
            ast.BooleanOperator.EQUALS: "=",
            ast.BooleanOperator.NOT_EQUALS: "<>",
            ast.BooleanOperator.LESS_THAN: "<",
            ast.BooleanOperator.LESS_THAN_OR_EQUAL: "<=",
            ast.BooleanOperator.GREATER_THAN: ">",
            ast.BooleanOperator.GREATER_THAN_OR_EQUAL: ">=",
        }
        if op in op_map:
            return f"{left} {op_map[op]} {right}"
    return translate_boolean_expression(expr, context)


def _translate_degree_centrality(proc, context) -> None:
    """CALL ivg.degreeCentrality({direction:'out', predicate:'CITES', topK:50}) YIELD node, score, degree"""
    opts = proc.options or {}
    _validate_centrality_proc_map("ivg.degreeCentrality", opts.keys())

    def _val(key, default):
        v = opts.get(key, default)
        if hasattr(v, "value"):
            return v.value
        return v

    direction = str(_val("direction", "out"))
    pred_v = _val("predicate", "")
    predicate = str(pred_v) if pred_v is not None else ""
    top_k = int(_val("topK", 10000))

    fn = f"{_schema_prefix}.kg_DegreeCentrality" if _schema_prefix else "kg_DegreeCentrality"
    cte_sql = (
        f"SELECT j.node_id AS node, j.score, j.degree\n"
        f"FROM JSON_TABLE(\n"
        f"  {fn}({_sql_arg(direction)}, {_sql_arg(predicate)}, {_sql_arg(top_k)}),\n"
        f"  '$[*]' COLUMNS(\n"
        f"    node_id VARCHAR(256) PATH '$.id',\n"
        f"    score DOUBLE PATH '$.score',\n"
        f"    degree INTEGER PATH '$.degree'\n"
        f"  )\n"
        f") j"
    )
    context.stages.insert(0, f"DegCent AS (\n{cte_sql}\n)")
    for item in proc.yield_items:
        context.variable_aliases[item] = "DegCent"
    if "score" in proc.yield_items:
        context.scalar_variables.add("score")
    if "degree" in proc.yield_items:
        context.scalar_variables.add("degree")


def _translate_betweenness(proc, context) -> None:
    """CALL ivg.betweenness({sampleSize:100, direction:'out', maxHops:0, topK:50, memBudgetMB:256}) YIELD node, score"""
    opts = proc.options or {}
    _validate_centrality_proc_map("ivg.betweenness", opts.keys())

    def _val(key, default):
        v = opts.get(key, default)
        if hasattr(v, "value"):
            return v.value
        return v

    sample_size = int(_val("sampleSize", 0))
    direction = str(_val("direction", "out"))
    max_hops = int(_val("maxHops", 0))
    top_k = int(_val("topK", 10000))
    mem_budget_mb = int(_val("memBudgetMB", 256))

    fn = f"{_schema_prefix}.kg_Betweenness" if _schema_prefix else "kg_Betweenness"
    cte_sql = (
        f"SELECT j.node_id AS node, j.score\n"
        f"FROM JSON_TABLE(\n"
        f"  {fn}({_sql_arg(sample_size)}, {_sql_arg(direction)}, {_sql_arg(max_hops)}, {_sql_arg(top_k)}, {_sql_arg(mem_budget_mb)}),\n"
        f"  '$[*]' COLUMNS(\n"
        f"    node_id VARCHAR(256) PATH '$.id',\n"
        f"    score DOUBLE PATH '$.score'\n"
        f"  )\n"
        f") j"
    )
    context.stages.insert(0, f"Betweenness AS (\n{cte_sql}\n)")
    for item in proc.yield_items:
        context.variable_aliases[item] = "Betweenness"
    if "score" in proc.yield_items:
        context.scalar_variables.add("score")


def _translate_closeness(proc, context) -> None:
    """CALL ivg.closeness({formula:'harmonic', direction:'out', maxHops:0, topK:50}) YIELD node, score (Phase 5 — pending T064)"""
    opts = proc.options or {}
    _validate_centrality_proc_map("ivg.closeness", opts.keys())

    def _val(key, default):
        v = opts.get(key, default)
        if hasattr(v, "value"):
            return v.value
        return v

    formula = str(_val("formula", "harmonic"))
    direction = str(_val("direction", "out"))
    max_hops = int(_val("maxHops", 0))
    top_k = int(_val("topK", 10000))

    fn = f"{_schema_prefix}.kg_Closeness" if _schema_prefix else "kg_Closeness"
    cte_sql = (
        f"SELECT j.node_id AS node, j.score\n"
        f"FROM JSON_TABLE(\n"
        f"  {fn}({_sql_arg(formula)}, {_sql_arg(direction)}, {_sql_arg(max_hops)}, {_sql_arg(top_k)}),\n"
        f"  '$[*]' COLUMNS(\n"
        f"    node_id VARCHAR(256) PATH '$.id',\n"
        f"    score DOUBLE PATH '$.score'\n"
        f"  )\n"
        f") j"
    )
    context.stages.insert(0, f"Closeness AS (\n{cte_sql}\n)")
    for item in proc.yield_items:
        context.variable_aliases[item] = "Closeness"
    if "score" in proc.yield_items:
        context.scalar_variables.add("score")


def _translate_eigenvector(proc, context) -> None:
    """CALL ivg.eigenvector({maxIter:30, tol:1e-6, topK:50}) YIELD node, score (Phase 6 — pending T080)"""
    opts = proc.options or {}
    _validate_centrality_proc_map("ivg.eigenvector", opts.keys())

    def _val(key, default):
        v = opts.get(key, default)
        if hasattr(v, "value"):
            return v.value
        return v

    max_iter = int(_val("maxIter", 30))
    tol = float(_val("tol", 1e-6))
    top_k = int(_val("topK", 10000))

    fn = f"{_schema_prefix}.kg_Eigenvector" if _schema_prefix else "kg_Eigenvector"
    cte_sql = (
        f"SELECT j.node_id AS node, j.score\n"
        f"FROM JSON_TABLE(\n"
        f"  {fn}({_sql_arg(max_iter)}, {_sql_arg(tol)}, {_sql_arg(top_k)}),\n"
        f"  '$[*]' COLUMNS(\n"
        f"    node_id VARCHAR(256) PATH '$.id',\n"
        f"    score DOUBLE PATH '$.score'\n"
        f"  )\n"
        f") j"
    )
    context.stages.insert(0, f"Eigenvector AS (\n{cte_sql}\n)")
    for item in proc.yield_items:
        context.variable_aliases[item] = "Eigenvector"
    if "score" in proc.yield_items:
        context.scalar_variables.add("score")


def _translate_leiden(proc, context) -> None:
    opts = proc.options or {}
    _validate_community_proc_map("ivg.leiden", opts.keys())

    def _val(key, default):
        v = opts.get(key, default)
        if hasattr(v, "value"):
            return v.value
        return v

    max_levels = int(_val("maxLevels", 10))
    gamma = float(_val("gamma", 1.0))
    tol = float(_val("tol", 1e-4))
    top_k = int(_val("topK", 10000))
    mem_budget_mb = int(_val("memBudgetMB", 256))
    seed_v = _val("randomSeed", None)
    random_seed = -1 if seed_v is None else int(seed_v)

    fn = f"{_schema_prefix}.kg_Leiden" if _schema_prefix else "kg_Leiden"
    cte_sql = (
        f"SELECT j.node_id AS node, j.community, j.size\n"
        f"FROM JSON_TABLE(\n"
        f"  {fn}({_sql_arg(max_levels)}, {_sql_arg(gamma)}, {_sql_arg(tol)}, {_sql_arg(top_k)}, {_sql_arg(mem_budget_mb)}, {_sql_arg(random_seed)}),\n"
        f"  '$[*]' COLUMNS(\n"
        f"    node_id VARCHAR(256) PATH '$.id',\n"
        f"    community INTEGER PATH '$.community',\n"
        f"    size INTEGER PATH '$.size'\n"
        f"  )\n"
        f") j"
    )
    context.stages.insert(0, f"Leiden AS (\n{cte_sql}\n)")
    for item in proc.yield_items:
        context.variable_aliases[item] = "Leiden"
    if "community" in proc.yield_items:
        context.scalar_variables.add("community")
    if "size" in proc.yield_items:
        context.scalar_variables.add("size")


def _translate_triangle_count(proc, context) -> None:
    opts = proc.options or {}
    _validate_community_proc_map("ivg.triangleCount", opts.keys())

    def _val(key, default):
        v = opts.get(key, default)
        if hasattr(v, "value"):
            return v.value
        return v

    top_k = int(_val("topK", 10000))

    fn = f"{_schema_prefix}.kg_TriangleCount" if _schema_prefix else "kg_TriangleCount"
    cte_sql = (
        f"SELECT j.node_id AS node, j.triangles, j.lcc\n"
        f"FROM JSON_TABLE(\n"
        f"  {fn}({_sql_arg(top_k)}),\n"
        f"  '$[*]' COLUMNS(\n"
        f"    node_id VARCHAR(256) PATH '$.id',\n"
        f"    triangles INTEGER PATH '$.triangles',\n"
        f"    lcc DOUBLE PATH '$.lcc'\n"
        f"  )\n"
        f") j"
    )
    context.stages.insert(0, f"TriangleCount AS (\n{cte_sql}\n)")
    for item in proc.yield_items:
        context.variable_aliases[item] = "TriangleCount"
    if "triangles" in proc.yield_items:
        context.scalar_variables.add("triangles")
    if "lcc" in proc.yield_items:
        context.scalar_variables.add("lcc")


def _translate_scc(proc, context) -> None:
    opts = proc.options or {}
    _validate_community_proc_map("ivg.scc", opts.keys())

    def _val(key, default):
        v = opts.get(key, default)
        if hasattr(v, "value"):
            return v.value
        return v

    top_k = int(_val("topK", 10000))

    fn = f"{_schema_prefix}.kg_SCC" if _schema_prefix else "kg_SCC"
    cte_sql = (
        f"SELECT j.node_id AS node, j.component, j.size\n"
        f"FROM JSON_TABLE(\n"
        f"  {fn}({_sql_arg(top_k)}),\n"
        f"  '$[*]' COLUMNS(\n"
        f"    node_id VARCHAR(256) PATH '$.id',\n"
        f"    component INTEGER PATH '$.component',\n"
        f"    size INTEGER PATH '$.size'\n"
        f"  )\n"
        f") j"
    )
    context.stages.insert(0, f"SCC AS (\n{cte_sql}\n)")
    for item in proc.yield_items:
        context.variable_aliases[item] = "SCC"
    if "component" in proc.yield_items:
        context.scalar_variables.add("component")
    if "size" in proc.yield_items:
        context.scalar_variables.add("size")


def _translate_kcore(proc, context) -> None:
    opts = proc.options or {}
    _validate_community_proc_map("ivg.kcore", opts.keys())

    def _val(key, default):
        v = opts.get(key, default)
        if hasattr(v, "value"):
            return v.value
        return v

    top_k = int(_val("topK", 10000))

    fn = f"{_schema_prefix}.kg_KCore" if _schema_prefix else "kg_KCore"
    cte_sql = (
        f"SELECT j.node_id AS node, j.coreness\n"
        f"FROM JSON_TABLE(\n"
        f"  {fn}({_sql_arg(top_k)}),\n"
        f"  '$[*]' COLUMNS(\n"
        f"    node_id VARCHAR(256) PATH '$.id',\n"
        f"    coreness INTEGER PATH '$.coreness'\n"
        f"  )\n"
        f") j"
    )
    context.stages.insert(0, f"KCore AS (\n{cte_sql}\n)")
    for item in proc.yield_items:
        context.variable_aliases[item] = "KCore"
    if "coreness" in proc.yield_items:
        context.scalar_variables.add("coreness")
