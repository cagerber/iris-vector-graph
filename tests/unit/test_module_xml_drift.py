"""Ensure module.xml / module-core.xml cover every iris_src/src/**/*.cls file."""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = REPO_ROOT / "iris_src" / "src"
GENERATOR = REPO_ROOT / "scripts" / "generate_module_xml_resources.py"


def _filesystem_resources() -> set[str]:
    out: set[str] = set()
    for cls_path in SRC_ROOT.rglob("*.cls"):
        rel = cls_path.relative_to(SRC_ROOT).as_posix()
        dotted = rel[:-4].replace("/", ".")
        out.add(f"{dotted}.CLS")
    return out


def _module_resources(path: Path) -> set[str]:
    text = path.read_text(encoding="utf-8")
    return set(re.findall(r'<Resource Name="([^"]+)"', text))


def test_module_resources_match_filesystem() -> None:
    fs = _filesystem_resources()
    assert fs, "expected ObjectScript classes under iris_src/src"
    core = _module_resources(REPO_ROOT / "module-core.xml")
    full = _module_resources(REPO_ROOT / "module.xml")
    assert not (core & full), f"overlap between core and full: {sorted(core & full)}"
    assert core | full == fs, (
        f"module drift: missing={sorted(fs - core - full)} "
        f"extra={sorted((core | full) - fs)}"
    )
    assert len(fs) == 44


def test_generator_check_is_clean() -> None:
    proc = subprocess.run(
        [sys.executable, str(GENERATOR), "--check"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr or proc.stdout
