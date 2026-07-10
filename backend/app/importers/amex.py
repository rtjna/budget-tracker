import csv
import io
import re
from datetime import datetime
from decimal import Decimal

from .base import BankImporter, ParsedRow, normalize_whitespace

DDMMYYYY = re.compile(r"^\d{2}/\d{2}/\d{4}$")


class AmexImporter(BankImporter):
    """American Express activity export: Date,Description,Amount.

    Dates are DD/MM/YYYY; amounts are positive for spending and negative
    for credits/payments, so the sign is flipped to the app-wide convention
    (negative = money out).
    """

    name = "amex"
    provider = "amex"
    account_kind = "credit"
    default_account_name = "Amex"

    def matches(self, header: list[str], sample_rows: list[list[str]]) -> bool:
        if [h.strip().lower() for h in header] != ["date", "description", "amount"]:
            return False
        return all(DDMMYYYY.match(row[0]) for row in sample_rows if row)

    def parse(self, text: str) -> list[ParsedRow]:
        reader = csv.reader(io.StringIO(text))
        header = next(reader, None)
        if header is None:
            return []
        rows: list[ParsedRow] = []
        for raw in reader:
            if not raw or not raw[0].strip():
                continue
            rows.append(
                ParsedRow(
                    date=datetime.strptime(raw[0].strip(), "%d/%m/%Y").date(),
                    description=normalize_whitespace(raw[1]),
                    amount=-Decimal(raw[2].replace(",", "").strip()),
                )
            )
        return rows
