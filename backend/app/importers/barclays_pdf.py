"""Barclays PDF statement parser.

Barclays statement PDFs embed subset fonts whose glyph->char maps are
re-scrambled for every document, so pdfplumber's text extraction is a
per-font substitution cipher. The layout is stable (columns, sections, sign
conventions); only the character maps drift.

The parser is layered:

- *extraction* (:func:`_extract`) turns the PDF into per-font word streams with
  positions, restoring the capital-B glyph that extracts as a newline;
- *derivation* (:mod:`.pdf_cipher`) self-calibrates the body and bold ciphers
  per document from constant anchor text, then completes any digit glyphs the
  anchors miss using the statement's own running balance as the oracle;
- *layout parsing* (:func:`_evaluate`) decodes rows positionally with a given
  map and reconciles them.

Transaction rows are set entirely in the body font; the bold header row locates
the Money out / Money in / Balance columns; amounts extract as a pounds word
plus a pence word ("2,200" "00") because the decimal point extracts as a space.

Every parse is validated against the statement's own "At a glance" totals and a
row-by-row running-balance ledger. If the decode drifts, the import fails loudly
(``StatementDecodeError``) instead of writing garbage. A carried hardcoded map
(from the original reference statement) is tried first as a cheap fast path, but
is never trusted without the same reconciliation passing.
"""

import io
import re
from datetime import date
from decimal import Decimal

from .base import ParsedRow, normalize_whitespace
from .pdf_cipher import (
    NEWLINE_SENTINEL,
    StatementDecodeError,
    apply_map,
    base_font,
    derive_substitution,
    solve_numeric,
)

BODY_FONT = "Expert-Sans-Regular"
HEADER_FONT = "Expert-Sans-RegularBold"

# Hardcoded body cipher from the original reference statement. Used only as a
# fast-path hypothesis; a re-scrambled statement falls back to derivation. The
# capital-B glyph extracts as a newline that extraction rewrites to the
# private-use sentinel, so it maps from the sentinel here. V/X/Z were unseen.
BODY_MAP: dict[str, str] = {
    "†": "a", "š": "b", "‡": "c", "á": "d", "\x8f": "e", "ç": "f", "å": "g",
    "ñ": "h", "â": "i", "ß": "j", "Œ": "k", "ˆ": "l", "\x90": "m", "‹": "n",
    "\x7f": "o", "ã": "p", "ï": "q", "„": "r", "Š": "s", "Ž": "t", "ƒ": "u",
    ".": "v", "!": "w", ")": "x", "‰": "y", "$": "z",
    "¸": "A", NEWLINE_SENTINEL: "B", "‘": "C", "(": "D", "ë": "E", "¢": "F",
    "<": "G", "|": "H", "˜": "I", "”": "J", "Á": "K", "è": "L", "\x8d": "M",
    "Ç": "N", "+": "O", "î": "P", "í": "Q", "&": "R", "™": "S", "ì": "T",
    "À": "U", "*": "W", "\x81": "Y",
    "’": "0", "–": "1", "“": "2", "ž": "3", "œ": "4", "à": "5", "—": "6",
    "Ÿ": "7", "ê": "8", "ä": "9",
    "\x9d": ",", "›": "£", "·": "-", "é": ":", "¬": "/", "-": "'", ";": "&",
}
_BODY_TRANS = str.maketrans(BODY_MAP)

# Constant text set in the body font on every statement; the crib for deriving
# the per-document body cipher. Rich in letters (incl. the lowercase month
# letters and the 'v' from "Services") but carries no digits, so digit glyphs
# are solved afterwards from the running balance.
BODY_ANCHORS = [
    "Your Barclays Bank Account statement",
    "Start balance",
    "End balance",
    "Money out",
    "Money in",
    "Statement date",
    "Last statement",
    "Anything Wrong",
    "Prudential Regulation Authority",
    "Financial Services Register number",
    "Authorised by the",
    # Barclays Bank UK PLC's Financial Services Register number is a constant on
    # every statement; it anchors four digit glyphs (7,5,9,6), so the arithmetic
    # solver is left with only a handful. Matched by its repeated-digit pattern
    # and rejected by consistency if it lands anywhere but the real number.
    "759676",
]

