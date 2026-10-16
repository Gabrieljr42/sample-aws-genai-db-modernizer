"""run_report writes the deliverables next to the local synthesis report."""

from __future__ import annotations

import json
import re
from pathlib import Path

from scripts import run_report
from src.storage.local_store import LocalArtifactStore

FIXTURE = Path(__file__).resolve().parents[1] / "report" / "fixtures" / "e2e09_report.json"
DB, JOB = "discourse", "job-x"


def test_writes_staged_deliverables_next_to_the_report(tmp_path: Path, monkeypatch) -> None:
    store = LocalArtifactStore(base_dir=str(tmp_path))
    store.write_json(f"{DB}/{JOB}/synthesis/v2/report.json", json.loads(FIXTURE.read_text()))
    monkeypatch.setattr("src.report.analysis_report.default_graph_fetcher", lambda *_: False)

    result = run_report.run(JOB, DB, str(tmp_path), assignment_version=None)

    # No graph and no legacy query-journeys artifacts -> 0 journeys embedded, which
    # is a warning, not a silent "complete".
    assert result["status"] == "partial", result
    assert result["journeys"] == 0
    assert any("0 query journeys" in w for w in result["warnings"])
    base = tmp_path / DB / JOB / "synthesis" / "v2"
    names = sorted(p.name for p in base.iterdir())
    assert "summary-executive-report.pdf" in names
    assert "summary-executive-report.pptx" in names
    assert any(n.startswith("discourse_decision-report_") and n.endswith(".html") for n in names)
    assert any(n.startswith("discourse_analysis-report_") and n.endswith(".html") for n in names)
    assert any(n.startswith("discourse_engineering-report_") and n.endswith(".md") for n in names)
    assert set(result["files"]) == {
        f"{DB}/{JOB}/synthesis/v2/{n}" for n in names if n != "report.json"
    }
    assert set(result["paths"]) == {str((tmp_path / f).resolve()) for f in result["files"]}


def test_renders_complete_with_a_real_graph_rebuild(tmp_path: Path) -> None:
    """No monkeypatching: the default fetcher rebuilds the graph from a minimal
    collector artifact, so the analysis report embeds a real journey and the
    overall status is "complete", not "partial"."""
    store = LocalArtifactStore(base_dir=str(tmp_path))
    store.write_json(f"{DB}/{JOB}/synthesis/v1/report.json", json.loads(FIXTURE.read_text()))
    store.write_json(
        f"{DB}/{JOB}/collector/output.json",
        {
            "queries": {
                "query_patterns": [
                    {
                        "query_id": "q1",
                        "query_text": "SELECT * FROM posts WHERE id = ?",
                        "query_type": "SELECT",
                        "tables_accessed": ["posts"],
                        "calls_per_second": 12.0,
                    }
                ]
            }
        },
    )

    result = run_report.run(JOB, DB, str(tmp_path), assignment_version=None)

    assert result["status"] == "complete", result
    assert result["journeys"] >= 1
    assert result["warnings"] == []


def test_missing_report_is_an_error_status(tmp_path: Path) -> None:
    result = run_report.run(JOB, DB, str(tmp_path), assignment_version=None)
    assert result["status"] == "error"
    assert "run /synthesize first" in result["message"]


def test_render_failure_still_prints_json_error_status(tmp_path: Path, monkeypatch) -> None:
    store = LocalArtifactStore(base_dir=str(tmp_path))
    store.write_json(f"{DB}/{JOB}/synthesis/v1/report.json", json.loads(FIXTURE.read_text()))

    def boom(*_a, **_k):
        raise ValueError("boom")

    monkeypatch.setattr("src.report.deliverables.render_deliverables", boom)

    result = run_report.run(JOB, DB, str(tmp_path), assignment_version=None)

    assert result["status"] == "error"
    assert "ValueError" in result["message"]


# ---------------------------------------------------------------------------
# The customer deliverables folder: artifacts/<db>/<job>/deliverables/
# ---------------------------------------------------------------------------

# Spelled out here on purpose, not imported: renaming a customer file must be a
# deliberate change to this test as well as to the code and the docs.
DOCUMENTED_FILES = {
    "assessment.pdf",
    "engineering-report.md",
    "assessment-data.json",
    "decision-report.html",
    "analysis-report.html",
    "index.md",
}


def _seed(tmp_path: Path, version: int = 2) -> LocalArtifactStore:
    store = LocalArtifactStore(base_dir=str(tmp_path))
    store.write_json(
        f"{DB}/{JOB}/synthesis/v{version}/report.json", json.loads(FIXTURE.read_text())
    )
    return store


