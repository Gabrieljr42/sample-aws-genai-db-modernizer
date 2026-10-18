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


def _schema_collector(
    text: str,
    *,
    tables: list[tuple[str, int | None, list[tuple[str, str]]]],
    accessed: list[str] | None = None,
    cps: float | None = None,
    source: str = "mysql",
) -> dict:
    """A collection of the given ``(name, rows, [(column, type)])`` tables and one query,
    ``q1``, that reads ``accessed`` (every table by default)."""
    t = _threshold()
    return {
        "job_id": "t",
        "metadata": {"source_database": {"engine": source}},
        "database_schema": {
            "tables": [
                {
                    "table_id": f"app.{name}",
                    "table_name": name,
                    "row_count": rows,
                    "columns": [{"column_name": c, "data_type": ty} for c, ty in columns],
                }
                for name, rows, columns in tables
            ]
        },
        "queries": {
            "query_patterns": [
                {
                    "query_id": "q1",
                    "query_text": text,
                    "query_type": "SELECT",
                    "tables_accessed": [f"app.{n}" for n in (accessed or [x[0] for x in tables])],
                    "calls_per_second": t.min_calls_per_second if cps is None else cps,
                }
            ]
        },
    }


BIG = _threshold().min_table_rows * 10
SMALL = 50
JOIN_TABLES = [
    ("orders", BIG, [("id", "bigint"), ("note", "text"), ("code", "varchar(20)")]),
    ("tags", SMALL, [("id", "bigint"), ("label", "varchar(40)"), ("code", "int")]),
]
JOIN = "SELECT * FROM orders o JOIN tags t ON t.id = o.id WHERE {} LIKE '%x.y'"


