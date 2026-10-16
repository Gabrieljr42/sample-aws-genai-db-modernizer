"""Each query's own pattern score (ADR-031 Decision 14, Amendment 5, #728).

A query's pattern dimension on an engine is the score of its own access-pattern
class (:func:`src.agents.referee.access_pattern.classify_query`) on that engine, read
from the pattern catalog (:func:`src.shared.catalog.access_classes.class_engine_scores`),
never the pattern score of its tables. :class:`QueryPatternModel` holds what that needs
for one collection -- the schema's keys, the source engine's dialect and the dimension
weights each engine's analysis used for each table -- and classifies a query the first
time it is scored. :meth:`QueryPatternModel.from_pipeline` is the one way production
code builds one, and :meth:`src.agents.referee.query_scoring.ScoringContext.from_pipeline`
the one place that calls it, so every per-query score in the pipeline reads the same
classes.

:func:`access_patterns_record` records, per query, the class routing used, why, the facts
its score read, its score on each engine and, per engine whose score is a parity anchor
(#936), the anchor note: the assignment writes it next to each version
(``assignment/v<N>/access_patterns.json``); routing reads none of it.

A ``utility`` or ``unparsed`` statement has no class score
(:meth:`QueryPatternModel.has_class_score`); the scorer keeps the pre-switch rule for
it alone (``query_scoring._unclassified_confidence``), as ADR-031 Decision 14
Amendment 5 states.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass, field

from src.agents.referee.access_pattern import (
    SCORED_CLASSES,
    AccessClassification,
    SchemaKeys,
    classify_query,
)
from src.shared.catalog.access_classes import class_engine_scores, class_parity_anchors
from src.tools.analysis.score_weights import table_weights
from src.tools.sql.query_facts import source_engine_of


def weights_from_analysis(
    analysis_outputs: Mapping[str, Mapping],
) -> dict[tuple[str, str], dict[str, float]]:
    """``(engine, table_id) -> weights`` each engine's analysis combined that table's
    dimensions with (:func:`src.tools.analysis.score_weights.table_weights`)."""
    return {
        (engine, str(rec["table_id"])): table_weights(engine, rec)
        for engine, analysis in analysis_outputs.items()
        for rec in (analysis or {}).get("table_recommendations") or []
        if rec.get("table_id")
    }


@dataclass(frozen=True)
class QueryPatternModel:
    """The per-query pattern dimension for one collection (see the module docstring).

    ``keys``: the collected schema's keys (``SchemaKeys.from_collector``); empty keys
    classify every query as if no column were a key. ``source_engine``: the dialect
    the query text is read in. ``weights``: ``(engine, table_id) -> dimension
    weights`` each engine's analysis used for that table, read from ``analysis.json``
    (the referee never reads the decision trace, ADR-017 Decision 3); a missing pair
    uses the default weights.
    """

    keys: SchemaKeys = field(default_factory=SchemaKeys)
    source_engine: str = ""
    weights: Mapping[tuple[str, str], Mapping[str, float]] = field(default_factory=dict)
    # query_id and text -> class, filled the first time a query is scored. The class
    # depends on the query's own text and the schema only, so it never goes stale.
    _classes: dict[tuple[str, str], AccessClassification] = field(
        default_factory=dict, compare=False, repr=False
    )

    @classmethod
    def from_pipeline(
        cls, collector_output: Mapping | None, analysis_outputs: Mapping[str, Mapping]
    ) -> QueryPatternModel:
        """The model for one pipeline run's collector and analysis outputs."""
        collector = collector_output or {}
        return cls(
            keys=SchemaKeys.from_collector(collector),
            source_engine=source_engine_of(collector),
            weights=weights_from_analysis(analysis_outputs),
        )

    def classification(self, query: Mapping) -> AccessClassification:
        """The access-pattern class of ``query`` (computed once per query)."""
        memo_key = (str(query.get("query_id") or ""), str(query.get("query_text") or ""))
        found = self._classes.get(memo_key)
        if found is None:
            found = classify_query(query, self.keys, self.source_engine)
            self._classes[memo_key] = found
        return found

    def has_class_score(self, query: Mapping) -> bool:
        """Whether ``query`` has an access-pattern class with a score (not ``utility``
        or ``unparsed``)."""
        return self.classification(query).name in SCORED_CLASSES

    def pattern_score(self, query: Mapping, engine: str) -> int:
        """``query``'s pattern score on ``engine``: its class's score there (0 for a
        statement with no class score, ``utility`` or ``unparsed``)."""
        result = self.classification(query)
        if result.name not in SCORED_CLASSES:
            return 0
        return int(class_engine_scores(result.facts)[result.name].get(engine, 0))


ACCESS_PATTERNS_FILE = "access_patterns.json"


def write_access_patterns(
    store,
    database_name: str,
    job_id: str,
    version: int,
    collector_output: Mapping | None = None,
) -> str:
    """Write ``assignment/v<version>/access_patterns.json`` next to every assignment
    version a writer creates (#728, #936); returns its key. ``collector_output`` is the
    job's collector output when the writer holds it, else it is read from the store."""
    prefix = f"{database_name}/{job_id}"
    collector = (
        collector_output
        if collector_output is not None
        else store.read_json(f"{prefix}/collector/output.json")
    )
    key = f"{prefix}/assignment/v{version}/{ACCESS_PATTERNS_FILE}"
    store.write_json(key, access_patterns_record(collector))
    return key


def access_patterns_record(collector_output: Mapping) -> dict:
    """Each query's access-pattern class, as routing scored it (see the module docstring).

    Per query: the class and its reason, the query facts its class score read, its
    tables, the class score on each active engine, and ``parity_anchors``: for each
    engine whose class score is a parity anchor (no engine-specific evidence yet, #936),
    the note and the borrowed ``(<pattern>, <score>)``. A ``utility`` or ``unparsed``
    statement has neither scores nor anchors.
    """
    model = QueryPatternModel.from_pipeline(collector_output, {})
    rows = []
    by_class: Counter[str] = Counter()
    for query in (collector_output.get("queries") or {}).get("query_patterns") or []:
        result = model.classification(query)
        by_class[result.name] += 1
        scored = model.has_class_score(query)
        rows.append(
            {
                "query_id": str(query.get("query_id") or ""),
                "class": result.name,
                "reason": result.reason,
                "facts": sorted(result.facts),
                "tables": list(query.get("tables_accessed") or []),
                "class_scores": (
                    dict(class_engine_scores(result.facts)[result.name]) if scored else {}
                ),
                "parity_anchors": (
                    dict(class_parity_anchors(result.name, result.facts)) if scored else {}
                ),
            }
        )
    return {
        "schema_version": 1,
        "about": (
            "Each query's access-pattern class and class scores, as routing scored its "
            "pattern dimension (ADR-031 Decision 14, Amendments 5 and 7, #728). Routing "
            "does not read this file."
        ),
        "by_class": dict(sorted(by_class.items())),
        "queries": rows,
    }
