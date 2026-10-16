"""The local UI lists the jobs and renders their results against the local API.

Route selection: the live app only ever links to these pages for a completed job
(see src/ui/src/AppRoutes.js and the pages that `href`/`navigate` to them). The
results page lives at `/analysis/results/:jobId`. Two older URLs redirect to it so
bookmarks keep working, each covered below: `/analysis/results-v2/:jobId` (the
page's name until its version suffix was dropped, #957) and `/analysis/report/:jobId`
(the earlier, orange-styled report, removed in #356). The job-monitoring page
(`/analysis/monitor/:jobId`) and the engine-analysis page were removed (#957); their
URLs fall through to the dashboard. `/analysis/patterns/:jobId` is exercised only with
its required `?target=` query param, since every real link to that route supplies one
(src/ui/src/pages/PatternAnalysis.js reads `target` from `useSearchParams` with no
default; a bare visit fetches `.../analysis/null` and 404s).
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from playwright.sync_api import Page

from src.report.deliverables import DELIVERABLES_DIR, FOLDER_FILENAMES
from tests.e2e.pipeline import PipelineResult

# The product UI is a developer helper; the customer deliverables are the HTML/PDF
# reports (checked cross-browser in test_report_html.py). Restrict this file to
# Chromium only. NOTE for ci/e2e.sh: pytest-playwright implements only_browser via a
# runtime pytest.skip() in pytest_runtest_setup (not deselection), so if this suite
# is ever run with `--browser chromium --browser webkit` under `--fail-on-skip`, the
# webkit parametrizations of every test below will be skipped-then-failed. Run this
# file with a single `--browser chromium` invocation, separate from any multi-browser
# `--fail-on-skip` run.
pytestmark = pytest.mark.only_browser("chromium")

# Mirrors the ENGINE_LABELS map used by the UI's results page
# (src/ui/src/pages/AnalysisResults.js) so this
# test can assert the real, human-readable engine name rather than the raw contract id.
ENGINE_LABELS = {
    "dynamodb": "DynamoDB",
    "documentdb": "DocumentDB",
    "opensearch": "OpenSearch",
    "elasticache": "ElastiCache",
    "aurora_mysql": "Aurora MySQL",
    "aurora_postgresql": "Aurora PostgreSQL",
}


def _watch(page: Page) -> dict[str, list[str]]:
    ev: dict[str, list[str]] = {"console": [], "pageerror": [], "failed": []}

    def on_console(msg: object) -> None:
        if getattr(msg, "type", None) == "error":
            ev["console"].append(msg.text)  # type: ignore[attr-defined]

    def on_pageerror(err: object) -> None:
        ev["pageerror"].append(str(err))

    def on_requestfailed(req: object) -> None:
        ev["failed"].append(req.url)  # type: ignore[attr-defined]

    def on_response(resp: object) -> None:
        if resp.status >= 400 and "/api/v1/" in resp.url:  # type: ignore[attr-defined]
            ev["failed"].append(f"{resp.status} {resp.url}")  # type: ignore[attr-defined]

    page.on("console", on_console)
    page.on("pageerror", on_pageerror)
    page.on("requestfailed", on_requestfailed)
    page.on("response", on_response)
    return ev


def _top_engine_label(r: PipelineResult) -> str:
    """The highest-ranked target engine from the synthesis report, as the UI labels it.

    Reads the path off ``r.report`` (the "report" step), not ``r.steps["synthesis"]``:
    the latter is only populated by the deterministic pipeline, while "report" is
    populated in every mode, including a job wrapped by ``from_existing_job``.
    """
    report_path = r.artifact_root / r.report["report"]
    report = json.loads(report_path.read_text())
    top: str = report["ranking"][0]["target"]
    return ENGINE_LABELS.get(top, top)


def test_dashboard_lists_both_jobs(page: Page, ui: str, all_runs: list[PipelineResult]) -> None:
    ev = _watch(page)
    page.goto(f"{ui}/dashboard")
    page.wait_for_load_state("networkidle")
    text = page.inner_text("body")
    for r in all_runs:
        assert r.job_id in text or r.db in text, f"{r.db}/{r.job_id} not on dashboard"
    assert ev == {"console": [], "pageerror": [], "failed": []}


def test_results_page_shows_the_ranked_engines(
    page: Page, ui: str, all_runs: list[PipelineResult]
) -> None:
    for r in all_runs:
        ev = _watch(page)
        page.goto(f"{ui}/analysis/results/{r.job_id}")
        page.wait_for_load_state("networkidle")
        text = page.inner_text("body")
        assert "undefined" not in text and "NaN" not in text, r.db
        assert r.db in text, (r.db, "source database name missing")
        assert _top_engine_label(r) in text, (
            r.db,
            "top-ranked engine missing",
            _top_engine_label(r),
        )
        assert "/mo" in text, (r.db, "no projected cost rendered")
        assert ev == {"console": [], "pageerror": [], "failed": []}, (r.db, ev)


def test_old_results_v2_route_redirects_to_results(
    page: Page, ui: str, all_runs: list[PipelineResult]
) -> None:
    """Old bookmarks/links to `/analysis/results-v2/:jobId` (#957 dropped the
    version suffix) must land on the `/analysis/results/:jobId` content."""
    for r in all_runs:
        ev = _watch(page)
        page.goto(f"{ui}/analysis/results-v2/{r.job_id}")
        page.wait_for_load_state("networkidle")
        assert page.url.rstrip("/").endswith(f"/analysis/results/{r.job_id}"), (
            r.db,
            page.url,
        )
        text = page.inner_text("body")
        assert "undefined" not in text and "NaN" not in text, r.db
        assert r.db in text, (r.db, "source database name missing")
        assert _top_engine_label(r) in text, (
            r.db,
            "top-ranked engine missing",
            _top_engine_label(r),
        )
        assert ev == {"console": [], "pageerror": [], "failed": []}, (r.db, ev)


def test_assignments_page_recommends_the_ranked_engines(
    page: Page, ui: str, all_runs: list[PipelineResult]
) -> None:
    for r in all_runs:
        ev = _watch(page)
        page.goto(f"{ui}/analysis/assignments/{r.job_id}")
        page.wait_for_load_state("networkidle")
        text = page.inner_text("body")
        assert "undefined" not in text and "NaN" not in text, r.db
        assert f"Your {r.db} workload" in text, (r.db, "workload summary missing")
        assert _top_engine_label(r) in text, (
            r.db,
            "top-ranked engine missing",
            _top_engine_label(r),
        )
        assert ev == {"console": [], "pageerror": [], "failed": []}, (r.db, ev)


def test_legacy_report_route_redirects_to_results(
    page: Page, ui: str, all_runs: list[PipelineResult]
) -> None:
    """Old bookmarks/links to the retired `/analysis/report/:jobId` page (#356,
    the earlier, orange-styled report) must land on the `/analysis/results/:jobId`
    content instead of the now-removed page."""
    for r in all_runs:
        ev = _watch(page)
        page.goto(f"{ui}/analysis/report/{r.job_id}")
        page.wait_for_load_state("networkidle")
        assert page.url.rstrip("/").endswith(f"/analysis/results/{r.job_id}"), (
            r.db,
            page.url,
        )
        text = page.inner_text("body")
        assert "undefined" not in text and "NaN" not in text, r.db
        assert r.db in text, (r.db, "source database name missing")
        assert _top_engine_label(r) in text, (
            r.db,
            "top-ranked engine missing",
            _top_engine_label(r),
        )
        assert ev == {"console": [], "pageerror": [], "failed": []}, (r.db, ev)


@pytest.mark.parametrize("route", ["analysis/monitor", "analysis/engine-analysis"])
def test_removed_job_pages_fall_through_to_the_dashboard(
    page: Page, ui: str, all_runs: list[PipelineResult], route: str
) -> None:
    """#957: the job-monitoring page (dead download buttons) and the engine-analysis
    page (pre-assignment figures that contradicted the results page) are gone; their
    URLs land on the dashboard instead of rendering either page."""
    r = all_runs[0]
    ev = _watch(page)
    page.goto(f"{ui}/{route}/{r.job_id}")
    page.wait_for_load_state("networkidle")
    assert page.url.rstrip("/").endswith("/dashboard"), (route, page.url)
    text = page.inner_text("body")
    assert "Engine analysis:" not in text, route
    assert "Download artifact" not in text and "Download logs" not in text, route
    assert ev == {"console": [], "pageerror": [], "failed": []}, (route, ev)


def test_pattern_analysis_page_renders_for_a_selected_engine(
    page: Page, ui: str, all_runs: list[PipelineResult]
) -> None:
    """Both samples route 'key value lookups' queries to dynamodb, so it is always
    a valid, non-empty ``target`` regardless of which sample is loaded."""
    for r in all_runs:
        ev = _watch(page)
        page.goto(f"{ui}/analysis/patterns/{r.job_id}?target=dynamodb")
        page.wait_for_load_state("networkidle")
        text = page.inner_text("body")
        assert "undefined" not in text and "NaN" not in text, r.db
        assert "patterns detected for dynamodb" in text, (r.db, "pattern analysis body missing")
        assert "Key Value Lookups" in text, (r.db, "expected signal missing")
        assert ev == {"console": [], "pageerror": [], "failed": []}, (r.db, ev)


def _moved_query_trace(r: PipelineResult) -> dict | None:
    """The full decision trace (report.json ``query_traces``) of the first query whose
    engine moved, or None when no query moved (a real run may move none)."""
    report = json.loads((r.artifact_root / r.report["report"]).read_text())
    listed = ((report.get("facts") or {}).get("query_trace") or {}).get("listed_queries") or []
    traces = {q["query_id"]: q for q in (report.get("query_traces") or {}).get("queries") or []}
    moved = [q["query_id"] for q in listed if q.get("moved") and q["query_id"] in traces]
    trace: dict | None = traces[moved[0]] if moved else None
    return trace


OPEN_TRACE = "Open the journey and decision trace for query "


def test_a_querys_trace_opens_from_the_routing_table(
    page: Page, ui: str, all_runs: list[PipelineResult]
) -> None:
    """#822: the results page's query routing table opens a query's journey and its
    "Why this engine" trace from the keyboard, with no schema design in the run.

    On the deterministic wordpress sample (or an external job) it opens a moved
    query and checks the move; on a run where no query moved it opens the first
    query and checks the trace renders.
    """
    r = next((x for x in all_runs if x.db == "wordpress"), all_runs[0])
    if not r.external:
        assert not list(r.job_dir().glob("schema-*")), "the deterministic run has no schema design"
        assert _moved_query_trace(r) is not None, "the wordpress sample moves queries"
    trace = _moved_query_trace(r)

    ev = _watch(page)
    page.goto(f"{ui}/analysis/results/{r.job_id}")
    page.wait_for_load_state("networkidle")
    section = page.locator("#query-routing-section")
    section.get_by_role("table", name="Query routing").wait_for()
    if not r.external:  # wordpress lists 107 queries: more than one page of 25
        section.get_by_role("button", name="Next page").wait_for()
        section.get_by_role("button", name="Previous page").wait_for()
    if trace is not None:
        qid = trace["query_id"]
        section.get_by_role("button", name="Routing changed or decided").click()
        section.get_by_label("Filter queries").fill(qid[:16])
        opener = section.get_by_role("button", name=f"{OPEN_TRACE}{qid}")
    else:
        opener = section.get_by_role("button", name=re.compile(f"^{OPEN_TRACE}")).first
        qid = (opener.get_attribute("aria-label") or "")[len(OPEN_TRACE) :]
    opener.focus()
    page.keyboard.press("Enter")

    modal = page.get_by_role("dialog")
    modal.get_by_text(f"Query Journey: {qid}").wait_for()
    modal.get_by_role("tab", name="Why this engine").click()
    modal.get_by_test_id("query-trace-final").first.wait_for()
    first = modal.get_by_test_id("query-trace-first")
    no_ledger = modal.get_by_test_id("query-trace-no-ledger")
    assert (
        first.count() + no_ledger.count() == 1
    ), "neither the first assignment nor the no-ledger note"
    text = modal.inner_text()
    if trace is not None:
        move = next(
            m for m in trace["moves"] if m["from_engine"] and m["from_engine"] != m["to_engine"]
        )
        modal.get_by_test_id("query-trace-moves").get_by_text(move["explanation"]).wait_for()
        assert trace["final_engine_name"] in text, (r.db, "final engine missing from the trace")
        assert "No later routing step changed this query." not in text
    assert "undefined" not in text and "NaN" not in text
    assert ev == {"console": [], "pageerror": [], "failed": []}, (r.db, ev)


def _decision_wave_stats(page: Page, r: PipelineResult) -> dict[str, str]:
    """Each wave's headline as the decision report prints it, by wave number."""
    page.goto(r.html("decision-report").resolve().as_uri())
    return {
        str(w.get_attribute("data-wave")): " ".join(
            w.locator("[data-figure=wave-stats]").inner_text().split()
        )
        for w in page.locator("[data-wave]").all()
    }


