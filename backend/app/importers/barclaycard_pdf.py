"""Barclaycard PDF statement parser.

Like the Barclays debit statements, Barclaycard PDFs embed subset fonts whose
glyph->char maps are re-scrambled per document. There are two independent
ciphers:

- section headers and totals are set in "Barclaycard-Co";
- transaction rows are set in "Tahoma" (whose lowercase 'y' is a zero-width
  glyph that never reaches the text layer).

The parser is layered like :mod:`.barclays_pdf`: extraction -> per-document
cipher derivation (:mod:`.pdf_cipher`) -> layout parsing. The carried hardcoded
maps from the original reference statement are tried first as a cheap fast path,
then auto-derivation takes over:

- "Co" letters come from the constant section labels; its digits from the two
  constant phone numbers plus the balance identity (which is self-contained in
  the Co font);
- "Tahoma" letters come from the constant "You had no ..." informational rows;
  its digits from the section totals (already read in Co) via the arithmetic
  solver.

There are no Money in/out columns: the SECTION a row sits in fixes its sign.
Validation is arithmetic and strict -- each section's rows must sum to its
total, and previous balance - payments + spending + interest must equal the new
balance. Any mismatch (including a re-scrambled font) refuses the import loudly.
"""

import io
import re
from datetime import date
from decimal import Decimal, InvalidOperation

from .base import ParsedRow, normalize_whitespace
from .pdf_cipher import (
    NEWLINE_SENTINEL,
    StatementDecodeError,
    apply_map,
    base_font,
    derive_substitution,
    iter_numeric_maps,
    resolve_separators,
    solve_numeric,
)

CO_FONT = "Barclaycard-Co"
ROW_FONT = "Tahoma"

# Hardcoded ciphers from the original reference statement, used only as a
# fast-path hypothesis and never trusted without reconciliation.
CO_MAP: dict[str, str] = {
    "ˆ": "a", "¸": "b", "Ž": "c", "‘": "d", "‰": "e", "·": "g", "(": "h",
    "–": "i", "“": "k", "\x8f": "l", "Š": "m", "‹": "n", "\x7f": "o",
    "ä": "p", "„": "r", "†": "s", "‡": "t", "ƒ": "u", "ñ": "v", "Ÿ": "w",
    "\x90": "y",
    "é": "C", "ë": "H", "è": "I", "â": "M", "à": "P", "<": "T", "\x81": "Y",
    "—": "0", "™": "1", "\x9d": "2", "œ": "3", "ê": "4", "š": "5", "å": "6",
    "á": "7", "˜": "8", "›": "9",
    "\x8d": "£", "¢": ",", "’": ".", "+": "'",
}

TAHOMA_MAP: dict[str, str] = {
    "‡": "a", "Š": "b", "‹": "c", "œ": "d", "·": "e", ".": "g", "‰": "h",
    "—": "i", "Œ": "k", "˜": "l", "ž": "m", "™": "n", "‘": "o", "”": "p",
    "Ÿ": "q", "“": "r", "ˆ": "s", "\x9d": "t", "’": "u", "–": "v",
    "†": "C", "å": "D", "\x8d": "J", "ä": "P", "ñ": "R", "á": "T", "\x90": "Y",
    "\x81": "0", "ç": "1", "ƒ": "2", "š": "4", "„": "5", "Ž": "6",
    "\x8f": "7", "›": "8", "â": "9",
    "¸": "£", "à": ",", "\x7f": ".", NEWLINE_SENTINEL: "%", "¢": "*",
}

# Constant text set in the Co font on every statement. The phone numbers give
# broad digit coverage (0,1,2,3,5,8,9); the remaining digits fall out of the
# balance identity.
CO_ANCHORS = [
    "Your transactions",
    "Your previous balance",
    "Payments towards your account",
    "How you've used your card",
    "Promotional transactions",
    "Interest and charges",
    "Your new balance",
    "Minimum payment",
    "0800 151 0900",
    "0333 200 9090",
]

# Constant informational rows set in the Tahoma font. Tahoma's lowercase 'y' is
# zero-width (never reaches the text layer), so it is stripped from the anchor
# plaintext before matching -- "You" is compared as "ou", which also supplies the
# 'u' that the month tails (un/ul/ug) need.
ZERO_WIDTH = "y"
ROW_ANCHORS = [
    "You had no promotional transactions",
    "You had no charges or interest transactions",
    "Cashback earned this month",
    "Your previous Cashback balance",
]

