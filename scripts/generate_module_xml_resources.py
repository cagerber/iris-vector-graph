#!/usr/bin/env python3
"""Generate module.xml, module-core.xml, module-vector.xml, and module-mcp.xml from iris_src/src/**/*.cls.

Module split:
- **iris-vector-graph-core**: pure ObjectScript graph primitives (no embedded Python).
- **iris-vector-graph**: depends on core; Python bridge, Cypher engine, BM25, etc.
- **iris-vector-graph-vector**: optional; classes declaring ``%Library.Vector`` (VECTOR license).
- **iris-vector-graph-mcp**: optional; ``%AI.MCP`` / ``%AI.Tool*`` (full IRIS only, not IRIS Health).
- **iris-vector-graph-embed**: optional; ``IVG.PageRankEmbedded`` (heavy embedded Python; not required for ODS graph deploy on Health).

A class is placed in the full module when its ``.cls`` file contains
``Language = python`` (embedded Python), or its resource name is listed in
``FULL_MODULE_EXTRA`` or ``SCHEMA_DEPENDENT_CLASSES`` below.
``SCHEMA_DEPENDENT_CLASSES`` must use runtime ``%SQL.Statement`` (not ``&sql``)
so the full module compiles before ``InitSchema``.
Classes in ``VECTOR_MODULE_CLASSES``, ``MCP_MODULE_CLASSES``, or
``EMBED_MODULE_CLASSES`` go to the vector, MCP, or embed optional modules.
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
MODULE_VECTOR = REPO_ROOT / "module-vector.xml"
MODULE_MCP = REPO_ROOT / "module-mcp.xml"
MODULE_EMBED = REPO_ROOT / "module-embed.xml"

ZPM_VERSION = "2.5.0-trifour.7"

# Full-module Graph_KG consumers (placement only). Use runtime %SQL.Statement —
# compile-time &sql fails because InitSchema runs only after the full IPM load
# (CypherEngine lives in the full module; chicken-and-egg with table existence).
SCHEMA_DEPENDENT_CLASSES: frozenset[str] = frozenset(
    {
        "Graph.KG.EdgeScan.CLS",
        "Graph.KG.Subgraph.CLS",
        "Graph.KG.TraversalBuild.CLS",
    }
)

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
    }
)

# Requires IRIS Vector Search license — compile fails with #15806 on IRISHealth.
VECTOR_MODULE_CLASSES: frozenset[str] = frozenset(
    {
        "Graph.KG.kgNodeEmbeddings.CLS",
        "Graph.KG.kgNodeEmbeddingsoptimized.CLS",
    }
)

# Optional embed module — not loaded on IRIS Health (ODS graph deploy).
EMBED_MODULE_CLASSES: frozenset[str] = frozenset(
    {
        "IVG.PageRankEmbedded.CLS",
    }
)

# Requires %AI.MCP / %AI.Tool* — not shipped on IRIS Health (e.g. CREST-ODS).
MCP_MODULE_CLASSES: frozenset[str] = frozenset(
    {
        "Graph.KG.MCPService.CLS",
        "Graph.KG.MCPToolSet.CLS",
        "Graph.KG.MCPTools.CLS",
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


def split_resources(
    resources: frozenset[str],
) -> tuple[list[str], list[str], list[str], list[str], list[str]]:
    by_name = {cls_path_to_resource(p): p for p in filesystem_class_files()}
    full: list[str] = []
    core: list[str] = []
    vector: list[str] = []
    mcp: list[str] = []
    embed: list[str] = []
    for name in sorted(resources):
        cls_path = by_name[name]
        text = cls_path.read_text(encoding="utf-8")
        if name in VECTOR_MODULE_CLASSES:
            vector.append(name)
        elif name in MCP_MODULE_CLASSES:
            mcp.append(name)
        elif name in EMBED_MODULE_CLASSES:
            embed.append(name)
        elif (
            name in SCHEMA_DEPENDENT_CLASSES
            or name in FULL_MODULE_EXTRA
            or _EMBEDDED_PYTHON.search(text)
        ):
            full.append(name)
        else:
            core.append(name)
    return core, full, vector, mcp, embed


def _resource_lines(names: list[str], indent: str) -> str:
    return "\n".join(f'{indent}<Resource Name="{n}"/>' for n in names)


def render_module_core(resources: list[str]) -> str:
    resources_block = _resource_lines(resources, "      ")
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<Export generator="Cache" version="25">
  <Document name="iris-vector-graph-core.ZPM">
    <Module>
      <Name>iris-vector-graph-core</Name>
      <Description>Pure ObjectScript graph primitives for IRIS. No Python required. No VECTOR license required.</Description>
      <Version>{ZPM_VERSION}</Version>
      <Packaging>module</Packaging>
      <SourcesRoot>iris_src/src</SourcesRoot>
{resources_block}
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
      <Description>Knowledge graph engine for InterSystems IRIS — temporal property graph, openCypher queries, graph analytics (PageRank, WCC, PPR), and pre-aggregated time-series analytics. Python SDK on PyPI: iris-vector-graph.</Description>
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
    </Module>
  </Document>
</Export>
"""