def test_results_page_shows_the_reports_wave_plan(
    page: Page, ui: str, all_runs: list[PipelineResult]
) -> None:
    """#957: the results page's migration roadmap shows the same waves as the
    decision report (and so the PDF and the engineering report), with the same
    headline for each wave, read from report.json."""
    for r in all_runs:
        expected = _decision_wave_stats(page, r)
        assert expected, (r.db, "the decision report shows no waves")
        report = json.loads((r.artifact_root / r.report["report"]).read_text())
        gates = {str(w["wave"]): w.get("gate") or "" for w in report["migration_waves"]}
        ev = _watch(page)
        page.goto(f"{ui}/analysis/results/{r.job_id}")
        page.wait_for_load_state("networkidle")
        section = page.locator("#migration-roadmap-section")
        section.wait_for()
        cards = section.locator("[data-wave]").all()
        shown = {
            str(c.get_attribute("data-wave")): " ".join(
                c.locator("[data-figure=wave-stats]").inner_text().split()
            )
            for c in cards
        }
        assert shown == expected, r.db
        for c in cards:
            n = str(c.get_attribute("data-wave"))
            if gates[n]:
                gate = c.locator("[data-figure=wave-gate]").inner_text()
                assert gate == f"Gate before the next wave: {gates[n]}", (r.db, n)
        count = report["facts"]["migration_wave_count"]
        assert f"Migration roadmap ({count} waves)" in section.inner_text(), r.db
        assert ev == {"console": [], "pageerror": [], "failed": []}, (r.db, ev)


