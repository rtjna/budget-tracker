"""Barclaycard PDF statement parser.

Like the Barclays debit statements, Barclaycard PDFs scramble their fonts'
character maps — but with a different layout and two different ciphers:

- section headers and totals are set in "Barclaycard-Co" (fully derived,
  including all ten digits)
- transaction rows are set in "Tahoma" (derived except: lowercase 'y' is a
  zero-width glyph that never reaches the text layer, the digit '3' did not
  appear in the reference statement, and several uppercase letters are
  unseen)

Structural workarounds for the gaps:
- any unknown glyph in a digit position must be '3' (all other digits are
  mapped), and the arithmetic validation below confirms it
- months are identified by their known lowercase tails ("u..n" = Jun,
  "e..c" = Dec), which are pairwise distinct
- unknown letters in descriptions decode to '?' — cosmetic only

There are no Money in/out columns: the SECTION a row sits in determines its
sign ("Payments towards your account" = money in; "How you've used your
card", promotional, and interest sections = money out).

Validation is arithmetic and strict: each section's rows must sum to the
section total, and previous balance - payments + spending + interest must
equal the new balance. Any mismatch (including a re-scrambled font in a
future statement) refuses the import loudly.
"""

import io
import re
from datetime import date
from decimal import Decimal

from .base import ParsedRow, normalize_whitespace
from .barclays_pdf import StatementDecodeError

CO_FONT = "Barclaycard-Co"
ROW_FONT = "Tahoma"

CO_MAP = str.maketrans({
    "ˆ": "a", "¸": "b", "Ž": "c", "‘": "d", "‰": "e", "·": "g", "(": "h",
    "–": "i", "“": "k", "\x8f": "l", "Š": "m", "‹": "n", "\x7f": "o",
    "ä": "p", "„": "r", "†": "s", "‡": "t", "ƒ": "u", "ñ": "v", "Ÿ": "w",
    "\x90": "y",
    "é": "C", "ë": "H", "è": "I", "â": "M", "à": "P", "<": "T", "\x81": "Y",
    "—": "0", "™": "1", "\x9d": "2", "œ": "3", "ê": "4", "š": "5", "å": "6",
    "á": "7", "˜": "8", "›": "9",
    "\x8d": "£", "¢": ",", "’": ".", "+": "'",
})

TAHOMA_MAP = str.maketrans({
    "‡": "a", "Š": "b", "‹": "c", "œ": "d", "·": "e", ".": "g", "‰": "h",
    "—": "i", "Œ": "k", "˜": "l", "ž": "m", "™": "n", "‘": "o", "”": "p",
    "Ÿ": "q", "“": "r", "ˆ": "s", "\x9d": "t", "’": "u", "–": "v",
    "†": "C", "å": "D", "\x8d": "J", "ä": "P", "ñ": "R", "á": "T", "\x90": "Y",
    "\x81": "0", "ç": "1", "ƒ": "2", "š": "4", "„": "5", "Ž": "6",
    "\x8f": "7", "›": "8", "â": "9",
    "¸": "£", "à": ",", "\x7f": ".", "\n": "%", "¢": "*",
})

# Month from known lowercase letters (Tahoma 'y' never extracts and several
# uppercase letters are unmapped, so "Jun" decodes to "?un", "July" to
# "?uly"). Matched longest-first with startswith so Jan ("an...") wins over
# May ("a...").
MONTH_TAILS = {
    "an": 1, "eb": 2, "ar": 3, "pr": 4, "a": 5, "un": 6,
    "ul": 7, "ug": 8, "ep": 9, "ct": 10, "ov": 11, "ec": 12,
}


def _month_from_tail(token: str) -> int | None:
    tail = "".join(c for c in token if c.islower())
    for key in sorted(MONTH_TAILS, key=len, reverse=True):
        if tail.startswith(key):
            return MONTH_TAILS[key]
    return None
MONTH_NAMES = {
    "January": 1, "February": 2, "March": 3, "April": 4, "May": 5, "June": 6,
    "July": 7, "August": 8, "September": 9, "October": 10, "November": 11,
    "December": 12,
}

AMOUNT = re.compile(r"£?([\d?]{1,3}(?:,[\d?]{3})*)\.([\d?]{2})$")
UNKNOWN = "?"


def decode_co(word: str) -> str:
    return "".join(CO_MAP.get(ord(c), UNKNOWN) if not c.isspace() else c for c in word)


def decode_row(word: str) -> str:
    return "".join(TAHOMA_MAP.get(ord(c), UNKNOWN) if not c.isspace() else c for c in word)


def _amount_from(text: str) -> Decimal | None:
    """Parse a decoded amount; unknown glyphs in digit positions can only be
    the digit 3 (every other digit is mapped)."""
    m = AMOUNT.search(text.replace(" ", ""))
    if m is None:
        return None
    digits = (m.group(1) + "." + m.group(2)).replace(",", "").replace(UNKNOWN, "3")
    return Decimal(digits)


