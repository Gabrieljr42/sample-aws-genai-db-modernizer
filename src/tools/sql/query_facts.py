"""Engine-neutral query facts read from SQL text: one detector per fact (#491).

A detector here reports a fact about one query -- never a score, never an
engine name, never a capability. Engines differ only in how a fact is turned
into support or a score (the capability matrix in
``src.agents.referee.capability_registry``, the exclusion rules in
``src.agents.referee.engine_exclusions``), so the same query reads the same
way whichever engine is being scored against it, and a fact is defined once
instead of once per engine module (#485 traced a routing bug to two copies of
the leading-wildcard check that had drifted apart).

Each fact has one definition, and every reader uses it (#893; the guard is
``tests/unit/tools/sql/test_one_definition_per_fact.py``). Two kinds:

- Parsed facts -- join count, ``GROUP BY``, aggregate functions, the text
  searches (leading-wildcard and parameterised ``LIKE``, full-text,
  regular-expression and similarity matches), the window functions, a
  recursive CTE, ``ORDER BY``, the row limit and top-N shape, a timestamp
  column and JSON access -- are the sqlglot facade's (:class:`src.tools.sql.facade.SqlFacts`),
  read in the source engine's dialect, so a comment, a string literal or a
  column name (``count``, ``rank``) is never read as SQL. When sqlglot can't
  read a statement, each is its regex fallback
  (:mod:`src.tools.sql.regex_facts`). The collectors store some of them on each
  query pattern (``has_joins``, ``join_count``, ``has_aggregations``,
  ``has_subqueries``, #796), the same facts read once.
- Text facts are a regex scan on the text: the ones no parse gives yet (a
  session key, a write statement, a computed join key, ...), each moved here
  from its detector by #491.

A reader passes the collector's source engine
(``metadata.source_database.engine``, :func:`source_engine_of`); a reader with
none gets sqlglot's generic dialect.

:func:`query_facts` returns one object per text, and each fact on it is
computed the first time a reader asks for it, then kept: a query read by
several detectors is scanned once per fact, and a reader never pays for a fact
it doesn't read (#895 review).
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from functools import cached_property, lru_cache

from src.shared.sql_comments import strip_leading_comments
from src.shared.unsupported_pattern import is_dedup_only_group_by
from src.tools.sql.facade import AccessFacts, SqlFacts, query_readings
from src.tools.sql.regex_facts import WildcardLike, scan_sql

__all__ = ["QueryFacts", "WildcardLike", "query_facts", "scan_sql", "source_engine_of"]

# ---------------------------------------------------------------------------
# Parsed facts (#893). Each is the sqlglot facade's reading (``SqlFacts``),
# which falls back to its regex fact only when the statement can't be parsed.
# A parse is skipped when the text can't hold the fact at all: each fact's
# hint below is a necessary condition of both readings (a GROUP BY needs the
# word ``group``, a window function the word ``over``, ...), so it never
# changes an answer, it only saves the parse
# (``tests/unit/tools/sql/test_query_facts.py::TestParsedFactHints``).
# ---------------------------------------------------------------------------

# A function-call shape: a name (or a closing quote or bracket of a quoted
# one) followed by ``(``, where the name is not a keyword that takes a
# parenthesised list (``IN (``, ``EXISTS (``, ``VALUES (``, ...). An aggregate
# function is always called this way.
_FUNCTION_CALL_HINT_RE = re.compile(
    r"(?:[`\"\]]|\b(?!(?:in|exists|values|as|on|using|from|join|where|and|or|not|select|"
    r"with|over|by|into|union)\b)[a-z_][\w$]*)\s*\(",
    re.IGNORECASE,
)
# The regex fallback's aggregate words (``src.tools.sql.regex_facts``).
_AGGREGATE_WORD_HINTS = (
    "count",
    "sum",
    "avg",
    "min",
    "max",
    "group_concat",
    "string_agg",
    "array_agg",
)
_JOIN_HINTS = ("join", ",", "apply")
_FULLTEXT_HINTS = ("match", "@@", "tsvector", "tsquery", "ts_rank", "contains", "freetext")
_PATTERN_MATCH_HINTS = ("regexp", "rlike", "~", "similar")
_ROW_LIMIT_HINTS = ("limit", "fetch", "top")
# The timestamp column names (``regex_facts.TIMESTAMP_COLUMN_NAMES``) each hold one.
_TIMESTAMP_HINTS = ("_at", "time", "date", "ttl")
_JSON_HINTS = ("json", "->", "#>", "@>")

# ---------------------------------------------------------------------------
# Aggregation (#338, #336)
#
# An aggregate function call needs real aggregation capability on one table
# just as much as a joined one: DynamoDB has no server-side SUM/COUNT/AVG
# regardless of how many tables feed it. A query with neither GROUP BY nor a
# join (a bare SELECT COUNT(*) FROM t) used to get no capability requirement
# the same way the join/aggregation gap did (#338) -- an aggregate function
# call alone is enough, so this fires independently of GROUP BY or join count.
# FOUND_ROWS()/SQL_CALC_FOUND_ROWS (MySQL's row-count-of-last-query modifier)
# and any window function count too: DynamoDB has no equivalent for any of
# them.
#
# GROUP BY alone, with no aggregate function, is usually a deduplication
# technique (#336) rather than an aggregation. Rather than re-deriving that
# distinction here, ``dedup_only_group_by`` is
# ``src.shared.unsupported_pattern.is_dedup_only_group_by`` (the same
# definition the risk report uses, #371), so the shared function is the single
# thing that decides it: GROUP BY needs aggregation exactly when that function
# says it is not just a dedup.
# ---------------------------------------------------------------------------

_FOUND_ROWS_RE = re.compile(r"\bsql_calc_found_rows\b|`?\bfound_rows\b`?\s*\(", re.IGNORECASE)

# ---------------------------------------------------------------------------
# Computed-expression join (review of #375, finding B2)
#
# A join whose ON condition compares a function call (lower(groups.name) =
# users.username_lower) needs an engine that evaluates expressions as part of
# a join -- a key-value store cannot do that at any stage, v1 or the reality
# check, no matter how few tables are involved. This is a stricter, narrower
# case than "complex_joins" (3+ tables): a 2-table join is otherwise fine for
# DynamoDB (a key-scoped lookup), but not when the join key itself is
# computed.
# ---------------------------------------------------------------------------

# One JOIN ... ON <condition> clause, bounded at the next clause keyword (or
# the end of the statement) so a function call elsewhere in the query (e.g. a
# WHERE clause unrelated to the join) is never mistaken for part of the join
# condition.
_JOIN_ON_CLAUSE_RE = re.compile(
    r"\bjoin\b.*?\bon\b\s*(?P<cond>.*?)(?=\b(?:where|group\s+by|order\s+by|having|"
    r"left|right|inner|full|cross|join|limit)\b|$)",
    re.IGNORECASE | re.DOTALL,
)
# A column reference -- optionally quoted, optionally table-qualified -- on
# one side of the comparison: "users"."username_lower" or users.username_lower
# or just id. Starting on a letter/underscore (optionally behind a opening
# quote) excludes a bind parameter ("$1", "?"), which starts on "$" or "?":
# a function compared against a parameter ("lower(g.name) = $1") tests a
# computed value against a filter value, not a join on a computed key, so it
# must not match either branch below (a second review of #375 found the
# first version matching this regardless of what followed the operator).
_COLUMN_REF_RE = r'"?[a-z_][a-z0-9_]*"?(?:\."?[a-z_][a-z0-9_]*"?)?'
# A function call: an identifier immediately followed by "(".
_FUNC_CALL_RE = r"[a-z_][a-z0-9_]*\s*\([^()]*\)"
_COMPARISON_OP_RE = r"(?:=|<>|!=|<=|>=|<|>)"
# A function call compared against a column, inside a join condition, on
# either side -- "lower(groups.name) = users.username_lower" (function on
# the left) or "users.username_lower = lower(groups.name)" (function on the
# right, a second review of #375 found this side was never checked). A
# plain "t1.id = t2.id" join condition does not trigger either branch.
_COMPUTED_EXPR_IN_CONDITION_RE = re.compile(
    rf"{_FUNC_CALL_RE}\s*{_COMPARISON_OP_RE}\s*{_COLUMN_REF_RE}\b"
    rf"|{_COLUMN_REF_RE}\s*{_COMPARISON_OP_RE}\s*{_FUNC_CALL_RE}",
    re.IGNORECASE,
)

# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------

_WRITE_WITH_SUBQUERY_RE = re.compile(
    r"\b(INSERT|UPDATE|DELETE)\b.*\bSELECT\b", re.IGNORECASE | re.DOTALL
)


# ---------------------------------------------------------------------------
# Relational shape keywords (the Aurora analysis's detectors, moved here from
# ``aurora_common_pattern_catalog``). Case-insensitive substring checks on the
# raw text: they read comments, literals and column names too (``limit`` in
# ``credit_limit``), which is why they are kept apart from the parsed facts.
# ---------------------------------------------------------------------------

# EXISTS (subquery), NOT EXISTS (subquery), IN (SELECT ...)
_EXISTS_OR_IN_SUBQUERY_RE = re.compile(
    r"""
    \bEXISTS\s*\(               # EXISTS (subquery)
    |
    \bNOT\s+EXISTS\s*\(         # NOT EXISTS (subquery)
    |
    \bIN\s*\(\s*SELECT\b        # IN (SELECT ...)
    """,
    re.IGNORECASE | re.VERBOSE,
)

IN_LIST_KEYWORDS: tuple[str, ...] = (
    " in (",
    " in(",
)

# ---------------------------------------------------------------------------
# MySQL syntax (the Aurora MySQL analysis's detectors, moved here from
# ``aurora_mysql_pattern_catalog``): case-insensitive substring checks.
# ---------------------------------------------------------------------------

DUPLICATE_KEY_KEYWORDS: tuple[str, ...] = ("on duplicate key", "replace into")

# A string aggregate, whatever the dialect calls it: MySQL's GROUP_CONCAT, the
# STRING_AGG of PostgreSQL and SQL Server, Oracle's and DB2's LISTAGG (#920: both
# Aurora engines have one).
_STRING_AGGREGATE_RE = re.compile(r"\b(?:group_concat|string_agg|listagg)\s*\(", re.IGNORECASE)

STORED_ROUTINE_KEYWORDS: tuple[str, ...] = (
    "call ",
    "delimiter",
    "create procedure",
    "create function",
)
# SQL Server runs a stored procedure with EXEC or EXECUTE as the statement (with an
# optional ``@rc =`` for its return code); ``EXEC ('...')`` runs a string instead. Only
# for a SQL Server source: PostgreSQL's and MySQL's EXECUTE runs a prepared statement.
_TSQL_EXEC_RE = re.compile(r"^exec(?:ute)?\s+(?:@\w+\s*=\s*)?[\[\w]", re.IGNORECASE)

# ---------------------------------------------------------------------------
# PostgreSQL syntax (the Aurora PostgreSQL analysis's detectors, moved here
# from ``aurora_pg_pattern_catalog``).
# ---------------------------------------------------------------------------

# Array operations:
#   - ANY(ARRAY[...])
#   - array_agg(
#   - unnest(
#   - ARRAY[...]
#   - @> ARRAY[...] (array containment, NOT json)
_ARRAY_OPERATION_RE = re.compile(
    r"""
    \bANY\s*\(\s*ARRAY\b
    |
    \barray_agg\s*\(
    |
    \bunnest\s*\(
    |
    \bARRAY\s*\[
    |
    @>\s*ARRAY\b
    """,
    re.IGNORECASE | re.VERBOSE,
)

# LATERAL joins: requires JOIN LATERAL or comma LATERAL or LATERAL( with word boundary
_LATERAL_JOIN_RE = re.compile(
    r"""
    \bJOIN\s+LATERAL\s*\(
    |
    ,\s*LATERAL\s*\(
    |
    \bLATERAL\s*\(
    """,
    re.IGNORECASE | re.VERBOSE,
)

# Upsert: ON CONFLICT keyword OR RETURNING used as a SQL clause.
# RETURNING as a SQL clause follows DML (not preceded by AS keyword).
# Matches: RETURNING id, RETURNING *, RETURNING col1, col2
# Does NOT match: AS returning FROM (alias usage)
_ON_CONFLICT_OR_RETURNING_RE = re.compile(
    r"""
    \bON\s+CONFLICT\b
    |
    (?<!AS\s)(?<!\bAS\b)\bRETURNING\s+[\w*]
    """,
    re.IGNORECASE | re.VERBOSE,
)


# ---------------------------------------------------------------------------
# Document shapes (the DocumentDB analysis's detectors, moved here from
# ``documentdb_analysis_tools``).
# ---------------------------------------------------------------------------

# BETWEEN, or < / <= / > / >= with a word character before them (``a>=1``,
# not ``a >= 1``), read on the lower-cased text (DynamoDB's and triage's range
# reading; one definition of a range predicate is #893's last group).
_RANGE_OPERATOR_WORD_RE = re.compile(r"\b(between|>=?|<=?)\b")
_NEGATION_WORD_RE = re.compile(r"\b(not\s+in|not\s+exists|<>|!=)\b", re.I)
_CORRELATED_SUBQUERY_SHAPE_RE = re.compile(
    r"\(\s*select\b.*?\bwhere\b.*?\b\w+\.\w+\s*=\s*\w+\.\w+", re.I | re.S
)


# ---------------------------------------------------------------------------
# Search and time-series keywords (the OpenSearch analysis's detectors, moved
# here from ``opensearch_pattern_catalog``): case-insensitive substring checks.
# ---------------------------------------------------------------------------

TIME_RANGE_KEYWORDS: tuple[str, ...] = (
    "between",
    ">=",
    "<=",
)

TIME_AGG_KEYWORDS: tuple[str, ...] = (
    "date_trunc",
    "date(",
    "hour(",
    "extract(",
)


# ---------------------------------------------------------------------------
# Cache shapes (the ElastiCache analysis's detectors, moved here from
# ``redis_analysis_tools``): case-insensitive substring checks.
# ---------------------------------------------------------------------------

# Spatial SQL functions or coordinate columns.
GEOSPATIAL_KEYWORDS: tuple[str, ...] = (
    "st_distance",
    "st_within",
    "st_contains",
    "st_intersects",
    "st_dwithin",
    "latitude",
    "longitude",
    "geography",
    "geometry",
)


# ---------------------------------------------------------------------------
# Workload signals (triage's detectors, moved here from
# ``src.agents.referee.triage``). Each reads the lower-cased text, as triage did.
# ---------------------------------------------------------------------------

_MIN_MAX_CALL_RE = re.compile(r"\b(max|min)\s*\(")
_STATUS_EQUALS_VALUE_RE = re.compile(r"\bstatus\s*=\s*[\?'\"]")
_IS_NULL_RE = re.compile(r"\bis\s+null\b")
_COUNT_STAR_OR_ONE_RE = re.compile(r"\bcount\s*\(\s*[\*1]\s*\)")


# ---------------------------------------------------------------------------
# Session keys (moved here from ``src.agents.referee.cache_overlay``, #296/#663):
# the one session fact triage, the cache overlay and the DynamoDB and ElastiCache
# analyses read (#893). The top-N shape is the facade's ``top_n`` now.
# ---------------------------------------------------------------------------

# Same keywords for triage's session_store signal and the cache overlay (``session_key``).
_SESSION_RE = re.compile(r"\b(session|token|sess_id|session_id|csrf)\b", re.IGNORECASE)
# The parts of a PascalCase / camelCase name: ``SessionToken`` -> Session, Token;
# ``CSRFToken`` -> CSRF, Token; ``sessionId`` -> session, Id; ``SessionID`` -> Session, ID.
_NAME_PART_RE = re.compile(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+|\d+")
_IDENTIFIER_RE = re.compile(r"[A-Za-z][A-Za-z0-9_]*")
# The two-part session-store key names a PascalCase or camelCase schema uses
# (#663, kept narrow after review: ``session_store`` alone selects analysis
# agents). ``<prefix>Token`` for these prefixes, or ``Session{Id,Key,Token}``.
# Not ``SessionTimeout``, ``TokenCount``, ``TrainingSession``, ``ExpressionToken``
# or ``PaymentTokenVault``: a name part alone is not a session key. The
# prefixes name a login, session or account-access credential: the session and
# auth ones, and the one-time tokens an account flow looks up by value
# (``ResetToken``, ``LoginToken``, ``RememberToken``, ``VerificationToken``,
# ``InviteToken``, ``IdToken``, ..., #742). A token that is not a credential
# (``PaymentToken``, ``DeviceToken``, ``UnsubscribeToken``) is not one.
_SESSION_TOKEN_PREFIXES = frozenset(
    {
        # session and auth
        "session",
        "csrf",
        "xsrf",
        "auth",
        "access",
        "refresh",
        "jwt",
        "api",
        "bearer",
        "id",
        "sso",
        # login and account flows
        "login",
        "remember",
        "reset",
        "verification",
        "verify",
        "confirmation",
        "confirm",
        "activation",
        "invite",
        "invitation",
        "magic",
        "otp",
    }
)
_SESSION_KEY_SUFFIXES = frozenset({"id", "key", "token"})
_WHERE_CLAUSE_RE = re.compile(r"\bwhere\b(.*)", re.IGNORECASE | re.DOTALL)


def _is_session_key_name(name: str) -> bool:
    """Whether a PascalCase, camelCase or snake_case ``name`` is a two-part session or
    token key (``SessionToken``, ``sessionId``, ``session_token``, ``auth_token``)."""
    parts = [p.lower() for chunk in name.split("_") for p in _NAME_PART_RE.findall(chunk)]
    if len(parts) != 2:
        return False
    head, tail = parts
    return (tail == "token" and head in _SESSION_TOKEN_PREFIXES) or (
        head == "session" and tail in _SESSION_KEY_SUFFIXES
    )


def _has_session_key(text: str) -> bool:
    """Whether ``text`` is a session/token statement (``session_store``, #663).

    Either ``_SESSION_RE`` matches a whole word (``session``, ``token``,
    ``session_id``, ...), unchanged from before, or the WHERE clause filters on a
    two-part session key such as T-SQL's ``[SessionToken]``, ``[CSRFToken]``,
    ``sessionId``, or the snake_case ``session_token``/``auth_token`` (#893: the
    session fact the DynamoDB and ElastiCache analyses read too, whose copies read
    ``session_token`` and this one did not; :func:`_is_session_key_name`). The second rule
    is deliberately narrow, because ``session_store`` selects analysis agents on
    its own: a name part such as ``[SessionTimeout]`` or ``[TokenCount]`` does not
    count, and neither does a ``[UserSession]`` query keyed by something else or
    a session key named only in the select list or inside a string literal. Needs
    the SQL in its original case.
    """
    text = text or ""
    if _SESSION_RE.search(text):
        return True
    # Literals blanked (``purpose = 'reset_token'`` names no column); a bracketed or
    # double-quoted identifier keeps its name (``scan_sql``).
    where = _WHERE_CLAUSE_RE.search(scan_sql(text))
    if where is None:
        return False
    return any(_is_session_key_name(name) for name in _IDENTIFIER_RE.findall(where.group(1)))


# ---------------------------------------------------------------------------
# Search depth (the reality check's OpenSearch justification floor, moved here
# from ``reality_check_floors``, #326/#375).
# ---------------------------------------------------------------------------

# A string literal ('...' or "...") is replaced with a single placeholder
# before any pattern below runs, so a literal's own characters are never
# mistaken for a SQL operator -- a review of #375 found a leading-wildcard
# LIKE pattern (LIKE 'abc%') being read as the pg_trgm similarity operator
# because the '%' inside the literal matched the same way `title % $1` does.
_STRING_LITERAL_RE = re.compile(r"'(?:[^'\\]|\\.)*'|\"(?:[^\"\\]|\\.)*\"")


# A search predicate: the query actually filters on a text-search pattern,
# not just an ordinary equality/range WHERE clause. Required before ranking,
# fuzzy matching or facets are even considered -- a bare GROUP BY, or a
# to_tsvector() call with no @@ match, is not a search at all (see below).
_SEARCH_PREDICATE_RE = re.compile(
    r"\b(like|ilike)\b|match\s*\(.+?\)\s*against\s*\(|to_tsquery\s*\(|"
    r"plainto_tsquery\s*\(|@@|similarity\s*\(|word_similarity\s*\(",
    re.IGNORECASE,
)

# Relevance ranking: an actual full-text match operation. to_tsvector() alone
# (document construction, e.g. CREATE EXTENSION's companion index build, or a
# plain SELECT to_tsvector(...) with no @@) does not count -- only a real
# match (the @@ operator, or constructing the query side with to_tsquery/
# plainto_tsquery) does.
_RELEVANCE_RANKING_RE = re.compile(
    r"match\s*\(.+?\)\s*against\s*\(|to_tsquery\s*\(|plainto_tsquery\s*\(|@@",
    re.IGNORECASE,
)

# Fuzzy matching: an actual similarity computation -- the similarity()/
# word_similarity() functions, the pg_trgm distance/threshold operators
# ("<%", "%>", word-similarity thresholds), or the real pg_trgm "%" operator
# (an identifier or closing paren, then "%", then a parameter placeholder,
# "$N" or "?", not immediately followed by a comparison or arithmetic
# operator). Checked against literal-stripped text only: a leading-wildcard
# LIKE pattern like 'abc%' has no "%" left once its literal is stripped, so
# it never matches this on its own (a review of #375 found the previous
# regex doing exactly that -- matching a LIKE pattern's own wildcard as the
# trigram operator). The negative lookahead after the parameter excludes
# plain modulo arithmetic against a parameter, e.g. "id % $2 = 0" or
# "id % $2 + 1" -- a real trigram "%" is used as a boolean predicate on its
# own, never followed immediately by another comparison or arithmetic
# operator (a second review of #375 found "id % $2 = 0" misread as fuzzy
# matching).
_FUZZY_MATCH_RE = re.compile(
    r"\bsimilarity\s*\(|\bword_similarity\s*\(|<%|%>|"
    r"[\w)]\s*%\s*(?:\$\d+|\?)(?!\s*(?:=|<>|!=|<=|>=|<|>|[+\-*/%]))",
    re.IGNORECASE,
)


def _has_search_depth(query_text: str, group_by: bool) -> bool:
    """Real search depth in one query (#326 finding 4): a search predicate
    plus relevance ranking, fuzzy matching, or a facet (``group_by``, the
    query's ``GROUP BY`` fact, alongside the same predicate) -- never a bare
    GROUP BY, a plain LIKE/prefix filter, a plain join, or to_tsvector()/pg_trgm
    document/extension setup with no match operator.
    """
    text = _STRING_LITERAL_RE.sub("?", query_text)
    # Each regex below needs a literal word or symbol; a substring test on the lower-cased
    # text (C speed) skips the regex on the long queries that have none of them.
    lower = text.lower()
    if any(word in lower for word in ("against", "tsquery", "@@")) and _RELEVANCE_RANKING_RE.search(
        text
    ):
        return True
    if ("similarity" in lower or "%" in lower) and _FUZZY_MATCH_RE.search(text):
        return True
    return group_by and bool(_SEARCH_PREDICATE_RE.search(text))


# ---------------------------------------------------------------------------
# Heterogeneous-source features (the Aurora engine choice's detectors, moved
# here from ``aurora_choice``, #381). Each runs on :func:`scan_sql`'s output.
# ---------------------------------------------------------------------------

# Requires MERGE to be followed by an optional TOP(n) clause, then (optionally "INTO"
# then) the start of a target identifier -- the shape an actual MERGE statement takes
# -- not just the bare keyword "merge" (e.g. a column or variable literally named
# ``merge``) coincidentally followed by "using" somewhere within the next 80
# characters. The negative lookahead excludes a handful of SQL keywords that can
# legitimately follow a bare "merge" column reference (``SELECT merge FROM ...
# USING (...)``) but never follow the MERGE statement itself. ``[#\w]`` (#381 review
# round 2) only requires the *first* character of the target identifier to look like
# one -- a word character, or ``#`` for a local temp table (``MERGE #target ...``) --
# rather than the whole identifier, since by the time this runs ``scan_sql`` has
# already turned a bracketed or double-quoted identifier into its bare name (``[dbo].
# [Target]`` -> ``dbo.Target``), and the ``.{0,80}\busing\b`` that follows is what
# actually confirms this is a MERGE statement, not the identifier shape.
_MERGE_RE = re.compile(
    r"\bmerge\s+(?:top\s*\([^)]*\)\s*)?(?:into\s+)?"
    r"(?!(?:from|where|select|group|order|having|join)\b)[#\w]"
    r".{0,80}\busing\b",
    re.IGNORECASE | re.DOTALL,
)


# ---------------------------------------------------------------------------
# Cache overlay shapes and the write check (moved here from
# ``src.agents.referee.cache_overlay``, #296/#303/#663/#825). The UI engine
# picker mirrors the write check (``src/ui/src/utils/assignmentSummary.js``).
# ---------------------------------------------------------------------------

_WHERE_RE = re.compile(r"\bwhere\b", re.IGNORECASE)
_KEYED_RE = re.compile(r"(?<![<>!])=|\bin\s*\(", re.IGNORECASE)

# Leading ``--`` or ``#`` line comments and ``/* ... */`` block comments, as
# many as the statement opens with, are stripped first (review of #303: a
# statement opening with a comment is typed ``OTHER`` by the collector because
# the SQL verb is no longer the first token). ``src.shared.sql_comments`` does
# it in linear time (#808).
# A DML keyword anywhere in the statement -- not just at the start -- so a
# data-modifying CTE (``WITH w AS (INSERT INTO ... RETURNING id) SELECT ...``)
# is caught too, the same way the collector's own query_type misses it.
_DML_KEYWORD_RE = re.compile(
    r"\b(insert\s+into|update\s+\S+\s+set|delete\s+from|merge\s+into)\b", re.IGNORECASE
)
# CALL/TRUNCATE/COPY are writes but have no DML keyword of their own; anchored
# at the start (after stripping a leading comment), since each is a statement
# in its own right, not a clause that can appear mid-query.
_LEADING_WRITE_VERB_RE = re.compile(r"^\s*(call|truncate|copy)\b", re.IGNORECASE)


def _strip_leading_comments(text: str, nested: bool = False) -> str:
    return strip_leading_comments(
        text, hash_comments=True, strip_following_space=True, nested=nested
    )


def _leading_comment_readings(text: str) -> tuple[str, ...]:
    """``text`` without its leading comments, read both ways (#825).

    PostgreSQL and SQL Server nest block comments, MySQL and Oracle end one at
    its first ``*/``. The write check (:func:`is_write_query`, behind
    :func:`can_own`, the cache overlay's write-heavy tables and schema design's
    write scope) sees only the query, and its callers span the resolver, the
    reality check, the validators and the override path, none of which share a
    source engine, so the dialect is not known here. A write under either
    reading is a write, which errs toward "write" rather than missing one.

    The nested reading counts only when it strips at least as much as the
    first-close reading. A terminated nested comment always ends at or after the
    first ``*/``, so a nested strip that leaves more text means the comment is
    unterminated there (``/* delete from /* x */ SELECT 1``): not valid
    PostgreSQL or T-SQL, and a MySQL read that must stay a read.
    """
    plain = _strip_leading_comments(text)
    nested = _strip_leading_comments(text, nested=True)
    return (plain,) if len(nested) > len(plain) else (plain, nested)


def _contains_any(lower_text: str, keywords: tuple[str, ...]) -> bool:
    """Whether lower-cased text contains any keyword (keywords compared as written)."""
    return any(kw in lower_text for kw in keywords)


class QueryFacts:
    """The text facts of one query (see the module docstring).

    Each fact is computed the first time a reader asks for it, then kept on the
    object (``functools.cached_property``), so a reader pays only for the facts
    it reads: a costly detector one analysis needs (the DocumentDB correlated
    subquery shape) never runs in the steps that don't read it (#895 review).
    """

    def __init__(self, text: str, source_engine: str = "") -> None:
        self._text = text
        self._source_engine = source_engine

    @cached_property
    def _lower(self) -> str:
        return self._text.lower()

    # -- Parsed facts (the facade's ``SqlFacts``, #893) ------------------------

    @cached_property
    def _readings(self) -> tuple[SqlFacts, AccessFacts]:
        """The facade's readings of the text, from one parse in the source engine's dialect."""
        return query_readings(self._text, self._source_engine)

    @cached_property
    def _parsed(self) -> SqlFacts:
        return self._readings[0]

    @cached_property
    def _access(self) -> AccessFacts:
        return self._readings[1]

    def _has(self, *hints: str) -> bool:
        return any(hint in self._lower for hint in hints)

    @cached_property
    def group_by(self) -> bool:
        """A ``GROUP BY`` clause (``SqlFacts.group_by``)."""
        return self._has("group") and self._parsed.group_by

    @cached_property
    def aggregate_function_count(self) -> int:
        """How many aggregate function calls (``SqlFacts.aggregate_function_count``)."""
        if not self._may_aggregate:
            return 0
        return self._parsed.aggregate_function_count

    @cached_property
    def _may_aggregate(self) -> bool:
        return self._has(*_AGGREGATE_WORD_HINTS) or bool(_FUNCTION_CALL_HINT_RE.search(self._text))

    @cached_property
    def aggregation(self) -> bool:
        """A ``GROUP BY`` or an aggregate function, windowed or not: exactly the facade's
        ``has_aggregation``, the collector's ``has_aggregations``. A ranking or offset
        window function alone is not one.
        """
        return self.group_by or self.aggregate_function_count > 0

    @cached_property
    def join_count(self) -> int:
        """Joins to another row source (``SqlFacts.join_count``, the collector's
        ``join_count``): ``FROM a, b`` is one join, a table function is none.
        """
        return self._parsed.join_count if self._has(*_JOIN_HINTS) else 0

    @cached_property
    def leading_wildcard_like(self) -> bool:
        """A ``LIKE``/``ILIKE`` whose pattern starts with ``%`` (``SqlFacts.wildcard_leading``)."""
        return self._has("like") and self._parsed.wildcard_leading

    @cached_property
    def wildcard_likes(self) -> tuple[WildcardLike, ...]:
        """Each leading-wildcard ``LIKE``, with the column it tests and whether it is a
        word search (``SqlFacts.wildcard_likes``, #493)."""
        return self._parsed.wildcard_likes if self._has("like") else ()

    @cached_property
    def like_parameter(self) -> bool:
        """A ``LIKE``/``ILIKE`` whose whole pattern is a bind parameter
        (``SqlFacts.like_parameter``): a MySQL digest's ``LIKE ?``.
        """
        return self._has("like") and self._parsed.like_parameter

    @cached_property
    def fulltext_search(self) -> bool:
        """A full-text match: ``MATCH ... AGAINST``, ``@@`` or a ``tsvector``/``tsquery``
        function, ``CONTAINS``/``FREETEXT`` (``SqlFacts.fulltext_search``).
        """
        return self._has(*_FULLTEXT_HINTS) and self._parsed.fulltext_search

    @cached_property
    def pattern_match(self) -> bool:
        """A regular-expression or ``SIMILAR TO`` match (``SqlFacts.pattern_match``)."""
        return self._has(*_PATTERN_MATCH_HINTS) and self._parsed.pattern_match

    @cached_property
    def similarity_match(self) -> bool:
        """A trigram similarity function (``SqlFacts.similarity_match``)."""
        return self._has("similarity") and self._parsed.similarity_match

    @cached_property
    def text_search(self) -> bool:
        """A text search: a leading-wildcard or parameterised ``LIKE``, a full-text,
        regular-expression or similarity match. A prefix ``LIKE 'abc%'`` is a range,
        not a search (ADR-031 Decision 14 Amendment 5).
        """
        return (
            self.leading_wildcard_like
            or self.like_parameter
            or self.fulltext_search
            or self.pattern_match
            or self.similarity_match
        )

    @cached_property
    def window_function(self) -> bool:
        """A function with an ``OVER`` clause (``SqlFacts.window_function``)."""
        return self._has("over") and self._parsed.window_function

    @cached_property
    def ranking_window_function(self) -> bool:
        """``ROW_NUMBER``/``RANK``/``DENSE_RANK`` over a window
        (``SqlFacts.ranking_window_function``).
        """
        return self._has("over") and self._parsed.ranking_window_function

    @cached_property
    def offset_window_function(self) -> bool:
        """``LAG``/``LEAD``/``NTILE`` over a window (``SqlFacts.offset_window_function``)."""
        return self._has("over") and self._parsed.offset_window_function

    @cached_property
    def windowed_aggregate(self) -> bool:
        """An aggregate over a window, ``SUM(...) OVER`` (``SqlFacts.windowed_aggregate``)."""
        return self._has("over") and self._parsed.windowed_aggregate

    @cached_property
    def recursive_cte(self) -> bool:
        """``WITH RECURSIVE``, or a CTE whose body has a ``UNION`` and reads itself
        (``SqlFacts.recursive_cte``; SQL Server and Oracle have no ``RECURSIVE``).
        """
        return self._has("with") and self._parsed.recursive_cte

    @cached_property
    def order_by(self) -> bool:
        """An ``ORDER BY`` that sorts a query's rows, not a window's (``SqlFacts.order_by``)."""
        return self._has("order") and self._parsed.order_by

    @cached_property
    def row_limit(self) -> bool:
        """A row limit: ``LIMIT``, ``TOP``, ``FETCH FIRST``/``NEXT`` (``SqlFacts.row_limit``);
        a ``credit_limit`` column is not one.
        """
        return self._has(*_ROW_LIMIT_HINTS) and self._parsed.row_limit

    @cached_property
    def top_n(self) -> bool:
        """A top-N read: one query with both an ``ORDER BY`` and a row limit
        (``SqlFacts.top_n``; ``TOP`` with no ``ORDER BY`` is not one).
        """
        return self.order_by and self.row_limit and self._parsed.top_n

    @cached_property
    def timestamp_column(self) -> bool:
        """A column named as a timestamp (``created_at``, ``updated_at``, ``timestamp``,
        ``event_time``, ``*_date``, ``last_*_at``, ...; ``SqlFacts.timestamp_column``).
        """
        return self._has(*_TIMESTAMP_HINTS) and self._parsed.timestamp_column

    @cached_property
    def multi_table_write(self) -> bool:
        """An UPDATE or DELETE whose rows come from a join with another table, in any
        dialect (``SqlFacts.multi_table_write``).
        """
        return self._has("update", "delete") and self._parsed.multi_table_write

    @cached_property
    def json_access(self) -> bool:
        """The query reads or builds JSON: a JSON function or operator, a cast to
        ``json``/``jsonb``, a JSON containment (``SqlFacts.json_access``).
        """
        return self._has(*_JSON_HINTS) and self._parsed.json_access

    @cached_property
    def range_with_order_by(self) -> bool:
        """``BETWEEN`` or a word character followed by ``<``/``<=``/``>``/``>=``, and an
        ``ORDER BY`` (the range predicates are #893's last group).
        """
        return bool(_RANGE_OPERATOR_WORD_RE.search(self._lower)) and self.order_by

    @cached_property
    def time_range_operator(self) -> bool:
        """``between``, ``>=`` or ``<=``."""
        return _contains_any(self._lower, TIME_RANGE_KEYWORDS)

    # -- Text facts ------------------------------------------------------------

    @cached_property
    def found_rows(self) -> bool:
        """MySQL's ``SQL_CALC_FOUND_ROWS`` modifier or a ``FOUND_ROWS()`` call (the row
        count of the previous query).
        """
        return bool(_FOUND_ROWS_RE.search(self._text))

    @cached_property
    def dedup_only_group_by(self) -> bool:
        """The ``GROUP BY`` only de-duplicates rows
        (``unsupported_pattern.is_dedup_only_group_by``, #336).
        """
        return self.group_by and is_dedup_only_group_by(self._text)

    @cached_property
    def computed_join_key(self) -> bool:
        """Some ``JOIN ... ON`` condition compares a function call with a column
        (``lower(g.name) = u.username_lower``, either side); a function compared with a
        bind parameter is a filter, not a computed join key.
        """
        return _has_computed_join_key(self._text)

    @cached_property
    def write_with_subquery(self) -> bool:
        """``INSERT``/``UPDATE``/``DELETE`` followed later by ``SELECT``."""
        return bool(_WRITE_WITH_SUBQUERY_RE.search(self._text))

    @cached_property
    def exists_or_in_subquery(self) -> bool:
        """``EXISTS (``, ``NOT EXISTS (`` or ``IN (SELECT``."""
        return bool(_EXISTS_OR_IN_SUBQUERY_RE.search(self._text))

    @cached_property
    def in_list(self) -> bool:
        """`` in (`` or `` in(``."""
        return _contains_any(self._lower, IN_LIST_KEYWORDS)

    @cached_property
    def duplicate_key_upsert(self) -> bool:
        """``on duplicate key`` or ``replace into``."""
        return _contains_any(self._lower, DUPLICATE_KEY_KEYWORDS)

    @cached_property
    def string_aggregate_call(self) -> bool:
        """``GROUP_CONCAT(``, ``STRING_AGG(`` or ``LISTAGG(`` outside comments and
        string literals."""
        return self._has("group_concat", "string_agg", "listagg") and bool(
            _STRING_AGGREGATE_RE.search(scan_sql(self._text))
        )

    @cached_property
    def stored_routine_keyword(self) -> bool:
        """A :data:`STORED_ROUTINE_KEYWORDS` substring (``call ``, ``delimiter``, ``create
        procedure``, ``create function``), or, for a SQL Server source, a statement that
        is an ``EXEC``/``EXECUTE`` of a procedure.
        """
        if _contains_any(self._lower, STORED_ROUTINE_KEYWORDS):
            return True
        if self._source_engine.strip().lower() != "sqlserver" or not self._has("exec"):
            return False
        return any(_TSQL_EXEC_RE.match(t) for t in _leading_comment_readings(self._text))

    @cached_property
    def array_operation(self) -> bool:
        """``ANY(ARRAY``, ``array_agg(``, ``unnest(``, ``ARRAY[`` or ``@> ARRAY``."""
        return bool(_ARRAY_OPERATION_RE.search(self._text))

    @cached_property
    def lateral_join(self) -> bool:
        """``JOIN LATERAL (``, ``, LATERAL (`` or ``LATERAL (``."""
        return bool(_LATERAL_JOIN_RE.search(self._text))

    @cached_property
    def on_conflict_or_returning(self) -> bool:
        """``ON CONFLICT``, or ``RETURNING <column>`` not preceded by ``AS``."""
        return bool(_ON_CONFLICT_OR_RETURNING_RE.search(self._text))

    @cached_property
    def negation_word(self) -> bool:
        """``NOT IN``, ``NOT EXISTS``, ``<>`` or ``!=`` between word characters."""
        return bool(_NEGATION_WORD_RE.search(self._text))

    @cached_property
    def correlated_subquery_shape(self) -> bool:
        """``( SELECT ... WHERE ... a.b = c.d``."""
        return bool(_CORRELATED_SUBQUERY_SHAPE_RE.search(self._text))

    @cached_property
    def comma_count(self) -> int:
        """How many commas the text has."""
        return self._lower.count(",")

    @cached_property
    def time_function_keyword(self) -> bool:
        """``date_trunc``, ``date(``, ``hour(`` or ``extract(``."""
        return _contains_any(self._lower, TIME_AGG_KEYWORDS)

    @cached_property
    def geospatial_keyword(self) -> bool:
        """A :data:`GEOSPATIAL_KEYWORDS` substring (``st_distance``, ..., ``latitude``,
        ``geometry``).
        """
        return _contains_any(self._lower, GEOSPATIAL_KEYWORDS)

    @cached_property
    def min_max_call(self) -> bool:
        """A ``MAX(`` or ``MIN(`` call."""
        return bool(_MIN_MAX_CALL_RE.search(self._lower))

    @cached_property
    def status_equals_value(self) -> bool:
        """``status = ?`` / ``status = '...`` / ``status = "...``."""
        return bool(_STATUS_EQUALS_VALUE_RE.search(self._lower))

    @cached_property
    def is_null(self) -> bool:
        """``IS NULL``."""
        return bool(_IS_NULL_RE.search(self._lower))

    @cached_property
    def count_star_or_one(self) -> bool:
        """``COUNT(*)`` or ``COUNT(1)``."""
        return bool(_COUNT_STAR_OR_ONE_RE.search(self._lower))

    @cached_property
    def _scanned(self) -> str:
        return scan_sql(self._text)

    @cached_property
    def _scanned_lower(self) -> str:
        return self._scanned.lower()

    @cached_property
    def search_depth(self) -> bool:
        """Real search depth (#326): relevance ranking or fuzzy matching, or a search
        predicate with a ``GROUP BY`` facet, read with string literals blanked.
        """
        return _has_search_depth(self._text, self.group_by)

    @cached_property
    def merge_statement(self) -> bool:
        """A ``MERGE [TOP (n)] [INTO] <target> ... USING`` statement, read on
        :func:`scan_sql`'s output.
        """
        return "merge" in self._scanned_lower and bool(_MERGE_RE.search(self._scanned))

    @cached_property
    def _where_split(self) -> list[str]:
        return _WHERE_RE.split(self._text, maxsplit=1)

    @cached_property
    def where_word(self) -> bool:
        """A ``WHERE`` word anywhere in the text."""
        return len(self._where_split) == 2

    @cached_property
    def keyed_after_where(self) -> bool:
        """An equality (``=`` not after ``<``/``>``/``!``) or an ``IN (`` after the first
        ``WHERE``.
        """
        return len(self._where_split) == 2 and bool(_KEYED_RE.search(self._where_split[1]))

    @cached_property
    def write_statement_text(self) -> bool:
        """After the leading comments (read both nested and first-close, #825), a DML
        keyword anywhere (``INSERT INTO``, ``UPDATE x SET``, ``DELETE FROM``,
        ``MERGE INTO``) or a leading ``CALL``/``TRUNCATE``/``COPY``.
        """
        return any(
            _DML_KEYWORD_RE.search(reading) or _LEADING_WRITE_VERB_RE.match(reading)
            for reading in _leading_comment_readings(self._text)
        )

    @cached_property
    def session_key(self) -> bool:
        """A session/token statement (#663): a session word (``session``, ``token``,
        ``sess_id``, ``session_id``, ``csrf``), or a two-part PascalCase/camelCase session
        key in the WHERE clause (``[SessionToken]``, ``sessionId``). Reads the original case.
        """
        return _has_session_key(self._text)

    @property
    def needs_aggregation(self) -> bool:
        """A window function, ``FOUND_ROWS``, or an aggregation that is more than a
        de-duplicating ``GROUP BY`` (#338, #336)."""
        if self.window_function or self.found_rows:
            return True
        return self.aggregation and not self.dedup_only_group_by


def _has_computed_join_key(text: str) -> bool:
    return any(
        _COMPUTED_EXPR_IN_CONDITION_RE.search(m.group("cond"))
        for m in _JOIN_ON_CLAUSE_RE.finditer(text)
    )


# Covers the 20,000-query ingestion default (``ingestion_limits``) twice
# over: triage also reads facts of each query's lower-cased text. A collection
# raised past that by ``MODERNIZER_MAX_UPLOAD_QUERIES`` only recomputes what
# its readers ask for, since each fact is lazy.
@lru_cache(maxsize=65536)
def _query_facts(text: str, source_engine: str) -> QueryFacts:
    return QueryFacts(text, source_engine)


def query_facts(query_text: str | None, source_engine: str | None = None) -> QueryFacts:
    """The facts of ``query_text`` (``None`` reads as empty text), one object per
    text and source engine: computed facts are kept for every later reader. The
    source engine (the collector's ``metadata.source_database.engine``) picks the
    dialect the parsed facts are read in; none reads them in sqlglot's generic one.
    """
    return _query_facts(query_text or "", (source_engine or "").strip().lower())


def source_engine_of(collector_output: Mapping | None) -> str:
    """The collection's source engine (``metadata.source_database.engine``), lower-cased."""
    source = ((collector_output or {}).get("metadata") or {}).get("source_database") or {}
    return str(source.get("engine") or "").strip().lower()
