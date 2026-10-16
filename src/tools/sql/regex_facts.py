"""The regex query facts: the facade's fallback when sqlglot can't read a statement.

These are the scans the collectors ran on raw query text before #796, moved
here unchanged so the facade (:func:`src.tools.sql.facade.sql_facts`) falls
back to exactly the same answer when it can't parse a statement, the way
:mod:`src.tools.sql.regex_tables` backs its table references. They read
comments, string literals and column names as SQL (a column named ``count``
is an aggregate, ``user_selectable`` is a second ``SELECT``, a ``-- join``
comment is a join), which is why the facade reads the syntax tree first.

- ``has_joins``: the text contains `` join `` (surrounded by spaces);
- ``join_count``: the number of ``join`` words;
- ``has_aggregation``: a ``count``/``sum``/``avg``/``min``/``max`` word, a
  ``group_concat``/``string_agg``/``array_agg`` word (#893) or ``GROUP BY`` anywhere;
- ``has_subquery``: more than one ``select`` substring;
- ``wildcard_leading``: ``LIKE`` followed by a quote and ``%`` (the
  capability gate's and the DynamoDB exclusion's own detector, which reads
  ``ILIKE`` and ``NOT LIKE`` too).

The facts #893 added to the facade fall back to the text detector each
replaced, one per fact; ``group_by`` and ``aggregate_function_count`` are
read so that ``has_aggregation`` is still exactly ``group_by`` or a positive
count:

- ``group_by``: ``GROUP BY``;
- ``aggregate_function_count``: how many ``count``/``sum``/``avg``/``min``/``max``/
  ``group_concat``/``string_agg``/``array_agg`` words;
- ``like_parameter``: ``LIKE``/``ILIKE`` followed by a bind parameter (``?``,
  ``$1``, ``:name``, ``@name``) that is not concatenated (``@p + '%'`` is a prefix);
- ``fulltext_search``: ``MATCH (...) AGAINST (``, a PostgreSQL ``tsvector``/
  ``tsquery`` function, ``tsvector``, ``ts_rank(``, ``@@``, or ``CONTAINS(``/
  ``CONTAINSTABLE(``/``FREETEXT(``/``FREETEXTTABLE(``;
- ``pattern_match``: ``REGEXP``/``RLIKE`` as a word, ``REGEXP_LIKE(``, ``~`` or
  ``~*`` followed by a space, ``SIMILAR TO``;
- ``similarity_match``: ``similarity(``, ``word_similarity(`` or
  ``strict_word_similarity(``;
- ``window_function``: ``)`` followed by ``OVER (``;
- ``ranking_window_function``: ``ROW_NUMBER()``/``RANK()``/``DENSE_RANK()``
  followed by ``OVER``;
- ``offset_window_function``: a ``LAG``/``LEAD``/``NTILE`` call followed by
  ``OVER``;
- ``windowed_aggregate``: a ``SUM``/``AVG``/``MIN``/``MAX``/``COUNT`` call followed
  by ``OVER``;
- ``recursive_cte``: ``WITH RECURSIVE``, or a CTE whose body has a ``UNION`` and
  names the CTE again, read on :func:`scan_sql`'s output (the Aurora engine
  choice's detector, #381);
- ``order_by``: ``ORDER BY``, not one inside ``OVER (...)``;
- ``row_limit``: ``LIMIT``, ``FETCH FIRST``/``NEXT``, or ``TOP`` followed by a
  number, ``(`` or a variable;
- ``top_n``: ``ORDER BY ... LIMIT``/``FETCH FIRST|NEXT``, or ``TOP (n) ... ORDER
  BY`` (the top-N regex triage and the cache overlay read before, #296/#663);
- ``timestamp_column``: a :data:`TIMESTAMP_COLUMN_NAMES` name as a word;
- ``json_access``: a ``json_*``/``jsonb_*`` call, ``->``/``->>``, ``#>``,
  ``::json``/``::jsonb``, ``@> '``, ``OPENJSON(``, ``FOR JSON``, ``IS JSON``;
- ``update_target_join``: ``UPDATE``, then ``JOIN`` before the first ``SET`` with no
  ``FROM`` between them.
"""

from __future__ import annotations

import re

