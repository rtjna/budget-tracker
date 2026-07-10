import csv
import io
from datetime import date, datetime
from decimal import Decimal

from .base import BankImporter, ParsedRow, normalize_whitespace

DATE_FORMATS = ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%d/%m/%Y")


def parse_date(value: str) -> date:
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            continue
    raise ValueError(f"Unrecognized Revolut date: {value!r}")

HEADER = [
    "type",
    "product",
    "started date",
    "completed date",
    "description",
    "amount",
    "fee",
    "currency",
    "state",
    "balance",
]


class RevolutImporter(BankImporter):
    """Revolut statement export:
    Type,Product,Started Date,Completed Date,Description,Amount,Fee,Currency,State,Balance

    Dates are "YYYY-MM-DD HH:MM:SS"; amounts are signed (negative = money out)
    with fees reported separately, so the effective amount is Amount - Fee.
    Only COMPLETED transactions are imported (pending/reverted are skipped
    until they complete in a later export).
    """

    name = "revolut"
    provider = "revolut"
    account_kind = "current"
    default_account_name = "Revolut"

    def matches(self, header: list[str], sample_rows: list[list[str]]) -> bool:
        return [h.strip().lower() for h in header] == HEADER

    def parse(self, text: str) -> list[ParsedRow]:
        reader = csv.DictReader(io.StringIO(text))
        reader.fieldnames = [h.strip().lower() for h in reader.fieldnames or []]
        rows: list[ParsedRow] = []
        for raw in reader:
            if (raw.get("state") or "").strip().upper() != "COMPLETED":
                continue
            completed = (raw.get("completed date") or "").strip()
            if not completed:
                continue
            amount = Decimal(raw["amount"].replace(",", "").strip())
            fee = Decimal((raw.get("fee") or "0").replace(",", "").strip() or "0")
            currency = (raw.get("currency") or "GBP").strip().upper()
            rows.append(
                ParsedRow(
                    date=parse_date(completed),
                    description=normalize_whitespace(raw["description"]),
                    amount=amount - fee,
                    currency=currency,
                    # Non-GBP currency pockets become their own accounts so
                    # native amounts are never summed as if they were GBP.
                    account=None if currency == "GBP" else f"Revolut {currency}",
                )
            )
        return rows
