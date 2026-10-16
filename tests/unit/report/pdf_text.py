"""Shared helper: the extracted text of the rendered PDF report."""

from __future__ import annotations

import io
from typing import Any

from pypdf import PdfReader

from src.report import pdf_report


def pdf_text(report: dict[str, Any], **kwargs: Any) -> str:
    """Render ``report`` to the PDF and return its text with whitespace collapsed."""
    data = pdf_report.render_executive_summary_pdf(report, None, **kwargs)
    reader = PdfReader(io.BytesIO(data))
    return " ".join(" ".join((p.extract_text() or "").split()) for p in reader.pages)
