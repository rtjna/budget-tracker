from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.models import Account, Category, Transaction
from app.stats import month_detail, monthly_overview, recurring


def make_db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    gbp = Account(name="Amex", provider="amex", kind="credit", currency="GBP")
    jpy = Account(name="Revolut JPY", provider="revolut", kind="current", currency="JPY")
    groceries = Category(name="Groceries")
    transfers = Category(name="Transfers")
    income = Category(name="Income")
    db.add_all([gbp, jpy, groceries, transfers, income])
    db.flush()
    return db, gbp, jpy, groceries, transfers


def add(db, acc, day, desc, amount, cat=None, peer=None):
    tx = Transaction(
        account_id=acc.id, date=day, description=desc, merchant=desc.upper(),
        amount=Decimal(amount), category_id=cat, transfer_peer_id=peer,
        fingerprint=f"{acc.id}|{day}|{desc}|{amount}",
    )
    db.add(tx)
    db.flush()
    return tx


def test_overview_converts_and_excludes_transfers():
    db, gbp, jpy, groceries, transfers = make_db()
    add(db, gbp, date(2026, 6, 5), "TESCO", "-10.00", cat=groceries.id)
    add(db, jpy, date(2026, 6, 7), "RAMEN", "-2000", cat=groceries.id)  # ~£10
    add(db, gbp, date(2026, 6, 25), "PAYMENT RECEIVED", "4000.00", cat=transfers.id)
    add(db, gbp, date(2026, 6, 28), "SALARY", "3000.00")
    linked = add(db, gbp, date(2026, 6, 20), "EXCHANGE", "-500.00")
    linked.transfer_peer_id = linked.id  # self-link stands in for a pair
    db.commit()

    data = monthly_overview(db)
    (june,) = [m for m in data["months"] if m["month"] == "2026-06"]
    assert june["spending"] == 20.0  # £10 + ¥2000 * 0.005
    assert june["income"] == 3000.0  # card payment + linked transfer excluded
    assert june["net"] == 2980.0
    assert data["categories"][0]["name"] == "Groceries"


def test_month_detail_merchants():
    db, gbp, _, groceries, _ = make_db()
    add(db, gbp, date(2026, 6, 5), "Tesco", "-10.00", cat=groceries.id)
    add(db, gbp, date(2026, 6, 6), "Tesco", "-5.00", cat=groceries.id)
    db.commit()
    detail = month_detail(db, "2026-06")
    assert detail["merchants"][0] == {"merchant": "TESCO", "total": 15.0, "count": 2}


def test_recurring_detects_monthly_subscription():
    db, gbp, _, _, _ = make_db()
    start = date(2026, 1, 15)
    for i in range(6):
        add(db, gbp, start + timedelta(days=30 * i), "NETFLIX.COM", "-9.99")
    # Noise: irregular merchant
    add(db, gbp, date(2026, 1, 2), "RANDOM SHOP", "-5.00")
    add(db, gbp, date(2026, 1, 9), "RANDOM SHOP", "-25.00")
    add(db, gbp, date(2026, 3, 1), "RANDOM SHOP", "-45.00")
    db.commit()

    items = recurring(db)
    assert len(items) == 1
    sub = items[0]
    assert sub["merchant"] == "NETFLIX.COM"
    assert sub["cadence"] == "monthly"
    assert sub["typical_amount"] == 9.99
    assert sub["occurrences"] == 6


def test_recurring_flags_price_change():
    db, gbp, _, _, _ = make_db()
    start = date(2026, 1, 10)
    for i in range(5):
        add(db, gbp, start + timedelta(days=30 * i), "SPOTIFY", "-10.99")
    add(db, gbp, start + timedelta(days=150), "SPOTIFY", "-12.99")
    db.commit()
    (sub,) = recurring(db)
    assert sub["price_change"] > 0
