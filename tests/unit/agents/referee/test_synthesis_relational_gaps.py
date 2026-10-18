"""Synthesis computes the relational move's gaps once, into the facts (#471, ADR-032)."""

from __future__ import annotations

from typing import Any

from src.agents.referee.synthesis_handler import (
    _write_synthesis_report,
    run_synthesis_deterministic,
)
from src.contracts.synthesis_output import ReportFacts, SynthesisOutputContract
from src.report.facts import report_facts
from src.shared.report_facts import compute_report_facts
from tests.unit.agents.referee.test_synthesis_unservable_capabilities import DB, JOB
from tests.unit.agents.referee.test_synthesis_unservable_capabilities import store as seeded_store


def _report(tmp_path, source: dict[str, Any], tables: list[dict[str, Any]]) -> dict[str, Any]:
    store = seeded_store.__wrapped__(tmp_path)  # type: ignore[attr-defined]
    key = f"{DB}/{JOB}/collector/output.json"
    collector = store.read_json(key)
    collector["metadata"]["source_database"] = source
    collector["database_schema"]["tables"] = tables
    store.write_json(key, collector)
    result = run_synthesis_deterministic(JOB, DB, store, assignment_version=1)
    _write_synthesis_report(store, result, assignment_version=1)
    report: dict[str, Any] = store.read_json(f"{DB}/{JOB}/synthesis/v1/report.json")
    return report


def test_synthesis_writes_the_gaps_of_the_retained_aurora_engine(tmp_path) -> None:
    report = _report(
        tmp_path,
        {"engine": "mysql", "version": "5.7.44"},
        [{"table_id": "users", "storage_engine": "MyISAM"}, {"table_id": "orders"}],
    )
    gaps = report["facts"]["relational_gaps"]
    assert gaps["target_engine"] == "aurora_mysql"
    assert (gaps["source_line"], gaps["target_lines"]) == ("5.7", ["8.0", "8.4"])
    assert [g["rule_id"] for g in gaps["gaps"]] == [
        "mysql-non-innodb-storage-engine",
        "mysql-older-major-version",
    ]
    assert gaps["gaps"][0]["tables"] == ["users"]
    SynthesisOutputContract.model_validate(report)


def test_the_gaps_change_nothing_else_in_the_report(tmp_path) -> None:
    clean = _report(
        tmp_path / "a", {"engine": "mysql", "version": "8.0.36"}, [{"table_id": "users"}]
    )
    gapped = _report(
        tmp_path / "b",
        {"engine": "mysql", "version": "8.0.36"},
        [{"table_id": "users", "storage_engine": "MyISAM"}],
    )
    assert clean["facts"]["relational_gaps"]["gaps"] == []
    assert gapped["facts"]["relational_gaps"]["gaps"]
    for report in (clean, gapped):
        report["facts"].pop("relational_gaps")
        report.pop("timestamp")
    assert gapped == clean


def test_a_report_rebuilt_without_the_collection_has_no_gaps() -> None:
    facts = compute_report_facts({"ranking": [], "migration_waves": []})
    assert facts["relational_gaps"] is None


def test_a_report_written_before_1_35_still_loads() -> None:
    facts = compute_report_facts({"ranking": [], "migration_waves": []})
    facts.pop("relational_gaps")
    assert ReportFacts.model_validate(facts).relational_gaps is None
    old = {"ranking": [], "facts": facts, "contract_version": "1.31"}
    assert "relational_gaps" not in report_facts(old)
