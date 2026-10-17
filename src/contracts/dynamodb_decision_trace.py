"""DynamoDB decision trace contract (``analysis-dynamodb/decision-trace.json``).

The DynamoDB analysis agent writes this trace beside ``analysis.json`` for
specialist calibration (ADR-017 Decision 3: it is not part of the
agent-to-agent contract, and the referee never reads it). It used to be a plain
dict, so a renamed field or a new denormalization ``subtype`` passed every
guard; this model types it, and its ``contract_version`` is checked on read
like every other stored contract (ADR-008 Decisions 1 and 4).

``trace_version`` is the trace's own calibration version from before this
contract (``"1.1"``); nothing checks it. ``contract_version`` is the version
readers check.

Version History:
- 1.0 (2026-10-17, #973): Initial version, the sections the trace already had
  (``trace_version`` 1.1) plus ``contract_version``. A file without
  ``contract_version`` was written before this contract, and still loads.
  ``timestamp_columns`` (#935) is optional: a trace saved before it has none.
  Every trace model keeps unknown keys (``extra="allow"``); the two LLM advisor
  answer models (``AggregateKeyDesign``, ``DenormStrategy``) keep pydantic's default,
  since they also parse the model's answer.
"""

from __future__ import annotations

from enum import Enum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from src.contracts.analysis_output import MigrationComplexity

# The shapes ``src/agents/referee/transaction_shape.py`` classifies a write into.
TransactionShapeKind = Literal["single item", "bounded", "unbounded"]


class DenormSubtype(str, Enum):
    """Denormalization sub-type classification.

    Each maps to a specific DynamoDB modeling strategy.
    """

    BOUNDED_PARENT_CHILD = "bounded-parent-child"
    MANY_TO_MANY_JUNCTION = "many-to-many-junction"
    CO_ACCESSED_TABLES = "co-accessed-tables"
    ADJACENCY_LIST = "adjacency-list"


def _is_none(value: object) -> bool:
    return value is None


# Every trace model keeps unknown keys, neither refused nor dropped: an older or
# newer trace loads (ADR-008 Decision 5), and a writer's round trip through the
# model loses no key a builder added without declaring it here.
_KEEP_UNKNOWN = ConfigDict(extra="allow")


class TraceSummary(BaseModel):
    """Query and pattern counts for the run."""

    model_config = _KEEP_UNKNOWN

    queries_analyzed: int
    queries_matched: int
    queries_unmatched: int
    patterns_detected: int
    anti_patterns_detected: int


class TransactionShapeTrace(BaseModel):
    """Which transaction shape a write matched, and why (ADR-031 Decision 3)."""

    model_config = _KEEP_UNKNOWN

    shape: TransactionShapeKind
    items: int | None
    reason: str


class QueryMatch(BaseModel):
    """The patterns and anti-patterns one query matched."""

    model_config = _KEEP_UNKNOWN

    query_id: str
    query_text_preview: str
    matched_patterns: list[str]
    matched_anti_patterns: list[str]
    # Written only for a write that has a shape; never written as null.
    transaction_shape: TransactionShapeTrace | None = Field(default=None, exclude_if=_is_none)


class PatternSummary(BaseModel):
    """One detected pattern, with its catalog base score."""

    model_config = _KEEP_UNKNOWN

    pattern_id: str
    pattern_type: str
    catalog_base_score: int | None
    queries_matched_count: int
    tables_involved: list[str]


class DerivationScores(BaseModel):
    """The four score dimensions of one table recommendation."""

    model_config = _KEEP_UNKNOWN

    pattern_match: int
    complexity: int
    performance: int
    cost: int


class RecommendationDerivation(BaseModel):
    """How one table's recommendation score was derived."""

    model_config = _KEEP_UNKNOWN

    table_id: str
    segments_contributing: list[str]
    score_breakdown: DerivationScores
    weights_used: dict[str, float]
    weighted_confidence: int


class TableGroup(BaseModel):
    """An FK-connected cluster of more than one table."""

    model_config = _KEEP_UNKNOWN

    group_root: str
    tables: list[str]


class TimestampNameFallback(BaseModel):
    """A query on which some column was read by name although the collection has types."""

    model_config = _KEEP_UNKNOWN

    query_id: str
    timestamp_column: bool
    read_by_name: list[str]


class TimestampColumnsTrace(BaseModel):
    """How the timestamp-column fact was read (#935): by column type, or by name."""

    model_config = _KEEP_UNKNOWN

    read_by: str
    name_fallbacks: list[TimestampNameFallback]


class PkClassificationTrace(BaseModel):
    """A table's primary key mapped to DynamoDB key candidates (``PkClassification``)."""

    model_config = _KEEP_UNKNOWN

    table_id: str
    pk_columns: list[str]
    partition_key_candidate: str | None
    sort_key_candidate: str | None
    needs_redesign: bool
    no_pk: bool


