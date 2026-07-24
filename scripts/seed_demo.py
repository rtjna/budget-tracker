"""Seed a demo database with realistic — entirely fictional — data.

Gives a fresh checkout something to show: ~14 months of salary, rent, bills,
groceries, subscriptions (including a price rise), a trip with foreign-currency
spending, investing, credit-card payment transfers, budgets, and balance
snapshots. Deterministic (seeded RNG), no real merchants, no real people.

Usage:
    DATA_DIR=/tmp/budget-demo backend/.venv/bin/python scripts/seed_demo.py
    DATA_DIR=/tmp/budget-demo STATIC_DIR=frontend/dist \
        backend/.venv/bin/uvicorn app.main:app --app-dir backend --port 8000

Refuses to touch an existing database: the target DATA_DIR must not already
contain budget.sqlite3, so it can never write into your real data.
"""

import os
import random
import sys
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

if not os.environ.get("DATA_DIR"):
    sys.exit("Set DATA_DIR to a fresh directory for the demo database.")
_db_file = Path(os.environ["DATA_DIR"]).expanduser() / "budget.sqlite3"
if _db_file.exists():
    sys.exit(f"{_db_file} already exists — pick an empty DATA_DIR; refusing to overwrite.")

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.categorize import seed_categories  # noqa: E402
from app.db import Base, SessionLocal, engine  # noqa: E402
from app.models import (  # noqa: E402
    Account,
    BalanceSnapshot,
    Budget,
    Category,
    Transaction,
    Trip,
)

rng = random.Random(7)
TODAY = date.today()


