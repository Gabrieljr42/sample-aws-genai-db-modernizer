"""The executive summary PDF: sections, cost figures, wave criteria, fonts and branding.

The PDF is a flowing report (reportlab platypus) built from ``report.json``'s
facts layer (ADR-032). These tests extract its text with pypdf and compare it with
``report.json`` on all three samples.
"""

from __future__ import annotations

import json
import re

from pypdf import PdfReader
from pypdf.generic import ArrayObject, IndirectObject

from src.report import pdf_markdown, pdf_report
from src.report.formatting import _cost_baseline_text, _fmt_usd
from src.report.roadmap import resolve_migration_waves
from src.tools.validation.customer_text_guard import scan_text
from tests.branding import hits as found
from tests.e2e.pipeline import PipelineResult

FORBIDDEN = ("undefined", "NaN", "[object Object]", "{{", "None%")


def _reader(run: PipelineResult) -> PdfReader:
    return PdfReader(str(run.deliverable("summary-executive-report.pdf")))


def _normalize(text: str) -> str:
    """Collapse all whitespace runs to single spaces.

    pypdf's ``extract_text()`` emits one token per line because it walks the
    content stream's individual show-text operations, not a laid-out line, so
    phrase matching has to be whitespace-insensitive.
    """
    return " ".join(text.split())


def _text(run: PipelineResult) -> str:
    """The document's text with each page's running header, footer and page number
    removed, so a paragraph or table row that continues on the next page reads as one."""
    furniture = _normalize(f"{pdf_report.PRODUCT_NAME} {run.db} {pdf_report.FOOTER}")
    pages = []
    for page in _reader(run).pages:
        text = _normalize(page.extract_text() or "")
        pages.append(re.sub(r" ?Page \d+ of \d+$", "", text.replace(furniture, " ")))
    return _normalize(" ".join(pages))


def _report(run: PipelineResult) -> dict:
    report: dict = json.loads((run.deliverable(".md").parent / "report.json").read_text())
    return report


def test_has_a_cover_and_every_section_heading(run: PipelineResult) -> None:
    pages = [_normalize(p.extract_text() or "") for p in _reader(run).pages]
    assert len(pages) >= 5
    assert pdf_report.PRODUCT_NAME in pages[0]
    assert run.db in pages[0]
    text = " ".join(pages[1:])
    report = _report(run)
    # Always present, in this order.
    always = (
        pdf_report.H_SUMMARY,
        pdf_report.H_COST,
        pdf_report.H_ENGINES,
        pdf_report.H_WAVES,
        pdf_report.H_RISKS,
    )
    # The document outline lists the sections in order, whatever a model wrote in the
    # summary (a heading word can occur in prose, so the page text cannot order them).
    outline = [o.title for o in _reader(run).outline if not isinstance(o, list)]
    assert outline[: len(always)] == list(always), outline
    for heading in always:
        assert heading in text
    # Present only when the report has the content.
    facts = report.get("facts") or {}
    has_close = bool((facts.get("close_calls") or {}).get("engine_pairs")) or bool(
        (facts.get("unservable") or {}).get("queries")
    )
    if has_close:
        assert pdf_report.H_CLOSE in outline
    if report.get("query_traces"):
        assert pdf_report.H_APPENDIX in outline


def test_page_numbers_and_header_on_every_page_after_the_cover(run: PipelineResult) -> None:
    pages = [_normalize(p.extract_text() or "") for p in _reader(run).pages]
    total = len(pages)
    for n, text in enumerate(pages[1:], start=2):
        assert f"Page {n} of {total}" in text, f"page {n}"
        assert pdf_report.PRODUCT_NAME in text, f"page {n}"


def _cost_section(text: str) -> str:
    """The Cost section's text, from its heading to the next section's."""
    start = text.rindex(pdf_report.H_COST, 0, text.index("Current monthly (source)"))
    return text[start : text.index(pdf_report.H_ENGINES, start)]


