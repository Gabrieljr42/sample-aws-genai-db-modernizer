"""The engine half of the rule catalog (ADR-031 Decision 8, #492).

One ``src/catalog/engines/<id>.toml`` per engine carries the engine-wide facts every
consumer must agree on: its display name, its colours, whether it is a real routing
target today (``status``), its architectural role and whether it can be a system of
record. This module loads those files into strict Pydantic models (an unknown, missing
or mistyped field fails at import time, so a broken catalog never reaches a run) and
derives the engine sets the rest of the code reads.

This first slice changes no behaviour: ``src.shared.engine_names``,
``src.shared.cache_engines``, ``src.shared.migration_wave_engines``,
``src.shared.report_facts`` and the PDF build their existing
constants, under their existing names, from here. The UI reads the same facts through
``src/ui/src/utils/engineCatalog.generated.js``, written by
``scripts/render_catalog.py`` (``--check`` runs in lint).

Fields:

- ``id``: the engine id the pipeline emits; must equal the file name.
- ``display_name``: the AWS-branded name every deliverable shows.
- ``display_order``: position in the display-name table (unique).
- ``ui_picker_order``: position in the assignment gate's engine picker (unique). Required,
  so a new engine always appears in the picker.
- ``status``: ``active`` (a routing target), ``future`` (a named placeholder that is
  never a routing candidate, ADR-031 Decision 9) or ``legacy`` (an id only older
  reports carry, kept so they still render; routing never emits it).
- ``role``: what the engine is for in the target architecture.
- ``system_of_record``: whether the engine can durably own a table. A cache fronts hot
  reads and a search read model indexes data synced from an owner; neither owns.
- ``colors.ui``: the live UI's hex colour (Sankey nodes, assignment gate).
- ``colors.ui_badge``: the live UI's badge colour name.
- ``colors.deck``: the executive report's accent hex (optional: an engine without one
  reads in the report's grey, #1020).
- ``pricing`` (optional table, #494): the engine's cost inputs. ``base_monthly`` is the
  fixed monthly operating cost the reality check weighs (minimum cluster plus ops
  burden); ``extra_engine_burden_monthly`` is the non-monetary burden of running this
  engine beyond the target engine count; ``max_burden_per_unique_query`` is the cut-off
  above which that burden per uniquely-served query makes the engine removable;
  ``acu_price_per_hour``, ``storage_price_per_gb_month`` and ``instance_hourly`` (instance
  type to hourly price) are the unit prices cost estimation multiplies.
  ``read_unit_price_per_million`` / ``read_unit_bytes`` and
  ``write_unit_price_per_million`` / ``write_unit_bytes`` are an engine's per-request
  prices (USD per million units, and the bytes one unit reads or writes); an engine
  billed by capacity has none. ``reads_billed_on_cache_miss_only``: a read served from
  the engine's buffer cache is not billed. The per-query cost dimension
  (``src.agents.referee.query_cost``, #494) and the cost estimates read them. An engine
  with no ``pricing`` table gets the ``EnginePricing`` defaults.
- ``score_weights`` (optional, #1002): the engine's score-dimension weights (ADR-031
  Decision 5), one ``[score_weights.<profile>]`` table per weight profile, each giving
  ``pattern_match``, ``complexity``, ``performance`` and ``cost`` and summing to 1. The
  first profile is the engine's default; OpenSearch has a second one for time-series
  tables. An engine with no ``score_weights`` uses ``DEFAULT_SCORE_WEIGHTS``. Every
  profile of every engine, and the default, gives cost the same weight (maintainer
  decision, #1002): the cost dimension is per query and engine-aware, so a per-engine
  cost weight would only add a flat bonus. The loader rejects a catalog where they differ.
"""

from __future__ import annotations

import math
import tomllib
from collections.abc import Iterable, Mapping
from functools import cache
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

CATALOG_DIR = Path(__file__).resolve().parents[2] / "catalog" / "engines"