# The bold header row "Date Description Money out Money in Balance" is constant
# and locates the amount columns; its font carries an independent cipher.
BOLD_ANCHORS = ["Date Description Money out Money in Balance"]

# The lowercase two-letter tail of a month abbreviation is unique across all
# twelve months, so the capital initial (which may be an unmapped glyph) is not
# needed to identify the month.
MONTH_TAILS = {
    "an": 1, "eb": 2, "ar": 3, "pr": 4, "ay": 5, "un": 6,
    "ul": 7, "ug": 8, "ep": 9, "ct": 10, "ov": 11, "ec": 12,
}

POUNDS = re.compile(r"^\d{1,3}(,\d{3})*$|^\d+$")
PENCE = re.compile(r"^\d{2}$")
NUMERIC_ISH = re.compile(r"^[\d?,]+$")
# Separator between the two dates may be an unresolved glyph, so accept any.
PERIOD = re.compile(r"(\d{2}) (\S{3}) \S (\d{2}) (\S{3}) (\d{4})")
PAGE_NO = re.compile(r"\s*Page \d+$")


def _decode(word: str) -> str:
    """Decode with the hardcoded reference cipher (used by tests)."""
    return word.translate(_BODY_TRANS)


def _base_font(fontname: str) -> str:
    return base_font(fontname)


def _month_from_tail(token: str) -> int | None:
    """Month number from a decoded 3-char abbreviation via its lowercase tail;
    tolerant of an unresolved capital initial ("?un" -> 6)."""
    tail = "".join(c for c in token if c.islower())
    return MONTH_TAILS.get(tail[:2]) if len(tail) >= 2 else None


def _year_for(month: int, period: tuple[int, int, int]) -> int:
    """Statement periods span at most two months; the year printed belongs to
    the period end. A December row on a Dec->Jan statement is the prior year."""
    start_month, end_month, end_year = period
    if start_month > end_month and month >= start_month:
        return end_year - 1
    return end_year


# ---------------------------------------------------------------------------
# Extraction: PDF -> per-font word streams with positions
# ---------------------------------------------------------------------------

def _extract(data: bytes) -> list[dict]:
    """Open the PDF and return one record per page with body/bold word streams.

    The capital-B glyph extracts as "\\n", which word grouping would swallow
    ("Bakshi" -> "akshi"); it is rewritten to a non-whitespace sentinel before
    words are built so the glyph survives and the cipher can learn it.
    """
    import pdfplumber
    from pdfplumber.utils.text import extract_words

    pages: list[dict] = []
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        for page in pdf.pages:
            chars = [
                {**c, "text": NEWLINE_SENTINEL}
                if c["text"] == "\n" and base_font(c["fontname"]) == BODY_FONT
                else c
                for c in page.chars
            ]
            words = extract_words(chars, extra_attrs=["fontname"])
            pages.append(
                {
                    "body": [w for w in words if base_font(w["fontname"]) == BODY_FONT],
                    "bold": [w for w in words if base_font(w["fontname"]) == HEADER_FONT],
                }
            )
    return pages


# ---------------------------------------------------------------------------
# Structuring: locate columns and group rows (map-independent)
# ---------------------------------------------------------------------------

def _columns(bold_words: list[dict], bold_map: dict[str, str]):
    """Locate the Money out / Money in / Balance column centres from the decoded
    bold header row. Returns (col_out, col_in, col_bal, header_y) or None."""
    ordered = sorted(bold_words, key=lambda w: w["x0"])
    decoded = [apply_map(w["text"], bold_map) for w in ordered]
    col_out = col_in = col_bal = header_y = None
    for i, text in enumerate(decoded):
        if text == "Money" and i + 1 < len(decoded):
            nxt, nw, w = decoded[i + 1], ordered[i + 1], ordered[i]
            if nxt == "out":
                col_out = (w["x0"] + nw["x1"]) / 2
            elif nxt == "in":
                col_in = (w["x0"] + nw["x1"]) / 2
        elif text == "Balance":
            col_bal = (ordered[i]["x0"] + ordered[i]["x1"]) / 2
            header_y = ordered[i]["top"]
    if None in (col_out, col_in, col_bal, header_y):
        return None
    return col_out, col_in, col_bal, header_y