_JOIN_WORD_RE = re.compile(r"\bjoin\b", re.IGNORECASE)
_AGGREGATION_WORD_RE = re.compile(
    r"\b(count|sum|avg|min|max|group_concat|string_agg|array_agg|group\s+by)\b", re.IGNORECASE
)
_LEADING_WILDCARD_RE = re.compile(r"LIKE\s+['\"]%", re.IGNORECASE)
# ``GROUP BY``, ``ORDER BY``, a pattern-match predicate, a literal-prefix ``LIKE`` and
# the ``SELECT`` word: the regex readings ``query_shape``'s shape check runs on
# comment- and literal-stripped text, defined here once (#893 review).
GROUP_BY_RE = re.compile(r"\bgroup\s+by\b", re.IGNORECASE)
ORDER_BY_RE = re.compile(r"\border\s+by\b", re.IGNORECASE)
PATTERN_PREDICATE_RE = re.compile(r"\b(?:I?LIKE|SIMILAR\s+TO|REGEXP|RLIKE)\b", re.IGNORECASE)
LITERAL_PREFIX_LIKE_RE = re.compile(r"\bI?LIKE\s+'[^%_']", re.IGNORECASE)
SELECT_WORD_RE = re.compile(r"\bSELECT\b", re.IGNORECASE)
_AGGREGATE_WORD_RE = re.compile(
    r"\b(?:count|sum|avg|min|max|group_concat|string_agg|array_agg)\b", re.IGNORECASE
)
# Not when the parameter is concatenated (``@Prefix + '%'``, ``$1 || '%'``): a prefix.
_LIKE_PARAMETER_RE = re.compile(
    r"\bI?LIKE\s+(?:\?|\$\d+\b|:\w+\b|@\w+\b)(?!\s*(?:\+|\|\|))", re.IGNORECASE
)
_FULLTEXT_SEARCH_RE = re.compile(
    r"""
    MATCH\s*\(.+?\)\s*AGAINST\s*\(
    |
    \b(?:to_tsvector|to_tsquery|plainto_tsquery|phraseto_tsquery|websearch_to_tsquery)\s*\(
    |
    \btsvector\b
    |
    \bts_rank(?:_cd)?\s*\(
    |
    @@
    |
    \b(?:contains|containstable|freetext|freetexttable)\s*\(
    """,
    re.IGNORECASE | re.VERBOSE,
)
_PATTERN_MATCH_RE = re.compile(
    r"\b(?:regexp|rlike)\b|\bregexp_like\s*\(|~\*?\s|\bsimilar\s+to\b", re.IGNORECASE
)
_SIMILARITY_MATCH_RE = re.compile(r"\b(?:strict_word_|word_)?similarity\s*\(", re.IGNORECASE)
_WINDOW_FUNCTION_RE = re.compile(r"\)\s*OVER\s*\(", re.IGNORECASE)
_ROW_LIMIT_RE = re.compile(
    r"\blimit\b|\bfetch\s+(?:first|next)\b|\btop\s*(?:\(|\d|@)", re.IGNORECASE
)
# A top-N read: ``ORDER BY ... LIMIT n``, or its T-SQL and ANSI spellings (#663),
# ``SELECT TOP (n) ... ORDER BY`` and ``ORDER BY ... [OFFSET ...] FETCH {FIRST|NEXT}``.
# TOP takes a number, a ``(expression)`` or a variable, so a column named ``top``
# is not one; TOP with no ORDER BY returns arbitrary rows and is not a top-N.
_TOP_N_RE = re.compile(
    r"order\s+by\b.*\b(?:limit|fetch\s+(?:first|next))\b|\btop\s*(?:\(|\d|@).*\border\s+by\b",
    re.IGNORECASE | re.DOTALL,
)
# The names of a timestamp column: every name the DynamoDB, OpenSearch,
# ElastiCache and triage detectors read before #893, as whole names.
TIMESTAMP_COLUMN_NAMES = (
    r"created_at|updated_at|timestamp|date|time|expires?_at|ttl|last_\w+_at|event_time|"
    r"log_time|ingested_at|open_date|close_date|created_date|modified_date|event_date"
)
TIMESTAMP_COLUMN_NAME_RE = re.compile(rf"(?:{TIMESTAMP_COLUMN_NAMES})", re.IGNORECASE)
_TIMESTAMP_COLUMN_RE = re.compile(rf"\b(?:{TIMESTAMP_COLUMN_NAMES})\b", re.IGNORECASE)
_JSON_ACCESS_RE = re.compile(
    r"\bjsonb?_\w+\s*\(|->>?|#>|::jsonb?\b|@>\s*'|\bopenjson\s*\(|\bfor\s+json\b|\bis\s+json\b",
    re.IGNORECASE,
)
_RANKING_WINDOW_FUNCTION_RE = re.compile(
    r"\b(?:ROW_NUMBER|RANK|DENSE_RANK)\s*\(\s*\)\s*OVER\b", re.IGNORECASE
)
# A call's arguments: anything but parentheses, or one level of nested ones.
_ARGUMENTS = r"\((?:[^()]|\([^()]*\))*\)"
_OFFSET_WINDOW_FUNCTION_RE = re.compile(
    rf"\b(?:LAG|LEAD|NTILE)\s*{_ARGUMENTS}\s*OVER\b", re.IGNORECASE
)
_WINDOWED_AGGREGATE_RE = re.compile(
    rf"\b(?:SUM|AVG|MIN|MAX|COUNT)\s*{_ARGUMENTS}\s*OVER\b", re.IGNORECASE
)


