"""Parity anchors are labelled, never come from a write pattern, and the trace says so
(#936; catalog PR C, #920).

A class score (``class_score``, read by the per-query access-pattern classes, #728) is
either the engine's own documented access path, citing its public documentation, or a
*parity anchor*: a number borrowed from another engine's (or another pattern's) entry
because the catalog has no engine-specific evidence yet. #936 accepts the anchors for
the switch, labelled, until each is replaced by documentation plus a measurement:

- every borrowed class score names the entry it borrows (``parity_anchor``) and its
  ``why`` says ``parity anchor: no engine-specific evidence yet``;
- no anchor's source is a write pattern (``write_pattern``), and no class lists one;
- an anchor borrows evidence, never another anchor;
- the shadow decision trace says, per query and engine, when the class score it used is
  a parity anchor;
- an engine that is not a system of record (OpenSearch, ElastiCache) is never credited
  for a write, whatever pattern the class lists (ADR-031 Decision 2 step 3).
"""

from __future__ import annotations

import re

import pytest

from src.shared.catalog.access_classes import (
    PARITY_ANCHOR_NOTE,
    access_classes,
    class_engine_scores,
    class_parity_anchors,
)
from src.shared.catalog.engines import engine_ids
from src.shared.catalog.patterns import EngineSupport, patterns

_CITED = re.compile(r"\(([a-z0-9-]+), (\d+)\)")
_DOCUMENTATION = (
    "documentation",
    "Reference Manual",
    "Developer Guide",
)


def _class_score_entries():
    return [
        (p, engine, support)
        for p in patterns()
        for engine, support in p.engines.items()
        if support.class_score is not None
    ]


@pytest.mark.parametrize(
    "pattern_id,engine,support",
    _class_score_entries(),
    ids=[f"{p.id}/{e}" for p, e, _ in _class_score_entries()],
)
def test_every_class_score_is_evidence_or_a_labelled_parity_anchor(pattern_id, engine, support):
    if support.parity_anchor is not None:
        assert PARITY_ANCHOR_NOTE in support.why
        cited = _CITED.findall(support.why)
        assert cited == [(support.parity_anchor, str(support.class_score))], support.why
        return
    # Not an anchor: it cites the engine's own public documentation and borrows no number.
    assert "parity anchor" not in support.why
    assert not _CITED.search(support.why), (pattern_id.id, engine, support.why)
    assert any(source in support.why for source in _DOCUMENTATION), (
        pattern_id.id,
        engine,
        support.why,
    )


def test_the_known_anchors_are_labelled():
    anchors = {
        (p.id, engine): support.parity_anchor
        for p, engine, support in _class_score_entries()
        if support.parity_anchor
    }
    assert anchors == {
        # #983 review: re-anchored on Aurora's own key lookup (80), was standard-crud 60.
        ("key-value-lookup", "documentdb"): "key-value-lookup",
        ("pagination", "documentdb"): "pagination",
        ("secondary-attribute-filter", "documentdb"): "secondary-attribute-filter",
        ("secondary-attribute-filter", "opensearch"): "secondary-attribute-filter",
    }


def test_an_anchor_borrows_a_read_patterns_evidence():
    by_id = {p.id: p for p in patterns()}
    for p, engine, support in _class_score_entries():
        if not support.parity_anchor:
            continue
        source = by_id[support.parity_anchor]
        assert not source.write_pattern, (p.id, engine, source.id)
        rated = {
            e.score if e.kind == "fit" else e.class_score
            for other, e in source.engines.items()
            if other != engine and not e.parity_anchor
        }
        assert support.class_score in rated, (p.id, engine, source.id, rated)


def test_no_class_lists_a_write_pattern():
    by_id = {p.id: p for p in patterns()}
    for access_class in access_classes():
        writes = [p for p in access_class.patterns if by_id[p].write_pattern]
        assert not writes, (access_class.id, writes)


def test_the_write_patterns_are_marked():
    marked = {p.id for p in patterns() if p.write_pattern}
    assert {"multi-row-mutation", "multi-table-update", "upsert-conflict"} <= marked
    assert not marked & {"secondary-attribute-filter", "pagination", "standard-crud"}


