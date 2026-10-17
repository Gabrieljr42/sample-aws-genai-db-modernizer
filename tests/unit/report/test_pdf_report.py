"""The PDF report is a flowing document built from report.json's facts (ADR-032).

Covers what does not need the pipeline: byte-identical renders, cost figures from
the facts layer, complete gates, the markdown subset it draws, and that nothing in
the repo still lists a PPTX deliverable.
"""

from __future__ import annotations

import io
import json
import re
from pathlib import Path
from typing import Any

import pytest
from pypdf import PdfReader

from src.report import pdf_markdown, pdf_report
from src.report.formatting import _cost_baseline_text, _fmt_usd
from src.report.risks import NO_OPEN_RISKS_CAVEAT
from tests.unit.report.pdf_text import pdf_text

FIXTURES = Path(__file__).parent / "fixtures"
REPO = Path(__file__).resolve().parents[3]


def _fixture(name: str) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((FIXTURES / name).read_text())
    return data


@pytest.fixture(scope="module")
def report() -> dict[str, Any]:
    return _fixture("wordpress_no_schema_design_report.json")


def _with_long_gate(report: dict[str, Any]) -> dict[str, Any]:
    """A wave gate far longer than any old clip budget, with a distinctive tail."""
    rep: dict[str, Any] = json.loads(json.dumps(report))
    rep["migration_waves"][-1]["gate"] = (
        "Dual-write validated per table group. "
        + "Parity confirmed on every shard. " * 40
        + "FINAL-CRITERION-ENDS-HERE."
    )
    return rep


def test_two_renders_are_byte_identical(report: dict[str, Any]) -> None:
    first = pdf_report.render_executive_summary_pdf(report)
    second = pdf_report.render_executive_summary_pdf(report)
    assert first == second
    assert first.startswith(b"%PDF")


def test_the_document_carries_no_wall_clock_date(report: dict[str, Any]) -> None:
    data = pdf_report.render_executive_summary_pdf(report)
    meta = PdfReader(io.BytesIO(data)).metadata
    assert meta is not None
    assert meta.creation_date is not None and meta.creation_date.year == 2000  # invariant mode


def test_a_gate_longer_than_a_page_line_is_printed_in_full(report: dict[str, Any]) -> None:
    rep = _with_long_gate(report)
    gate = " ".join(rep["migration_waves"][-1]["gate"].split())
    assert f"Gate before the next wave: {gate}" in pdf_text(rep)


def test_cost_section_prints_the_stored_figures(report: dict[str, Any]) -> None:
    text = pdf_text(report)
    tco = report["tco_analysis"]
    current, savings = _cost_baseline_text(tco)
    assert f"Projected monthly (target): {_fmt_usd(tco['projected_monthly_cost'])}" in text
    assert f"Current monthly (source): {current}" in text
    assert f"Savings: {savings}" in text
    for row in tco["cost_breakdown"]:
        assert _fmt_usd(row["monthly_cost_usd"]) in text


def test_changing_the_stored_total_changes_the_printed_total(report: dict[str, Any]) -> None:
    """The renderer formats the facts; it does not add the rows up itself."""
    rep = json.loads(json.dumps(report))
    rep["tco_analysis"]["projected_monthly_cost"] = 1234.5
    assert "$1,234.50" in pdf_text(rep)


def test_the_cost_section_never_prints_a_retired_engine_saving(report: dict[str, Any]) -> None:
    """``tco_analysis.eliminated_engine_costs`` is not read (#949)."""
    rep = json.loads(json.dumps(report))
    rep["tco_analysis"]["eliminated_engine_costs"] = [
        {"database": "documentdb", "monthly_cost_usd": 4242.42}
    ]
    text = pdf_text(rep)
    assert "4242.42" not in text
    assert "Already removed from this comparison" not in text


def test_the_summary_keeps_its_cost_sentence(report: dict[str, Any]) -> None:
    sentence = re.search(r"Estimated monthly cost: \$[\d,.]+\.", report["summary_deterministic"])
    assert sentence, "fixture has no cost sentence"
    assert sentence.group(0) in pdf_text(report)


def test_every_page_after_the_cover_has_a_header_and_page_number(report: dict[str, Any]) -> None:
    pages = PdfReader(io.BytesIO(pdf_report.render_executive_summary_pdf(report))).pages
    for n, page in enumerate(pages[1:], start=2):
        text = " ".join((page.extract_text() or "").split())
        assert f"Page {n} of {len(pages)}" in text
        assert pdf_report.PRODUCT_NAME in text


def test_a_long_table_flows_over_pages_with_its_header_repeated() -> None:
    rows = ["| Went to | Could run on | Close calls |", "|---|---|---|"]
    rows += [f"| DynamoDB | Aurora MySQL | row-{i:03d} |" for i in range(160)]
    pdf_report.register_fonts()
    from reportlab.platypus import SimpleDocTemplate

    st = pdf_report._md_styles(pdf_report._styles())
    flow = pdf_markdown.to_flowables(rows, st, width=500, glyphs=None)
    buf = io.BytesIO()
    SimpleDocTemplate(buf, invariant=1).build(flow)
    pages = PdfReader(io.BytesIO(buf.getvalue())).pages
    assert len(pages) >= 3
    texts = [" ".join((p.extract_text() or "").split()) for p in pages]
    assert all("Went to" in t for t in texts), "header row not repeated"
    joined = " ".join(texts)
    assert "row-000" in joined and "row-159" in joined