def test_every_cost_figure_matches_report_json(run: PipelineResult) -> None:
    """The renderer owns the Cost section: every figure there is report.json's."""
    report = _report(run)
    cost = _cost_section(_text(run))
    tco = report["tco_analysis"]
    facts = report["facts"]
    assert _fmt_usd(facts["projected_monthly_cost_usd"]) == _fmt_usd(tco["projected_monthly_cost"])
    assert f"Projected monthly (target): {_fmt_usd(tco['projected_monthly_cost'])}" in cost
    assert _fmt_usd(facts["engine_cost_total_usd"]) in cost
    for e in facts["engines"]:
        assert _fmt_usd(e["monthly_cost_usd"]) in cost, e["engine"]
    current, savings = _cost_baseline_text(tco)
    assert f"Current monthly (source): {current}" in cost
    assert f"Savings: {savings}" in cost


def test_the_summary_is_printed_as_the_run_wrote_it(run: PipelineResult) -> None:
    """A deterministic summary keeps its cost sentence; a model's summary
    (``summary_source == "llm"``) is the model's own text, printed as written and
    without the deterministic sentence."""
    report = _report(run)
    text = _text(run)
    cost = _fmt_usd(report["tco_analysis"]["projected_monthly_cost"])
    sentence = f"Estimated monthly cost: {cost}"
    model_text_shown = report.get("summary_source") == "llm" and pdf_report._trust(report)
    if model_text_shown:
        glyphs = pdf_report._glyphs()
        assert sentence not in text
        assert _normalize(pdf_markdown.printable(report["summary"], glyphs)) in text
    else:
        assert sentence in text


def test_no_manufactured_retired_engine_saving(run: PipelineResult) -> None:
    text = _text(run)
    assert "Already removed from this comparison" not in text


def test_every_wave_gate_appears_complete(run: PipelineResult) -> None:
    text = _text(run)
    waves = resolve_migration_waves(_report(run))
    assert waves
    glyphs = pdf_report._glyphs()
    for w in waves:
        gate = _normalize(str(w.get("gate") or ""))
        assert gate, f"wave {w.get('wave')} has no gate"
        expected = _normalize(pdf_markdown.printable(gate, glyphs))
        assert f"Gate before the next wave: {expected}" in text, f"wave {w.get('wave')}"


def test_no_truncated_figure_or_criterion(run: PipelineResult) -> None:
    """No ellipsis cuts a cost figure or anything in the wave plan (the gates and
    rationales included). The only ellipses left are in the risk register's quoted
    SQL and in the appendix's SQL snippets."""
    text = _text(run)
    cost = text[text.index("Current monthly (source)") : text.index(pdf_report.H_ENGINES, 400)]
    waves = text[text.index("Migration roadmap (") : text.index("Overall risk:")]
    for name, section in (("cost", cost), ("wave plan", waves)):
        assert "…" not in section and "..." not in section, name


def test_no_internal_wording(run: PipelineResult) -> None:
    """The rendered PDF text passes the same customer-text scan as the other deliverables."""
    text = "\n".join(p.extract_text() or "" for p in _reader(run).pages)
    findings = scan_text(text, "summary-executive-report.pdf")
    assert findings == [], [(f.kind, f.text, f.excerpt) for f in findings]


def test_no_placeholder_text(run: PipelineResult) -> None:
    text = "\n".join(p.extract_text() or "" for p in _reader(run).pages)
    assert [s for s in FORBIDDEN if s in text] == []
    assert run.db in text, f"source database name {run.db!r} missing from report text"


def test_pdf_is_a_multi_page_document_not_a_slide_deck(run: PipelineResult) -> None:
    page = _reader(run).pages[1]
    # Portrait, not 16:9.
    assert float(page.mediabox.height) > float(page.mediabox.width)


def _resolve(obj):
    return obj.get_object() if isinstance(obj, IndirectObject) else obj


def _fonts_shown(content: bytes) -> set[str]:
    """Font resource names (e.g. ``F2+0``) actually used to draw a glyph.

    reportlab's ``Canvas`` always registers the base ``Helvetica`` font as ``/F1``
    in every page's ``/Resources`` and opens each page with a no-op
    ``BT /F1 ... Tf ... ET`` before the real drawing begins — the resource is
    present, but nothing is ever shown with it (verified against this deck's own
    content streams: ``/F1`` is set but never followed by a ``Tj``/``TJ`` while
    it is the active font). Counting unused resources as "used" would flag that
    harmless reportlab artifact as a fallback-font bug on every single page.
    """
    text = content.decode("latin1")
    current: str | None = None
    used: set[str] = set()
    for m in re.finditer(r"/(F\d+(?:\+\d+)?)\s+[\d.]+\s+Tf|(Tj|TJ)", text):
        if m.group(1):
            current = m.group(1)
        elif current:
            used.add(current)
    return used


