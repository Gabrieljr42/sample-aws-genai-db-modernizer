"""A leading-wildcard LIKE is an unindexed substring match (#493, option C).

Maintainer decision (2026-10-16): penalise a literal leading-wildcard LIKE as a scan only
when the scan matters (a frequent query on a large table); a word search
(``'%word%'`` on a text column) gets redesign guidance instead; every engine's reason is
stated in the decision trace and the deliverables; a parameterised ``LIKE $1`` stays
unpenalised (#912). Aurora MySQL and DocumentDB lack a substring index (they scan),
Aurora PostgreSQL has one (``pg_trgm``, an extension to create), OpenSearch serves it
natively: ADR-031 Decision 2 step 5 holds, a penalty only lands on an engine that lacks
the capability.
"""

from __future__ import annotations

import pytest

from src.agents.referee.assignment_resolver import AssignmentResolver
from src.agents.referee.query_scoring import ScoringContext, merge_substring_scan
from src.agents.referee.substring_match import (
    engine_reason,
    scan_adjustments,
    substring_match_risks,
    substring_matches,
)
from src.shared.catalog.patterns import capability_engines, pattern, scan_engines
from src.shared.engine_names import ENGINE_DISPLAY_NAMES, display_engine
from src.tools.sql.query_facts import query_facts

SCAN = ("aurora_mysql", "documentdb")
NATIVE = ("aurora_postgresql", "opensearch")
DESCRIBED = (*NATIVE, *SCAN)
SUFFIX = "SELECT id FROM users WHERE email LIKE '%@example.com'"
WORD = "SELECT id FROM posts WHERE title LIKE '%term%'"


def _threshold():
    t = pattern("substring-match").scan_threshold
    assert t is not None
    return t


def _collector(
    text: str = SUFFIX,
    *,
    cps: float | None = None,
    rows: int | None = None,
    column_type: str = "varchar(255)",
    source: str = "mysql",
) -> dict:
    t = _threshold()
    cps = t.min_calls_per_second if cps is None else cps
    rows = t.min_table_rows if rows is None else rows
    return {
        "job_id": "t",
        "metadata": {"source_database": {"engine": source}},
        "database_schema": {
            "tables": [
                {
                    "table_id": "app.users",
                    "table_name": "users",
                    "row_count": rows,
                    "columns": [
                        {"column_name": "id", "data_type": "bigint"},
                        {"column_name": "email", "data_type": column_type},
                        {"column_name": "title", "data_type": column_type},
                    ],
                }
            ]
        },
        "queries": {
            "query_patterns": [
                {
                    "query_id": "q1",
                    "query_text": text,
                    "query_type": "SELECT",
                    "tables_accessed": ["app.users"],
                    "calls_per_second": cps,
                    "has_joins": False,
                    "join_count": 0,
                    "has_aggregations": False,
                    "filter_tables": [],
                }
            ]
        },
    }


def _penalties(collector: dict) -> dict[str, int]:
    penalties, _ = scan_adjustments(collector)
    return {engine: penalties.get(("q1", engine), 0) for engine in DESCRIBED}


class TestTheCatalogModelsItAsItsOwnPattern:
    def test_mysql_and_documentdb_scan_postgresql_and_opensearch_index_it(self) -> None:
        assert scan_engines("substring-match") == frozenset(SCAN)
        assert capability_engines("substring_index") == frozenset(NATIVE)

    def test_the_thresholds_are_defined_once_with_a_sourced_why(self) -> None:
        t = _threshold()
        assert t.min_calls_per_second > 0 and t.min_table_rows > 0
        assert "MySQL Reference Manual" in t.why

    def test_each_reason_names_no_engine_display_names_are_generated(self) -> None:
        for support in pattern("substring-match").engines.values():
            for text in (support.why, support.guidance):
                for name in ENGINE_DISPLAY_NAMES.values():
                    assert name not in text, (name, text)

    def test_the_four_reasons(self) -> None:
        match = substring_matches(_collector(WORD))["q1"]
        reasons = {e: engine_reason(e, match) or "" for e in DESCRIBED}
        assert "pg_trgm" in reasons["aurora_postgresql"]
        assert "extension to create" in reasons["aurora_postgresql"]
        assert "FULLTEXT" in reasons["aurora_mysql"] and "word-based" in reasons["aurora_mysql"]
        assert "$regex" in reasons["documentdb"] and "$text" in reasons["documentdb"]
        assert "natively" in reasons["opensearch"]
        for engine, reason in reasons.items():
            assert reason.startswith(display_engine(engine))
        assert engine_reason("dynamodb", match) is None