def test_results_page_downloads_the_deliverables_folder(
    page: Page, ui: str, all_runs: list[PipelineResult]
) -> None:
    """#957: the results page's download panel lists the job's deliverables/
    folder in the folder definition's order and downloads each file as it is on
    disk."""
    for r in all_runs:
        folder = r.job_dir() / DELIVERABLES_DIR
        expected = [name for name in FOLDER_FILENAMES if (folder / name).is_file()]
        assert "assessment.pdf" in expected and "index.md" in expected, (r.db, expected)
        ev = _watch(page)
        page.goto(f"{ui}/analysis/results/{r.job_id}")
        page.wait_for_load_state("networkidle")
        section = page.locator("#deliverables-section")
        rows = section.locator("[data-deliverable]")
        rows.first.wait_for()
        assert [x.get_attribute("data-deliverable") for x in rows.all()] == expected, r.db
        for name in ("assessment.pdf", "engineering-report.md", "index.md"):
            with page.expect_download() as info:
                section.get_by_role("link", name=f"Download {name}").click()
            download = info.value
            assert download.suggested_filename == name, (r.db, name)
            assert Path(download.path()).read_bytes() == (folder / name).read_bytes(), (r.db, name)
        # A download ends its navigation, which the browser reports as a failed
        # request for exactly that file; anything else failing is a real error.
        downloads = {f"{r.job_id}/deliverables/{n}" for n in expected}
        ev["failed"] = [u for u in ev["failed"] if u.split("/assessments/", 1)[-1] not in downloads]
        assert ev == {"console": [], "pageerror": [], "failed": []}, (r.db, ev)


def test_ui_shows_only_the_product_name(
    page: Page, ui: str, all_runs: list[PipelineResult]
) -> None:
    """#957: no page names "ATX", "AWS Transform" or "transform"; the header says
    "Database Modernizer Assessment Tool"."""
    r = all_runs[0]
    for path in (
        "/dashboard",
        "/analysis/create",
        f"/analysis/monitor/summary/{r.job_id}",
        f"/analysis/results/{r.job_id}",
        "/settings",
    ):
        page.goto(f"{ui}{path}")
        page.wait_for_load_state("networkidle")
        text = page.inner_text("body")
        assert not re.search(r"\bATX\b|AWS\s+Transform|transform", text, re.IGNORECASE), path
        assert "Database Modernizer Assessment Tool" in text, path
        assert page.title() == "Database Modernizer Assessment Tool", path
