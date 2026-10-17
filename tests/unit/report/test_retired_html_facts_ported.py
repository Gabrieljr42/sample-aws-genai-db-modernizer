"""What only the retired decision report said now lives in the PDF report and the
engineering report (#987).

The decision report HTML was retired. Before it went, every fact and section it
showed was checked against the PDF report, the engineering report and the UI;
these are the ones nothing else carried, ported with the facts layer's figures
and the decision report's own wording:

- each engine's scope (source tables, retained schema, key designs, synced
  indexes) and the "N tables migrate" total, with the workload total;
- the notes under the engine table: the retained relational core, the search
  read model, the tables the migration moves, and what "incl. shared" counts;
- in the PDF: how the overall risk level is derived, that no language model
  decides an assignment, and why a generated summary was withheld.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest

from src.report import engineering_report, pdf_report
from src.report.architecture import SHARED_TABLES_NOTE
from src.report.facts import report_facts
from tests.unit.report.pdf_text import pdf_text

FIXTURES = Path(__file__).parent / "fixtures"

RULE_NOTE = (
    "Overall risk is the highest severity among the open risks; risks the assignment "
    "resolved do not count."
)
NO_MODEL_DECIDES = (
    "Engine and query assignments are produced deterministically — no language model "
    "decides which engine a table or query goes to. The executive summary is written over "
    "already-computed results and cannot change a recommendation."
)


def _fixture(name: str) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((FIXTURES / name).read_text())
    return data


def _flat(text: str) -> str:
    return " ".join(text.split())


@pytest.fixture(scope="module")
def e2e09() -> dict[str, Any]:
    """Retained, migration targets with shared tables, a cache layer, a search read model."""
    return _fixture("e2e09_report.json")


@pytest.fixture(scope="module")
def e2e09_pdf(e2e09: dict[str, Any]) -> str:
    return pdf_text(e2e09)


def _engines_section(md: str) -> str:
    start = md.index("## Target engines")
    end = md.index("\n## ", start + 1)
    return md[start:end]


# -- scope column and the "tables migrate" total -------------------------------


def test_the_engine_table_has_each_engines_scope_and_the_tables_migrate_total(
    e2e09: dict[str, Any],
) -> None:
    section = _engines_section(engineering_report.render_engineering_report_md(e2e09))
    assert "| Engine | Role | Workload | Scope | Why |" in section
    assert "| Aurora PostgreSQL | Retained |" in section
    assert "| source schema retained |" in section
    assert "| 75 source tables (86 incl. shared) |" in section
    assert "| 5 indexes, synced from owners |" in section
    assert "| 8 key designs |" in section
    facts = report_facts(e2e09)
    total = next(line for line in section.splitlines() if line.startswith("| Total |"))
    assert f"| {facts['tables_migrate']} tables migrate |" in total
    assert f"| {facts['workload_total_percent']:.0f}% |" in total


def test_the_total_is_the_stored_fact_not_a_recount(e2e09: dict[str, Any]) -> None:
    rep = copy.deepcopy(e2e09)
    rep["facts"] = copy.deepcopy(report_facts(e2e09))
    rep["facts"]["tables_migrate"] = 1
    section = _engines_section(engineering_report.render_engineering_report_md(rep))
    assert "| 1 table migrates |" in section


def test_the_pdf_engine_section_has_the_same_scope_and_total(
    e2e09: dict[str, Any], e2e09_pdf: str
) -> None:
    facts = report_facts(e2e09)
    assert "75 source tables (86 incl. shared)" in e2e09_pdf
    assert "source schema retained" in e2e09_pdf
    assert f"{facts['tables_migrate']} tables migrate" in e2e09_pdf


# -- the notes under the engine table ------------------------------------------


def test_the_engine_notes_say_what_each_role_means(e2e09: dict[str, Any], e2e09_pdf: str) -> None:
    facts = report_facts(e2e09)
    notes = [
        "Aurora PostgreSQL is retained as the relational core (source-compatible, no migration).",
        "OpenSearch is a search read model: it serves its queries from data synced from the "
        "engines that own those tables, holds no system-of-record data, and is built after "
        "its owners.",
        f"The migration moves the {facts['tables_migrate']} tables assigned to DocumentDB, "
        "DynamoDB.",
        SHARED_TABLES_NOTE,
    ]
    md = _flat(_engines_section(engineering_report.render_engineering_report_md(e2e09)))
    md = md.replace("**", "")
    for note in notes:
        assert note in md, note
        assert _flat(note) in e2e09_pdf, note


def test_no_role_note_for_a_role_the_plan_does_not_have() -> None:
    rep = _fixture("wordpress_report.json")  # two migration targets, a cache, nothing retained
    md = _flat(_engines_section(engineering_report.render_engineering_report_md(rep)))
    md = md.replace("**", "")
    assert "retained as the relational core" not in md
    assert "search read model:" not in md
    assert "The migration moves the 21 tables assigned to DynamoDB, Aurora MySQL." in md


def test_no_engine_table_and_no_notes_without_engines() -> None:
    md = engineering_report.render_engineering_report_md({"database_name": "x"})
    assert "## Target engines" not in md
    assert "tables migrate" not in md


# -- PDF only: the risk rule, the determinism statement, the withheld summary --


def _with_open_risk(report: dict[str, Any]) -> dict[str, Any]:
    rep = copy.deepcopy(report)
    rep.pop("facts", None)
    rep["risk_assessment"] = {
        "overall_risk_level": "MEDIUM",
        "risks": [
            {
                "risk_id": "RISK-001",
                "risk_type": "MIGRATION_COMPLEXITY",
                "severity": "MEDIUM",
                "description": "[dynamodb] aggregation: DynamoDB has no native equivalent.",
            }
        ],
        "mitigation_strategies": [],
    }
    return rep


def test_the_pdf_states_how_the_overall_risk_is_derived(e2e09: dict[str, Any]) -> None:
    assert RULE_NOTE in pdf_text(_with_open_risk(e2e09))


def test_the_risk_rule_is_not_stated_without_open_risks() -> None:
    rep = {"database_name": "x", "risk_assessment": {"overall_risk_level": "LOW", "risks": []}}
    assert RULE_NOTE not in pdf_text(rep)


def test_the_pdf_says_no_language_model_decides_an_assignment(e2e09_pdf: str) -> None:
    assert _flat(NO_MODEL_DECIDES) in e2e09_pdf
    assert _flat(NO_MODEL_DECIDES) in pdf_text({"database_name": "x"})


def test_the_pdf_says_why_a_generated_summary_was_withheld() -> None:
    rep = _fixture("wordpress_no_schema_design_report.json")
    rep["summary"] = "A model narrative about schema work that never ran."
    text = pdf_text(rep, trust_generated_summary=False)
    assert "never ran" not in text
    assert (
        "The generated narrative was withheld because it referenced schema work this run "
        "did not perform; the figures here are unaffected."
    ) in text


def test_no_withheld_note_when_no_narrative_was_generated() -> None:
    """A deterministic run generated no narrative, so nothing was withheld."""
    rep = _fixture("wordpress_no_schema_design_report.json")
    assert rep["summary"] == rep["summary_deterministic"]
    assert "withheld" not in pdf_text(rep, trust_generated_summary=False)


def test_the_pdf_says_why_a_fallback_summary_replaced_the_narrative() -> None:
    rep = _fixture("wordpress_report.json")
    rep["summary_source"] = "deterministic_fallback"
    text = pdf_text(rep, trust_generated_summary=True)
    assert "The generated narrative was withheld because it" in text
    assert "this summary is built from the effective assignment." in text


def test_no_withheld_note_on_a_trusted_generated_summary() -> None:
    rep = _fixture("wordpress_report.json")
    rep["summary"] = "The model wrote this paragraph."
    rep["summary_source"] = "llm"
    text = pdf_text(rep, trust_generated_summary=True)
    assert "The model wrote this paragraph." in text
    assert "withheld" not in text


def test_the_pdf_points_only_at_files_that_exist() -> None:
    assert "decision" not in pdf_report.SEE_ALSO_NOTE.lower()
    assert "engineering-report.md" in pdf_report.SEE_ALSO_NOTE


# -- PDF: the open ties and the moves after approval (#987 review) -------------

TIED = [
    {"query_id": "q1", "tied_engines": ["documentdb", "dynamodb"], "assigned_engine": "dynamodb"},
]
REROUTES = [
    {
        "query_id": "q2",
        "from_engine": "dynamodb",
        "to_engine": "aurora_mysql",
        "reason_category": "client_side_join",
        "score_before": 80,
        "score_after": 70,
        "approved_version": 2,
    },
]


def _with(report: dict[str, Any], **fields: Any) -> dict[str, Any]:
    rep = copy.deepcopy(report)
    rep.pop("facts", None)
    rep.update(fields)
    return rep


def test_the_pdf_lists_the_unresolved_engine_ties(e2e09: dict[str, Any]) -> None:
    text = pdf_text(_with(e2e09, tied_queries=TIED))
    assert "Unresolved engine ties (1 query)" in text
    assert "pending the customer's own choice" in text
    assert "q1 DocumentDB, DynamoDB DynamoDB" in text


def test_the_pdf_lists_the_queries_moved_after_approval(e2e09: dict[str, Any]) -> None:
    from src.report.engine_choices import REROUTE_OVERRIDE_TEXT

    text = pdf_text(_with(e2e09, post_schema_reroutes=REROUTES))
    assert "Moved after approval (1 query)" in text
    assert _flat(REROUTE_OVERRIDE_TEXT) in text


def test_no_tie_or_move_section_without_either(e2e09: dict[str, Any]) -> None:
    text = pdf_text(_with(e2e09, tied_queries=[], post_schema_reroutes=[]))
    assert "Unresolved engine ties" not in text
    assert "Moved after approval" not in text
