from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.models import Account, Category, Transaction
from app.stats import category_merchants, month_detail, monthly_overview, recurring


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


def test_category_totals_respect_window():
    db, gbp, _, groceries, _ = make_db()
    add(db, gbp, date(2020, 1, 5), "OLD TESCO", "-500.00", cat=groceries.id)
    for month in (5, 6):
        add(db, gbp, date(2026, month, 5), "TESCO", "-10.00", cat=groceries.id)
    db.commit()
    data = monthly_overview(db, months=12)
    (cat,) = [c for c in data["categories"] if c["name"] == "Groceries"]
    assert cat["total"] == 20.0  # the 2020 transaction is outside the window


def test_category_merchants_uses_calendar_window():
    # Regression (M5): the old "last N month-keys with data" window let a
    # 2020 transaction into a 12-month view, disagreeing with the overview.
    db, gbp, _, groceries, _ = make_db()
    add(db, gbp, date(2020, 1, 5), "OLD TESCO", "-500.00", cat=groceries.id)
    for month in (5, 6):
        add(db, gbp, date(2026, month, 5), "TESCO", "-10.00", cat=groceries.id)
    db.commit()

    data = category_merchants(db, groceries.id, months=12)
    assert data["merchants"] == [{"merchant": "TESCO", "total": 20.0, "count": 2}]
    # ... and it agrees with monthly_overview's category total.
    overview = monthly_overview(db, months=12)
    (cat,) = [c for c in overview["categories"] if c["name"] == "Groceries"]
    assert cat["total"] == sum(m["total"] for m in data["merchants"])


def test_category_merchants_refunds_subtract():
    db, gbp, jpy, groceries, _ = make_db()
    add(db, gbp, date(2026, 6, 5), "TESCO", "-30.00", cat=groceries.id)
    add(db, gbp, date(2026, 6, 9), "TESCO", "10.00", cat=groceries.id)  # refund
    add(db, jpy, date(2026, 6, 7), "RAMEN", "-2000", cat=groceries.id)  # ~£10
    add(db, gbp, date(2026, 6, 28), "SALARY", "3000.00")  # income: not a merchant here
    db.commit()

    data = category_merchants(db, groceries.id, months=12)
    by_name = {m["merchant"]: m for m in data["merchants"]}
    assert by_name["TESCO"]["total"] == 20.0 and by_name["TESCO"]["count"] == 2
    assert by_name["RAMEN"]["total"] == 10.0  # GBP-converted

    # Uncategorized view (id 0) must not list the salary as spending.
    uncat = category_merchants(db, 0, months=12)
    assert all(m["merchant"] != "SALARY" for m in uncat["merchants"])


def test_coverage_counts_by_account_and_month():
    db, gbp, jpy, groceries, _ = make_db()
    add(db, gbp, date(2026, 5, 5), "TESCO", "-10.00", cat=groceries.id)
    add(db, gbp, date(2026, 5, 9), "TESCO", "-5.00", cat=groceries.id)
    add(db, jpy, date(2026, 6, 1), "RAMEN", "-2000")
    # transfers still count as data presence
    t = add(db, gbp, date(2026, 6, 2), "EXCHANGE", "-50.00")
    t.transfer_peer_id = t.id
    db.commit()

    from app.stats import coverage

    data = coverage(db)
    assert data["months"] == ["2026-05", "2026-06"]
    amex = next(a for a in data["accounts"] if a["name"] == "Amex")
    assert amex["months"] == {"2026-05": 2, "2026-06": 1}
    assert amex["total"] == 3
    assert amex["latest"] == "2026-06-02"
    revolut_jpy = next(a for a in data["accounts"] if a["name"] == "Revolut JPY")
    assert revolut_jpy["months"] == {"2026-06": 1}


