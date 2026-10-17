"""Which queries tie between engines, and which moved after the customer's approval."""

from __future__ import annotations

import re
from typing import Any

from src.shared.engine_names import display_engine
from src.shared.flag_reasons import flag_reason_text
from src.shared.report_facts import (
    CONSOLIDATED_FROM_TO_RE,
    ENGINE_BY_NAME,
    FULLY_ELIMINATED_RE,
    engine_id_from_name,
)

from . import escaping
from .facts import report_facts
from .formatting import plural_noun

# The engine-name maps and the reality-check line patterns live in the facts
# layer (#615), which reads the reality check's text once; re-exported here.
_ENGINE_BY_NAME = ENGINE_BY_NAME
_ENGINE_NAME_ALT = "|".join(re.escape(n) for n in sorted(ENGINE_BY_NAME, key=len, reverse=True))
_FULLY_ELIMINATED_RE = FULLY_ELIMINATED_RE
_CONSOLIDATED_FROM_TO_RE = CONSOLIDATED_FROM_TO_RE
_engine_from_reason = engine_id_from_name


def _tied_queries_md(report: dict[str, Any]) -> list[str]:
    """Engineering Report section for unresolved query ties (#503); empty
    without any.

    ``report["tied_queries"]`` is the structured list (``TiedQuery``):
    every query an exact score tie left unresolved, each with the engines
    it ties between and the deterministic placeholder it currently sits
    under -- the same per-query fact the approval gate shows from
    ``QueryAssignment.tied_engines`` directly, so the two never disagree.
    """
    tied = [t for t in (report.get("tied_queries") or []) if isinstance(t, dict)]
    if not tied:
        return []
    n = int(report_facts(report).get("tied_query_count") or 0)
    out = [
        f"## Unresolved engine ties ({n} {plural_noun(n, 'query', 'queries')})",
        "",
        "No routing rule decides between the engines listed for each of these "
        "queries; each is listed under a deterministic placeholder pending the "
        "customer's own choice.",
        "",
        "| Query | Tied engines | Currently listed under |",
        "|---|---|---|",
    ]
    for t in sorted(tied, key=lambda t: str(t.get("query_id", ""))):
        tied_names = ", ".join(display_engine(e) for e in sorted(t.get("tied_engines") or []))
        out.append(
            f"| `{escaping.md_code(str(t.get('query_id', '?')))}` "
            f"| {escaping.md_cell(tied_names)} "
            f"| {escaping.md_cell(display_engine(t.get('assigned_engine', '')))} |"
        )
    out.append("")
    return out


# How a customer keeps a rerouted query where they approved it (#629). One
# sentence, in the engineering report's section.
REROUTE_OVERRIDE_TEXT = (
    "To keep any of these on the engine you approved, change its engine in the "
    "assignment review; that writes a new assignment version and redesigns only "
    "the engines it touches."
)


def _reroutes(report: dict[str, Any]) -> list[dict[str, Any]]:
    rows = [r for r in (report.get("post_schema_reroutes") or []) if isinstance(r, dict)]
    return sorted(rows, key=lambda r: str(r.get("query_id", "")))


def _fmt_score(value: Any) -> str:
    return "n/a" if not isinstance(value, (int, float)) else f"{value:g}"


def _n_reroutes(report: dict[str, Any]) -> int:
    """How many queries moved after approval: the facts layer's count (#615)."""
    return int(report_facts(report).get("post_schema_reroute_count") or 0)


def reroute_summary(report: dict[str, Any]) -> str | None:
    """One sentence stating how many queries moved after approval, or None."""
    rows = _reroutes(report)
    if not rows:
        return None
    n = _n_reroutes(report)
    approved = rows[0].get("approved_version")
    version = f" (version {approved})" if approved else ""
    facts = report_facts(report)
    sot = int(facts.get("source_of_truth_reroute_count") or 0)
    design = int(facts.get("design_review_reroute_count") or 0) if sot else n
    parts: list[str] = []
    if design:
        parts.append(
            f"schema design review found that {design} "
            f"{plural_noun(design, 'query', 'queries')} cannot run on the approved engine, "
            "so each moved to the best-scoring engine you kept"
        )
    if sot:
        # #992: a saved routing that split a table's writes, re-routed on resume.
        parts.append(
            f"{sot} {plural_noun(sot, 'write')} moved so that each table's writes land "
            "on one engine, its source of truth"
        )
    return f"After you approved the assignment{version}, " + "; and ".join(parts) + "."


def _reroutes_md(report: dict[str, Any]) -> list[str]:
    """Engineering Report section for queries moved after approval (#629)."""
    rows = _reroutes(report)
    if not rows:
        return []
    n = _n_reroutes(report)
    out = [
        f"## Moved after approval ({n} {plural_noun(n, 'query', 'queries')})",
        "",
        reroute_summary(report) or "",
        "",
        "| Query | Approved on | Moved to | Why | Score after / before |",
        "|---|---|---|---|---|",
    ]
    for r in rows:
        out.append(
            f"| `{escaping.md_code(str(r.get('query_id', '?')))}` "
            f"| {escaping.md_cell(display_engine(str(r.get('from_engine', ''))))} "
            f"| {escaping.md_cell(display_engine(str(r.get('to_engine', ''))))} "
            f"| {escaping.md_cell(flag_reason_text(r.get('reason_category')))} "
            f"| {_fmt_score(r.get('score_after'))} / {_fmt_score(r.get('score_before'))} |"
        )
    out += ["", REROUTE_OVERRIDE_TEXT, ""]
    return out