# Month from the lowercase tail (Tahoma 'y' never extracts and some capitals are
# unmapped, so "Jun" decodes to "?un" and "May" to "?a"). Matched longest-first.
MONTH_TAILS = {
    "an": 1, "eb": 2, "ar": 3, "pr": 4, "a": 5, "un": 6,
    "ul": 7, "ug": 8, "ep": 9, "ct": 10, "ov": 11, "ec": 12,
}

MONTH_NAMES = {
    "January": 1, "February": 2, "March": 3, "April": 4, "May": 5, "June": 6,
    "July": 7, "August": 8, "September": 9, "October": 10, "November": 11,
    "December": 12,
}

AMOUNT = re.compile(r"£?([\d?]{1,3}(?:,[\d?]{3})*)\.([\d?]{2})$")
UNKNOWN = "?"
ROW_X_LIMIT = 310


# ---------------------------------------------------------------------------
# Public helpers retained for tests (hardcoded-map based)
# ---------------------------------------------------------------------------

def decode_co(word: str) -> str:
    return apply_map(word, CO_MAP)


def decode_row(word: str) -> str:
    return apply_map(word, TAHOMA_MAP)


def _amount_of(text: str, strict: bool) -> Decimal | None:
    """Parse a decoded amount. In non-strict (legacy hardcoded) mode an unknown
    glyph in a digit position can only be the digit 3 (every other digit is
    mapped in the reference cipher); in strict mode an unresolved glyph makes the
    amount unreadable so the caller refuses it."""
    m = AMOUNT.search(text.replace(" ", ""))
    if m is None:
        return None
    digits = (m.group(1) + "." + m.group(2)).replace(",", "")
    if UNKNOWN in digits:
        if strict:
            return None
        digits = digits.replace(UNKNOWN, "3")
    try:
        return Decimal(digits)
    except InvalidOperation:
        return None


def _amount_from(text: str) -> Decimal | None:
    return _amount_of(text, strict=False)


def _month_from_tail(token: str) -> int | None:
    tail = "".join(c for c in token if c.islower())
    for key in sorted(MONTH_TAILS, key=len, reverse=True):
        if tail.startswith(key):
            return MONTH_TAILS[key]
    return None


def _num(text: str, strict: bool) -> int | None:
    if UNKNOWN in text:
        if strict:
            return None
        text = text.replace(UNKNOWN, "3")
    try:
        return int(text)
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Extraction: PDF -> per-font word streams with positions
# ---------------------------------------------------------------------------

def _extract(data: bytes) -> list[dict]:
    import pdfplumber
    from pdfplumber.utils.text import extract_words

    pages: list[dict] = []
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        for page in pdf.pages:
            chars = [
                {**c, "text": NEWLINE_SENTINEL}
                if c["text"] == "\n" and base_font(c["fontname"]) in (CO_FONT, ROW_FONT)
                else c
                for c in page.chars
            ]
            words = extract_words(chars, extra_attrs=["fontname"])
            pages.append(
                {
                    "co": [w for w in words if base_font(w["fontname"]) == CO_FONT],
                    "row": [
                        w
                        for w in words
                        if base_font(w["fontname"]) == ROW_FONT and w["x0"] < ROW_X_LIMIT
                    ],
                }
            )
    return pages


# ---------------------------------------------------------------------------
# Co-font reading: statement date, section anchors, transaction page
# ---------------------------------------------------------------------------

def _read_co(pages: list[dict], co_map: dict[str, str], strict: bool):
    """Return (statement, anchors, tx_page_index) read from the Co font, or
    raise ``StatementDecodeError`` if the transactions section is not found."""
    statement: tuple[int, int] | None = None
    for page in pages:
        co_text = " ".join(apply_map(w["text"], co_map) for w in page["co"])
        if statement is None:
            m = re.search(r"\b[\d?]{2} ([A-Za-z?]+) ([\d?]{4})\b", co_text)
            if m:
                month = _month_from_tail(m.group(1))
                year = _num(m.group(2), strict)
                if month is not None and year is not None:
                    statement = (month, year)

    tx_index = None
    anchors: dict[str, tuple[float, Decimal | None]] = {}
    for idx, page in enumerate(pages):
        lines: dict[int, list] = {}
        for w in page["co"]:
            lines.setdefault(round(w["top"]), []).append(w)
        page_anchors: dict[str, tuple[float, Decimal | None]] = {}
        for top, ws in lines.items():
            ws.sort(key=lambda w: w["x0"])
            decoded = normalize_whitespace(" ".join(apply_map(w["text"], co_map) for w in ws))
            for label, key in (
                ("Your previous balance", "previous"),
                ("Payments towards your account", "payments"),
                ("How you've used your card", "spend"),
                ("Promotional transactions", "promotional"),
                ("Interest and charges", "interest"),
                ("Your new balance", "new"),
            ):
                if decoded.startswith(label):
                    page_anchors[key] = (top, _amount_of(decoded, strict))
        if {"previous", "payments", "spend", "new"} <= set(page_anchors):
            tx_index = idx
            anchors = page_anchors
            break

    if tx_index is None:
        raise StatementDecodeError(
            "No Barclaycard transactions section found — is this a Barclaycard statement?"
        )
    if statement is None:
        raise StatementDecodeError("Could not read the statement date")
    return statement, anchors, tx_index


