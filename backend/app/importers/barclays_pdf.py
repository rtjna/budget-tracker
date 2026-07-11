"""Barclays PDF statement parser.

Barclays statement PDFs scramble their embedded fonts' character maps, so
text extraction yields a substitution cipher. The cipher is stable per font
family; this module carries the derived translation table for the body font
(Expert-Sans-Regular) and decodes transactions positionally:

- transaction rows are set entirely in the body font
- the bold header row locates the Money out / Money in / Balance columns
- amounts extract as a pounds word + a pence word ("2,200" "00")
- the capital-B glyph extracts as a newline, which word extraction would
  swallow; the char stream is patched before words are built

Every parse is validated against the statement's own "At a glance" totals —
if the decode drifts (e.g. Barclays changes the font), the import fails
loudly instead of writing garbage.
"""

import io
import re
from datetime import date
from decimal import Decimal

from .base import ParsedRow, normalize_whitespace

BODY_FONT = "Expert-Sans-Regular"
HEADER_FONT = "Expert-Sans-RegularBold"

# Derived from ground truth; capital B extracts as nothing. V/X/Z unseen.
BODY_MAP = str.maketrans({
    "†": "a", "š": "b", "‡": "c", "á": "d", "\x8f": "e", "ç": "f", "å": "g",
    "ñ": "h", "â": "i", "ß": "j", "Œ": "k", "ˆ": "l", "\x90": "m", "‹": "n",
    "\x7f": "o", "ã": "p", "ï": "q", "„": "r", "Š": "s", "Ž": "t", "ƒ": "u",
    ".": "v", "!": "w", ")": "x", "‰": "y", "$": "z",
    "¸": "A", "‘": "C", "(": "D", "ë": "E", "¢": "F", "<": "G", "|": "H",
    "˜": "I", "”": "J", "Á": "K", "è": "L", "\x8d": "M", "Ç": "N", "+": "O",
    "î": "P", "í": "Q", "&": "R", "™": "S", "ì": "T", "À": "U", "*": "W",
    "\x81": "Y",
    "’": "0", "–": "1", "“": "2", "ž": "3", "œ": "4", "à": "5", "—": "6",
    "Ÿ": "7", "ê": "8", "ä": "9",
    "\x9d": ",", "›": "£", "·": "-", "é": ":", "¬": "/", "-": "'", ";": "&",
})

# Bold-font tokens (their own cipher) used only to find column positions.
BOLD_MONEY = "™˜ƒ‰š"
BOLD_OUT = "˜›“"
BOLD_IN = "–ƒ"
BOLD_BALANCE = "œ†‡†ƒˆ‰"

MONTHS = {m: i + 1 for i, m in enumerate(
    ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
)}

POUNDS = re.compile(r"^\d{1,3}(,\d{3})*$|^\d+$")
PENCE = re.compile(r"^\d{2}$")
PERIOD = re.compile(r"(\d{2}) (\w{3}) - (\d{2}) (\w{3}) (\d{4})")
PAGE_NO = re.compile(r"\s*Page \d+$")


class StatementDecodeError(Exception):
    pass


def _decode(word: str) -> str:
    return word.translate(BODY_MAP)


def _base_font(fontname: str) -> str:
    return fontname.split("+", 1)[-1]


def _year_for(month: int, period: tuple[int, int, int]) -> int:
    """Statement periods span at most two months; the year printed belongs to
    the period end. A December row on a Dec->Jan statement is the prior year."""
    start_month, end_month, end_year = period
    if start_month > end_month and month >= start_month:
        return end_year - 1
    return end_year


