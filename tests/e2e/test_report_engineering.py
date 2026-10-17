"""The engineering report: its sections, figures, wording and branding (#987).

The engineering report is the supporting deliverable next to the PDF report and
the results page. These checks read the run's ``deliverables/engineering-report.md``
on every sample and, in external-job mode, on a headless run's own job, so the
headless e2e-llm run checks it as its ``engineering`` bucket (``ci/llm/run.py``).
"""

from __future__ import annotations

import json
import re

from src.report.facts import report_facts
from tests.branding import hits
from tests.e2e.pipeline import PipelineResult

# Placeholders a broken renderer leaks: "undefined"/"NaN"/"null"/"None" as whole
# words, "[object Object]" and "{{" anywhere.
FORBIDDEN_WORDS = ("undefined", "NaN", "null", "None")
FORBIDDEN_SUBSTRINGS = ("[object Object]", "{{", "None%")

ALWAYS = (
    "# Database Modernization — Engineering Report",
    "## Target engines",
    "## Cost",
)


def _md(run: PipelineResult) -> str:
    return run.deliverable("engineering-report.md").read_text(encoding="utf-8")


def _report(run: PipelineResult) -> dict:
    report: dict = json.loads(run.report_json().read_text())
    return report


def test_has_every_always_present_section(run: PipelineResult) -> None:
    md = _md(run)
    for heading in ALWAYS:
        assert f"\n{heading}" in f"\n{md}", heading
    if _report(run).get("migration_waves"):
        assert "\n## Migration roadmap (" in md


def test_leaks_no_placeholder(run: PipelineResult) -> None:
    md = _md(run)
    leaked = [w for w in FORBIDDEN_WORDS if re.search(rf"\b{re.escape(w)}\b", md)]
    leaked += [s for s in FORBIDDEN_SUBSTRINGS if s in md]
    assert leaked == []


def test_names_no_other_product(run: PipelineResult) -> None:
    # Internal wording is checked by test_customer_facing_wording.py.
    assert hits(_md(run)) == []


def test_the_engine_table_total_is_the_stored_fact(run: PipelineResult) -> None:
    """The "N tables migrate" total the retired decision report showed as a tile."""
    facts = report_facts(_report(run))
    n = int(facts.get("tables_migrate") or 0)
    noun = "table migrates" if n == 1 else "tables migrate"
    total = next(line for line in _md(run).splitlines() if line.startswith("| Total |"))
    assert f"| {n} {noun} |" in total
