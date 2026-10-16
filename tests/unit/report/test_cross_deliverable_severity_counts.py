"""The decision report and the deck must agree on severity counts (#270).

#270's round-1 review: the decision report's risk posture line and the
deck's Risk Profile subtitle count the same ``risk_assessment.risks`` list
through two independent code paths (``risks._severity_counts_text`` vs.
the old deck's severity dict). A future change to either one, in
isolation, must not let them disagree -- this test renders one report and
checks both deliverables' counts match, term for term, including an unrated
severity.
"""

from __future__ import annotations

import copy
import json
import re
from pathlib import Path
from typing import Any

from src.report import renderers

FIXTURE = Path(__file__).parent / "fixtures" / "wordpress_report.json"


def _risk(risk_id: str, severity: str) -> dict[str, Any]:
    return {
        "risk_id": risk_id,
        "risk_type": "MIGRATION_COMPLEXITY",
        "severity": severity,
        "description": f"[aurora_mysql] {risk_id} needs a mitigation",
    }


def _report() -> dict[str, Any]:
    data: dict = json.loads(FIXTURE.read_text())
    data = copy.deepcopy(data)
    data["risk_assessment"]["risks"] = [
        _risk("RISK-001", "CRITICAL"),
        _risk("RISK-002", "HIGH"),
        _risk("RISK-003", "MEDIUM"),
        _risk("RISK-004", "MEDIUM"),
        _risk("RISK-005", "LOW"),
        _risk("RISK-006", "SOMETHING_ELSE"),
    ]
    data["risk_assessment"]["overall_risk_level"] = "CRITICAL"
    return data


def _counts_by_label(text: str) -> dict[str, int]:
    """``"1 critical, 2 medium, 1 unrated"`` -> ``{"critical": 1, "medium": 2,
    "unrated": 1}``, case-insensitively, so the deck's uppercase labels and
    the decision report's lowercase ones compare equal."""
    return {label.lower(): int(count) for count, label in re.findall(r"(\d+) (\w+)", text)}


def _decision_report_counts(report: dict[str, Any]) -> dict[str, int]:
    html = renderers.render_decision_report_html(report)
    m = re.search(r"open migration risks? \(([^)]*)\)", html)
    assert m, html
    return _counts_by_label(m.group(1))