def regex_has_joins(query_text: str) -> bool:
    """`` join `` with a space on each side, case-insensitive."""
    return " join " in (query_text or "").lower()


def regex_join_count(query_text: str) -> int:
    """How many times the word ``join`` appears."""
    return len(_JOIN_WORD_RE.findall(query_text or ""))


def regex_has_aggregation(query_text: str) -> bool:
    """An aggregate function name or ``GROUP BY`` anywhere in the text."""
    return bool(_AGGREGATION_WORD_RE.search(query_text or ""))


def regex_has_subquery(query_text: str) -> bool:
    """More than one ``select`` substring, case-insensitive."""
    return (query_text or "").lower().count("select") > 1


def regex_wildcard_leading(query_text: str) -> bool:
    """``LIKE '%...`` / ``LIKE "%...`` (``ILIKE``, ``NOT LIKE`` included)."""
    return bool(_LEADING_WILDCARD_RE.search(query_text or ""))


def regex_group_by(query_text: str) -> bool:
    """``GROUP BY``, case-insensitive."""
    return bool(GROUP_BY_RE.search(query_text or ""))


def regex_aggregate_function_count(query_text: str) -> int:
    """How many aggregate function words (``count``, ..., ``group_concat``) the text has."""
    return len(_AGGREGATE_WORD_RE.findall(query_text or ""))


def regex_like_parameter(query_text: str) -> bool:
    """``LIKE``/``ILIKE`` followed by a bind parameter."""
    return bool(_LIKE_PARAMETER_RE.search(query_text or ""))


def regex_fulltext_search(query_text: str) -> bool:
    """A MySQL, PostgreSQL or SQL Server full-text match."""
    return bool(_FULLTEXT_SEARCH_RE.search(query_text or ""))


def regex_pattern_match(query_text: str) -> bool:
    """A regular-expression or ``SIMILAR TO`` match."""
    return bool(_PATTERN_MATCH_RE.search(query_text or ""))


def regex_similarity_match(query_text: str) -> bool:
    """A trigram similarity function call."""
    return bool(_SIMILARITY_MATCH_RE.search(query_text or ""))


def regex_window_function(query_text: str) -> bool:
    """``)`` followed by ``OVER (``."""
    return bool(_WINDOW_FUNCTION_RE.search(query_text or ""))


def regex_ranking_window_function(query_text: str) -> bool:
    """``ROW_NUMBER()``/``RANK()``/``DENSE_RANK()`` followed by ``OVER``."""
    return bool(_RANKING_WINDOW_FUNCTION_RE.search(query_text or ""))


def regex_offset_window_function(query_text: str) -> bool:
    """A ``LAG``/``LEAD``/``NTILE`` call followed by ``OVER``."""
    return bool(_OFFSET_WINDOW_FUNCTION_RE.search(query_text or ""))


def regex_windowed_aggregate(query_text: str) -> bool:
    """A ``SUM``/``AVG``/``MIN``/``MAX``/``COUNT`` call followed by ``OVER``."""
    return bool(_WINDOWED_AGGREGATE_RE.search(query_text or ""))


def regex_recursive_cte(query_text: str) -> bool:
    """``WITH RECURSIVE`` or a self-referencing ``UNION`` CTE, on :func:`scan_sql`'s output."""
    return _has_recursive_cte(scan_sql(query_text or ""))


# ---------------------------------------------------------------------------
# Comments, strings and quoted identifiers neutralised in one pass (moved here
# from ``query_facts`` with the recursive-CTE detector it feeds, #893).
# ---------------------------------------------------------------------------

