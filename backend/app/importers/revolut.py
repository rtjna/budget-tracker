import csv
import io
from datetime import datetime
from decimal import Decimal

from .base import BankImporter, ParsedRow, normalize_whitespace

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
            rows.append(
                ParsedRow(
                    date=datetime.strptime(completed, "%Y-%m-%d %H:%M:%S").date(),
                    description=normalize_whitespace(raw["description"]),
                    amount=amount - fee,
                )
            )
        return rows