def _month_from(token: str) -> int | None:
    return _month_from_tail(token)


def parse_pdf(data: bytes) -> list[ParsedRow]:
    import pdfplumber

    rows: list[ParsedRow] = []
    anchors: dict[str, tuple[float, Decimal | None]] = {}
    statement: tuple[int, int] | None = None  # (month, year)

    with pdfplumber.open(io.BytesIO(data)) as pdf:
        tx_page = None
        for page in pdf.pages:
            words = page.extract_words(extra_attrs=["fontname"])
            co_words = [w for w in words if w["fontname"].split("+")[-1] == CO_FONT]

            if statement is None:
                co_text = " ".join(decode_co(w["text"]) for w in co_words)
                # e.g. "Please pay by 27 ?uly 2026" — uppercase J is unmapped,
                # so identify the month by its lowercase tail.
                m = re.search(r"\b[\d?]{2} ([A-Za-z?]+) ([\d?]{4})\b", co_text)
                if m:
                    month = _month_from_tail(m.group(1))
                    if month is not None:
                        statement = (month, int(m.group(2).replace(UNKNOWN, "3")))

            # find the transactions page via its section headers
            lines: dict[int, list] = {}
            for w in co_words:
                lines.setdefault(round(w["top"]), []).append(w)
            page_anchors = {}
            for top, ws in lines.items():
                ws.sort(key=lambda w: w["x0"])
                decoded = normalize_whitespace(" ".join(decode_co(w["text"]) for w in ws))
                for label, key in (
                    ("Your previous balance", "previous"),
                    ("Payments towards your account", "payments"),
                    ("How you've used your card", "spend"),
                    ("Promotional transactions", "promotional"),
                    ("Interest and charges", "interest"),
                    ("Your new balance", "new"),
                ):
                    if decoded.startswith(label):
                        page_anchors[key] = (top, _amount_from(decoded))
            if {"previous", "payments", "spend", "new"} <= set(page_anchors):
                tx_page = page
                anchors = page_anchors
                break

        if tx_page is None:
            raise StatementDecodeError(
                "No Barclaycard transactions section found — is this a Barclaycard statement?"
            )
        if statement is None:
            raise StatementDecodeError("Could not read the statement date")

        stmt_month, stmt_year = statement
        row_words = [
            w
            for w in tx_page.extract_words(extra_attrs=["fontname"])
            if w["fontname"].split("+")[-1] == ROW_FONT and w["x0"] < 310
        ]
        grouped: list[list[dict]] = []
        for w in sorted(row_words, key=lambda w: (w["top"], w["x0"])):
            if grouped and abs(w["top"] - grouped[-1][0]["top"]) <= 2.5:
                grouped[-1].append(w)
            else:
                grouped.append([w])
        lines = {min(w["top"] for w in ws): ws for ws in grouped}

        # Section boundaries by y position.
        def section_for(top: float) -> str | None:
            spans = [
                ("in", anchors["payments"][0], anchors.get("spend", anchors["new"])[0]),
                ("out", anchors["spend"][0], anchors.get("promotional", anchors["new"])[0]),
            ]
            if "promotional" in anchors:
                spans.append(("out", anchors["promotional"][0], anchors.get("interest", anchors["new"])[0]))
            if "interest" in anchors:
                spans.append(("out", anchors["interest"][0], anchors["new"][0]))
            for sign, lo, hi in spans:
                if lo < top < hi:
                    return sign
            return None

        totals = {"in": Decimal(0), "out": Decimal(0)}
        for top in sorted(lines):
            sign = section_for(top)
            if sign is None:
                continue
            ws = sorted(lines[top], key=lambda w: w["x0"])
            decoded = [decode_row(w["text"]) for w in ws]
            amount = _amount_from(decoded[-1])
            if amount is None or "£" not in decoded[-1]:
                continue
            body = decoded[:-1]
            when: date | None = None
            if len(body) >= 2 and re.fullmatch(r"[\d?]{2}", body[0]):
                month = _month_from(body[1])
                if month is not None:
                    day = int(body[0].replace(UNKNOWN, "3"))
                    year = stmt_year if month <= stmt_month else stmt_year - 1
                    when = date(year, month, day)
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


def _validate(rows: list[ParsedRow], totals: dict, anchors: dict) -> None:
    if not rows:
        raise StatementDecodeError("No Barclaycard transactions decoded")
    problems = []
    payments_total = anchors["payments"][1]
    if payments_total is not None and totals["in"] != payments_total:
        problems.append(f"payments: statement says £{payments_total}, parsed £{totals['in']}")
    # The out side spans card use + promotional + interest sections.
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
