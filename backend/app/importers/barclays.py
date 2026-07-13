import csv
import io
import re
from datetime import datetime
from decimal import Decimal

from .base import BankImporter, ParsedRow, normalize_whitespace

DDMMYYYY = re.compile(r"^\d{2}/\d{2}/\d{4}$")

HEADER = ["number", "date", "account", "amount", "subcategory", "memo"]


class BarclaysImporter(BankImporter):
    """Barclays online banking statement export:
    Number,Date,Account,Amount,Subcategory,Memo

    Dates are DD/MM/YYYY; amounts are already signed (negative = money out).
    """

    name = "barclays"
    provider = "barclays"
    account_kind = "current"
    default_account_name = "Barclays"

    def matches(self, header: list[str], sample_rows: list[list[str]]) -> bool:
        if [h.strip().lower() for h in header] != HEADER:
            return False
        return all(DDMMYYYY.match(row[1].strip()) for row in sample_rows if len(row) > 1)

    def parse(self, text: str) -> list[ParsedRow]:
        reader = csv.reader(io.StringIO(text))
        next(reader, None)
        parsed: list[tuple[str, ParsedRow]] = []
        for raw in reader:
            if len(raw) < 6 or not raw[1].strip():
                continue
            parsed.append(
                (
                    raw[2].strip(),
                    ParsedRow(
                        date=datetime.strptime(raw[1].strip(), "%d/%m/%Y").date(),
                        description=normalize_whitespace(raw[5]),
                        amount=Decimal(raw[3].replace(",", "").strip()),
                    ),
                )
            )
        # The Account column ("20-04-15 38290008") distinguishes accounts when
        # an export contains several. A single-account file keeps the plain
        # default name (matching data imported before this existed); with
        # multiple accounts, each gets a stable, privacy-conscious suffix from
        # the last four digits of its identifier.
        identifiers = {ident for ident, _ in parsed if ident}
        if len(identifiers) > 1:
            for ident, row in parsed:
                digits = re.sub(r"\D", "", ident)
                row.account = (
                    f"{self.default_account_name} •{digits[-4:]}"
                    if digits
                    else self.default_account_name
                )
        return [row for _, row in parsed]