def month_start(offset: int) -> date:
    """First day of the month `offset` months before the current one."""
    index = TODAY.year * 12 + TODAY.month - 1 - offset
    return date(index // 12, index % 12 + 1, 1)


def main() -> None:
    Base.metadata.create_all(engine)
    db = SessionLocal()
    seed_categories(db)
    for extra in ("Sports", "Investing"):
        if not db.query(Category).filter_by(name=extra).first():
            db.add(Category(name=extra))
    db.commit()
    cat = {c.name: c.id for c in db.query(Category)}

    cur = Account(name="Everyday Current", provider="demobank", kind="current", currency="GBP")
    cc = Account(name="Rewards Credit Card", provider="democard", kind="credit", currency="GBP")
    eur = Account(name="Travel Card EUR", provider="demotravel", kind="current", currency="EUR")
    db.add_all([cur, cc, eur])
    db.flush()

    counter = 0

    def add(acc, day, desc, amount, category=None, source="rule"):
        nonlocal counter
        if day > TODAY:
            return None
        counter += 1
        tx = Transaction(
            account_id=acc.id,
            date=day,
            description=desc,
            merchant=desc,
            amount=Decimal(str(amount)),
            category_id=cat[category] if category else None,
            category_source=source if category else None,
            fingerprint=f"demo|{counter}",
        )
        db.add(tx)
        return tx

    trip = Trip(name="Lisbon", start_date=month_start(3) + timedelta(days=11),
                end_date=month_start(3) + timedelta(days=18))
    db.add(trip)
    db.flush()

    for offset in range(13, -1, -1):
        start = month_start(offset)

        add(cur, start.replace(day=28) if offset else TODAY, "MONTHLY SALARY", 3450, "Income")
        add(cur, start, "OAKFIELD LETTINGS RENT", -1395, "Housing")
        add(cur, start.replace(day=2), "RIVERTON COUNCIL TAX", -158, "Utilities & Bills")
        add(cur, start.replace(day=4), "VOLT ENERGY", -92, "Utilities & Bills")
        add(cur, start.replace(day=5), "FIBRELINK BROADBAND", -32, "Utilities & Bills")
        # A subscription price rise three months ago — the recurring table
        # flags it and the insights panel reports it.
        add(cur, start.replace(day=6), "STREAMBOX", -15.99 if offset <= 2 else -12.99, "Subscriptions")
        add(cur, start.replace(day=9), "TUNEHUB MUSIC", -11.99, "Subscriptions")
        add(cur, start.replace(day=3), "IRONWORKS GYM", -38, "Sports")
        add(cur, start.replace(day=2), "VANTAGE INDEX FUND", -500, "Investing")

        for _ in range(4):
            add(cur, start + timedelta(days=rng.randrange(28)), "FRESHMART",
                -round(rng.uniform(38, 88), 2), "Groceries")
        add(cur, start + timedelta(days=rng.randrange(28)), "CORNER GREENGROCER",
            -round(rng.uniform(8, 22), 2), "Groceries")
        for _ in range(rng.randrange(6, 10)):
            add(cur, start + timedelta(days=rng.randrange(28)), "BEAN AND LEAF",
                -round(rng.uniform(3.1, 4.9), 2), "Coffee", source="model")
        for _ in range(rng.randrange(3, 6)):
            add(cc, start + timedelta(days=rng.randrange(28)),
                rng.choice(["PIZZA UNION", "NOODLE HOUSE", "THE OLD CROWN"]),
                -round(rng.uniform(16, 64), 2), "Eating Out", source="model")
        for _ in range(rng.randrange(8, 13)):
            add(cur, start + timedelta(days=rng.randrange(28)), "METRO TRAVEL",
                -2.80, "Transport")
        if rng.random() < 0.6:
            add(cc, start + timedelta(days=rng.randrange(28)), "CITYCABS",
                -round(rng.uniform(12, 28), 2), "Transport")
        for _ in range(rng.randrange(1, 3)):
            add(cc, start + timedelta(days=rng.randrange(28)), "MEGAMART ONLINE",
                -round(rng.uniform(18, 110), 2), "Shopping", source="model")

        # Credit-card payment: a linked transfer pair, excluded from spending.
        pay = round(rng.uniform(500, 900), 2)
        out = add(cur, start.replace(day=15), "CARD PAYMENT THANK YOU", -pay, "Transfers")
        back = add(cc, start.replace(day=15), "PAYMENT RECEIVED", pay, "Transfers")
        if out and back:
            db.flush()
            out.transfer_peer_id = back.id
            back.transfer_peer_id = out.id

    # One refund to exercise refund semantics.
    add(cc, month_start(1) + timedelta(days=12), "MEGAMART ONLINE REFUND", 34.99, "Shopping")

    # The Lisbon trip: flights booked well before, hotel, then EUR spending.
    trip_txs = [
        add(cur, month_start(5) + timedelta(days=8), "BUDGETJET AIRWAYS", -286, "Travel"),
        add(cur, month_start(4) + timedelta(days=2), "HOTEL MIRADOURO", -540, "Travel"),
    ]
    for i in range(11):
        day = trip.start_date + timedelta(days=i % 8)
        trip_txs.append(
            add(eur, day, rng.choice(
                ["TASCA DO BAIRRO LISBOA", "PASTELARIA CENTRAL", "MERCADO DA BAIXA",
                 "TRAM 28 TICKETS", "MIRADOURO CAFE"]),
                -round(rng.uniform(9, 62), 2),
                rng.choice(["Eating Out", "Eating Out", "Transport", "Entertainment"])))
    for t in trip_txs:
        if t:
            t.trip_id = trip.id

    # This month: a first-ever merchant, a large one-off, and a travel spike
    # so the "What changed" panel has something true to say.
    add(cc, TODAY - timedelta(days=3), "APEX FURNITURE CO", -449, "Shopping", source="model")
    add(cur, TODAY - timedelta(days=5), "RAILPASS EUROPE", -380, "Travel")

    # A few uncategorized rows so the review queue isn't empty.
    for i, desc in enumerate(["AMZ MKTP UK 2K4L9", "SUMUP *FOOD TRUCK", "PAYPAL *GADGETS"]):
        add(cur, TODAY - timedelta(days=4 + i), desc, -round(rng.uniform(6, 45), 2))

    db.commit()

    # Budgets from this data's own averages, and balance snapshots for net worth.
    from app.stats import average_category_spend

    for cid, avg in average_category_spend(db).items():
        if avg >= 1:
            db.add(Budget(category_id=cid, amount=round(avg / 5) * 5 or 5,
                          effective_from=TODAY.replace(day=1)))
    db.add_all([
        BalanceSnapshot(account_id=cur.id, date=TODAY, balance=4850),
        BalanceSnapshot(account_id=cc.id, date=TODAY, balance=-1240),
        BalanceSnapshot(account_id=eur.id, date=TODAY, balance=210),
    ])
    db.commit()

    n = db.query(Transaction).count()
    print(f"Seeded {n} demo transactions into {_db_file}")


if __name__ == "__main__":
    main()
