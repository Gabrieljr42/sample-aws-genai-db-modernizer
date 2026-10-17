"""Linear-time scanning of SQL comments, literals and quoted identifiers (#886).

The regexes these replace (``'(?:[^'\\]|\\.|'')*'``, ``\\[[^\\]]*\\]`` and
friends) retry from every opener when the closer is missing, so a long run of
unterminated openers (``'\\'\\'...``, ``[[[...``) rescanned the rest of the text
once per opener: quadratic. :func:`spans` walks the text once, left to right,
and answers exactly what those alternations did: the span that opens first
wins, and an opener with no closer is plain text. Once one kind of opener has
no closer left, every later opener of that kind is skipped without a search
(a closer after a later opener would also be a closer after the earlier one),
so each character is looked at a bounded number of times.

Two grammars are provided, one per old regex:

- :data:`LITERALS_AND_COMMENTS`: ``/* */``, ``--``, ``'...'`` and ``"..."``,
  both with doubled-quote and backslash escapes (``unsupported_pattern``'s
  former ``_STRIP_RE``);
- :data:`QUOTED_IDENTIFIERS`: ``"..."`` (doubled-quote escape only),
  `` `...` `` and ``[...]``, with no comment or literal awareness
  (``query_shape``'s former ``_QUOTED_IDENT_RE``).
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterator
from dataclasses import dataclass

__all__ = [
    "COMMENTS_LITERALS_AND_IDENTIFIERS",
    "LITERALS_AND_COMMENTS",
    "QUOTED_IDENTIFIERS",
    "Grammar",
    "quoted_span_end",
    "spans",
    "sub_spans",
]

_STOPS = {
    (quote, backslash): re.compile(f"[{quote}\\\\]" if backslash else quote)
    for quote in ("'", '"')
    for backslash in (True, False)
}


def quoted_span_end(text: str, start: int, quote: str, backslash: bool) -> int:
    """End (exclusive) of the ``quote``-quoted span opening at ``start``, or -1.

    A doubled quote is an escaped quote, and with ``backslash`` so is ``\\``
    followed by any character (newline included). Same answer as the regex
    ``'(?:[^'\\]|\\.|'')*'`` (without the backslash parts when ``backslash`` is
    false): when the text ends inside the span, the span closes at the first
    quote of its last doubled pair, as the regex does when it backtracks; with
    no pair it is unterminated.
    """
    stops = _STOPS[quote, backslash]
    k = start + 1
    last_pair = -1
    while True:
        stop = stops.search(text, k)
        if stop is None:
            return last_pair + 1 if last_pair >= 0 else -1
        pos = stop.start()
        if text[pos] == "\\":
            if pos + 1 >= len(text):
                return last_pair + 1 if last_pair >= 0 else -1
            k = pos + 2
        elif text.startswith(quote * 2, pos):
            last_pair = pos
            k = pos + 2
        else:
            return pos + 1


@dataclass(frozen=True)
class Grammar:
    """The openers a scan looks for, and how each one's span ends."""

    opener: re.Pattern[str]
    end: Callable[[str, int, str], int]


def _end_literals_and_comments(text: str, start: int, opener: str) -> int:
    if opener == "--":
        newline = text.find("\n", start)
        return len(text) if newline < 0 else newline
    if opener == "/*":
        found = text.find("*/", start + 2)
        return found + 2 if found >= 0 else -1
    return quoted_span_end(text, start, opener, backslash=True)


def _end_quoted_identifiers(text: str, start: int, opener: str) -> int:
    if opener == "[":
        found = text.find("]", start + 1)
        return found + 1 if found >= 0 else -1
    if opener == "`":
        found = text.find("`", start + 1)
        return found + 1 if found >= 0 else -1
    return quoted_span_end(text, start, opener, backslash=False)


def _end_comments_literals_and_identifiers(text: str, start: int, opener: str) -> int:
    if opener in ("--", "/*"):
        return _end_literals_and_comments(text, start, opener)
    if opener in ("`", "["):
        return _end_quoted_identifiers(text, start, opener)
    return quoted_span_end(text, start, opener, backslash=opener == "'")


LITERALS_AND_COMMENTS = Grammar(re.compile(r"/\*|--|['\"]"), _end_literals_and_comments)
QUOTED_IDENTIFIERS = Grammar(re.compile(r"[\"`\[]"), _end_quoted_identifiers)
# All of them in one scan, the span that opens first winning: ``/* */`` and ``--``
# comments, ``'...'`` literals with backslash escapes, ``"..."`` identifiers without
# (``query_shape._normalized_sql``, #871).
COMMENTS_LITERALS_AND_IDENTIFIERS = Grammar(
    re.compile(r"/\*|--|['\"`\[]"), _end_comments_literals_and_identifiers
)


def spans(text: str, grammar: Grammar) -> Iterator[tuple[int, int, str]]:
    """Every ``(start, end, opener)`` span of ``text``, left to right, in linear time."""
    unclosed: set[str] = set()
    i = 0
    while (match := grammar.opener.search(text, i)) is not None:
        start, opener = match.start(), match.group()
        end = -1
        if opener not in unclosed:
            end = grammar.end(text, start, opener)
            if end < 0:
                unclosed.add(opener)
        if end < 0:
            i = start + 1
            continue
        yield start, end, opener
        i = end


def sub_spans(text: str, grammar: Grammar, repl: Callable[[str], str]) -> str:
    """``text`` with each span replaced by ``repl(span_text)``."""
    out: list[str] = []
    last = 0
    for start, end, _ in spans(text, grammar):
        out.append(text[last:start])
        out.append(repl(text[start:end]))
        last = end
    out.append(text[last:])
    return "".join(out)