class AggregateTrace(BaseModel):
    """Related tables that migrate together (``scoring.Aggregate``)."""

    model_config = _KEEP_UNKNOWN

    aggregate_id: str
    root_table: str
    member_tables: list[str]
    co_access_evidence: list[dict[str, Any]]
    co_access_confidence: int
    combined_migration_complexity: MigrationComplexity


class GsiCandidateTrace(BaseModel):
    """A frequently queried column group outside the PK (``GsiCandidate``)."""

    model_config = _KEEP_UNKNOWN

    table_id: str
    partition_key_columns: list[str]
    sort_key_columns: list[str]
    total_frequency_per_hour: float
    query_ids: list[str]
    existing_index_name: str | None
    is_sparse: bool
    estimated_population_rate: float | None


class DenormalizationOpportunityTrace(BaseModel):
    """A classified denormalization opportunity (``DenormalizationOpportunity``)."""

    model_config = _KEEP_UNKNOWN

    subtype: DenormSubtype
    tables: list[str]
    cardinality_ratio: float | None
    co_access_frequency: float | None
    hierarchy_depth: int | None
    is_cyclic: bool | None
    evidence: dict[str, Any]


class SecondaryIndexDominanceTrace(BaseModel):
    """A table whose dominant access uses a secondary index (``SecondaryIndexDominance``)."""

    model_config = _KEEP_UNKNOWN

    table_id: str
    dominant_index_name: str
    dominant_index_columns: list[str]
    frequency_share: float
    alternative_pk_candidate: str | None
    alternative_sk_candidate: str | None


class AggregateKeyDesign(BaseModel):
    """Recommended DynamoDB key design for an aggregate."""

    aggregate_id: str
    partition_key: str
    sort_key: str | None = None
    rationale: str
    supporting_access_patterns: list[str] = Field(
        default_factory=list,
        description="query_ids from the input that justify this key design",
    )


class DenormStrategy(BaseModel):
    """Denormalization strategy recommendation for a detected opportunity."""

    opportunity_id: str  # references DenormalizationOpportunity
    strategy: str  # e.g., "embed child items", "adjacency list with GSI"
    rationale: str
    supporting_access_patterns: list[str] = Field(
        default_factory=list,
        description="query_ids from the input that justify this strategy",
    )


class LlmAdvisorTrace(BaseModel):
    """What the LLM advisor did: ``skipped``, ``success``, ``awaiting_external``, ..."""

    model_config = _KEEP_UNKNOWN

    status: str
    duration_seconds: float
    attempts: int
    aggregate_key_designs: list[AggregateKeyDesign]
    denormalization_strategies: list[DenormStrategy]


class ManyToManyRelationship(BaseModel):
    """A junction table joining two tables."""

    model_config = _KEEP_UNKNOWN

    type: Literal["M:N"]
    junction_table: str
    table_a: str
    table_b: str
    columns_a: list[str]
    columns_b: list[str]
    description: str


class ForeignKeyRelationship(BaseModel):
    """One foreign key, one-to-one or one-to-many."""

    model_config = _KEEP_UNKNOWN

    type: Literal["1:1", "1:M"]
    from_table: str
    to_table: str
    from_columns: list[str]
    to_columns: list[str]
    description: str


Relationship = Annotated[
    ManyToManyRelationship | ForeignKeyRelationship, Field(discriminator="type")
]


class DynamoDBDecisionTrace(BaseModel):
    """``analysis-dynamodb/decision-trace.json``: how the DynamoDB analysis reached its result."""

    model_config = _KEEP_UNKNOWN

    contract_version: str = Field(
        default="1.0", pattern=r"^\d+\.\d+$", description="Contract version (MAJOR.MINOR format)"
    )
    trace_version: str = Field(
        default="1.1", description="The trace's calibration version from before this contract"
    )
    agent: str = "dynamodb-analysis-agent"
    summary: TraceSummary
    query_matches: list[QueryMatch]
    pattern_summaries: list[PatternSummary]
    recommendation_derivations: list[RecommendationDerivation]
    table_groups: list[TableGroup]
    # Written since #935; a trace saved before it has none, so it is never written as null.
    timestamp_columns: TimestampColumnsTrace | None = Field(default=None, exclude_if=_is_none)
    pk_classifications: list[PkClassificationTrace]
    aggregates: list[AggregateTrace]
    gsi_candidates: list[GsiCandidateTrace]
    denormalization_opportunities: list[DenormalizationOpportunityTrace]
    secondary_index_dominance: list[SecondaryIndexDominanceTrace]
    llm_advisor: LlmAdvisorTrace
    relationships: list[Relationship] = Field(default_factory=list)