def _structure(pages: list[dict], bold_map: dict[str, str]) -> list[dict]:
    """Per page: column geometry (from bold) and body rows grouped into lines.

    Grouping depends only on positions, so it is done once and reused across
    every candidate cipher during digit solving.
    """
    structured: list[dict] = []
    for page in pages:
        cols = _columns(page["bold"], bold_map)
        if cols is None:
            continue  # not a transaction page
        col_out, col_in, col_bal, header_y = cols
        table = [w for w in page["body"] if w["top"] > header_y + 2]
        lines: list[list[dict]] = []
        for w in sorted(table, key=lambda w: (round(w["top"]), w["x0"])):
            if lines and abs(w["top"] - lines[-1][0]["top"]) <= 2.5:
                lines[-1].append(w)
            else:
                lines.append([w])
        structured.append(
            {
                "cols": cols,
                "lines": lines,
                "body": page["body"],
                "amount_zone": min(col_out, col_in) - 40,
            }
        )
    return structured


def _numeric_glyphs(structured: list[dict], body_map: dict[str, str]) -> set[str]:
    """Glyphs that occur in amount/date positions but are not yet mapped.

    These are the digit glyphs the letter anchors could not cover; only they are
    handed to the arithmetic solver, so unresolved *description* letters never
    get mistaken for digits.
    """
    glyphs: set[str] = set()
    for page in structured:
        zone = page["amount_zone"]
        for line in page["lines"]:
            for i, w in enumerate(line):
                decoded = apply_map(w["text"], body_map)
                in_amount = w["x0"] >= zone and NUMERIC_ISH.fullmatch(decoded)
                # leading day-of-month token on a dated row
                is_day = i == 0 and len(w["text"]) == 2 and NUMERIC_ISH.fullmatch(decoded)
                if in_amount or is_day:
                    glyphs |= {c for c in w["text"] if c not in body_map}
    return glyphs


# ---------------------------------------------------------------------------
# Layout parsing: decode rows with a given map and reconcile
# ---------------------------------------------------------------------------

def _has_unknown(text: str) -> bool:
    return "?" in text