EngineStatus = Literal["active", "future", "legacy"]
EngineRole = Literal[
    "relational", "key_value", "document", "search", "cache", "graph", "wide_column"
]
# The colour names the UI's badge component accepts.
BadgeColor = Literal["blue", "grey", "green", "red"]
_HEX = r"^#[0-9A-Fa-f]{6}$"
# The four score dimensions a weight profile weighs (ADR-031 Decision 5).
SCORE_DIMENSIONS = ("pattern_match", "complexity", "performance", "cost")
# The split an engine with no ``score_weights`` uses (ADR-031 Decision 5's default).
DEFAULT_SCORE_WEIGHTS: Mapping[str, float] = {
    "pattern_match": 0.40,
    "complexity": 0.30,
    "performance": 0.20,
    "cost": 0.10,
}


class EngineColors(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    ui: str = Field(pattern=_HEX)
    ui_badge: BadgeColor
    deck: str | None = Field(default=None, pattern=_HEX)


class EnginePricing(BaseModel):
    """An engine's cost inputs (all USD; see the module docstring)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    # The defaults apply to an engine whose file sets no value (and to an id the catalog
    # does not know): the same numbers the reality check used before the catalog held them.
    base_monthly: float = Field(default=100, ge=0)
    extra_engine_burden_monthly: float = Field(default=300, ge=0)
    max_burden_per_unique_query: float = Field(default=100, ge=0)
    acu_price_per_hour: float | None = Field(default=None, ge=0)
    storage_price_per_gb_month: float | None = Field(default=None, ge=0)
    instance_hourly: dict[str, float] = Field(default_factory=dict)
    read_unit_price_per_million: float | None = Field(default=None, ge=0)
    read_unit_bytes: int | None = Field(default=None, gt=0)
    write_unit_price_per_million: float | None = Field(default=None, ge=0)
    write_unit_bytes: int | None = Field(default=None, gt=0)
    reads_billed_on_cache_miss_only: bool = False

    @model_validator(mode="after")
    def _a_unit_price_has_its_unit_size(self) -> EnginePricing:
        for side in ("read", "write"):
            priced = getattr(self, f"{side}_unit_price_per_million") is not None
            sized = getattr(self, f"{side}_unit_bytes") is not None
            if priced != sized:
                raise ValueError(f"{side}_unit_price_per_million needs {side}_unit_bytes")
        return self


class EngineEntry(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    display_name: str = Field(min_length=1)
    display_order: int = Field(ge=1)
    ui_picker_order: int = Field(ge=1)
    status: EngineStatus
    role: EngineRole
    system_of_record: bool
    colors: EngineColors
    pricing: EnginePricing | None = None
    score_weights: dict[str, dict[str, float]] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _roles_that_never_own(self) -> EngineEntry:
        if self.role in ("cache", "search") and self.system_of_record:
            raise ValueError(f"{self.id}: a {self.role} engine is never a system of record")
        return self

    @model_validator(mode="before")
    @classmethod
    def _score_weights_are_whole_profiles(cls, data: object) -> object:
        if not isinstance(data, Mapping) or "score_weights" not in data:
            return data
        profiles = data["score_weights"]
        if not isinstance(profiles, Mapping) or not profiles:
            raise ValueError("score_weights needs at least one [score_weights.<profile>] table")
        for name, weights in profiles.items():
            if not isinstance(weights, Mapping) or set(weights) != set(SCORE_DIMENSIONS):
                raise ValueError(
                    f"score_weights.{name} must give exactly {', '.join(SCORE_DIMENSIONS)}"
                )
            if any(isinstance(w, bool) or not isinstance(w, int | float) for w in weights.values()):
                raise ValueError(f"score_weights.{name}: every weight is a number")
            if any(not 0 <= w <= 1 for w in weights.values()):
                raise ValueError(f"score_weights.{name}: every weight is between 0 and 1")
            if not math.isclose(sum(weights.values()), 1.0, abs_tol=1e-9):
                raise ValueError(f"score_weights.{name}: the weights must sum to 1")
        return data


class CatalogError(ValueError):
    """The engine catalog on disk is inconsistent (raised at import time)."""


def load_engine_catalog(directory: Path = CATALOG_DIR) -> tuple[EngineEntry, ...]:
    """Every engine file in ``directory``, validated, in ``display_order``.

    A deleted engine file is not detected here: the only lists to check it against are
    the hand-kept ones the catalog replaces. ``tests/unit/shared/test_engine_catalog.py``
    (a release gate) fails instead, naming the contract enums, triage sets and runners
    that still name the engine.
    """
    entries: list[EngineEntry] = []
    for path in sorted(directory.glob("*.toml")):
        try:
            with path.open("rb") as fh:
                data = tomllib.load(fh)
            entry = EngineEntry.model_validate(data)
        except (tomllib.TOMLDecodeError, ValueError) as exc:
            raise CatalogError(f"{path.name}: {exc}") from exc
        if entry.id != path.stem:
            raise CatalogError(f"{path.name}: id {entry.id!r} does not match the file name")
        entries.append(entry)
    if not entries:
        raise CatalogError(f"no engine files under {directory}")
    for field in ("display_order", "ui_picker_order"):
        orders = [getattr(e, field) for e in entries]
        if len(set(orders)) != len(orders):
            raise CatalogError(f"{field} values repeat: {sorted(orders)}")
    _one_cost_weight(entries)
    return tuple(sorted(entries, key=lambda e: e.display_order))


def _one_cost_weight(entries: Iterable[EngineEntry]) -> None:
    """Every engine weights cost the same (ADR-031 Decision 5, #1002), the default too:
    an engine with no ``score_weights`` scores with ``DEFAULT_SCORE_WEIGHTS``."""
    costs = {"<default>": DEFAULT_SCORE_WEIGHTS["cost"]}
    for e in entries:
        for name, weights in e.score_weights.items():
            costs[f"{e.id}.{name}"] = weights["cost"]
    if len(set(costs.values())) > 1:
        raise CatalogError(
            f"the cost weight must be the same for every engine (ADR-031 Decision 5): {costs}"
        )


@cache
def engines() -> tuple[EngineEntry, ...]:
    """The loaded catalog (read once per process)."""
    return load_engine_catalog()


def engine_ids(
    *,
    status: EngineStatus | Iterable[EngineStatus] | None = None,
    role: EngineRole | Iterable[EngineRole] | None = None,
    system_of_record: bool | None = None,
) -> frozenset[str]:
    """Ids of the engines matching every given filter (``None`` means any)."""
    statuses = {status} if isinstance(status, str) else set(status) if status else None
    roles = {role} if isinstance(role, str) else set(role) if role else None
    return frozenset(
        e.id
        for e in engines()
        if (statuses is None or e.status in statuses)
        and (roles is None or e.role in roles)
        and (system_of_record is None or e.system_of_record is system_of_record)
    )


def pricing(engine: str) -> EnginePricing:
    """``engine``'s cost inputs: its ``[pricing]`` table, else the ``EnginePricing`` defaults."""
    for e in engines():
        if e.id == engine and e.pricing is not None:
            return e.pricing
    return EnginePricing()


def default_score_weights() -> dict[str, float]:
    """The score-dimension weights an engine with no ``score_weights`` uses."""
    return dict(DEFAULT_SCORE_WEIGHTS)


def score_weight_profiles(engine: str) -> tuple[dict[str, float], ...]:
    """``engine``'s score-weight profiles in file order (its default first), or the
    default weights alone for an engine with none or an id the catalog does not know."""
    for e in engines():
        if e.id == engine and e.score_weights:
            return tuple(dict(w) for w in e.score_weights.values())
    return (default_score_weights(),)


def score_weights(engine: str, profile: str | None = None) -> dict[str, float]:
    """One of ``engine``'s score-weight profiles: ``profile`` by name, else its default.

    Raises ``KeyError`` for a profile the engine's file does not define.
    """
    if profile is None:
        return score_weight_profiles(engine)[0]
    for e in engines():
        if e.id == engine and profile in e.score_weights:
            return dict(e.score_weights[profile])
    raise KeyError(f"{engine} has no score_weights.{profile}")


# Engines routing can target today: every routing-facing role set is restricted to these.
ACTIVE: tuple[EngineStatus, ...] = ("active",)
# Engines a report may name as a real component: active ones plus legacy ids.
REPORTABLE: tuple[EngineStatus, ...] = ("active", "legacy")