def test_writes_the_deliverables_folder_with_exactly_the_documented_set(
    tmp_path: Path, monkeypatch
) -> None:
    _seed(tmp_path)
    # Model I/O next to the report must never be copied into deliverables/.
    (tmp_path / DB / JOB / "synthesis" / "v2" / "llm_input.json").write_text("{}")
    (tmp_path / DB / JOB / "synthesis" / "v2" / "llm_response.json").write_text("{}")
    monkeypatch.setattr("src.report.analysis_report.default_graph_fetcher", lambda *_: False)

    result = run_report.run(JOB, DB, str(tmp_path), assignment_version=None)

    folder = tmp_path / DB / JOB / "deliverables"
    assert {p.name for p in folder.iterdir()} == DOCUMENTED_FILES
    assert result["deliverables_dir"] == str(folder.resolve())
    assert {Path(p).name for p in result["deliverables"]} == DOCUMENTED_FILES
    # The same bytes as the versioned copy next to report.json.
    v2 = tmp_path / DB / JOB / "synthesis" / "v2"
    assert (folder / "assessment.pdf").read_bytes() == (
        v2 / "summary-executive-report.pdf"
    ).read_bytes()
    assert (folder / "assessment.pdf").read_bytes()[:5] == b"%PDF-"


def test_assessment_data_is_the_report_json(tmp_path: Path, monkeypatch) -> None:
    """The "Assessment Data (JSON)" the reports point at is the report.json."""
    _seed(tmp_path)
    monkeypatch.setattr("src.report.analysis_report.default_graph_fetcher", lambda *_: False)

    run_report.run(JOB, DB, str(tmp_path), assignment_version=None)

    data = json.loads((tmp_path / DB / JOB / "deliverables" / "assessment-data.json").read_text())
    identity = data.pop("_artifact")
    assert identity["artifact"] == "assessment-data"
    assert data == json.loads(FIXTURE.read_text())


def test_index_lists_each_file_its_audience_and_the_assignment_version(
    tmp_path: Path, monkeypatch
) -> None:
    _seed(tmp_path, version=3)
    monkeypatch.setattr("src.report.analysis_report.default_graph_fetcher", lambda *_: False)

    run_report.run(JOB, DB, str(tmp_path), assignment_version=None)

    index = (tmp_path / DB / JOB / "deliverables" / "index.md").read_text()
    assert "Assignment version: v3" in index
    assert f"Job: {JOB}" in index
    for name in DOCUMENTED_FILES - {"index.md"}:
        row = next((line for line in index.splitlines() if f"`{name}`" in line), None)
        assert row is not None, name
        # | file | what it is | who it is for |
        assert row.count("|") == 4 and all(c.strip() for c in row.strip("|").split("|")), row
    assert "Assessment Data (JSON)" in index
    # Branding: the one product name, nothing else.
    assert "Database Modernizer Assessment Tool" in index
    assert not re.search(r"transform|\bATX\b", index, re.IGNORECASE)


def test_an_older_version_leaves_the_deliverables_folder_alone(tmp_path: Path, monkeypatch) -> None:
    """deliverables/ always holds the latest assignment version."""
    _seed(tmp_path, version=1)
    _seed(tmp_path, version=2)
    monkeypatch.setattr("src.report.analysis_report.default_graph_fetcher", lambda *_: False)
    run_report.run(JOB, DB, str(tmp_path), assignment_version=None)
    index = tmp_path / DB / JOB / "deliverables" / "index.md"
    before = index.read_text()

    result = run_report.run(JOB, DB, str(tmp_path), assignment_version=1)

    assert result["status"] != "error", result
    assert result["deliverables_dir"] is None
    assert "v2" in result["deliverables_note"]
    assert index.read_text() == before
    assert "Assignment version: v2" in before


def test_a_rerender_drops_stale_documented_files_and_keeps_others(
    tmp_path: Path, monkeypatch
) -> None:
    """A file this round did not render must not survive from an older round,
    where the index would pass it off as current; files we never write stay."""
    _seed(tmp_path)
    monkeypatch.setattr("src.report.analysis_report.default_graph_fetcher", lambda *_: False)
    run_report.run(JOB, DB, str(tmp_path), assignment_version=None)
    folder = tmp_path / DB / JOB / "deliverables"
    (folder / "my-notes.txt").write_text("customer notes")

    def boom(*_a, **_k):
        raise ValueError("boom")

    monkeypatch.setattr("src.report.analysis_report.build_export_data", boom)
    result = run_report.run(JOB, DB, str(tmp_path), assignment_version=None)

    assert result["status"] == "partial"
    names = {p.name for p in folder.iterdir()}
    assert "analysis-report.html" not in names
    assert "my-notes.txt" in names
    index = (folder / "index.md").read_text()
    assert "`analysis-report.html`" not in index.split("Not rendered")[0]
    assert "analysis-report.html" in index.split("Not rendered")[1]