class TestTheFacts:
    @pytest.mark.parametrize(
        ("text", "word"),
        [
            (WORD, True),
            (SUFFIX, False),
            ("SELECT 1 FROM t WHERE title LIKE CONCAT('%', ?, '%')", True),
            ("SELECT 1 FROM t WHERE [Body] LIKE N'%x%'", True),
            ("SELECT 1 FROM t WHERE [Name] LIKE '%' + @Term + '%'", True),
            ("SELECT 1 FROM t WHERE title LIKE '%' || $1", False),
            ("SELECT 1 FROM t WHERE title NOT ILIKE '%a_b%'", False),
        ],
    )
    def test_a_leading_wildcard_and_its_shape(self, text: str, word: bool) -> None:
        assert query_facts(text).leading_wildcard_like
        likes = query_facts(text).wildcard_likes
        assert likes and likes[0].word_search is word

    @pytest.mark.parametrize(
        "text",
        [
            "SELECT 1 FROM t WHERE title LIKE $1",
            "SELECT 1 FROM t WHERE title LIKE ?",
            "SELECT 1 FROM t WHERE title LIKE 'abc%'",
            "SELECT 1 FROM t WHERE title LIKE CONCAT(?, '%')",
        ],
    )
    def test_a_parameterised_or_prefix_pattern_is_not_one(self, text: str) -> None:
        assert not query_facts(text).leading_wildcard_like
        assert query_facts(text).wildcard_likes == ()


class TestThePenaltyOnlyWhenTheScanMatters:
    def test_a_frequent_substring_match_on_a_large_table_costs_the_scan_engines(self) -> None:
        penalty = pattern("substring-match").penalty
        assert _penalties(_collector()) == {
            "aurora_postgresql": 0,
            "opensearch": 0,
            "aurora_mysql": penalty,
            "documentdb": penalty,
        }

    def test_a_rare_query_costs_nothing(self) -> None:
        rare = _threshold().min_calls_per_second / 2
        assert set(_penalties(_collector(cps=rare)).values()) == {0}

    def test_a_small_table_costs_nothing(self) -> None:
        small = _threshold().min_table_rows - 1
        assert set(_penalties(_collector(rows=small)).values()) == {0}

    def test_a_word_search_costs_nothing_and_gets_the_redesign(self) -> None:
        collector = _collector(WORD)
        assert set(_penalties(collector).values()) == {0}
        _, notes = scan_adjustments(collector)
        assert "MATCH ... AGAINST" in notes[("q1", "aurora_mysql")]
        assert "$text" in notes[("q1", "documentdb")]

    def test_a_word_pattern_on_a_non_text_column_is_a_substring_match(self) -> None:
        collector = _collector("SELECT id FROM users WHERE email LIKE '%42%'", column_type="int")
        assert _penalties(collector)["aurora_mysql"] == pattern("substring-match").penalty

    def test_a_parameterised_pattern_stays_unpenalised(self) -> None:
        """#912: the value of LIKE $1 is unknown."""
        collector = _collector("SELECT id FROM users WHERE email LIKE $1")
        assert scan_adjustments(collector) == ({}, {})

    def test_the_note_states_the_reason_for_the_lower_score(self) -> None:
        _, notes = scan_adjustments(_collector())
        mysql = notes[("q1", "aurora_mysql")]
        assert f"loses {pattern("substring-match").penalty} points" in mysql
        pg = notes[("q1", "aurora_postgresql")]
        assert "pg_trgm" in pg and "Aurora MySQL scores" in pg and "DocumentDB scores" in pg

    def test_below_the_threshold_the_scan_is_stated_as_a_risk(self) -> None:
        _, notes = scan_adjustments(_collector(cps=0.01))
        assert "Not penalised" in notes[("q1", "aurora_mysql")]


class TestTheScorerMergesIt:
    def test_from_pipeline_carries_the_scan_penalty_and_its_note(self) -> None:
        analysis: dict[str, dict] = {e: {"workload_analysis": {}} for e in DESCRIBED}
        ctx = ScoringContext.from_pipeline(analysis, {"query_capabilities": {}}, _collector())
        assert ctx.anti_pattern_map[("q1", "aurora_mysql")] == pattern("substring-match").penalty
        assert "FULLTEXT" in ctx.anti_pattern_notes[("q1", "aurora_mysql")]
        assert ("q1", "aurora_postgresql") not in ctx.anti_pattern_map

    def test_a_bigger_penalty_already_on_the_pair_keeps_its_note(self) -> None:
        merged, notes = merge_substring_scan(
            {("q1", "aurora_mysql"): 35}, {("q1", "aurora_mysql"): "other"}, _collector(), SCAN
        )
        assert merged[("q1", "aurora_mysql")] == 35
        assert notes[("q1", "aurora_mysql")] == "other"


