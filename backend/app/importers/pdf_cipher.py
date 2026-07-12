"""Per-document cipher auto-derivation for scrambled-font bank statements.

Barclays and Barclaycard statement PDFs embed subset fonts whose glyph->char
maps are re-scrambled for every document, so pdfplumber's text extraction is a
per-font substitution cipher. Hardcoded maps (one per reference statement) break
the moment the bank re-issues the font subset.

This module solves each font's substitution *from the document itself*,
cryptogram-style, using text that is constant on every statement as a crib:

1.  Anchor matching -- a known plaintext phrase and a window of scrambled words
    that could encode it must share a *word-length signature* and a
    *repeated-letter pattern* (the "isomorph"); both survive any 1:1
    substitution. Unique, globally-consistent matches fix glyph->char pairs.
2.  The parser then completes any digit glyphs the anchors did not cover by
    brute force, using the statement's own arithmetic (running balance / section
    totals) as the oracle -- see ``solve_numeric``.

The layer is deliberately PDF-free: it operates on plain lists of word records
(``dict`` with ``text``/``x0``/``x1``/``top``/``fontname``) so it can be unit
tested by applying random ciphers to synthetic word streams, with no real PDF.

Nothing here is *trusted*: the calling parser always reconciles the decoded
figures against the statement's printed totals and raises
``StatementDecodeError`` on any mismatch.
"""

from __future__ import annotations

from itertools import permutations
from typing import Callable, Iterable, Sequence


class StatementDecodeError(Exception):
    """Raised when a statement cannot be decoded/reconciled (public contract)."""


# A scrambled glyph that pdfplumber renders as a control/whitespace char would
# be eaten by word grouping. Extraction replaces such glyphs with a private-use
# sentinel so the glyph identity survives into the word stream; the cipher then
# learns the sentinel like any other glyph.
NEWLINE_SENTINEL = ""

UNKNOWN = "?"


def base_font(fontname: str) -> str:
    """pdfplumber prefixes subset fonts with 'ABCDEF+'; drop the tag."""
    return fontname.split("+", 1)[-1]


def isomorph(text: str) -> tuple[int, ...]:
    """Canonical repeated-symbol pattern, e.g. 'balance' -> (0,1,2,1,3,4,5).

    Two strings related by a 1:1 substitution always share their isomorph, so a
    mismatch rules a candidate out and a match makes it plausible.
    """
    order: dict[str, int] = {}
    return tuple(order.setdefault(ch, len(order)) for ch in text)


def apply_map(text: str, glyph_map: dict[str, str], unknown: str = UNKNOWN) -> str:
    """Decode a scrambled word; unmapped glyphs become ``unknown``.

    Whitespace is passed through so multi-word tokens keep their spaces.
    """
    return "".join(
        ch if ch.isspace() else glyph_map.get(ch, unknown) for ch in text
    )


def _extend(g2c: dict[str, str], c2g: dict[str, str], scrambled: str, plain: str):
    """Return the new glyph->char pairs needed to align ``scrambled``->``plain``
    with the map so far, or ``None`` if that alignment is inconsistent (a glyph
    would need two chars, or two glyphs would claim one char)."""
    new: dict[str, str] = {}
    for g, c in zip(scrambled, plain):
        if g.isspace() or c.isspace():
            if g != c:
                return None
            continue
        want = g2c.get(g, new.get(g))
        if want is not None:
            if want != c:
                return None
        elif c in c2g or c in new.values():
            return None
        else:
            new[g] = c
    return new


def _anchor_windows(words: Sequence[str], anchor: str) -> list[tuple[str, str]]:
    """All windows of consecutive words that could encode ``anchor`` (matching
    per-word length + combined isomorph). Returns (scrambled, plain) pairs."""
    plain_words = anchor.split(" ")
    n = len(plain_words)
    lengths = [len(p) for p in plain_words]
    plain = "".join(plain_words)
    target = isomorph(plain)
    hits: list[tuple[str, str]] = []
    for i in range(len(words) - n + 1):
        window = words[i : i + n]
        if [len(w) for w in window] != lengths:
            continue
        scrambled = "".join(window)
        if isomorph(scrambled) == target:
            hits.append((scrambled, plain))
    return hits


def derive_substitution(
    words: Sequence[str], anchors: Iterable[str]
) -> dict[str, str]:
    """Derive a (partial) glyph->char map for one font from constant anchors.

    ``words`` is the font's scrambled word stream in reading order. ``anchors``
    are known plaintext phrases. Anchors are applied when they have exactly one
    window consistent with the map built so far; this repeats to a fixed point so
    earlier anchors disambiguate later ones. Ambiguous or absent anchors are
    simply skipped -- the caller validates coverage downstream.
    """
    g2c: dict[str, str] = {}
    c2g: dict[str, str] = {}
    candidates = {a: _anchor_windows(words, a) for a in anchors}
    pending = [a for a, wins in candidates.items() if wins]

    progress = True
    while progress:
        progress = False
        for anchor in list(pending):
            consistent = []
            for scrambled, plain in candidates[anchor]:
                add = _extend(g2c, c2g, scrambled, plain)
                if add is not None:
                    consistent.append(add)
            if len(consistent) == 1:
                for g, c in consistent[0].items():
                    g2c[g] = c
                    c2g[c] = g
                pending.remove(anchor)
                progress = True
            elif not consistent:
                pending.remove(anchor)
    return g2c


