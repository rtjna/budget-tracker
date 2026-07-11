from datetime import date
from decimal import Decimal

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.importing import import_file
from app.models import Account, Transaction

ZKB_CSV = '''"Date";"Booking text";"Curr";"Amount details";"ZKB reference";"Reference number";"Debit CHF";"Credit CHF";"Value date";"Balance CHF";"Payment purpose";"Details"
"06.07.2026";"Debit TWINT: NEUE ZURCHER ZEITUNG AG ZURICH";"";"";"L1";"";"2.50";"";"06.07.2026";"2436.46";"";""
"27.12.2025";"Credit TWINT: BECK, LEYLA MARIA +41791234567";"";"";"L2";"";"";"37.50";"27.12.2025";"2191.81";"";""
"21.02.2025";"Debit eBanking Mobile: Revolut Bank UAB, Mingerstrasse 20, CH-3030";"";"";"Z2";"";"3'700.00";"";"21.02.2025";"365.21";"Somebody, CH";"Revolut Bank UAB"
"01.01.2025";"Balance carried forward";"";"";"Z3";"";"";"";"01.01.2025";"100.00";"";""
'''


def make_db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def test_zkb_import():
    db = make_db()
    batch = import_file(db, "Account statement.csv", ZKB_CSV)
    assert batch.source == "zkb"
    assert batch.new_count == 3  # amountless balance row skipped

    account = db.scalar(select(Account).where(Account.name == "ZKB"))
    assert account.currency == "CHF"

    txs = {t.description: t for t in db.scalars(select(Transaction))}
    nzz = txs["NEUE ZURCHER ZEITUNG AG ZURICH"]  # channel prefix stripped
    assert nzz.amount == Decimal("-2.50")
    assert nzz.date == date(2026, 7, 6)
    assert txs["BECK, LEYLA MARIA +41791234567"].amount == Decimal("37.50")
    revolut = txs["Revolut Bank UAB, Mingerstrasse 20, CH-3030"]
    assert revolut.amount == Decimal("-3700.00")  # apostrophe separator handled

    again = import_file(db, "Account statement.csv", ZKB_CSV)
    assert again.new_count == 0 and again.duplicate_count == 3
