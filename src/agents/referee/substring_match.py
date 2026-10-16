"""The leading-wildcard LIKE as an unindexed substring match (#493, option C).

One engine-neutral fact, read from the facts layer
(:func:`src.tools.sql.query_facts.wildcard_likes`): a query's ``LIKE`` predicates
whose literal pattern starts with ``%``. The catalog
(``src/catalog/patterns/substring-match.toml``) says what each engine does with
one: Aurora PostgreSQL indexes it with ``pg_trgm`` (an extension to create),
OpenSearch serves it natively, and Aurora MySQL and DocumentDB run it only as a
scan of every row. Lacking a substring index is the capability gap ADR-031
Decision 2 step 5 lets a penalty follow, so a ``scan`` engine loses the
pattern's penalty, but only when the scan matters: the query reaches both
``scan_threshold`` values (a frequent query on a large table). A word search
(``'%word%'`` on a text column) is never penalised; each engine's redesign for
it (FULLTEXT, ``$text``, ``pg_trgm``) is stated as guidance instead. A
parameterised pattern (``LIKE $1``) is not a leading wildcard here (#912).

Every reader takes its numbers and sentences from here: the scorer
(:func:`scan_adjustments`, merged into the anti-pattern map by
``ScoringContext.from_pipeline``), the decision trace (the notes it returns,
which the resolver appends to the assignment reason of the engine a query lands
on) and synthesis (:func:`substring_match_risks`, one risk per engine, which
every deliverable renders through the facts layer). Engine names come from
``display_engine``; the catalog's ``why`` and ``guidance`` are verb phrases
written without one.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from src.shared.catalog.engines import CatalogError
from src.shared.catalog.patterns import EngineSupport, PatternEntry, ScanThreshold, pattern
from src.shared.engine_names import display_engine
from src.tools.sql.query_facts import query_facts, source_engine_of

__all__ = [
    "SubstringMatch",
    "engine_reason",
    "scan_adjustments",
    "scan_penalty",
    "substring_match_risks",
    "substring_matches",
]

# Column types a word search runs on; a column the collector did not describe counts as
# text, since a LIKE compares strings.
_TEXT_TYPE_WORDS = ("char", "text", "clob", "string")


@dataclass(frozen=True)
class SubstringMatch:
    """One query with a leading-wildcard ``LIKE``, and whether its scan matters.

    ``word_search``: every such predicate in it is a word search on a text column.
    ``table_rows``: the largest row count among its tables, None when none is known.
    ``scan_matters``: not a word search, and both ``scan_threshold`` values reached.
    """

    query_id: str
    word_search: bool
    calls_per_second: float
    table_rows: int | None
    scan_matters: bool


def _pattern() -> PatternEntry:
    return pattern("substring-match")


def _threshold() -> ScanThreshold:
    threshold = _pattern().scan_threshold
    if threshold is None:  # the loader requires one with scan entries
        raise CatalogError("substring-match.toml: scan_threshold is missing")
    return threshold


def _entries() -> dict[str, EngineSupport]:
    return dict(_pattern().engines)


def _tables_by_name(collector: Mapping) -> dict[str, Mapping]:
    tables = ((collector.get("database_schema") or {}).get("tables")) or []
    out: dict[str, Mapping] = {}
    for table in tables:
        if not isinstance(table, Mapping):
            continue
        for key in (table.get("table_id"), table.get("table_name")):
            if key:
                out.setdefault(str(key).lower(), table)
                out.setdefault(str(key).rsplit(".", 1)[-1].lower(), table)
    return out


def _query_tables(query: Mapping, tables: Mapping[str, Mapping]) -> list[Mapping]:
    found = []
    for name in query.get("tables_accessed") or []:
        key = str(name).lower()
        table = tables.get(key) or tables.get(key.rsplit(".", 1)[-1])
        if table is not None and table not in found:
            found.append(table)
    return found


def _is_text_column(column: str | None, tables: list[Mapping]) -> bool:
    if column is None:
        return True
    for table in tables:
        for col in table.get("columns") or []:
            if str(col.get("column_name") or "").lower() == column:
                kind = str(col.get("data_type") or "").lower()
                return not kind or any(word in kind for word in _TEXT_TYPE_WORDS)
    return True


def _rows(tables: list[Mapping]) -> int | None:
    counts = [
        int(t["row_count"])
        for t in tables
        if isinstance(t.get("row_count"), int | float) and not isinstance(t.get("row_count"), bool)
    ]
    return max(counts) if counts else None


def substring_matches(collector: Mapping | None) -> dict[str, SubstringMatch]:
    """``query_id -> SubstringMatch`` for every query with a leading-wildcard ``LIKE``."""
    collector = collector or {}
    threshold = _threshold()
    tables = _tables_by_name(collector)
    source_engine = source_engine_of(collector)
    out: dict[str, SubstringMatch] = {}
    for query in (collector.get("queries") or {}).get("query_patterns") or []:
        qid = str(query.get("query_id") or "")
        likes = query_facts(str(query.get("query_text") or ""), source_engine).wildcard_likes
        if not qid or not likes:
            continue
        query_tables = _query_tables(query, tables)
        word = all(
            like.word_search and _is_text_column(like.column, query_tables) for like in likes
        )
        cps = float(query.get("calls_per_second") or 0.0)
        rows = _rows(query_tables)
        matters = (
            not word
            and cps >= threshold.min_calls_per_second
            and rows is not None
            and rows >= threshold.min_table_rows
        )
        out[qid] = SubstringMatch(qid, word, cps, rows, matters)
    return out


def scan_penalty(match: SubstringMatch, engine: str) -> int:
    """The points ``engine`` loses on ``match``: the pattern's penalty for a scan engine
    whose scan matters, else 0."""
    support = _entries().get(engine)
    if support is None or support.kind != "scan" or not match.scan_matters:
        return 0
    return _pattern().penalty


def _threshold_text() -> str:
    t = _threshold()
    return f"{t.min_calls_per_second:g} call(s) per second on {t.min_table_rows:,} rows"


def _load_text(match: SubstringMatch) -> str:
    rows = f"{match.table_rows:,} rows" if match.table_rows is not None else "an unmeasured table"
    return f"{match.calls_per_second:g} call(s) per second on {rows}"


def engine_reason(engine: str, match: SubstringMatch) -> str | None:
    """Why ``engine`` serves ``match`` as it does, as one or two sentences; None for an
    engine the pattern says nothing useful about (DynamoDB is ruled out before scoring)."""
    support = _entries().get(engine)
    if support is None or support.kind not in ("native", "scan"):
        return None
    text = f"{display_engine(engine)} {support.why}"
    if support.kind != "scan":
        return text
    if match.word_search:
        return f"{text} This is a word search, so it is not penalised; redesign: {support.guidance}"
    penalty = scan_penalty(match, engine)
    if penalty:
        return (
            f"{text} It loses {penalty} points on this query: {_load_text(match)} reaches the "
            f"scan threshold of {_threshold_text()}."
        )
    return (
        f"{text} Not penalised: {_load_text(match)} is below the scan threshold of "
        f"{_threshold_text()}; the scan stays cheap only while the table stays small or the "
        "query rare."
    )


def scan_adjustments(
    collector: Mapping | None, engines: Iterable[str] | None = None
) -> tuple[dict[tuple[str, str], int], dict[tuple[str, str], str]]:
    """``(query_id, engine) -> penalty`` and ``-> note`` for every leading-wildcard query.

    A penalty only for a scan engine whose scan matters. A note for every engine the
    pattern describes (``engines``, when given, limits them to the candidates): its own
    reason, plus the reason each other candidate scores lower when one does.
    """
    entries = _entries()
    candidates = [e for e in (engines if engines is not None else entries) if e in entries]
    penalties: dict[tuple[str, str], int] = {}
    notes: dict[tuple[str, str], str] = {}
    for qid, match in substring_matches(collector).items():
        reasons = {e: r for e in candidates if (r := engine_reason(e, match))}
        lower = {e: p for e in candidates if (p := scan_penalty(match, e))}
        for engine, reason in reasons.items():
            if engine in lower:
                penalties[(qid, engine)] = lower[engine]
            others = [
                f"{display_engine(o)} scores {p} lower here: {display_engine(o)} "
                f"{entries[o].why}"
                for o, p in sorted(lower.items())
                if o != engine
            ]
            notes[(qid, engine)] = " ".join([reason, *others])
    return penalties, notes


def _sentence(text: str) -> str:
    return text[:1].upper() + text[1:]


def _excerpt(text: str, limit: int = 80) -> str:
    one_line = " ".join(text.replace("`", "").split())
    return one_line if len(one_line) <= limit else one_line[: limit - 1].rstrip() + "…"


def substring_match_risks(
    collector: Mapping | None,
    query_engine: Mapping[str, str],
    candidates: Iterable[str],
) -> list[dict]:
    """One risk per final engine that serves leading-wildcard queries and has something
    to say about them (no risk for an engine that serves them natively with nothing to
    do, such as OpenSearch). ``query_engine``: each in-scope query's final engine.
    ``candidates``: the analysed engines, whose lower scores the risk also states.

    MEDIUM when a scan engine keeps a query whose scan matters, LOW otherwise. The
    description carries the engine's reason (``engine_reason``) and, for the queries
    another candidate scored lower, that engine's reason too.
    """
    collector = collector or {}
    entries = _entries()
    matches = substring_matches(collector)
    texts = {
        str(q.get("query_id")): str(q.get("query_text") or "")
        for q in (collector.get("queries") or {}).get("query_patterns") or []
        if q.get("query_id")
    }
    tables = {
        str(q.get("query_id")): sorted({str(t) for t in q.get("tables_accessed") or []})
        for q in (collector.get("queries") or {}).get("query_patterns") or []
        if q.get("query_id")
    }
    others = [c for c in candidates if entries.get(c) and entries[c].kind == "scan"]
    by_engine: dict[str, list[SubstringMatch]] = {}
    for qid, match in matches.items():
        engine = query_engine.get(qid)
        if engine:
            by_engine.setdefault(engine, []).append(match)
    risks: list[dict] = []
    for engine in sorted(by_engine):
        support = entries.get(engine)
        if support is None or support.kind not in ("native", "scan"):
            continue
        members = sorted(by_engine[engine], key=lambda m: m.query_id)
        words = [m for m in members if m.word_search]
        scans = [m for m in members if scan_penalty(m, engine)]
        if support.kind == "native" and not support.guidance:
            continue
        name = display_engine(engine)
        n = len(members)
        listed = "; ".join(_excerpt(texts.get(m.query_id, m.query_id)) for m in members[:3])
        if n > 3:
            listed += f"; and {n - 3} more"
        parts = [
            f"[{engine}] {n} {'query uses' if n == 1 else 'queries use'} a leading-wildcard "
            f"LIKE on {name}: {listed}. {name} {support.why}"
        ]
        if scans:
            parts.append(f"{len(scans)} of them reach the scan threshold of {_threshold_text()}.")
        if words:
            parts.append(f"{len(words)} of them are word searches.")
        for other in sorted(o for o in others if o != engine):
            lower = [m for m in members if scan_penalty(m, other)]
            if lower:
                parts.append(
                    f"{display_engine(other)} scored {scan_penalty(lower[0], other)} lower on "
                    f"{len(lower)} of them: {display_engine(other)} {entries[other].why}"
                )
        mitigation = []
        if support.kind == "native":
            mitigation.append(_sentence(support.guidance))
        elif words:
            mitigation.append(f"For the word searches, {support.guidance}")
        if support.kind == "scan" and len(words) < n:
            mitigation.append(
                "Keep the scan only while the table stays small and the query rare; "
                "otherwise serve the substring match from an engine with a substring index."
            )
        risks.append(
            {
                "risk_type": "PERFORMANCE_DEGRADATION",
                "severity": "MEDIUM" if scans else "LOW",
                "description": " ".join(parts),
                "affected_tables": sorted({t for m in members for t in tables.get(m.query_id, [])}),
                "mitigation": " ".join(mitigation),
                "query_ids": [m.query_id for m in members],
            }
        )
    return risks
