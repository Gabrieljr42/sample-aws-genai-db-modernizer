"""Issue #524: the deck's and decision report's per-engine Scope column (now the
engineering report's and the PDF report's) showed
"0 source tables" for DynamoDB while the same deliverables' "Tables migrate"
tile, and the migration roadmap, both said 8 source tables migrate to it.

On the wordpress ``--llm-mode none`` run, no engine has a schema design, so
``table_mappings`` is empty and every entry in
``recommended_architecture.databases`` carries ``table_count: 0`` (that field
is only ever populated from a schema design's table mappings). The tile and
the roadmap both read a migration wave's own ``table_count`` instead; the
per-engine Scope column read ``recommended_architecture.databases`` with no
such fallback, so it kept showing 0 for the same engine the other two
numbers, on the same page, already called 8.

``_architecture_engines`` (``src/report/architecture.py``) now reads that same
per-wave ``table_count`` (via ``resolve_migration_waves``) for a migration
target with no completed schema design, so the Scope column, the tile and
the roadmap all come from the one field. An engine with a completed schema
design is never affected: its own mapped-table figure (however it reads, 0
included) is left alone.

Fixtures:
- ``wordpress_no_schema_design_report.json``: the real synthesis report from
  ``uv run python scripts/run_assessment.py --all -y --llm-mode none`` on the
  bundled wordpress sample, exactly the shape that triggered the defect.
- ``e2e09_report.json``: the real, schema-designed run already used by
  ``test_report_renderers.py``, reused here (rigged) for the completed-design
  guard.
"""

from __future__ import annotations

import copy
import json
import re
from pathlib import Path
from typing import Any

import pytest

from src.report import architecture, renderers

FIXTURE = Path(__file__).parent / "fixtures" / "wordpress_no_schema_design_report.json"
DESIGNED_FIXTURE = Path(__file__).parent / "fixtures" / "e2e09_report.json"


@pytest.fixture(scope="module")
def report() -> dict[str, Any]:
    data: dict = json.loads(FIXTURE.read_text())
    return data


@pytest.fixture(scope="module")
def designed_report() -> dict[str, Any]:
    data: dict = json.loads(DESIGNED_FIXTURE.read_text())
    return data


def test_fixture_reproduces_the_defect_shape(report: dict[str, Any]) -> None:
    """Sanity check on the fixture itself: no schema design ran, so every
    ``recommended_architecture.databases`` entry carries ``table_count: 0``,
    while the migration roadmap already knows DynamoDB serves 8 source tables."""
    assert all(
        s.get("status") != "completed" for s in (report.get("schema_designs") or {}).values()
    )
    assert report.get("table_mappings") == []
    dbs = {d["service"]: d["table_count"] for d in report["recommended_architecture"]["databases"]}
    assert dbs["dynamodb"] == 0
    dynamodb_wave = next(w for w in report["migration_waves"] if w["engines"] == ["dynamodb"])
    assert dynamodb_wave["table_count"] == 8


def test_architecture_engines_falls_back_to_the_migration_wave_count(
    report: dict[str, Any],
) -> None:
    rows = {e["engine"]: e for e in architecture._architecture_engines(report)}
    dynamodb = rows["dynamodb"]
    assert dynamodb["role"] == "Migration target"
    assert dynamodb["migrates"] == 8
    assert dynamodb["scope"] == "8 source tables"


def test_engineering_report_scope_total_and_roadmap_agree(report: dict[str, Any]) -> None:
    md = renderers.render_engineering_report_md(report)
    total_n = int(
        re.search(r"^\| Total \| \| [^|]* \| (\d+) tables? migrates? \|", md, re.M).group(1)
    )
    row = re.search(
        r"^\| DynamoDB \| Migration target \| [\d.]+% \| (\d+) source tables? \|", md, re.M
    )
    assert row is not None, md
    scope_n = int(row.group(1))
    wave = md[md.index("DynamoDB", md.index("## Migration roadmap")) :]
    roadmap = re.search(r"^- (\d+) source tables?:", wave, re.M)
    assert roadmap is not None, md
    roadmap_n = int(roadmap.group(1))
    assert scope_n == total_n == roadmap_n == 8


def test_designed_engine_with_zero_mapped_tables_keeps_its_own_figure(
    designed_report: dict[str, Any],
) -> None:
    """A completed schema design is never second-guessed by the wave fallback,
    even when its own mapped-table count reads 0 and a migration wave offers a
    different, larger number for the same engine -- the #524 fix only fires
    for an engine with no completed design."""
    rigged = copy.deepcopy(designed_report)
    for d in rigged["recommended_architecture"]["databases"]:
        if d["service"] == "documentdb":
            d["table_count"] = 0
    assert rigged["schema_designs"]["documentdb"]["status"] == "completed"
    rigged["migration_waves"] = [
        {
            "wave": 1,
            "engines": ["documentdb"],
            "moves_from": ["aurora_postgresql"],
            "homogeneity": None,
            "table_count": 999,
        }
    ]

    rows = {e["engine"]: e for e in architecture._architecture_engines(rigged)}
    documentdb = rows["documentdb"]
    assert documentdb["role"] == "Migration target"
    # Its own figure (0, with the design's "incl. shared" count carried along),
    # not the wave's 999.
    assert documentdb["migrates"] == 0
    assert documentdb["scope"] == "0 source tables (86 incl. shared)"

    # Not double counted: the aggregate total is dynamodb's real 15 only --
    # documentdb contributes 0, never the wave's 999 on top.
    migrated_total = sum(
        e["migrates"]
        for e in architecture._architecture_engines(rigged)
        if e["role"] == "Migration target"
    )
    assert migrated_total == 15
