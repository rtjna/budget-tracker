import csv
import io
import re
from datetime import datetime
from decimal import Decimal

from .base import BankImporter, ParsedRow, normalize_whitespace

DDMMYYYY = re.compile(r"^\d{2}/\d{2}/\d{4}$")

HEADER = ["date", "account/card no", "amount", "subcategory", "memo"]

# Direction comes from the subcategory, not the sign — payments/refunds are
# money in, everything else is card spending. abs() makes this robust whether
# the export signs amounts or not.
CREDIT_WORDS = ("payment", "refund", "credit", "cashback")


class BarclaycardCsvImporter(BankImporter):
    """Barclaycard online CSV export:
    Date,Account/Card No,Amount,Subcategory,Memo
    (often preceded by a bare "barclaycard" title line, handled upstream).

    Dates are DD/MM/YYYY. The Memo repeats the merchant — a short code, wide
    whitespace, then the full form ("Revolut**1788*      Revolut**1788*,
    Dublin"); the last segment is kept, matching the PDF importer's
    descriptions so merchants group consistently across formats.
    """

    name = "barclaycard"
    provider = "barclaycard"
    account_kind = "credit"
    default_account_name = "Barclaycard"

    def matches(self, header: list[str], sample_rows: list[list[str]]) -> bool:
        if [h.strip().lower() for h in header] != HEADER:
            return False
        return all(DDMMYYYY.match(row[0].strip()) for row in sample_rows if row and row[0].strip())

    def parse(self, text: str) -> list[ParsedRow]:
        reader = csv.reader(io.StringIO(text))
        header = next(reader, None)
        if header is not None and len([f for f in header if f.strip()]) <= 1:
            next(reader, None)  # title line, then the real header
        parsed: list[tuple[str, ParsedRow]] = []
        for raw in reader:
            if len(raw) < 5 or not raw[0].strip():
                continue
            amount = abs(Decimal(raw[2].replace(",", "").strip()))
            credit = any(w in raw[3].lower() for w in CREDIT_WORDS)
            memo = ",".join(raw[4:]).strip()  # tolerate unquoted commas in memo
            memo_parts = [p for p in re.split(r"\s{2,}", memo) if p]
            description = normalize_whitespace(memo_parts[-1] if memo_parts else memo)
            parsed.append(
                (
                    raw[1].strip(),
                    ParsedRow(
                        date=datetime.strptime(raw[0].strip(), "%d/%m/%Y").date(),
                        description=description,
                        amount=amount if credit else -amount,
                    ),
                )
            )
        # Multiple cards in one export split into per-card accounts; the
        # common single-card case keeps the plain name (matching the PDF
        # importer's account).
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
