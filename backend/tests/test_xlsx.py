import io
from datetime import datetime
from decimal import Decimal

from openpyxl import Workbook

from app.importers import detect_importer
from app.importers.revolut import RevolutImporter
from app.xlsx import is_xlsx, xlsx_to_csv_text

REVOLUT_HEADER = [
    "Type", "Product", "Started Date", "Completed Date", "Description",
    "Amount", "Fee", "Currency", "State", "Balance",
]


def make_revolut_xlsx() -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.append(REVOLUT_HEADER)
    ws.append([
        "CARD_PAYMENT", "Current",
        datetime(2026, 7, 5, 9, 12, 44), datetime(2026, 7, 6, 11, 3, 1),
        "Pret A Manger", -6.25, 0, "GBP", "COMPLETED", 120.50,
    ])
    ws.append([
        "TRANSFER", "Current",
        datetime(2026, 7, 1), datetime(2026, 7, 1),  # midnight -> date-only string
        "To Savings", -50, 0, "GBP", "COMPLETED", 70.50,
    ])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def test_xlsx_roundtrip_through_revolut_importer():
    data = make_revolut_xlsx()
    assert is_xlsx(data)
    text = xlsx_to_csv_text(data)

    lines = text.strip().splitlines()
    importer = detect_importer(lines[0].split(","), [lines[1].split(",")])
    assert isinstance(importer, RevolutImporter)

    rows = importer.parse(text)
    assert len(rows) == 2
    assert rows[0].amount == Decimal("-6.25")
    assert rows[0].date.isoformat() == "2026-07-06"
    assert rows[1].date.isoformat() == "2026-07-01"  # midnight datetime handled


def test_csv_not_mistaken_for_xlsx():
    assert not is_xlsx(b"Date,Description,Amount\n")