def render_module_vector(resources: list[str]) -> str:
    resources_block = _resource_lines(resources, "      ")
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<Export generator="Cache" version="25">
  <Document name="iris-vector-graph-vector.ZPM">
    <Module>
      <Name>iris-vector-graph-vector</Name>
      <Description>VECTOR Search license required — kg_NodeEmbeddings persistence (%Library.Vector). Not a dependency of iris-vector-graph-core or iris-vector-graph.</Description>
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
    </Module>
  </Document>
</Export>
"""


def render_module_mcp(resources: list[str]) -> str:
    resources_block = _resource_lines(resources, "      ")
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<Export generator="Cache" version="25">
  <Document name="iris-vector-graph-mcp.ZPM">
    <Module>
      <Name>iris-vector-graph-mcp</Name>
      <Description>Requires %AI.MCP (full IRIS; not IRIS Health). MCP service and knowledge-graph tools. Not a dependency of iris-vector-graph-core or iris-vector-graph.</Description>
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
    </Module>
  </Document>
</Export>
"""


def render_module_embed(resources: list[str]) -> str:
    resources_block = _resource_lines(resources, "      ")
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<Export generator="Cache" version="25">
  <Document name="iris-vector-graph-embed.ZPM">
    <Module>
      <Name>iris-vector-graph-embed</Name>
      <Description>Optional embedded-Python PageRank helpers. Not a dependency of iris-vector-graph-core or iris-vector-graph. Skip on IRIS Health ODS graph deploy.</Description>
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
    </Module>
  </Document>