def _balance_identity_ok(anchors: dict) -> bool:
    """Whether the Co-read totals satisfy previous - payments + card use = new.
    Used as the self-contained oracle for solving Co digit glyphs."""
    need = ("previous", "payments", "spend", "new")
    if any(anchors.get(k) is None or anchors[k][1] is None for k in need):
        return False
    out = sum(
        (anchors[k][1] for k in ("spend", "promotional", "interest") if anchors.get(k) and anchors[k][1] is not None),
        Decimal(0),
    )
    return anchors["previous"][1] - anchors["payments"][1] + out == anchors["new"][1]


# ---------------------------------------------------------------------------
# Layout parsing: decode rows with given maps and reconcile
# ---------------------------------------------------------------------------

def _section_spans(anchors: dict):
    spans = [
        ("in", anchors["payments"][0], anchors.get("spend", anchors["new"])[0]),
        ("out", anchors["spend"][0], anchors.get("promotional", anchors["new"])[0]),
    ]
    if "promotional" in anchors:
        spans.append(("out", anchors["promotional"][0], anchors.get("interest", anchors["new"])[0]))
    if "interest" in anchors:
        spans.append(("out", anchors["interest"][0], anchors["new"][0]))
    return spans


def _evaluate(pages, co_map, row_map, strict, co_read=None) -> list[ParsedRow]:
    statement, anchors, tx_index = co_read or _read_co(pages, co_map, strict)
    stmt_month, stmt_year = statement
    spans = _section_spans(anchors)

    def section_for(top: float) -> str | None:
        for sign, lo, hi in spans:
            if lo < top < hi:
                return sign
        return None

    grouped: list[list[dict]] = []
    for w in sorted(pages[tx_index]["row"], key=lambda w: (w["top"], w["x0"])):
        if grouped and abs(w["top"] - grouped[-1][0]["top"]) <= 2.5:
            grouped[-1].append(w)
        else:
            grouped.append([w])
    lines = {min(w["top"] for w in ws): ws for ws in grouped}

    rows: list[ParsedRow] = []
    totals = {"in": Decimal(0), "out": Decimal(0)}
    for top in sorted(lines):
        sign = section_for(top)
        if sign is None:
            continue
        ws = sorted(lines[top], key=lambda w: w["x0"])
        decoded = [apply_map(w["text"], row_map) for w in ws]
        amount = _amount_of(decoded[-1], strict)
        if amount is None:
            if strict and _looks_amount(decoded[-1]):
                raise StatementDecodeError(f"unresolved glyph in amount {decoded[-1]!r}")
            continue
        body = decoded[:-1]
        when: date | None = None
        if len(body) >= 2 and re.fullmatch(r"[\d?]{2}", body[0]):
            month = _month_from_tail(body[1])
            day = _num(body[0], strict)
            if month is not None and day is not None:
                year = stmt_year if month <= stmt_month else stmt_year - 1
                try:
                    when = date(year, month, day)
                except ValueError as e:
                    raise StatementDecodeError(f"invalid date {body[0]!r} {body[1]!r}: {e}")
                body = body[2:]
        if when is None:
            continue  # informational rows ("You had no ...") carry no date
        description = normalize_whitespace(" ".join(body))
        # Tahoma's lowercase 'y' is zero-width and vanishes mid-word.
        description = re.sub(r"\bPa ?ment\b", "Payment", description)
        totals[sign] += amount
        rows.append(
            ParsedRow(
                date=when,
                description=description or "Barclaycard transaction",
                amount=amount if sign == "in" else -amount,
            )
        )

    _validate(rows, totals, anchors)
    return rows


def _looks_amount(decoded: str) -> bool:
    return bool(re.search(r"[\d?][.,][\d?]{2}$", decoded.replace(" ", "")))


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------

def parse_pdf(data: bytes) -> list[ParsedRow]:
    return _parse(_extract(data))