def _page_content_bytes(page) -> bytes:
    contents = _resolve(page.get("/Contents"))
    if contents is None:
        return b""
    if isinstance(contents, ArrayObject):
        return b"".join(bytes(_resolve(part).get_data()) for part in contents)
    return bytes(contents.get_data())


def test_fonts_are_embedded(run: PipelineResult) -> None:
    missing = []
    checked_pages = 0
    for n, page in enumerate(_reader(run).pages, start=1):
        shown = _fonts_shown(_page_content_bytes(page))
        if not shown:
            continue  # e.g. an image-only page draws no text, so nothing to check
        checked_pages += 1

        fonts = _resolve(_resolve(page.get("/Resources") or {}).get("/Font") or {})
        for key, ref in fonts.items():
            name = str(key).lstrip("/")
            if name not in shown:
                continue  # resource present (reportlab default) but never drawn
            font = _resolve(ref)
            base = str(font.get("/BaseFont"))
            if base.endswith(("Helvetica", "Helvetica-Bold")):
                missing.append(f"p{n}:{base} (fallback font used)")
                continue
            desc = font.get("/FontDescriptor")
            if font.get("/Subtype") == "/Type0":
                desc = _resolve(font["/DescendantFonts"][0]).get("/FontDescriptor")
            desc = _resolve(desc) if desc else None
            if not desc or not any(k in desc for k in ("/FontFile", "/FontFile2", "/FontFile3")):
                missing.append(f"p{n}:{base}")
    # Without this, a page-shown-fonts regression (e.g. every page becoming
    # "image-only") would make `missing` trivially empty and this test
    # vacuously pass without having checked anything.
    assert checked_pages > 0, "no page had any drawn text -- nothing was actually checked"
    assert missing == []


# Branding and internal classification metadata a public sample must not carry
# (issue #181): the only product name is "Database Modernizer Assessment Tool".
CLASSIFICATION = re.compile(r"confidential|pending_classification|msip_label", re.IGNORECASE)


def _branding_problems(text: str) -> list[str]:
    return [*found(text), *CLASSIFICATION.findall(text)]


def test_pdf_metadata_names_the_assessment(run: PipelineResult) -> None:
    meta = _reader(run).metadata
    assert meta is not None
    assert meta.author == pdf_report.PRODUCT_NAME
    assert meta.creator == pdf_report.PRODUCT_NAME
    assert _branding_problems(" ".join(str(v) for v in meta.values())) == []


def test_every_page_after_the_cover_carries_the_license_copyright_once(
    run: PipelineResult,
) -> None:
    pages = list(_reader(run).pages)
    for n, page in enumerate(pages, start=1):
        text = _normalize(page.extract_text() or "")
        assert _branding_problems(text) == [], f"page {n}"
        if n == 1:
            continue
        assert (
            text.count(pdf_report.COPYRIGHT) == 1
        ), f"page {n}: {text.count(pdf_report.COPYRIGHT)}"
        assert f"{pdf_report.COPYRIGHT} For informational purposes only." in text, f"page {n}"


def test_the_summary_points_at_files_in_the_deliverables_folder(run: PipelineResult) -> None:
    """The PDF's "see also" names real files in deliverables/, unclipped."""
    text = _text(run)
    note = _normalize(pdf_report.SEE_ALSO_NOTE)
    assert note in text
    # It closes the executive summary, before the Cost section.
    assert text.index(note) < text.index("Current monthly (source)")
    folder = run.job_dir() / "deliverables"
    for name in ("decision-report.html", "engineering-report.md"):
        assert (folder / name).is_file(), name


def test_there_is_no_pptx_deliverable(run: PipelineResult) -> None:
    paths = run.report.get("paths", [])
    assert any(p.endswith("summary-executive-report.pdf") for p in paths)
    assert not [p for p in paths if p.lower().endswith(".pptx")]
    assert not list(run.job_dir().rglob("*.pptx"))