def _evaluate(structured: list[dict], pages: list[dict], body_map: dict[str, str]) -> list[ParsedRow]:
    """Decode every row with ``body_map`` and reconcile against the statement's
    own totals and running balance. Raises ``StatementDecodeError`` on any
    mismatch or on an unresolved glyph inside an amount or date."""
    rows: list[ParsedRow] = []
    totals = {"in": Decimal(0), "out": Decimal(0)}
    glance: dict[str, Decimal] = {}
    period: tuple[int, int, int] | None = None
    running: Decimal | None = None
    balance_errors: list[str] = []

    for page in pages:
        page_text = " ".join(apply_map(w["text"], body_map) for w in page["body"])
        if period is None:
            period = _period_from(page_text)
        _collect_glance(page_text, glance)

    for page in structured:
        col_out, col_in, col_bal, _ = page["cols"]
        amount_zone = page["amount_zone"]
        current_date: date | None = None

        for line in page["lines"]:
            decoded = [(apply_map(w["text"], body_map), w) for w in line]
            texts = [t for t, _ in decoded]

            start = 0
            if len(texts) >= 2 and _looks_dated(texts[0], texts[1]):
                if _has_unknown(texts[0]):
                    raise StatementDecodeError(f"unresolved glyph in date {texts[0]!r}")
                month = _month_from_tail(texts[1])
                if month is None:
                    raise StatementDecodeError(f"unresolved month {texts[1]!r}")
                if period is None:
                    raise StatementDecodeError("Transaction row before statement period was found")
                try:
                    current_date = date(_year_for(month, period), month, int(texts[0]))
                except ValueError as e:
                    raise StatementDecodeError(f"invalid date {texts[0]!r} {texts[1]!r}: {e}")
                start = 2

            desc_words: list[str] = []
            amounts: list[tuple[Decimal, float]] = []
            i = start
            while i < len(decoded):
                text, w = decoded[i]
                if (
                    w["x0"] >= amount_zone
                    and POUNDS.fullmatch(text)
                    and i + 1 < len(decoded)
                    and PENCE.fullmatch(decoded[i + 1][0])
                ):
                    value = Decimal(text.replace(",", "") + "." + decoded[i + 1][0])
                    center = (w["x0"] + decoded[i + 1][1]["x1"]) / 2
                    amounts.append((value, center))
                    i += 2
                    continue
                # An amount-zone token that looks numeric but did not parse (an
                # unresolved digit glyph) must not silently fall into the
                # description and skew nothing -- refuse loudly.
                if w["x0"] >= amount_zone and NUMERIC_ISH.fullmatch(text) and _has_unknown(text):
                    raise StatementDecodeError(f"unresolved glyph in amount {text!r}")
                desc_words.append(text)
                i += 1

            desc = PAGE_NO.sub("", normalize_whitespace(" ".join(desc_words)))

            if not desc and not amounts:
                continue
            if desc.startswith(("Anything Wrong", "If you've spotted")):
                break
            if desc.startswith(("Start balance", "End balance", "Continued")):
                for value, center in amounts:
                    if abs(center - col_bal) < abs(center - col_out) and running is None:
                        running = value
                    elif desc.startswith("End balance") and running is not None and value != running:
                        balance_errors.append(f"end balance printed {value}, ledger says {running}")
                continue
            if desc.startswith("Ref:") and rows:
                rows[-1].description += f" ({desc})"
                continue

            tx_amounts = []
            printed_balance = None
            for value, center in amounts:
                column = min(
                    (("out", col_out), ("in", col_in), ("bal", col_bal)),
                    key=lambda c: abs(center - c[1]),
                )[0]
                if column in ("out", "in"):
                    tx_amounts.append((value, column))
                else:
                    printed_balance = value

            if tx_amounts and current_date:
                value, column = tx_amounts[0]
                totals[column] += value
                signed = -value if column == "out" else value
                rows.append(
                    ParsedRow(date=current_date, description=desc or "Barclays transaction", amount=signed)
                )
                if running is not None:
                    running += signed
                    if printed_balance is not None and printed_balance != running:
                        balance_errors.append(
                            f"{current_date} {desc[:40]!r}: printed balance "
                            f"{printed_balance}, ledger says {running}"
                        )
                        running = printed_balance
            elif desc and rows and not amounts:
                rows[-1].description = normalize_whitespace(rows[-1].description + " " + desc)

    if balance_errors:
        raise StatementDecodeError(
            "Running-balance check failed — not importing: " + "; ".join(balance_errors[:5])
        )
    _validate(rows, totals, glance)
    return rows


def _looks_dated(day: str, month: str) -> bool:
    return bool(re.fullmatch(r"[\d?]{2}", day)) and (
        _month_from_tail(month) is not None or (len(month) == 3 and "?" in month)
    )


def _period_from(page_text: str) -> tuple[int, int, int] | None:
    m = PERIOD.search(page_text)
    if not m:
        return None
    start = _month_from_tail(m.group(2))
    end = _month_from_tail(m.group(4))
    if start and end:
        return (start, end, int(m.group(5)))
    return None


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------

def parse_pdf(data: bytes) -> list[ParsedRow]:
    pages = _extract(data)
    return _parse(pages)