def test_investing_excluded_from_spending_and_income_but_reported():
    db, gbp, _, groceries, _ = make_db()
    investing = Category(name="Investing")
    db.add(investing)
    db.flush()
    add(db, gbp, date(2026, 6, 5), "TESCO", "-10.00", cat=groceries.id)
    add(db, gbp, date(2026, 6, 10), "VANGUARD DD", "-1000.00", cat=investing.id)
    add(db, gbp, date(2026, 6, 20), "VANGUARD SELL", "400.00", cat=investing.id)
    add(db, gbp, date(2026, 6, 28), "SALARY", "3000.00")
    db.commit()

    data = monthly_overview(db)
    (june,) = [m for m in data["months"] if m["month"] == "2026-06"]
    assert june["spending"] == 10.0  # the Vanguard buy is not spending
    assert june["income"] == 3000.0  # the Vanguard sell is not income
    assert june["invested"] == 600.0  # net: £1000 in, £400 back out
    assert all(c["name"] != "Investing" for c in data["categories"])
    detail = month_detail(db, "2026-06")
    assert all(c["name"] != "Investing" for c in detail["categories"])


def test_investing_only_month_still_appears():
    db, gbp, _, groceries, _ = make_db()
    investing = Category(name="Investing")
    db.add(investing)
    db.flush()
    add(db, gbp, date(2026, 5, 5), "TESCO", "-10.00", cat=groceries.id)
    add(db, gbp, date(2026, 6, 10), "VANGUARD DD", "-500.00", cat=investing.id)
    db.commit()

    data = monthly_overview(db)
    by_month = {m["month"]: m for m in data["months"]}
    assert by_month["2026-06"]["invested"] == 500.0
    assert by_month["2026-06"]["spending"] == 0.0


def test_unknown_currency_excluded_and_reported_never_rate_one():
    import pytest as _pytest

    from app.stats import to_gbp

    with _pytest.raises(ValueError):
        to_gbp(Decimal("100"), "SEK")

    db, gbp, _, groceries, _ = make_db()
    sek = Account(name="Revolut SEK", provider="revolut", kind="current", currency="SEK")
    db.add(sek)
    db.flush()
    add(db, gbp, date(2026, 6, 5), "TESCO", "-10.00", cat=groceries.id)
    add(db, sek, date(2026, 6, 6), "STOCKHOLM SHOP", "-1300.00", cat=groceries.id)
    db.commit()

    data = monthly_overview(db)
    (june,) = [m for m in data["months"] if m["month"] == "2026-06"]
    assert june["spending"] == 10.0  # SEK row excluded, not counted 1:1
    assert data["excluded_currencies"] == {"currencies": ["SEK"], "transactions": 1}
    assert recurring(db) == []  # doesn't crash on the SEK account either


def test_year_summary_totals_shares_and_year_list():
    from app.stats import year_summary

    db, gbp, _, groceries, _ = make_db()
    coffee = Category(name="Coffee")
    db.add(coffee)
    db.flush()
    add(db, gbp, date(2025, 11, 3), "OLD TESCO", "-10.00", cat=groceries.id)
    add(db, gbp, date(2026, 3, 5), "TESCO", "-75.00", cat=groceries.id)
    add(db, gbp, date(2026, 5, 9), "PRET", "-25.00", cat=coffee.id)
    add(db, gbp, date(2026, 4, 28), "SALARY", "3000.00")
    db.commit()

    s = year_summary(db, 2026)
    assert s["years"] == [2025, 2026]
    assert s["spending"] == 100.0 and s["income"] == 3000.0 and s["net"] == 2900.0
    by_name = {c["name"]: c for c in s["categories"]}
    assert by_name["Groceries"]["total"] == 75.0 and by_name["Groceries"]["share"] == 75.0
    assert by_name["Coffee"]["share"] == 25.0

    # Default year is the latest with data; unknown years fall back too.
    assert year_summary(db)["year"] == 2026
    assert year_summary(db, 1999)["year"] == 2026
    assert year_summary(db, 2025)["spending"] == 10.0
