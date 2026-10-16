"""Which engines the source runs today, and the only savings a plan may claim (#949).

A saving, or a "retired engine" line, may name only an engine the customer runs
today: the source engine itself (``metadata.source_database.engine``) or one the
collection records in ``metadata.engines_in_use`` (collector contract 3.2). An
engine the plan analysed as a candidate and then dropped (by the assignment, the
reality check or a consolidation) costs the customer nothing today, so dropping it
saves nothing; ``report.json`` keeps it as ``considered_not_recommended``, with no
dollar figure.

Report readers (both directions of the contract change, synthesis 1.25):

- :func:`retired_engine_savings` reads ``tco_analysis.retired_engine_savings``.
  ``tco_analysis.eliminated_engine_costs``, which a report before 1.25 filled with
  every dropped candidate, is never a saving: no collection recorded an engine in
  use before 3.2, so none of its rows was one.
- :func:`considered_not_recommended` reads the field, and for a report before 1.25
  maps those legacy rows to it, without their dollars.

Pure functions over plain dicts: ``src.shared`` imports neither ``src.agents`` nor
``src.report`` (ADR-032 Decision 3).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def engines_in_use(collector_output: Mapping[str, Any] | None) -> frozenset[str]:
    """The source engine and every engine the collection records as in use, lower-cased."""
    metadata = (collector_output or {}).get("metadata") or {}
    source = (metadata.get("source_database") or {}).get("engine")
    listed = metadata.get("engines_in_use") or []
    return frozenset(
        e.strip().lower() for e in [source, *listed] if isinstance(e, str) and e.strip()
    )


def retired_engine_savings(report: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Rows ``{database, monthly_cost_usd}`` for engines the source runs today that the
    plan retires. ``report`` is a ``report.json`` dict (or its ``tco_analysis``)."""
    tco = report.get("tco_analysis", report) or {}
    return [
        dict(row)
        for row in tco.get("retired_engine_savings") or []
        if isinstance(row, Mapping)
        and row.get("database")
        and isinstance(row.get("monthly_cost_usd"), (int, float))
    ]


def considered_not_recommended(report: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Candidate engines the plan analysed and dropped: ``{engine, absorbed_by}`` each."""
    stored = report.get("considered_not_recommended")
    if isinstance(stored, list):
        return [
            {"engine": str(e["engine"]), "absorbed_by": e.get("absorbed_by")}
            for e in stored
            if isinstance(e, Mapping) and e.get("engine")
        ]
    legacy = (report.get("tco_analysis") or {}).get("eliminated_engine_costs") or []
    return [
        {"engine": str(row["database"]), "absorbed_by": None}
        for row in legacy
        if isinstance(row, Mapping) and row.get("database")
    ]