# Matches a CTE's own name definition, whether it is the first one after ``WITH`` or a
# later one introduced by a comma (``WITH a AS (...), b AS (...)``) -- #381 review: the
# pre-review version only matched the first, so a recursive CTE named anywhere but first
# in the list went undetected. ``[#\w]+`` (#381 review round 2) rather than
# ``[a-zA-Z_]\w*``: by the time this runs, ``scan_sql`` has already turned a bracketed
# or double-quoted CTE name (``[Tree]``, ``"Tree"``) into its bare identifier, so the
# name itself is always a plain word here regardless of how the query originally quoted
# it.
_CTE_NAME_RE = re.compile(r"(?:\bwith\b|,)\s*([#\w]+)\s*(?:\([^)]*\))?\s+as\s*\(", re.IGNORECASE)


# The characters :func:`scan_sql` acts on: ``--``, ``/*``, a quote (with an optional
# ``N`` prefix), ``[`` and ``"``. Everything between two of them passes through.
_SCAN_SPECIAL_RE = re.compile(r"--|/\*|[Nn]?'|\[|\"")


def scan_sql(text: str) -> str:
    """One left-to-right pass over ``text`` neutralizing comments, strings and
    quoted identifiers, so every feature regex below runs against SQL as the engine
    would actually parse it, never raw source text (#381 review round 2).

    Two independent regex passes (strip strings, then strip comments) can each
    mis-handle what the other produces: a comment containing an apostrophe
    (``-- don't touch``) made the string-literal pass, if it ran first, swallow
    everything up to the *next* quote -- which could be an entire real statement
    later in the query. Scanning once, left to right, in the order these characters
    are actually encountered removes that ordering bug entirely.

    Each construct below is replaced the way the engine would resolve it, not simply
    deleted (so a real token on either side is never glued to its neighbour):

    - ``--`` to end of line: a space (the newline itself is kept, so later-line
      offsets are not shifted and a line comment never merges two lines).
    - ``/* ... */``: a space, with nesting support (SQL Server supports nested
      block comments) -- the whole span up to the matching outer ``*/`` is one
      comment, so text between an inner ``*/`` and the outer one is never
      mistaken for live code.
    - ``'...'`` string literals, with ``''``-doubled escapes and an optional
      ``N``/``n`` (Unicode) prefix: a space. Content is never a real keyword
      match, only text a customer or generator happened to write.
    - ``[ident]`` (T-SQL brackets, ``]]``-escaped) and ``"ident"`` (ANSI/Oracle/
      PostgreSQL double quotes, ``""``-escaped): the bare identifier, with the
      quoting removed. These name a column, table or CTE -- not a string -- so a
      feature regex must still be able to see and match the name inside
      (``MERGE [dbo].[Target]``, ``WITH "Tree" AS (...)``).

    Every other character passes through unchanged.
    """
    out: list[str] = []
    i, n = 0, len(text)
    while i < n:
        # Copy the plain run up to the next character that starts a construct.
        hit = _SCAN_SPECIAL_RE.search(text, i)
        if hit is None:
            out.append(text[i:])
            break
        if hit.start() > i:
            out.append(text[i : hit.start()])
            i = hit.start()
        c = text[i]
        if c == "-" and i + 1 < n and text[i + 1] == "-":
            j = text.find("\n", i)
            out.append(" ")
            i = n if j == -1 else j
            continue
        if c == "/" and i + 1 < n and text[i + 1] == "*":
            depth = 1
            i += 2
            while i < n and depth > 0:
                if text[i : i + 2] == "/*":
                    depth += 1
                    i += 2
                elif text[i : i + 2] == "*/":
                    depth -= 1
                    i += 2
                else:
                    i += 1
            out.append(" ")
            continue
        if c == "'" or (c in "Nn" and i + 1 < n and text[i + 1] == "'"):
            if c in "Nn":
                i += 1
            i += 1  # opening quote
            while i < n:
                if text[i] == "'" and i + 1 < n and text[i + 1] == "'":
                    i += 2
                    continue
                if text[i] == "'":
                    i += 1
                    break
                i += 1
            out.append(" ")
            continue
        if c == "[":
            j = i + 1
            body: list[str] = []
            while j < n:
                if text[j] == "]" and j + 1 < n and text[j + 1] == "]":
                    body.append("]")
                    j += 2
                    continue
                if text[j] == "]":
                    j += 1
                    break
                body.append(text[j])
                j += 1
            out.append("".join(body))
            i = j
            continue
        if c == '"':
            j = i + 1
            body = []
            while j < n:
                if text[j] == '"' and j + 1 < n and text[j + 1] == '"':
                    body.append('"')
                    j += 2
                    continue
                if text[j] == '"':
                    j += 1
                    break
                body.append(text[j])
                j += 1
            out.append("".join(body))
            i = j
            continue
        out.append(c)
        i += 1
    return "".join(out)


