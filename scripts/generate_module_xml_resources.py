#!/usr/bin/env python3
"""Generate module.xml and module-core.xml Resource lists from iris_src/src/**/*.cls.

Core vs full split:
- **iris-vector-graph-core**: pure ObjectScript graph/vector primitives.
- **iris-vector-graph**: depends on core; Python bridge, MCP, Cypher engine, BM25, etc.

A class is placed in the full module when its ``.cls`` file contains
``Language = python`` (embedded Python) or its resource name is listed in
``FULL_MODULE_EXTRA`` below.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "iris_src" / "src"
MODULE_CORE = REPO_ROOT / "module-core.xml"
MODULE_FULL = REPO_ROOT / "module.xml"

ZPM_VERSION = "2.5.0-trifour.1"

# Bridge / integration classes without embedded Python — belong in full module.
FULL_MODULE_EXTRA: frozenset[str] = frozenset(
    {
        "Graph.KG.ArnoAccel.CLS",
        "Graph.KG.BM25Index.CLS",
        "Graph.KG.BenchFormat.CLS",
        "Graph.KG.Benchmark.CLS",
        "Graph.KG.Edge.CLS",
        "Graph.KG.EmbedQueue.CLS",
        "Graph.KG.Loader.CLS",
        "Graph.KG.PyOps.CLS",
        "Graph.KG.Service.CLS",
        "iris.vector.graph.GraphOperators.CLS",
        "User.Exec.CLS",
    }
)

_EMBEDDED_PYTHON = re.compile(r"Language\s*=\s*python", re.IGNORECASE)


def cls_path_to_resource(cls_path: Path) -> str:
    """Map ``iris_src/src/Graph/KG/Foo.cls`` → ``Graph.KG.Foo.CLS``."""
    rel = cls_path.relative_to(SRC_ROOT).as_posix()
    if not rel.endswith(".cls"):
        raise ValueError(f"not a .cls file: {cls_path}")
    dotted = rel[:-4].replace("/", ".")
    return f"{dotted}.CLS"


def filesystem_class_files() -> list[Path]:
    paths = sorted(SRC_ROOT.rglob("*.cls"))
    if not paths:
        raise FileNotFoundError(f"no .cls files under {SRC_ROOT}")
    return paths


def filesystem_resources() -> frozenset[str]:
    return frozenset(cls_path_to_resource(p) for p in filesystem_class_files())


def split_resources(resources: frozenset[str]) -> tuple[list[str], list[str]]:
    by_name = {cls_path_to_resource(p): p for p in filesystem_class_files()}
    full: list[str] = []
    core: list[str] = []
    for name in sorted(resources):
        cls_path = by_name[name]
        text = cls_path.read_text(encoding="utf-8")
        if name in FULL_MODULE_EXTRA or _EMBEDDED_PYTHON.search(text):
            full.append(name)
        else:
            core.append(name)
    return core, full


def _resource_lines(names: list[str], indent: str) -> str:
    return "\n".join(f'{indent}<Resource Name="{n}"/>' for n in names)


def render_module_core(resources: list[str]) -> str:
    resources_block = _resource_lines(resources, "      ")
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<Export generator="Cache" version="25">
  <Document name="iris-vector-graph-core.ZPM">
    <Module>
      <Name>iris-vector-graph-core</Name>
      <Description>Pure ObjectScript graph + vector primitives for IRIS. No Python required.</Description>
      <Version>{ZPM_VERSION}</Version>
      <Packaging>module</Packaging>
      <SourcesRoot>iris_src/src</SourcesRoot>
{resources_block}
      <Keywords>
        <Keyword>vector</Keyword>
        <Keyword>graph</Keyword>
        <Keyword>knowledge-graph</Keyword>
        <Keyword>ANN</Keyword>
        <Keyword>PLAID</Keyword>
        <Keyword>PageRank</Keyword>
        <Keyword>objectscript</Keyword>
      </Keywords>
    </Module>
  </Document>
</Export>
"""


