import csv
import io
from datetime import datetime
from decimal import Decimal

from .base import BankImporter, ParsedRow, normalize_whitespace

HEADER = [
    "date",
    "booking text",
    "curr",
    "amount details",
    "zkb reference",
    "reference number",
    "debit chf",
    "credit chf",
    "value date",
    "balance chf",
    "payment purpose",
    "details",
]

# Channel prefixes ("Debit TWINT: ", "Credit eBanking Mobile: ", ...) pollute
# merchant grouping; the counterparty after the colon is the useful part.
CHANNEL_PREFIXES = ("debit", "credit")


def _amount(raw: str) -> Decimal | None:
    raw = raw.replace("'", "").replace(",", "").strip()
    return Decimal(raw) if raw else None


def _clean_description(booking: str) -> str:
    lowered = booking.lower()
    if lowered.startswith(CHANNEL_PREFIXES) and ": " in booking:
        return booking.split(": ", 1)[1]
    return booking


class ZkbImporter(BankImporter):
    """Zürcher Kantonalbank eBanking export (English UI): semicolon-delimited,
    DD.MM.YYYY dates, separate Debit/Credit CHF columns, apostrophe thousands
    separators."""

    name = "zkb"
    provider = "zkb"
    account_kind = "current"
    default_account_name = "ZKB"

    def matches(self, header: list[str], sample_rows: list[list[str]]) -> bool:
        return [h.strip().lower() for h in header] == HEADER

    def parse(self, text: str) -> list[ParsedRow]:
        reader = csv.reader(io.StringIO(text), delimiter=";")
        next(reader, None)
        rows: list[ParsedRow] = []
        for raw in reader:
            if len(raw) < 10 or not raw[0].strip():
                continue
            debit = _amount(raw[6])
            credit = _amount(raw[7])
            if debit is None and credit is None:
                continue  # balance/summary sub-rows carry no amount
            amount = (credit or Decimal(0)) - (debit or Decimal(0))
            rows.append(
                ParsedRow(
                    date=datetime.strptime(raw[0].strip(), "%d.%m.%Y").date(),
                    description=normalize_whitespace(_clean_description(raw[1])),
                    amount=amount,
                    currency="CHF",
                )
            )
        return rows