class TestTheLoaderRejects:
    def test_an_anchor_with_no_class_score(self):
        with pytest.raises(ValueError, match="parity_anchor"):
            EngineSupport(
                kind="unscored",
                supported=True,
                why=f"Class score (#728): {PARITY_ANCHOR_NOTE} (pagination, 85).",
                parity_anchor="pagination",
            )

    def test_an_anchor_whose_why_does_not_say_so(self):
        with pytest.raises(ValueError, match="parity anchor"):
            EngineSupport(
                kind="unscored",
                supported=True,
                why="Class score (#728): 85 (pagination, 85).",
                class_score=85,
                parity_anchor="pagination",
            )

    def test_an_anchor_on_a_write_pattern(self, tmp_path):
        from src.shared.catalog.patterns import CatalogError, load_pattern_catalog

        catalog = tmp_path / "patterns"
        catalog.mkdir()
        (catalog / "multi-row-mutation.toml").write_text(_pattern_file("multi-row-mutation"))
        borrowed = _pattern_file("secondary-attribute-filter").replace(
            'parity_anchor = "secondary-attribute-filter"', 'parity_anchor = "multi-row-mutation"'
        )
        borrowed = borrowed.replace("(secondary-attribute-filter, 75)", "(multi-row-mutation, 75)")
        (catalog / "secondary-attribute-filter.toml").write_text(borrowed)
        with pytest.raises(CatalogError, match="write pattern"):
            load_pattern_catalog(catalog)


def _pattern_file(pattern_id: str) -> str:
    from src.shared.catalog.patterns import PATTERNS_DIR

    return (PATTERNS_DIR / f"{pattern_id}.toml").read_text()


class TestTheTraceSaysSo:
    def test_the_anchored_engines_of_each_class(self):
        anchored = {c.id: sorted(class_parity_anchors(c.id)) for c in access_classes()}
        assert anchored["key_lookup"] == ["documentdb"]
        assert anchored["key_range"] == ["documentdb"]
        assert anchored["ad_hoc_filter"] == ["documentdb", "opensearch"]
        assert anchored["text_search"] == []
        for notes in (class_parity_anchors(c.id) for c in access_classes()):
            assert all(note.startswith(PARITY_ANCHOR_NOTE) for note in notes.values())

    def test_an_engine_with_its_own_evidence_is_not_anchored(self):
        assert "aurora_postgresql" not in class_parity_anchors("ad_hoc_filter")
        assert "dynamodb" not in class_parity_anchors("key_lookup")

    def test_the_shadow_row_names_the_anchor(self):
        from src.agents.referee.access_pattern_shadow import build_shadow, weights_from_analysis
        from tests.unit.agents.referee.test_access_pattern_shadow import (
            AURORA_WEIGHTS,
            DDB_WEIGHTS,
            _collection,
            _engine_analysis,
        )

        collector = _collection()
        collector["queries"]["query_patterns"].append(
            {
                "query_id": "adhoc",
                "query_text": "SELECT * FROM orders WHERE status = $1",
                "query_type": "SELECT",
                "tables_accessed": ["shop.orders"],
                "calls_per_second": 5.0,
            }
        )
        triage = {
            "selected_agents": [{"agent_type": "aurora_postgresql"}, {"agent_type": "dynamodb"}],
            "query_capabilities": {},
            "signals": [],
        }
        analysis = {
            "aurora_postgresql": _engine_analysis(80, AURORA_WEIGHTS),
            "dynamodb": _engine_analysis(80, DDB_WEIGHTS),
        }
        shadow = build_shadow(triage, analysis, collector, weights_from_analysis(analysis))
        rows = {r["query_id"]: r for r in shadow["queries"]}
        assert rows["adhoc"]["class"] == "ad_hoc_filter"
        notes = rows["adhoc"]["parity_anchors"]
        assert sorted(notes) == ["documentdb", "opensearch"]
        assert notes["documentdb"] == (f"{PARITY_ANCHOR_NOTE} (secondary-attribute-filter, 75)")
        # A key lookup: DocumentDB's 60 is borrowed; DynamoDB's and Aurora's are evidence.
        assert sorted(rows["q1"]["parity_anchors"]) == ["documentdb"]
        # The aggregation class has no anchor.
        assert rows["q3"]["parity_anchors"] == {}


class TestASystemOfRecordOnlyIsCreditedForAWrite:
    @pytest.mark.parametrize("access_class", [c.id for c in access_classes()])
    def test_no_read_model_or_cache_scores_a_write(self, access_class):
        scores = class_engine_scores(frozenset({"write"}))[access_class]
        for engine in engine_ids(system_of_record=False):
            assert scores.get(engine, 0) == 0, (access_class, engine)

    def test_opensearch_still_scores_the_read(self):
        assert class_engine_scores()["ad_hoc_filter"]["opensearch"] > 0

    def test_a_system_of_record_keeps_its_write_score(self):
        assert (
            class_engine_scores(frozenset({"write"}))["ad_hoc_filter"]["aurora_postgresql"]
            == class_engine_scores()["ad_hoc_filter"]["aurora_postgresql"]
        )