class TestTheScannedTableAndColumnAreRead:
    """#942: the row count is the table that holds the LIKE column, and the column's type
    is read from the collected schema, not the largest table or a guess."""

    def _match(self, collector: dict):
        return substring_matches(collector)["q1"]

    def test_a_search_of_a_small_table_joined_to_a_large_one_does_not_scan_the_large_one(
        self,
    ) -> None:
        match = self._match(_schema_collector(JOIN.format("t.label"), tables=JOIN_TABLES))
        assert (match.table_rows, match.scan_matters, match.approximations) == (SMALL, False, ())
        penalties, _ = scan_adjustments(
            _schema_collector(JOIN.format("t.label"), tables=JOIN_TABLES)
        )
        assert penalties == {}

    def test_a_search_of_the_large_table_is_a_scan_that_matters(self) -> None:
        match = self._match(_schema_collector(JOIN.format("o.note"), tables=JOIN_TABLES))
        assert (match.table_rows, match.scan_matters, match.approximations) == (BIG, True, ())

    def test_an_unqualified_column_is_found_in_the_one_table_that_holds_it(self) -> None:
        match = self._match(_schema_collector(JOIN.format("label"), tables=JOIN_TABLES))
        assert (match.table_rows, match.scan_matters, match.approximations) == (SMALL, False, ())

    def test_the_alias_names_the_table_not_the_table_name_order(self) -> None:
        text = "SELECT * FROM tags o JOIN orders t ON t.id = o.id WHERE o.label LIKE '%x.y'"
        match = self._match(_schema_collector(text, tables=JOIN_TABLES))
        assert (match.table_rows, match.scan_matters) == (SMALL, False)

    def test_the_type_is_the_type_of_the_table_that_holds_the_column(self) -> None:
        """``code`` is text in orders and an integer in tags: a word pattern on tags.code
        is a substring match, on orders.code a word search."""
        word = "SELECT * FROM orders o JOIN tags t ON t.id = o.id WHERE {}.code LIKE '%42%'"
        tags = self._match(_schema_collector(word.format("t"), tables=JOIN_TABLES))
        orders = self._match(_schema_collector(word.format("o"), tables=JOIN_TABLES))
        assert (tags.word_search, orders.word_search) == (False, True)
        # The older reading took the first query table that has a column of that name.
        assert tags.approximations == () and orders.approximations == ()

    def test_a_function_of_the_column_names_its_table_and_reads_the_type_as_text(self) -> None:
        text = "SELECT * FROM orders o JOIN tags t ON t.id = o.id WHERE lower(t.label) LIKE '%x.y'"
        match = self._match(_schema_collector(text, tables=JOIN_TABLES))
        assert (match.table_rows, match.scan_matters) == (SMALL, False)
        word = text.replace("%x.y", "%word%")
        wrapped = self._match(_schema_collector(word, tables=JOIN_TABLES))
        assert wrapped.word_search is True
        assert any("wraps column label" in n for n in wrapped.approximations)

    def test_a_column_with_no_collected_type_is_read_as_text_and_says_so(self) -> None:
        tables = [("tags", SMALL, [("id", "bigint"), ("label", "")])]
        match = self._match(
            _schema_collector("SELECT 1 FROM tags WHERE label LIKE '%word%'", tables=tables)
        )
        assert match.word_search is True
        assert any("label has no collected type" in n for n in match.approximations)

    def test_a_traced_table_with_no_row_count_says_the_scan_cannot_be_sized(self) -> None:
        tables: list[tuple[str, int | None, list[tuple[str, str]]]] = [
            ("tags", None, [("id", "bigint"), ("label", "varchar(40)")])
        ]
        text = "SELECT 1 FROM tags WHERE label LIKE '%x.y'"
        collector = _schema_collector(text, tables=tables)
        match = self._match(collector)
        assert (match.table_rows, match.scan_matters) == (None, False)
        assert "table tags has no collected row count" in match.approximations[0]
        _, notes = scan_adjustments(collector)
        assert (
            "Read approximately: table tags has no collected row count"
            in notes[("q1", "aurora_mysql")]
        )

    def test_an_ambiguous_column_keeps_the_older_reading_and_says_so(self) -> None:
        match = self._match(_schema_collector(JOIN.format("id"), tables=JOIN_TABLES))
        assert (match.table_rows, match.scan_matters) == (BIG, True)
        (note,) = match.approximations
        assert "column id was not traced to one collected table (ambiguous" in note
        assert "largest table the query reads counts" in note

    def test_a_column_of_a_derived_table_keeps_the_older_reading_and_says_so(self) -> None:
        text = "SELECT * FROM (SELECT note AS n FROM orders) d WHERE d.n LIKE '%x.y'"
        match = self._match(_schema_collector(text, tables=JOIN_TABLES, accessed=["orders"]))
        assert (match.table_rows, match.scan_matters) == (BIG, True)
        assert len(match.approximations) == 1

    def test_a_table_the_collector_did_not_describe_keeps_the_older_reading(self) -> None:
        text = "SELECT * FROM ghosts WHERE name LIKE '%x.y'"
        match = self._match(_schema_collector(text, tables=JOIN_TABLES, accessed=["orders"]))
        assert (match.table_rows, match.approximations != ()) == (BIG, True)

    def test_an_expression_over_two_columns_keeps_the_older_reading(self) -> None:
        text = "SELECT * FROM orders o JOIN tags t ON t.id = o.id WHERE concat(o.note, t.label) LIKE '%x.y'"
        match = self._match(_schema_collector(text, tables=JOIN_TABLES))
        assert (match.table_rows, match.scan_matters) == (BIG, True)
        assert "names no single column" in match.approximations[0]

    def test_a_statement_the_parser_cannot_read_keeps_the_older_reading(self) -> None:
        text = "SELECT * FROM orders WHERE note LIKE '%x.y' AND ((("
        match = self._match(_schema_collector(text, tables=JOIN_TABLES, accessed=["orders"]))
        assert (match.table_rows, match.scan_matters) == (BIG, True)
        assert match.approximations

    def test_each_predicate_is_read_on_its_own_table(self) -> None:
        both = (
            "SELECT * FROM orders o JOIN tags t ON t.id = o.id "
            "WHERE t.label LIKE '%x.y' AND o.note LIKE '%word%'"
        )
        match = self._match(_schema_collector(both, tables=JOIN_TABLES))
        # The suffix match is on the small table; the word search on the large one is a
        # word search: nothing scans a large table.
        assert (match.word_search, match.table_rows, match.scan_matters) == (False, SMALL, False)
        swapped = both.replace("t.label LIKE '%x.y'", "t.label LIKE '%word%'").replace(
            "o.note LIKE '%word%'", "o.note LIKE '%x.y'"
        )
        match = self._match(_schema_collector(swapped, tables=JOIN_TABLES))
        assert (match.table_rows, match.scan_matters) == (BIG, True)

    def test_the_decision_trace_states_the_approximation_and_a_clean_reading_does_not(
        self,
    ) -> None:
        ambiguous = _schema_collector(JOIN.format("id"), tables=JOIN_TABLES)
        _, notes = scan_adjustments(ambiguous)
        assert "Read approximately: column id was not traced" in notes[("q1", "aurora_mysql")]
        assert "Read approximately" in notes[("q1", "aurora_postgresql")]
        _, clean = scan_adjustments(_schema_collector(JOIN.format("o.note"), tables=JOIN_TABLES))
        assert all("Read approximately" not in n for n in clean.values())

    def test_the_trace_text_passes_the_customer_wording_guard(self) -> None:
        from src.tools.validation.customer_text_guard import scan_text

        for text in (JOIN.format("id"), "SELECT * FROM ghosts WHERE name LIKE '%x.y'"):
            _, notes = scan_adjustments(
                _schema_collector(text, tables=JOIN_TABLES, accessed=["orders"])
            )
            for note in notes.values():
                assert scan_text(note, strict_reason_codes=False) == [], note