def parse_pdf(data: bytes) -> list[ParsedRow]:
    import pdfplumber

    rows: list[ParsedRow] = []
    totals = {"in": Decimal(0), "out": Decimal(0)}
    glance: dict[str, Decimal] = {}
    period: tuple[int, int, int] | None = None
    # Running-balance ledger check: every printed balance must equal the
    # start balance plus every parsed transaction so far. Catches per-row
    # sign/column errors that cancelling totals would miss.
    running: Decimal | None = None
    balance_errors: list[str] = []

    from pdfplumber.utils.text import extract_words

    with pdfplumber.open(io.BytesIO(data)) as pdf:
        for page in pdf.pages:
            # The capital-B glyph extracts as "\n"; restore it before word
            # grouping or it vanishes as whitespace ("Bakshi" -> "akshi").
            chars = [
                {**c, "text": "B"}
                if c["text"] == "\n" and _base_font(c["fontname"]) == BODY_FONT
                else c
                for c in page.chars
            ]
            words = extract_words(chars, extra_attrs=["fontname"])
            body = [w for w in words if _base_font(w["fontname"]) == BODY_FONT]
            bold = [w for w in words if _base_font(w["fontname"]) == HEADER_FONT]

            page_text = " ".join(_decode(w["text"]) for w in body)
            if period is None:
                m = PERIOD.search(page_text)
                if m and m.group(2) in MONTHS and m.group(4) in MONTHS:
                    period = (MONTHS[m.group(2)], MONTHS[m.group(4)], int(m.group(5)))
            _collect_glance(page_text, glance)

            # Column anchors from the bold header row.
            col_out = col_in = col_bal = None
            header_y = None
            for i, w in enumerate(bold):
                if w["text"] == BOLD_MONEY and i + 1 < len(bold):
                    nxt = bold[i + 1]
                    if nxt["text"] == BOLD_OUT:
                        col_out = (w["x0"] + nxt["x1"]) / 2
                    elif nxt["text"] == BOLD_IN:
                        col_in = (w["x0"] + nxt["x1"]) / 2
                elif w["text"] == BOLD_BALANCE:
                    col_bal = (w["x0"] + w["x1"]) / 2
                    header_y = w["top"]
            if col_out is None or col_in is None or col_bal is None or header_y is None:
                continue  # not a transaction page

            table = [w for w in body if w["top"] > header_y + 2]

            lines: list[list[dict]] = []
            for w in sorted(table, key=lambda w: (round(w["top"]), w["x0"])):
                if lines and abs(w["top"] - lines[-1][0]["top"]) <= 2.5:
                    lines[-1].append(w)
                else:
                    lines.append([w])

            current_date: date | None = None
            amount_zone = min(col_out, col_in) - 40

            for line in lines:
                decoded = [(_decode(w["text"]), w) for w in line]
                texts = [t for t, _ in decoded]

                start = 0
                if len(texts) >= 2 and re.fullmatch(r"\d{2}", texts[0]) and texts[1] in MONTHS:
                    if period is None:
                        raise StatementDecodeError("Transaction row before statement period was found")
                    month = MONTHS[texts[1]]
                    current_date = date(_year_for(month, period), month, int(texts[0]))
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
                    desc_words.append(text)
                    i += 1

                desc = PAGE_NO.sub("", normalize_whitespace(" ".join(desc_words)))

                if not desc and not amounts:
                    continue
                if desc.startswith(("Anything Wrong", "If you've spotted")):
                    break  # everything below is boilerplate, not table
                if desc.startswith(("Start balance", "End balance", "Continued")):
                    for value, center in amounts:
                        if abs(center - col_bal) < abs(center - col_out) and running is None:
                            running = value  # start balance anchors the ledger
                        elif desc.startswith("End balance") and running is not None and value != running:
                            balance_errors.append(
                                f"end balance printed {value}, ledger says {running}"
                            )
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
                        ParsedRow(
                            date=current_date,
                            description=desc or "Barclays transaction",
                            amount=signed,
                        )
                    )
                    if running is not None:
                        running += signed
                        if printed_balance is not None and printed_balance != running:
                            balance_errors.append(
                                f"{current_date} {desc[:40]!r}: printed balance "
                                f"{printed_balance}, ledger says {running}"
                            )
                            running = printed_balance  # re-anchor; report once
                elif desc and rows and not amounts:
                    # Wrapped description ("...Ruben Tjon A" / "Meeuw").
                    rows[-1].description = normalize_whitespace(rows[-1].description + " " + desc)

    if balance_errors:
        raise StatementDecodeError(
            "Running-balance check failed — not importing: " + "; ".join(balance_errors[:5])
        )
    _validate(rows, totals, glance)
    return rows


def _collect_glance(page_text: str, glance: dict) -> None:
    for label, key in (("Money in", "in"), ("Money out", "out")):
        m = re.search(rf"{label} £([\d,]+) (\d{{2}})", page_text)
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