def unmapped_glyphs(words: Iterable[str], glyph_map: dict[str, str]) -> set[str]:
    """Glyphs present in ``words`` that the map does not yet cover."""
    seen = {ch for w in words for ch in w if not ch.isspace()}
    return seen - glyph_map.keys()


def resolve_separators(tokens: Iterable[str], glyph_map: dict[str, str]) -> None:
    """Add the decimal-point and thousands-comma glyphs to ``glyph_map`` from the
    structure of amount tokens (e.g. "£1,234.56"), so they are not later
    mistaken for digits.

    The pence separator is the glyph three places from the right whenever the two
    trailing characters are digit-or-unmapped; the thousands comma is the glyph
    four places to the left of the decimal. Both are decided by majority vote and
    added only if that character role is still unassigned.
    """
    from collections import Counter

    def digit_or_unknown(ch: str) -> bool:
        return ch not in glyph_map or glyph_map[ch].isdigit()

    tokens = list(tokens)
    dec_votes: Counter = Counter()
    for t in tokens:
        if len(t) >= 3 and digit_or_unknown(t[-1]) and digit_or_unknown(t[-2]):
            g = t[-3]
            if g not in glyph_map and not g.isspace():
                dec_votes[g] += 1
    if not dec_votes:
        return
    decimal = dec_votes.most_common(1)[0][0]
    if "." not in glyph_map.values():
        glyph_map[decimal] = "."

    # Thousands comma: four places left of the decimal, but only when a digit
    # actually precedes it (>= 5 chars before the point), so a 3-digit amount's
    # leading currency glyph is not mistaken for a comma.
    com_votes: Counter = Counter()
    for t in tokens:
        p = t.rfind(decimal)
        if p >= 5:
            g = t[p - 4]
            if g not in glyph_map and all(digit_or_unknown(x) for x in t[:p] if x != g):
                com_votes[g] += 1
    if com_votes:
        comma = com_votes.most_common(1)[0][0]
        if "," not in glyph_map.values() and comma not in glyph_map:
            glyph_map[comma] = ","

    # Currency glyph: an unmapped glyph that only ever appears as the first
    # character of an amount token (digits also appear in interior positions).
    leading: set[str] = set()
    interior: set[str] = set()
    for t in tokens:
        for i, ch in enumerate(t):
            if ch in glyph_map or ch.isspace():
                continue
            (leading if i == 0 else interior).add(ch)
    for g in leading - interior:
        if "£" not in glyph_map.values():
            glyph_map[g] = "£"
            break


def _perm_count(n: int, k: int) -> int:
    total = 1
    for i in range(k):
        total *= n - i
    return total


def solve_numeric(
    base_map: dict[str, str],
    unknown_glyphs: Sequence[str],
    reconcile: Callable[[dict[str, str]], bool],
    *,
    digits: str = "0123456789",
    max_perms: int = 50_000,
) -> dict[str, str] | None:
    """Complete digit glyphs the anchors missed, using arithmetic as the oracle.

    ``unknown_glyphs`` are glyphs seen in numeric positions but not in
    ``base_map``. Every digit already mapped is excluded; the rest are assigned
    to the unknown glyphs by brute force, and ``reconcile`` (the parser's totals/
    balance check) selects the assignment that makes the statement add up.

    Returns the completed map, or ``None`` if nothing reconciles. Raises when the
    remaining search space is too large to trust -- a statement with too few
    numeric anchors is refused loudly rather than guessed at.
    """
    if not unknown_glyphs:
        return base_map if reconcile(base_map) else None
    used = {c for c in base_map.values() if c in digits}
    free = [d for d in digits if d not in used]
    if len(free) < len(unknown_glyphs):
        return None
    if _perm_count(len(free), len(unknown_glyphs)) > max_perms:
        raise StatementDecodeError(
            f"too few numeric anchors: {len(unknown_glyphs)} digit glyphs "
            f"undetermined against {len(free)} free digits, cannot solve reliably"
        )
    for combo in permutations(free, len(unknown_glyphs)):
        trial = dict(base_map, **dict(zip(unknown_glyphs, combo)))
        if reconcile(trial):
            return trial
    return None


def iter_numeric_maps(
    base_map: dict[str, str],
    unknown_glyphs: Sequence[str],
    accept: Callable[[dict[str, str]], bool],
    *,
    digits: str = "0123456789",
    max_perms: int = 50_000,
):
    """Yield every digit-completion of ``base_map`` that ``accept`` approves.

    Used when a single arithmetic constraint (e.g. a balance identity) leaves the
    digit glyphs ambiguous, so the caller can disambiguate each candidate against
    a further constraint (e.g. the transaction-row sums).
    """
    if not unknown_glyphs:
        if accept(base_map):
            yield base_map
        return
    used = {c for c in base_map.values() if c in digits}
    free = [d for d in digits if d not in used]
    if len(free) < len(unknown_glyphs):
        return
    if _perm_count(len(free), len(unknown_glyphs)) > max_perms:
        raise StatementDecodeError(
            f"too few numeric anchors: {len(unknown_glyphs)} digit glyphs "
            f"undetermined against {len(free)} free digits, cannot solve reliably"
        )
    for combo in permutations(free, len(unknown_glyphs)):
        trial = dict(base_map, **dict(zip(unknown_glyphs, combo)))
        if accept(trial):
            yield trial