def _parse(pages: list[dict]) -> list[ParsedRow]:
    # Bold cipher (column geometry only) is always derived from its constant
    # header row -- cheap and layout-critical.
    bold_stream = [
        w["text"] for page in pages for w in sorted(page["bold"], key=lambda w: (round(w["top"]), w["x0"]))
    ]
    bold_map = derive_substitution(bold_stream, BOLD_ANCHORS)
    structured = _structure(pages, bold_map)
    if not structured:
        raise StatementDecodeError("No transaction table found — is this a Barclays statement PDF?")

    # Fast path: the carried hardcoded cipher, trusted only if it reconciles.
    try:
        return _evaluate(structured, pages, BODY_MAP)
    except StatementDecodeError:
        pass

    # Auto-derivation: letters from anchors, then digits from the running balance.
    body_stream = [
        w["text"] for page in pages for w in sorted(page["body"], key=lambda w: (round(w["top"]), w["x0"]))
    ]
    body_map = derive_substitution(body_stream, BODY_ANCHORS)
    _resolve_comma(structured, body_map)  # so the thousands separator isn't taken for a digit
    unknown = sorted(_numeric_glyphs(structured, body_map))

    def reconciles(candidate: dict[str, str]) -> bool:
        try:
            _evaluate(structured, pages, candidate)
            return True
        except StatementDecodeError:
            return False

    solved = solve_numeric(body_map, unknown, reconciles)
    if solved is None:
        raise StatementDecodeError(
            "Could not reconcile the statement after auto-deriving the font cipher — "
            "not importing (too few anchors, or the layout changed)."
        )
    return _evaluate(structured, pages, solved)


def _resolve_comma(structured: list[dict], body_map: dict[str, str]) -> None:
    """Identify the thousands-separator glyph structurally and add it to the map.

    Barclays prints amounts >= £1000 as "d,ddd", so in any amount-zone pounds
    word of length >= 5 whose other characters are digit-or-unmapped, the glyph
    four places from the right is the comma. Mapping it first keeps the digit
    solver from mistaking it for a digit.
    """
    from collections import Counter

    def digit_or_unknown(ch: str) -> bool:
        return ch not in body_map or body_map[ch].isdigit()

    votes: Counter = Counter()
    for page in structured:
        zone = page["amount_zone"]
        for line in page["lines"]:
            for w in line:
                t = w["text"]
                if w["x0"] < zone or len(t) < 5 or len(t) % 4 != 1:
                    continue  # d,ddd -> 5, d,ddd,ddd -> 9, ...
                if all(digit_or_unknown(c) for j, c in enumerate(t) if (len(t) - 1 - j) % 4 != 0):
                    sep = {t[len(t) - 1 - k] for k in range(3, len(t), 4)}
                    if len(sep) == 1:
                        g = sep.pop()
                        if g not in body_map:
                            votes[g] += 1
    if votes:
        comma = votes.most_common(1)[0][0]
        if "," not in body_map.values():
            body_map[comma] = ","


def _collect_glance(page_text: str, glance: dict) -> None:
    # The currency glyph may be unresolved in the auto-derived path; accept an
    # optional single non-digit char (£ or ?) before the amount.
    for label, key in (("Money in", "in"), ("Money out", "out")):
        m = re.search(rf"{label}\s+[^\d\s]?([\d,]+)\s+(\d{{2}})", page_text)
        if m and key not in glance:
            glance[key] = Decimal(m.group(1).replace(",", "") + "." + m.group(2))


def _validate(rows: list[ParsedRow], totals: dict, glance: dict) -> None:
    if not rows:
        raise StatementDecodeError("No transactions decoded — is this a Barclays statement PDF?")
    problems = [
        f"money {key}: statement says £{glance[key]}, parsed £{totals[key]}"
        for key in ("in", "out")
        if key in glance and totals[key] != glance[key]
    ]
    if problems:
        raise StatementDecodeError(
            "Decoded transactions do not reconcile with the statement's own totals "
            f"({'; '.join(problems)}). The font mapping may have changed — not importing."
        )