class TestMarkdownSubset:
    def _inline(self, text: str) -> str:
        pdf_report.register_fonts()
        return pdf_markdown.inline(text, pdf_report._md_styles(pdf_report._styles()), None)

    def test_escapes_from_md_text_read_back_as_the_original(self) -> None:
        assert self._inline(r"a \(b\) \_c\_ \!") == "a (b) _c_ !"

    def test_entities_are_unescaped_then_reescaped_for_markup(self) -> None:
        assert self._inline("x &lt;&gt; y") == "x &lt;&gt; y"

    def test_bold_and_code(self) -> None:
        out = self._inline("**RISK-001** in `db.t1`")
        assert "<b>RISK-001</b>" in out and "db.t1" in out

    def test_backslashes_md_text_added_around_a_code_span_are_dropped(self) -> None:
        out = self._inline(r"`wp\_api\_keys`")
        assert "wp_api_keys" in out and "\\" not in out

    def test_arrow_the_font_lacks_reads_to(self) -> None:
        assert pdf_markdown.printable("A → B", None) == "A to B"

    def test_a_character_with_no_glyph_is_replaced_not_dropped(self) -> None:
        glyphs = {ord(c) for c in "ab "}
        assert pdf_markdown.printable("a中b", glyphs) == "a?b"


TEXT_SOURCE_SUFFIXES = {
    ".md",
    ".py",
    ".js",
    ".jsx",
    ".ts",
    ".json",
    ".toml",
    ".yml",
    ".yaml",
    ".sh",
    ".txt",
    ".css",
}


def test_nothing_in_the_repo_lists_a_pptx_deliverable() -> None:
    """The deck is gone: no renderer, doc, command, script or UI source names it."""
    roots = ["docs", ".claude", "scripts", "ci", "src", "README.md", "AGENTS.md", "mkdocs.yml"]
    skip_parts = {"node_modules", "build", "__pycache__", "decisions", ".venv"}
    offenders = []
    for root in roots:
        base = REPO / root
        files = [base] if base.is_file() else [p for p in base.rglob("*") if p.is_file()]
        for p in files:
            # Text sources this repo authors. Rendered HTML pages are left out: a downstream copy of
            # the repo may keep presentation pages that mention a deck, and those list no deliverables.
            if skip_parts & set(p.parts) or p.suffix not in TEXT_SOURCE_SUFFIXES:
                continue
            try:
                text = p.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            if re.search(r"pptx", text, re.IGNORECASE):
                offenders.append(str(p.relative_to(REPO)))
    assert offenders == []
    assert not [p for p in (REPO / "src").rglob("*pptx*") if p.suffix != ".pyc"]


@pytest.mark.parametrize("name", sorted(p.name for p in FIXTURES.glob("*report*.json")))
def test_every_saved_report_fixture_renders(name: str) -> None:
    """Older saved reports (no facts block, no waves, no schema design) still load."""
    text = pdf_text(_fixture(name))
    for heading in (pdf_report.H_SUMMARY, pdf_report.H_COST, pdf_report.H_ENGINES):
        assert heading in text


def test_an_empty_report_renders_every_always_present_section() -> None:
    text = pdf_text({"database_name": "x"})
    for heading in (
        pdf_report.H_SUMMARY,
        pdf_report.H_COST,
        pdf_report.H_ENGINES,
        pdf_report.H_WAVES,
        pdf_report.H_RISKS,
    ):
        assert heading in text
    assert pdf_report.H_CLOSE not in text and pdf_report.H_APPENDIX not in text


def test_a_report_with_no_risks_prints_the_no_open_risks_caveat() -> None:
    rep = {"database_name": "x", "risk_assessment": {"overall_risk_level": "LOW", "risks": []}}
    assert " ".join(NO_OPEN_RISKS_CAVEAT.split()) in pdf_text(rep)


def test_the_copyright_line_is_the_license_files_line() -> None:
    license_lines = (REPO / "LICENSE").read_text(encoding="utf-8").splitlines()
    assert pdf_report.COPYRIGHT in license_lines
    assert pdf_report.FOOTER == f"{pdf_report.COPYRIGHT} For informational purposes only."


def test_the_footer_has_no_year_and_the_summary_points_at_the_folder(
    report: dict[str, Any],
) -> None:
    text = pdf_text(report)
    assert pdf_report.FOOTER in text
    assert "©" not in text
    assert " ".join(pdf_report.SEE_ALSO_NOTE.split()) in text


def test_a_model_summary_is_printed_as_written_and_the_cost_section_still_has_the_figures(
    report: dict[str, Any],
) -> None:
    rep = json.loads(json.dumps(report))
    rep["ranking"][0]["schema_design_available"] = True
    rep["summary"] = "The model wrote this paragraph about the migration."
    rep["summary_source"] = "llm"
    text = pdf_text(rep)
    assert "The model wrote this paragraph about the migration." in text
    assert "Estimated monthly cost" not in text
    assert (
        f"Projected monthly (target): {_fmt_usd(rep['tco_analysis']['projected_monthly_cost'])}"
        in text
    )


def test_the_cost_section_shows_only_retired_engine_savings(report: dict[str, Any]) -> None:
    """A saving is printed from ``retired_engine_savings`` (an engine the source runs
    today), never from ``eliminated_engine_costs`` (a dropped candidate, #949)."""
    rep = json.loads(json.dumps(report))
    rep["tco_analysis"]["retired_engine_savings"] = [
        {"database": "elasticache", "monthly_cost_usd": 271.8}
    ]
    rep["tco_analysis"]["eliminated_engine_costs"] = [
        {"database": "documentdb", "monthly_cost_usd": 4242.42}
    ]
    text = pdf_text(rep)
    assert "Retired by this plan: $271.80/mo (ElastiCache)" in text
    assert "4242.42" not in text
