from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.importers import detect_importer
from app.importers.amex import AmexImporter
from app.importers.barclays import BarclaysImporter
from app.importers.revolut import RevolutImporter
from app.importers.base import ParsedRow
from app.importing import CrossFormatOverlapError, UnrecognizedFileError, import_file, import_rows
from app.models import Transaction

AMEX_CSV = """Date,Description,Amount
09/07/2026,TESCO STORE 1234 LONDON,4.60
09/07/2026,TESCO STORE 1234 LONDON,4.60
26/06/2026,PAYMENT RECEIVED - THANK YOU,-100.00
"""

BARCLAYS_CSV = """Number,Date,Account,Amount,Subcategory,Memo
1,08/07/2026,20-00-00 12345678,-52.30,Payment,SAINSBURYS SMKT  ON 07 JUL          BCC
2,07/07/2026,20-00-00 12345678,"1,500.00",Credit,ACME LTD SALARY
"""

REVOLUT_CSV = """Type,Product,Started Date,Completed Date,Description,Amount,Fee,Currency,State,Balance
CARD_PAYMENT,Current,2026-07-05 09:12:44,2026-07-06 11:03:01,Pret A Manger,-6.25,0.00,GBP,COMPLETED,120.50
EXCHANGE,Current,2026-07-04 10:00:00,2026-07-04 10:00:00,Exchanged to EUR,-200.00,1.50,GBP,COMPLETED,126.75
CARD_PAYMENT,Current,2026-07-03 20:00:00,,Pending Coffee,-3.00,0.00,GBP,PENDING,330.25
"""


def make_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def header_and_sample(text):
    lines = text.strip().splitlines()
    return lines[0].split(","), [line.split(",") for line in lines[1:4]]


def test_detection():
    assert isinstance(detect_importer(*header_and_sample(AMEX_CSV)), AmexImporter)
    assert isinstance(detect_importer(*header_and_sample(BARCLAYS_CSV)), BarclaysImporter)
    assert isinstance(detect_importer(*header_and_sample(REVOLUT_CSV)), RevolutImporter)
    assert detect_importer(["What", "Even", "Is"], [["a", "b", "c"]]) is None


def test_amex_signs_flipped():
    rows = AmexImporter().parse(AMEX_CSV)
    assert rows[0].amount == Decimal("-4.60")  # spend becomes negative
    assert rows[2].amount == Decimal("100.00")  # payment becomes positive


def test_barclays_parses_signed_amounts_and_thousands():
    rows = BarclaysImporter().parse(BARCLAYS_CSV)
    assert rows[0].date == date(2026, 7, 8)
    assert rows[0].amount == Decimal("-52.30")
    assert rows[0].description == "SAINSBURYS SMKT ON 07 JUL BCC"
    assert rows[1].amount == Decimal("1500.00")


def test_revolut_subtracts_fee_and_skips_pending():
    rows = RevolutImporter().parse(REVOLUT_CSV)
    assert len(rows) == 2  # pending row skipped
    assert rows[0].date == date(2026, 7, 6)  # completed date, not started
    assert rows[0].amount == Decimal("-6.25")
    assert rows[1].amount == Decimal("-201.50")  # fee included


def test_import_dedup_keeps_legitimate_same_day_duplicates():
    db = make_session()
    batch = import_file(db, "amex.csv", AMEX_CSV)
    assert batch.new_count == 3  # both identical Tescos kept
    assert batch.duplicate_count == 0

    again = import_file(db, "amex.csv", AMEX_CSV)
    assert again.new_count == 0
    assert again.duplicate_count == 3


def test_import_creates_separate_accounts():
    db = make_session()
    import_file(db, "b.csv", BARCLAYS_CSV)
    import_file(db, "r.csv", REVOLUT_CSV)
    accounts = {t.account.name for t in db.scalars(select(Transaction))}
    assert accounts == {"Barclays", "Revolut"}


def test_unrecognized_file_rejected():
    db = make_session()
    with pytest.raises(UnrecognizedFileError):
        import_file(db, "junk.csv", "foo,bar\n1,2\n")


# --- M4: cross-format double-count guard (Barclays PDF vs. CSV) ---


def barclays_pdf_rows():
    # Same statement as BARCLAYS_CSV, as the PDF parser would render it:
    # identical dates and amounts, different description text.
    return [
        ParsedRow(date=date(2026, 7, 8), description="Sainsburys Smkt", amount=Decimal("-52.30")),
        ParsedRow(date=date(2026, 7, 7), description="Acme Limited", amount=Decimal("1500.00")),
    ]


def import_pdf_rows(db, rows):
    return import_rows(
        db, source="barclays_pdf", filename="statement.pdf", rows=rows,
        provider="barclays", kind="current", default_account_name="Barclays",
    )


def test_pdf_after_csv_overlap_refused():
    db = make_session()
    import_file(db, "b.csv", BARCLAYS_CSV)
    with pytest.raises(CrossFormatOverlapError) as exc:
        import_pdf_rows(db, barclays_pdf_rows())
    msg = str(exc.value)
    assert "2 of 2" in msg and "2026-07-07" in msg and "2026-07-08" in msg
    # Nothing was double-inserted.
    assert db.scalar(select(func.count()).select_from(Transaction)) == 2


def test_csv_after_pdf_overlap_refused():
    db = make_session()
    import_pdf_rows(db, barclays_pdf_rows())
    with pytest.raises(CrossFormatOverlapError):
        import_file(db, "b.csv", BARCLAYS_CSV)


def test_minor_overlap_allowed():
    # 1 of 3 pairs overlapping (<=50%) is a plausible coincidence, not a
    # re-imported statement.
    db = make_session()
    import_file(db, "b.csv", BARCLAYS_CSV)
    rows = barclays_pdf_rows()[:1] + [
        ParsedRow(date=date(2026, 8, 1), description="Tesco", amount=Decimal("-9.99")),
        ParsedRow(date=date(2026, 8, 2), description="Boots", amount=Decimal("-3.49")),
    ]
    batch = import_pdf_rows(db, rows)
    assert batch.new_count == 3


def test_same_format_reimport_still_just_dedups():
    # Re-importing the identical CSV is caught by fingerprints, not refused.
    db = make_session()
    import_file(db, "b.csv", BARCLAYS_CSV)
    again = import_file(db, "b.csv", BARCLAYS_CSV)
    assert again.new_count == 0 and again.duplicate_count == 2


REVOLUT_MULTICURRENCY_CSV = """Type,Product,Started Date,Completed Date,Description,Amount,Fee,Currency,State,Balance
CARD_PAYMENT,Current,2026-07-05 09:00:00,2026-07-05 09:00:00,Boots,-5.00,0.00,GBP,COMPLETED,100.00
CARD_PAYMENT,Current,2026-07-04 09:00:00,2026-07-04 09:00:00,Ramen Shop,-3000,0.00,JPY,COMPLETED,10000
EXCHANGE,Current,2026-07-03 09:00:00,2026-07-03 09:00:00,Exchanged to JPY,-20.00,0.00,GBP,COMPLETED,105.00
"""


def test_revolut_multicurrency_splits_accounts():
    db = make_session()
    batch = import_file(db, "r.csv", REVOLUT_MULTICURRENCY_CSV)
    assert batch.new_count == 3
    by_account = {
        t.account.name: (t.account.currency, t.amount) for t in db.scalars(select(Transaction))
    }
    assert by_account["Revolut"][0] == "GBP"
    assert by_account["Revolut JPY"] == ("JPY", Decimal("-3000"))
