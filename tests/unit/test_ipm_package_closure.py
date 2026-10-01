"""IPM package closure: every in-repo class a shipped class needs ships in exactly one package.

``module-core.xml`` (trifour-iris-vector-graph-core) and ``module.xml``
(trifour-iris-vector-graph, depends on core) each list their ``<Resource>`` classes, and
``ipm-packages.yaml`` repeats them as the per-package file filter. A class referenced by a
shipped class but listed in neither is absent on the server (``<CLASS DOES NOT EXIST>``
at runtime, as ``Graph.KG.TraversalBuild`` was in 2.20.6). Core may reach into full only
through ``CORE_REACHES_FULL``, since full depends on core.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SOURCES = ROOT / "iris_src" / "src"

# Tables the Python SDK creates by DDL (iris_vector_graph/schema.py, configurable vector
# dimension); their .cls twins must not ship or they pre-create a clashing table.
DDL_OWNED = {"Graph.KG.kgNodeEmbeddings"}

# Full classes core reaches anyway. ArnoAccel: NKGAccel probes its optional Rust callout via
# IsAvailable(). Edge: TraversalBuild reads its table Graph_KG.rdf_edges in embedded SQL.
# Neither can move to core: IPM refuses a resource another installed module owns, so core
# 2.20.7 would fail to install over full 2.20.6 ("already defined as part of module").
CORE_REACHES_FULL = {"Graph.KG.ArnoAccel", "Graph.KG.Edge"}


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


def _references(name: str, classes: dict[str, Path], tables: dict[str, str]) -> set[str]:
    code = "\n".join(
        line
        for line in classes[name].read_text(errors="ignore").splitlines()
        if not re.match(r"\s*(///|//|#;|;)", line)
    )
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