def _analysis(score: int, patterns: list[dict] | None = None) -> dict:
    return {
        "table_recommendations": [{"table_id": "app.users", "confidence_score": score}],
        "workload_analysis": {"patterns_detected": patterns or [], "anti_patterns_detected": []},
    }


# OpenSearch scores only the queries its own analysis matched (PATTERN_ONLY_ENGINES).
_OS_MATCH = [{"pattern_type": "wildcard-search", "query_ids": ["q1"], "table_ids": ["app.users"]}]


class TestAMySQLSource:
    """The case with the most routing risk (review of #911): on a MySQL source the
    source-compatible engine scans a frequent substring match on a large table."""

    def _resolve(self, collector: dict):
        analysis = {"aurora_mysql": _analysis(80), "opensearch": _analysis(70, _OS_MATCH)}
        triage = {
            "selected_agents": [{"agent_type": e} for e in analysis],
            "signals": [],
            "query_capabilities": {"q1": ["text_pattern_match"]},
        }
        return AssignmentResolver().resolve(triage, analysis, collector)

    @pytest.mark.usefixtures("table_confidence_scores")  # #728: scores per table
    def test_aurora_mysql_loses_the_scan_penalty_and_the_reason_is_stated(self) -> None:
        assignment = self._resolve(_collector())
        qa = assignment.query_assignments[0]
        # 80 - 20 = 60 on Aurora MySQL, 70 on OpenSearch: the query moves on the score,
        # and the decision trace says why.
        assert qa.assigned_engine == "opensearch"
        assert "natively" in qa.assignment_reason
        assert "Aurora MySQL scores 20 lower here: Aurora MySQL scans" in qa.assignment_reason

    def test_below_the_threshold_it_stays_on_aurora_mysql_with_the_scan_as_a_risk(self) -> None:
        qa = self._resolve(_collector(cps=0.01)).query_assignments[0]
        assert qa.assigned_engine == "aurora_mysql"
        assert "FULLTEXT" in qa.assignment_reason and "Not penalised" in qa.assignment_reason

    def test_a_word_search_stays_on_aurora_mysql_with_the_redesign(self) -> None:
        qa = self._resolve(_collector(WORD)).query_assignments[0]
        assert qa.assigned_engine == "aurora_mysql"
        assert "MATCH ... AGAINST" in qa.assignment_reason


class TestTheDeliverablesStateItAsARisk:
    def test_a_scan_engine_that_keeps_a_frequent_scan_is_a_medium_risk(self) -> None:
        risks = substring_match_risks(_collector(), {"q1": "aurora_mysql"}, DESCRIBED)
        assert len(risks) == 1
        risk = risks[0]
        assert risk["severity"] == "MEDIUM" and risk["query_ids"] == ["q1"]
        assert risk["description"].startswith("[aurora_mysql] ")
        assert "FULLTEXT" in risk["description"] and "DocumentDB scored 20 lower" in (
            risk["description"]
        )
        assert risk["mitigation"]

    def test_aurora_postgresql_states_the_extension_to_create(self) -> None:
        risk = substring_match_risks(_collector(WORD), {"q1": "aurora_postgresql"}, DESCRIBED)[0]
        assert risk["severity"] == "LOW"
        assert "pg_trgm" in risk["description"] and "CREATE EXTENSION pg_trgm" in risk["mitigation"]

    def test_opensearch_serves_it_natively_with_nothing_to_do(self) -> None:
        assert substring_match_risks(_collector(), {"q1": "opensearch"}, DESCRIBED) == []


class TestEveryStringPassesTheCustomerWordingGuard:
    """Every reason and risk this pattern writes is customer-facing text, held to the
    same guard the e2e wording tests run, with the bare-shape check used for reason fields
    (a product name such as pg_trgm must never read as a reason code)."""

    @pytest.mark.parametrize(
        "collector",
        [_collector(), _collector(cps=0.01), _collector(WORD)],
        ids=["scan", "rare", "word"],
    )
    def test_reasons_notes_and_risks(self, collector: dict) -> None:
        from src.tools.validation.customer_text_guard import scan_json, scan_text

        _, notes = scan_adjustments(collector)
        texts = list(notes.values())
        for engine in DESCRIBED:
            risks = substring_match_risks(collector, {"q1": engine}, DESCRIBED)
            # The leading [engine] tag is the risk's structured engine (scan_json skips it).
            texts += [r["description"].split("] ", 1)[1] for r in risks]
            texts += [r["mitigation"] for r in risks]
            for risk in risks:
                assert scan_json({"risks": [risk]}) == [], risk
        assert texts
        for text in texts:
            assert scan_text(text, strict_reason_codes=False) == [], text
