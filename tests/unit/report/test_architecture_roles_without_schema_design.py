"""Engine roles and waves from the REAL recommended_architecture (#281 follow-up).

``build_architecture_recommendation`` lists an engine with workload but no
schema design in ``recommended_architecture.databases`` only when NO engine has
a schema design (``llm_mode=none``). In a mixed run that engine is the retained
one: it must stay out of ``databases`` so ``report_facts.engine_role`` calls it
"Retained" and the old deck renderer put it in the no-migration Wave 1,
rather than "Migration target" in a migration wave.
"""

from __future__ import annotations

from src.agents.referee.synthesis_data import EngineArtifacts, SynthesisData
from src.agents.referee.synthesis_handler import _build_schema_summaries
from src.agents.referee.synthesis_report import build_architecture_recommendation
from src.report import architecture


def _rank(target: str, conf: int, aq: int, wp: float, designed: bool) -> dict:
    return {
        "target": target,
        "confidence_score": conf,
        "assigned_queries": aq,
        "workload_percent": wp,
        "schema_design_available": designed,
        "target_tables": 3 if designed else 0,
        "tables_analyzed": 50,
        "patterns_detected": 0,
        "monthly_cost_usd": 0,
    }


def _report(
    ranking: list[dict],
    engines: dict[str, dict | None],
    mappings: list[dict],
    collector: dict | None = None,
) -> dict:
    data = SynthesisData(
        job_id="job-1",
        database_name="wordpress",
        engines={e: EngineArtifacts(e, schema_design=s) for e, s in engines.items()},
        collector=collector or {},
    )
    return {
        "database_name": "wordpress",
        "job_id": "job-1",
        "timestamp": "2026-10-04T00:00:00Z",
        "ranking": ranking,
        "recommended_architecture": build_architecture_recommendation(data, ranking, mappings),
        "schema_designs": _build_schema_summaries(data),
    }


def _roles(report: dict) -> dict[str, str]:
    return {e["engine"]: e["role"] for e in architecture._architecture_engines(report)}


def test_unversioned_run_with_a_design_does_not_claim_schema_design_was_skipped() -> None:
    """#370 recheck: an unversioned run (no assignment) never adds
    assigned_queries to a ranking entry. designed_and_not_designed_engines()
    used to gate every entry on workload regardless, so an engine with a
    real design (schema_design_available=True) was wrongly treated as
    undesigned here too, and the rationale appended the false "Schema
    design was not run, so no tables are allocated yet." even though a
    design exists.
    """
    ranking = [
        {
            "target": "dynamodb",
            "confidence_score": 90,
            "schema_design_available": True,
            "target_tables": 12,
            "tables_analyzed": 50,
            "patterns_detected": 0,
            "monthly_cost_usd": 0,
        },
    ]
    mappings = [{"source_table": "wp.t1", "recommended_database": "dynamodb"}]
    data = SynthesisData(job_id="job-1", database_name="wordpress")

    arch = build_architecture_recommendation(data, ranking, mappings)

    assert "Schema design was not run" not in arch["rationale"]


def test_no_schema_design_never_retains_a_heterogeneous_source_engine() -> None:
    """#287 round-1 review (C1): SQL Server, Oracle and DB2 have no Aurora dialect
    of their own (``SOURCE_ENGINE_TO_AURORA`` has no entry for them), so the
    Aurora engine the heterogeneous resolver (#381) picks is always a real
    migration target, never retained -- even with no schema design at all.
    """
    ranking = [
        _rank("aurora_postgresql", 70, 60, 55.0, False),
        _rank("dynamodb", 60, 40, 45.0, False),
    ]
    report = _report(
        ranking,
        {e["target"]: None for e in ranking},
        [],
        collector={"metadata": {"source_database": {"engine": "sqlserver"}}},
    )

    services = [d["service"] for d in report["recommended_architecture"]["databases"]]
    assert "aurora_postgresql" in services
    assert _roles(report) == {
        "aurora_postgresql": "Migration target",
        "dynamodb": "Migration target",
    }
    assert "retained" not in report["recommended_architecture"]["rationale"].lower()


