from datetime import date
from decimal import Decimal

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.models import Account, Category, Transaction
from app.splitwise import sync

ME, FRIEND = 111, 222


class FakeClient:
    def __init__(self, expenses):
        self._expenses = expenses

    def current_user_id(self):
        return ME

    def expenses(self):
        yield from self._expenses


def user(uid, paid, owed):
    return {"user_id": uid, "paid_share": paid, "owed_share": owed}


def make_db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    bank = Account(name="Revolut", provider="revolut", kind="current", currency="GBP")
    db.add_all([bank, Category(name="Travel"), Category(name="Eating Out"), Category(name="Income")])
    db.flush()
    return db, bank


def test_corrections_signs_and_mapping():
    db, _ = make_db()
    client = FakeClient([
        # I paid £100 train for two -> +50 correction, Travel
        {"id": 1, "date": "2026-06-10T00:00:00Z", "currency_code": "GBP", "payment": False,
         "description": "Trains to Bath", "category": {"name": "Bus/train"},
         "users": [user(ME, "100.0", "50.0"), user(FRIEND, "0.0", "50.0")]},
        # Friend paid dinner, I owe £30 -> -30 correction, Eating Out
        {"id": 2, "date": "2026-06-11T00:00:00Z", "currency_code": "GBP", "payment": False,
         "description": "Dinner", "category": {"name": "Dining out"},
         "users": [user(ME, "0.0", "30.0"), user(FRIEND, "60.0", "30.0")]},
        # Lumped entry, unknown category -> uncategorized correction
        {"id": 3, "date": "2026-06-12T00:00:00Z", "currency_code": "GBP", "payment": False,
         "description": "Breakfast and lunch", "category": {"name": "General"},
         "users": [user(ME, "42.0", "21.0"), user(FRIEND, "0.0", "21.0")]},
    ])
    stats = sync(db, client=client)
    assert stats["corrections"] == 3
    assert stats["uncategorized"] == 1

    txs = {t.description: t for t in db.scalars(select(Transaction))}
    trains = txs["Trains to Bath (Splitwise share)"]
    assert trains.amount == Decimal("50.00")  # positive = offsets Travel
    travel = db.scalar(select(Category).where(Category.name == "Travel"))
    assert trains.category_id == travel.id
    dinner = txs["Dinner (Splitwise share)"]
    assert dinner.amount == Decimal("-30.00")  # consumption my bank never saw
    assert txs["Breakfast and lunch (Splitwise share)"].category_id is None

    # Idempotent
    stats2 = sync(db, client=client)
    assert stats2["corrections"] == 0 and stats2["skipped"] == 3


def test_settlement_links_bank_tx_and_retries_when_missing():
    db, bank = make_db()
    payment = {"id": 9, "date": "2026-06-20T00:00:00Z", "currency_code": "GBP", "payment": True,
               "description": "Payment", "category": {"name": "Payment"},
               "users": [user(ME, "0.0", "20.0"), user(FRIEND, "20.0", "0.0")]}  # friend pays me £20
    client = FakeClient([payment])

    # No bank transaction yet -> pending, nothing created
    stats = sync(db, client=client)
    assert stats["settlements_pending"] == 1
    assert db.scalar(select(Transaction)) is None

    # Bank inflow arrives; next sync links it
    inflow = Transaction(account_id=bank.id, date=date(2026, 6, 21),
                         description="From Friend", merchant="FROM FRIEND",
                         amount=Decimal("20.00"), fingerprint="bankfp")
    db.add(inflow)
    db.commit()

    stats = sync(db, client=client)
    assert stats["settlements_linked"] == 1
    mirror = db.scalar(select(Transaction).where(Transaction.account_id != bank.id))
    assert mirror.amount == Decimal("-20.00")
    assert inflow.transfer_peer_id == mirror.id and mirror.transfer_peer_id == inflow.id