def _parse(pages: list[dict]) -> list[ParsedRow]:
    # Fast path: the carried hardcoded ciphers, trusted only if they reconcile.
    try:
        return _evaluate(pages, CO_MAP, TAHOMA_MAP, strict=False)
    except StatementDecodeError:
        pass

    co_stream = _stream(pages, "co")
    row_stream = _stream(pages, "row")

    # --- Co font: letters from section labels, digits from the phone numbers ---
    co_map = derive_substitution(co_stream, CO_ANCHORS)
    resolve_separators(_amount_tokens(pages, "co", co_map), co_map)
    unknown_co = sorted(_digit_glyphs(pages, "co", co_map))

    def balance_ok(candidate: dict[str, str]) -> bool:
        try:
            _, anchors, _ = _read_co(pages, candidate, strict=True)
        except StatementDecodeError:
            return False
        return _balance_identity_ok(anchors)

    # --- Tahoma font: letters from informational rows, digits from section sums ---
    row_anchors = [a.replace(ZERO_WIDTH, "") for a in ROW_ANCHORS]
    row_map = derive_substitution(row_stream, row_anchors)
    resolve_separators(_amount_tokens(pages, "row", row_map), row_map)
    unknown_row = sorted(_digit_glyphs(pages, "row", row_map))

    # The balance identity alone can leave the Co digits ambiguous (a digit swap
    # that preserves it); disambiguate by requiring the Tahoma row sums to
    # reconcile against each Co candidate's section totals.
    for co_solved in iter_numeric_maps(co_map, unknown_co, balance_ok):
        co_read = _read_co(pages, co_solved, strict=True)

        def reconcile_row(candidate: dict[str, str]) -> bool:
            try:
                _evaluate(pages, co_solved, candidate, strict=True, co_read=co_read)
                return True
            except StatementDecodeError:
                return False

        row_solved = solve_numeric(row_map, unknown_row, reconcile_row)
        if row_solved is not None:
            return _evaluate(pages, co_solved, row_solved, strict=True, co_read=co_read)

    raise StatementDecodeError(
        "Could not reconcile Barclaycard transactions after auto-deriving the font "
        "ciphers (too few anchors, or the layout changed) — not importing."
    )


def _stream(pages: list[dict], font: str) -> list[str]:
    return [
        w["text"]
        for page in pages
        for w in sorted(page[font], key=lambda w: (round(w["top"]), w["x0"]))
    ]


def _amount_tokens(pages: list[dict], font: str, glyph_map: dict[str, str]) -> list[str]:
    """Raw scrambled tokens that look like amounts, used to locate the currency /
    decimal / comma glyphs structurally.

    With only letters mapped, an amount decodes to all-unknown, so amount tokens
    are recognised as those whose decode contains no letter (description words
    always carry some mapped letters) and are long enough to hold pence.
    """
    out = []
    for page in pages:
        for w in page[font]:
            dec = apply_map(w["text"], glyph_map)
            core = dec.replace(" ", "")
            if len(core) >= 4 and not any(c.isalpha() for c in dec):
                out.append(w["text"])
    return out


def _digit_glyphs(pages: list[dict], font: str, glyph_map: dict[str, str]) -> set[str]:
    """Unmapped glyphs sitting in amount or day-of-month positions -- the digit
    glyphs the anchors did not cover."""
    glyphs: set[str] = set()
    for page in pages:
        for w in page[font]:
            dec = apply_map(w["text"], glyph_map)
            core = dec.replace(" ", "").lstrip("£?")
            if re.fullmatch(r"[\d?,]+\.[\d?]{2}", core) or re.fullmatch(r"[\d?]{1,2}", dec):
                glyphs |= {c for c in w["text"] if c not in glyph_map and not c.isspace()}
    return glyphs


def _validate(rows: list[ParsedRow], totals: dict, anchors: dict) -> None:
    if not rows:
        raise StatementDecodeError("No Barclaycard transactions decoded")
    problems = []
    payments_total = anchors["payments"][1]
    if payments_total is not None and totals["in"] != payments_total:
        problems.append(f"payments: statement says £{payments_total}, parsed £{totals['in']}")
    expected_out = sum(
        (anchors[k][1] for k in ("spend", "promotional", "interest") if anchors.get(k) and anchors[k][1] is not None),
        Decimal(0),
    )
    if expected_out and totals["out"] != expected_out:
        problems.append(f"card use: statement sections say £{expected_out}, parsed £{totals['out']}")
    previous, new = anchors["previous"][1], anchors["new"][1]
    if previous is not None and new is not None:
        expected = previous - totals["in"] + totals["out"]
        if expected != new:
            problems.append(
                f"balance identity: {previous} - {totals['in']} + {totals['out']} "
                f"= {expected}, statement says {new}"
            )
    if problems:
        raise StatementDecodeError(
            "Decoded Barclaycard transactions do not reconcile "
            f"({'; '.join(problems)}). The font mapping may have changed — not importing."
        )