def test_rationale_names_the_retained_engine_when_it_is_the_only_engine() -> None:
    """#287 round-1 review (I1): an Aurora-only run (SINGLE_DATABASE) with no
    schema design has an empty ``databases`` list -- the retained engine is the
    whole architecture, not "insufficient data". #287 round 2 (I1, S1): the
    retained engine's own workload share is quoted, and (item 3) the "schema
    design was not run" note applies here too -- nothing migrates, but schema
    design genuinely was not run.
    """
    ranking = [_rank("aurora_mysql", 70, 60, 100.0, False)]
    report = _report(
        ranking,
        {"aurora_mysql": None},
        [],
        collector={"metadata": {"source_database": {"engine": "mysql"}}},
    )

    arch = report["recommended_architecture"]
    assert arch["databases"] == []
    assert arch["rationale"] == (
        "Aurora MySQL retained as the relational core (100% of queries), with no "
        "data migration. Schema design was not run, so no tables are allocated yet."
    )
    assert _roles(report) == {"aurora_mysql": "Retained"}


def test_rationale_names_the_retained_engine_in_the_aurora_plus_cache_shape() -> None:
    """#287 round-1 review (I1): the discourse-like shape -- a PostgreSQL source
    kept on Aurora PostgreSQL, fronted by a cache -- has only the cache in
    ``databases`` (HYBRID_WITH_CACHE); the retained Aurora side must still be
    named, not silently dropped from the rationale. #287 round 2 (I1): the
    retained engine's own workload share is quoted alongside it.
    """
    ranking = [
        _rank("aurora_postgresql", 70, 55, 85.0, False),
        _rank("elasticache", 50, 20, 15.0, False),
    ]
    report = _report(
        ranking,
        {e["target"]: None for e in ranking},
        [],
        collector={"metadata": {"source_database": {"engine": "postgresql"}}},
    )

    arch = report["recommended_architecture"]
    assert [d["service"] for d in arch["databases"]] == ["elasticache"]
    assert arch["rationale"] == (
        "Hybrid architecture: Aurora PostgreSQL retained as the relational core "
        "(85% of queries) and ElastiCache for caching hot data. Schema design "
        "was not run, so no tables are allocated yet."
    )
    assert _roles(report) == {
        "aurora_postgresql": "Retained",
        "elasticache": "Cache layer",
    }


def test_rationale_names_the_other_engine_by_function_when_retained_leads() -> None:
    """#287 round-2 review (I1/I2): once the retained Aurora engine carries more
    of the workload than the other non-cache engine, calling that other engine
    "primary data storage" directly contradicts "retained as the relational
    core" in the same sentence. Named by function instead, joined with ';' per
    the reviewer's example.
    """
    ranking = [
        _rank("aurora_postgresql", 70, 70, 77.0, False),
        _rank("dynamodb", 60, 15, 15.0, False),
        _rank("elasticache", 50, 8, 8.0, False),
    ]
    report = _report(
        ranking,
        {e["target"]: None for e in ranking},
        [],
        collector={"metadata": {"source_database": {"engine": "postgresql"}}},
    )

    arch = report["recommended_architecture"]
    assert arch["rationale"] == (
        "Hybrid architecture: Aurora PostgreSQL retained as the relational core "
        "(77% of queries); DynamoDB for key-value access; ElastiCache for caching "
        "hot data. Schema design was not run, so no tables are allocated yet."
    )
    assert _roles(report) == {
        "aurora_postgresql": "Retained",
        "dynamodb": "Migration target",
        "elasticache": "Cache layer",
    }


def test_rationale_keeps_primary_data_storage_when_retained_engine_is_the_minority() -> None:
    """#287 round-2 review (I1): the reviewer's fix is conditional on the retained
    engine actually carrying most of the workload -- when the other engine
    carries more, calling it "primary data storage" is still accurate, so the
    wording (and the 'and'-joined shape) is unchanged.
    """
    ranking = [
        _rank("aurora_mysql", 70, 10, 10.0, False),
        _rank("dynamodb", 60, 70, 70.0, False),
        _rank("elasticache", 50, 20, 20.0, False),
    ]
    report = _report(
        ranking,
        {e["target"]: None for e in ranking},
        [],
        collector={"metadata": {"source_database": {"engine": "mysql"}}},
    )

    arch = report["recommended_architecture"]
    assert arch["rationale"] == (
        "Hybrid architecture: Aurora MySQL retained as the relational core "
        "(10% of queries) and DynamoDB for primary data storage and ElastiCache "
        "for caching hot data. Schema design was not run, so no tables are "
        "allocated yet."
    )
    assert _roles(report) == {
        "aurora_mysql": "Retained",
        "dynamodb": "Migration target",
        "elasticache": "Cache layer",
    }