</Export>
"""


def parse_module_resources(path: Path) -> frozenset[str]:
    text = path.read_text(encoding="utf-8")
    return frozenset(re.findall(r'<Resource Name="([^"]+)"', text))


def generate(*, check_only: bool = False) -> tuple[int, int, int, int, int, int]:
    all_resources = filesystem_resources()
    core, full, vector, mcp, embed = split_resources(all_resources)
    partitions = core, full, vector, mcp, embed
    if sum(len(p) for p in partitions) != len(all_resources):
        raise RuntimeError("core/full/vector/mcp/embed partition does not cover all classes")
    overlap = (
        (set(core) & set(full))
        | (set(core) & set(vector))
        | (set(core) & set(mcp))
        | (set(core) & set(embed))
        | (set(full) & set(vector))
        | (set(full) & set(mcp))
        | (set(full) & set(embed))
        | (set(vector) & set(mcp))
        | (set(vector) & set(embed))
        | (set(mcp) & set(embed))
    )
    if overlap:
        raise RuntimeError(f"module overlap: {sorted(overlap)}")

    core_xml = render_module_core(core)
    full_xml = render_module_full(full)
    vector_xml = render_module_vector(vector)
    mcp_xml = render_module_mcp(mcp)
    embed_xml = render_module_embed(embed)

    if check_only:
        drift: list[str] = []
        if parse_module_resources(MODULE_CORE) != frozenset(core):
            drift.append(str(MODULE_CORE))
        if parse_module_resources(MODULE_FULL) != frozenset(full):
            drift.append(str(MODULE_FULL))
        if parse_module_resources(MODULE_VECTOR) != frozenset(vector):
            drift.append(str(MODULE_VECTOR))
        if parse_module_resources(MODULE_MCP) != frozenset(mcp):
            drift.append(str(MODULE_MCP))
        if parse_module_resources(MODULE_EMBED) != frozenset(embed):
            drift.append(str(MODULE_EMBED))
        if MODULE_CORE.read_text(encoding="utf-8") != core_xml:
            drift.append(f"{MODULE_CORE} (content)")
        if MODULE_FULL.read_text(encoding="utf-8") != full_xml:
            drift.append(f"{MODULE_FULL} (content)")
        if MODULE_VECTOR.read_text(encoding="utf-8") != vector_xml:
            drift.append(f"{MODULE_VECTOR} (content)")
        if MODULE_MCP.read_text(encoding="utf-8") != mcp_xml:
            drift.append(f"{MODULE_MCP} (content)")
        if MODULE_EMBED.read_text(encoding="utf-8") != embed_xml:
            drift.append(f"{MODULE_EMBED} (content)")
        if drift:
            print("DRIFT:", ", ".join(drift), file=sys.stderr)
            return len(all_resources), len(core), len(full), len(vector), len(mcp), len(embed)
        print(
            f"OK: {len(all_resources)} classes "
            f"({len(core)} core, {len(full)} full, {len(vector)} vector, "
            f"{len(mcp)} mcp, {len(embed)} embed)"
        )
        return len(all_resources), len(core), len(full), len(vector), len(mcp), len(embed)

    MODULE_CORE.write_text(core_xml, encoding="utf-8", newline="\n")
    MODULE_FULL.write_text(full_xml, encoding="utf-8", newline="\n")
    MODULE_VECTOR.write_text(vector_xml, encoding="utf-8", newline="\n")
    MODULE_MCP.write_text(mcp_xml, encoding="utf-8", newline="\n")
    MODULE_EMBED.write_text(embed_xml, encoding="utf-8", newline="\n")
    print(
        f"Wrote {MODULE_CORE.name} ({len(core)}), "
        f"{MODULE_FULL.name} ({len(full)}), "
        f"{MODULE_VECTOR.name} ({len(vector)}), "
        f"{MODULE_MCP.name} ({len(mcp)}), "
        f"{MODULE_EMBED.name} ({len(embed)}); total {len(all_resources)} classes"
    )
    return len(all_resources), len(core), len(full), len(vector), len(mcp), len(embed)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="exit 1 if module XML files differ from generator output",
    )
    args = parser.parse_args()
    generate(check_only=args.check)
    if args.check:
        expected_core, expected_full, expected_vector, expected_mcp, expected_embed = (
            split_resources(filesystem_resources())
        )
        if parse_module_resources(MODULE_CORE) != frozenset(expected_core):
            return 1
        if parse_module_resources(MODULE_FULL) != frozenset(expected_full):
            return 1
        if parse_module_resources(MODULE_VECTOR) != frozenset(expected_vector):
            return 1
        if parse_module_resources(MODULE_MCP) != frozenset(expected_mcp):
            return 1
        if parse_module_resources(MODULE_EMBED) != frozenset(expected_embed):
            return 1
        if MODULE_CORE.read_text(encoding="utf-8") != render_module_core(
            list(expected_core)
        ):
            return 1
        if MODULE_FULL.read_text(encoding="utf-8") != render_module_full(
            list(expected_full)
        ):
            return 1
        if MODULE_VECTOR.read_text(encoding="utf-8") != render_module_vector(
            list(expected_vector)
        ):
            return 1
        if MODULE_MCP.read_text(encoding="utf-8") != render_module_mcp(
            list(expected_mcp)
        ):
            return 1
        if MODULE_EMBED.read_text(encoding="utf-8") != render_module_embed(
            list(expected_embed)
        ):
            return 1
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
