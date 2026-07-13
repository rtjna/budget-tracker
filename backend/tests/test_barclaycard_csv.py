from datetime import date
from decimal import Decimal

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.importing import import_file
from app.models import Transaction

BCC_CSV = '''barclaycard
Date,Account/Card No,Amount,Subcategory,Memo
06/07/2026,************4003,800.00,Purchase,Revolut**1788*        Revolut**1788*\\, Dublin
29/06/2026,************4003,"2,200.00",Payment received,PAYMENT THANK YOU      Payment\\, Thank You
15/06/2026,************4003,12.50,Refund,SHOP RETURN      Some Shop\\, London
'''.replace("\\,", ",")


def make_db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def test_barclaycard_csv_import():
    db = make_db()
    batch = import_file(db, "barclaycard.csv", BCC_CSV)
    assert batch.source == "barclaycard"
    assert batch.new_count == 3

    txs = {t.description: t for t in db.scalars(select(Transaction))}
    purchase = txs["Revolut**1788*, Dublin"]  # memo tail kept, matches PDF form
    assert purchase.amount == Decimal("-800.00")  # purchases = money out
    assert purchase.date == date(2026, 7, 6)
    assert purchase.account.name == "Barclaycard"
    assert purchase.account.kind == "credit"
    assert txs["Payment, Thank You"].amount == Decimal("2200.00")  # payments in
    assert txs["Some Shop, London"].amount == Decimal("12.50")  # refunds in

    again = import_file(db, "barclaycard.csv", BCC_CSV)
    assert again.new_count == 0 and again.duplicate_count == 3


def test_multi_card_export_splits_accounts():
    db = make_db()
    csv_text = (
        "Date,Account/Card No,Amount,Subcategory,Memo\n"
        "01/07/2026,************4003,10.00,Purchase,SHOP A\n"
        "01/07/2026,************9911,10.00,Purchase,SHOP A\n"
    )
    import_file(db, "b.csv", csv_text)
    accounts = {t.account.name for t in db.scalars(select(Transaction))}
    assert accounts == {"Barclaycard •4003", "Barclaycard •9911"}


def test_not_mistaken_for_amex():
    from app.importers import detect_importer
    from app.importers.barclaycard_csv import BarclaycardCsvImporter

    header = ["Date", "Account/Card No", "Amount", "Subcategory", "Memo"]
    sample = [["06/07/2026", "***4003", "800.00", "Purchase", "X"]]
    assert isinstance(detect_importer(header, sample), BarclaycardCsvImporter)


def test_returned_direct_debit_is_money_out():
    csv_text = (
        "barclaycard\n"
        "Date,Account/Card No,Amount,Subcategory,Memo\n"
        '02/04/2025,************4003,"3,000.00",Payment,"RETURNED DIRECT DEBIT      Returned Direct Debit"\n'
        '31/03/2025,************4003,"3,000.00",Payment,"PAYMENT BY DIRECT DEBIT      Payment By Direct Debit"\n'
    )
    from app.importers.barclaycard_csv import BarclaycardCsvImporter

    rows = BarclaycardCsvImporter().parse(csv_text)
    by_desc = {r.description: r.amount for r in rows}
    assert by_desc["Returned Direct Debit"] == Decimal("-3000.00")  # reversal: money out
    assert by_desc["Payment By Direct Debit"] == Decimal("3000.00")