def _has_recursive_cte(text: str) -> bool:
    """A CTE whose own body both ``UNION``s and refers back to its own name.

    SQL Server and Oracle have no ``WITH RECURSIVE`` keyword (unlike PostgreSQL and
    MySQL 8): any ``name AS (...)`` CTE can be recursive if its body unions the anchor
    member with one that references ``name`` again -- checked for every CTE in the
    ``WITH`` list, not only the first (#381 review). Requiring both a self-reference and
    a ``UNION`` inside the CTE's own (paren-balanced) body avoids counting a CTE that is
    merely referenced again later in the main query -- normal, non-recursive CTE use.
    """
    lower = text.lower()
    if "recursive" in lower and re.search(r"\bwith\s+recursive\b", text, re.IGNORECASE):
        return True
    if "union" not in lower:  # a self-referencing CTE body must UNION its anchor member
        return False
    for m in _CTE_NAME_RE.finditer(text):
        name = m.group(1)
        depth = 1
        i = m.end()
        start = i
        while i < len(text) and depth > 0:
            if text[i] == "(":
                depth += 1
            elif text[i] == ")":
                depth -= 1
            i += 1
        body = text[start:i]
        if re.search(r"\bunion\b", body, re.IGNORECASE) and re.search(
            rf"\b{re.escape(name)}\b", body, re.IGNORECASE
        ):
            return True
    return False


_OVER_RE = re.compile(r"\bover\s*\(", re.IGNORECASE)


def _without_windows(text: str) -> str:
    """``text`` with each ``OVER (...)`` window blanked (balanced parentheses)."""
    out, i = [], 0
    for match in _OVER_RE.finditer(text):
        if match.start() < i:
            continue
        out.append(text[i : match.start()])
        depth, j = 1, match.end()
        while j < len(text) and depth:
            depth += {"(": 1, ")": -1}.get(text[j], 0)
            j += 1
        out.append(" ")
        i = j
    out.append(text[i:])
    return "".join(out)


def regex_order_by(query_text: str) -> bool:
    """``ORDER BY``, not one inside a window's ``OVER (...)``."""
    return bool(ORDER_BY_RE.search(_without_windows(query_text or "")))


def regex_row_limit(query_text: str) -> bool:
    """``LIMIT``, ``FETCH FIRST``/``NEXT``, or ``TOP`` with a count."""
    return bool(_ROW_LIMIT_RE.search(query_text or ""))


def regex_top_n(query_text: str) -> bool:
    """``ORDER BY ... LIMIT``/``FETCH``, or ``TOP (n) ... ORDER BY``, not counting an
    ``ORDER BY`` inside a window's ``OVER (...)``."""
    return bool(_TOP_N_RE.search(_without_windows(query_text or "")))


def regex_timestamp_column(query_text: str) -> bool:
    """A timestamp column name as a word."""
    return bool(_TIMESTAMP_COLUMN_RE.search(query_text or ""))


def regex_json_access(query_text: str) -> bool:
    """A JSON function, operator, cast or containment."""
    return bool(_JSON_ACCESS_RE.search(query_text or ""))


_UPDATE_RE = re.compile(r"\bupdate\b", re.IGNORECASE)
_SET_RE = re.compile(r"\bset\b", re.IGNORECASE)
_JOIN_RE = re.compile(r"\bjoin\b", re.IGNORECASE)
_FROM_RE = re.compile(r"\bfrom\b", re.IGNORECASE)


def regex_update_target_join(query_text: str) -> bool:
    """``UPDATE ... JOIN ... SET``: a ``JOIN`` before the first ``SET``, no ``FROM``."""
    text = query_text or ""
    update = _UPDATE_RE.search(text)
    if update is None:
        return False
    set_ = _SET_RE.search(text, update.end())
    if set_ is None:
        return False
    head = text[update.end() : set_.start()]
    return bool(_JOIN_RE.search(head)) and not _FROM_RE.search(head)