def render_module_full(resources: list[str]) -> str:
    resources_block = _resource_lines(resources, "      ")
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<Export generator="Cache" version="25">
  <Document name="iris-vector-graph.ZPM">
    <Module>
      <Name>iris-vector-graph</Name>
      <Description>Knowledge graph engine for InterSystems IRIS — temporal property graph, openCypher queries, vector search (HNSW/IVFFlat/BM25/PLAID), graph analytics (PageRank, WCC, PPR), and pre-aggregated time-series analytics. Python SDK on PyPI: iris-vector-graph.</Description>
      <Version>{ZPM_VERSION}</Version>
      <Packaging>module</Packaging>
      <SourcesRoot>iris_src/src</SourcesRoot>
      <Dependencies>
        <ModuleReference>
          <Name>iris-vector-graph-core</Name>
          <Version>{ZPM_VERSION}</Version>
        </ModuleReference>
      </Dependencies>
{resources_block}
      <Keywords>
        <Keyword>vector</Keyword>
        <Keyword>graph</Keyword>
        <Keyword>knowledge-graph</Keyword>
        <Keyword>cypher</Keyword>
        <Keyword>temporal-graph</Keyword>
        <Keyword>vector-search</Keyword>
        <Keyword>graphql</Keyword>
        <Keyword>FHIR</Keyword>
        <Keyword>PLAID</Keyword>
        <Keyword>openCypher</Keyword>
        <Keyword>analytics</Keyword>
      </Keywords>
    </Module>
  </Document>
</Export>
"""


def parse_module_resources(path: Path) -> frozenset[str]:
    text = path.read_text(encoding="utf-8")
    return frozenset(re.findall(r'<Resource Name="([^"]+)"', text))


def generate(*, check_only: bool = False) -> tuple[int, int, int]:
    all_resources = filesystem_resources()
    core, full = split_resources(all_resources)
    if len(core) + len(full) != len(all_resources):
        raise RuntimeError("core/full partition does not cover all classes")
    overlap = set(core) & set(full)
    if overlap:
        raise RuntimeError(f"core/full overlap: {sorted(overlap)}")

    core_xml = render_module_core(core)
    full_xml = render_module_full(full)

    if check_only:
        drift: list[str] = []
        if parse_module_resources(MODULE_CORE) != frozenset(core):
            drift.append(str(MODULE_CORE))
        if parse_module_resources(MODULE_FULL) != frozenset(full):
            drift.append(str(MODULE_FULL))
        if MODULE_CORE.read_text(encoding="utf-8") != core_xml:
            drift.append(f"{MODULE_CORE} (content)")
        if MODULE_FULL.read_text(encoding="utf-8") != full_xml:
            drift.append(f"{MODULE_FULL} (content)")
        if drift:
            print("DRIFT:", ", ".join(drift), file=sys.stderr)
            return len(all_resources), len(core), len(full)
        print(f"OK: {len(all_resources)} classes ({len(core)} core, {len(full)} full)")
        return len(all_resources), len(core), len(full)

    MODULE_CORE.write_text(core_xml, encoding="utf-8", newline="\n")
    MODULE_FULL.write_text(full_xml, encoding="utf-8", newline="\n")
    print(
        f"Wrote {MODULE_CORE.name} ({len(core)} resources) and "
        f"{MODULE_FULL.name} ({len(full)} resources); total {len(all_resources)} classes"
    )
    return len(all_resources), len(core), len(full)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="exit 1 if module XML files differ from generator output",
    )
    args = parser.parse_args()
    total, core_n, full_n = generate(check_only=args.check)
    if args.check:
        core_set = parse_module_resources(MODULE_CORE)
        full_set = parse_module_resources(MODULE_FULL)
        expected_core, expected_full = split_resources(filesystem_resources())
        if core_set != frozenset(expected_core) or full_set != frozenset(expected_full):
            return 1
        if MODULE_CORE.read_text(encoding="utf-8") != render_module_core(
            list(expected_core)
        ):
            return 1
        if MODULE_FULL.read_text(encoding="utf-8") != render_module_full(
            list(expected_full)
        ):
            return 1
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
