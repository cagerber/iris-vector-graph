"""IPM package closure: every in-repo class a shipped class needs ships in exactly one package.

``module-core.xml`` (trifour-iris-vector-graph-core) and ``module.xml``
(trifour-iris-vector-graph, depends on core) each list their ``<Resource>`` classes, and
``ipm-packages.yaml`` repeats them as the per-package file filter. A class referenced by a
shipped class but listed in neither is absent on the server (``<CLASS DOES NOT EXIST>``
at runtime, as ``Graph.KG.TraversalBuild`` was in 2.20.6). Core may reach into full only
through ``CORE_REACHES_FULL``, since full depends on core.

Compile-time dependencies are stricter, because core installs and compiles before full
exists: ``Extends``, property/relationship types, ``CompileAfter``/``DependsOn`` and
embedded-SQL tables must resolve inside the class's own package (or core). Embedded SQL
may not name a table no shipped class defines either: the Graph_KG tables are created and
migrated at runtime by the Python schema, so 2.20.7 core failed to compile both on a fresh
namespace and over a pre-spec-214 ``rdf_edges`` (``Field 'GRAPH_ID' not found``).
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SOURCES = ROOT / "iris_src" / "src"

# Tables the Python SDK creates by DDL (iris_vector_graph/schema.py, configurable vector
# dimension); their .cls twins must not ship or they pre-create a clashing table.
DDL_OWNED = {"Graph.KG.kgNodeEmbeddings"}

# Full classes core reaches at RUNTIME only (never at compile time — the test below checks).
# ArnoAccel: NKGAccel probes its optional Rust callout via ##class(...).IsAvailable().
# Edge: TraversalBuild reads its table Graph_KG.rdf_edges through dynamic SQL.
# Neither can move to core: IPM refuses a resource another installed module owns, so a core
# release would fail to install over full 2.20.6 ("already defined as part of module").
CORE_REACHES_FULL = {"Graph.KG.ArnoAccel", "Graph.KG.Edge"}

# In-repo classes the Python SDK names but neither package ships: optional SDK features
# whose calls are guarded (or unused by the estate). Anything else the Python names must ship.
PYTHON_SDK_ONLY = {
    "Graph.KG.Centrality",
    "Graph.KG.Communities",
    "Graph.KG.EdgeScan",
    "Graph.KG.EmbedQueue",
    "Graph.KG.IVFIndex",
    "Graph.KG.Ledger",
    "Graph.KG.Snapshot",
    "Graph.KG.TemporalIndex",
    "Graph.KG.TestEdge",
}

# Classes ODS GraphImport (ods/tools/ods_kg_backend/graph_import.py) calls via iris.cls;
# ods pins the same set in tests/test_graph_import.py. Never allowlistable.
ODS_KG_BACKEND_CALLS = {"Graph.KG.Traversal", "Graph.KG.BM25Index"}


def _classes() -> dict[str, Path]:
    out = {}
    for path in SOURCES.rglob("*.cls"):
        match = re.search(r"(?m)^Class\s+([\w.%]+)", path.read_text(errors="ignore"))
        if match:
            out[match.group(1)] = path
    return out


def _sql_tables(classes: dict[str, Path]) -> dict[str, str]:
    out = {}
    for name, path in classes.items():
        match = re.search(r"SqlTableName\s*=\s*(\w+)", path.read_text(errors="ignore"))
        package, _, short = name.rpartition(".")
        schema = package.replace(".", "_") if package else "SQLUser"
        out[f"{schema}.{match.group(1) if match else short}".lower()] = name
    return out


def _code(path: Path) -> str:
    return "\n".join(
        line
        for line in path.read_text(errors="ignore").splitlines()
        if not re.match(r"\s*(///|//|#;|;)", line)
    )


def _references(name: str, classes: dict[str, Path], tables: dict[str, str]) -> set[str]:
    code = _code(classes[name])
    found = {
        other
        for other in classes
        if other != name and re.search(rf"(?<![\w.%]){re.escape(other)}(?!\w)", code)
    }
    for match in re.finditer(r"(?<![\w.])([A-Za-z_%]\w*\.\w+)(?![\w.(])", code):
        table = tables.get(match.group(1).lower())
        if table and table != name:
            found.add(table)
    return found - DDL_OWNED


def _compile_time(name: str, classes: dict[str, Path], tables: dict[str, str]) -> set[str]:
    """In-repo classes, and ``table:<name>`` for unowned tables, the compiler must resolve."""
    code = _code(classes[name])
    names: set[str] = set()
    if match := re.search(r"(?m)^Class\s+[\w.%]+\s+Extends\s+\(?([\w.%,\s]+?)\)?\s*(\[|\{|$)", code):
        names |= {n.strip() for n in match.group(1).split(",")}
    names |= set(
        re.findall(r"(?m)^(?:Property|Relationship)\s+\w+\s+As\s+(?:(?:list|array)\s+Of\s+)?([\w.%]+)", code)
    )
    for keyword in re.findall(r"(?:CompileAfter|DependsOn)\s*=\s*\(?([\w.%,\s]+)\)?", code):
        names |= {n.strip() for n in keyword.split(",")}
    out = {n for n in names if n in classes}
    for sql in re.findall(r"&sql\((.*?)\)\s*$", code, flags=re.IGNORECASE | re.MULTILINE):
        for table in re.findall(r"(?i)\b(?:FROM|JOIN|INTO|UPDATE)\s+([\w.]+)", sql):
            owner = tables.get(table.lower())
            out.add(owner if owner in classes else f"table:{table}")
    return out - {name}


def _manifest_resources(manifest: str) -> set[str]:
    text = (ROOT / manifest).read_text()
    return {r[: -len(".CLS")] for r in re.findall(r'<Resource Name="([\w.%]+\.CLS)"', text)}


def _declared_includes() -> dict[str, set[str]]:
    out: dict[str, set[str]] = {}
    current = ""
    for line in (ROOT / "ipm-packages.yaml").read_text().splitlines():
        if match := re.match(r"\s*-\s*id:\s*(\S+)", line):
            current = match.group(1)
            out[current] = set()
        elif current and (match := re.match(r"\s*-\s*(iris_src/src/\S+\.cls)\s*$", line)):
            out[current].add(match.group(1))
    return out


def _closure(
    seeds: set[str], classes: dict[str, Path], tables: dict[str, str], stop: frozenset[str] | set[str] = frozenset()
) -> set[str]:
    seen, todo = set(seeds), list(seeds)
    while todo:
        for ref in _references(todo.pop(), classes, tables) - stop:
            if ref not in seen:
                seen.add(ref)
                todo.append(ref)
    return seen


def test_shipped_classes_reference_only_shipped_classes() -> None:
    classes = _classes()
    tables = _sql_tables(classes)
    core = _manifest_resources("module-core.xml")
    full = _manifest_resources("module.xml")

    assert not core & full, f"shipped by both packages: {sorted(core & full)}"
    assert core | full <= set(classes), f"no source for {sorted((core | full) - set(classes))}"

    assert CORE_REACHES_FULL <= full
    core_missing = _closure(core, classes, tables, CORE_REACHES_FULL) - core
    full_missing = _closure(full, classes, tables) - core - full
    assert not core_missing, f"core classes need classes core does not ship: {sorted(core_missing)}"
    assert not full_missing, f"full classes need unshipped classes: {sorted(full_missing)}"


def test_compile_time_dependencies_resolve_inside_the_package() -> None:
    classes = _classes()
    tables = _sql_tables(classes)
    core = _manifest_resources("module-core.xml")
    full = _manifest_resources("module.xml")
    offences = [
        f"{name} -> {dep}"
        for shipped, allowed in ((core, core), (full, core | full))
        for name in sorted(shipped)
        for dep in sorted(_compile_time(name, classes, tables) - allowed)
    ]
    assert not offences, (
        "compile-time dependency outside the package (use dynamic SQL / runtime ##class "
        "for tables the Python schema owns or classes full ships):\n" + "\n".join(offences)
    )


def test_python_iris_cls_references_ship() -> None:
    classes = _classes()
    shipped = _manifest_resources("module-core.xml") | _manifest_resources("module.xml")
    named: set[str] = set()
    for path in (ROOT / "iris_vector_graph").rglob("*.py"):
        code = "\n".join(
            line for line in path.read_text(errors="ignore").splitlines() if not line.lstrip().startswith("#")
        )
        named |= set(re.findall(r"(?<![\w.])(Graph\.KG\.\w+)(?![\w.])", code)) & set(classes)
    assert not PYTHON_SDK_ONLY & shipped, f"allowlisted but shipped: {sorted(PYTHON_SDK_ONLY & shipped)}"
    missing = (named | ODS_KG_BACKEND_CALLS) - shipped - PYTHON_SDK_ONLY - DDL_OWNED
    assert not missing, f"Python calls classes no package ships (iris.cls: error finding class): {sorted(missing)}"
    assert ODS_KG_BACKEND_CALLS <= shipped


def test_ipm_packages_yaml_includes_match_the_manifests() -> None:
    classes = _classes()
    includes = _declared_includes()
    for package, manifest in (
        ("trifour-iris-vector-graph", "module.xml"),
        ("trifour-iris-vector-graph-core", "module-core.xml"),
    ):
        expected = {
            classes[c].relative_to(ROOT).as_posix() for c in _manifest_resources(manifest)
        }
        assert includes.get(package) == expected, package
