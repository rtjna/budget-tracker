"""Dashboard aggregates.

All amounts are converted to GBP with approximate, hardcoded rates — good
enough for trend dashboards, not for accounting. Transfers between own
accounts (linked pairs and the Transfers category) are excluded everywhere.
"""

from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal
from statistics import median

from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from .models import Category, Transaction

# Approximate GBP conversion rates (mid-2026). Update occasionally by hand.
GBP_RATES = {
    "GBP": Decimal("1"),
    "USD": Decimal("0.74"),
    "EUR": Decimal("0.85"),
    "CHF": Decimal("0.92"),
    "JPY": Decimal("0.0050"),
    "THB": Decimal("0.023"),
}

TRANSFER_CATEGORY = "Transfers"


def to_gbp(amount: Decimal, currency: str) -> Decimal:
    return Decimal(amount) * GBP_RATES.get(currency, Decimal("1"))


def _spending_transactions(db: Session) -> list[Transaction]:
    """All transactions that count as real money movement: transfer pairs and
    the Transfers category excluded."""
    transfers_id = db.scalar(select(Category.id).where(Category.name == TRANSFER_CATEGORY))
    query = (
        select(Transaction)
        .options(joinedload(Transaction.account))
        .where(Transaction.transfer_peer_id.is_(None))
    )
    if transfers_id is not None:
        query = query.where(
            (Transaction.category_id != transfers_id) | (Transaction.category_id.is_(None))
        )
    return list(db.scalars(query))


def monthly_overview(db: Session, months: int = 12) -> dict:
    txs = _spending_transactions(db)
    categories = {c.id: c.name for c in db.scalars(select(Category))}

    by_month: dict[str, dict] = defaultdict(
        lambda: {"spending": Decimal(0), "income": Decimal(0), "by_category": defaultdict(Decimal)}
    )
    category_totals: dict[int | None, Decimal] = defaultdict(Decimal)

    for tx in txs:
        gbp = to_gbp(tx.amount, tx.account.currency)
        month = tx.date.strftime("%Y-%m")
        bucket = by_month[month]
        if gbp < 0:
            bucket["spending"] += -gbp
            bucket["by_category"][tx.category_id] += -gbp
            category_totals[tx.category_id] += -gbp
        else:
            bucket["income"] += gbp

    keys = sorted(by_month)[-months:]
    return {
        "months": [
            {
                "month": m,
                "spending": float(by_month[m]["spending"]),
                "income": float(by_month[m]["income"]),
                "net": float(by_month[m]["income"] - by_month[m]["spending"]),
                "by_category": {
                    str(cid if cid is not None else 0): float(v)
                    for cid, v in by_month[m]["by_category"].items()
                },
            }
            for m in keys
        ],
        "categories": sorted(
            (
                {
                    "id": cid if cid is not None else 0,
                    "name": categories.get(cid, "Uncategorized"),
                    "total": float(total),
                }
                for cid, total in category_totals.items()
            ),
            key=lambda c: -c["total"],
        ),
    }


def month_detail(db: Session, month: str) -> dict:
    txs = [
        t
        for t in _spending_transactions(db)
        if t.date.strftime("%Y-%m") == month and t.amount < 0
    ]
    categories = {c.id: c.name for c in db.scalars(select(Category))}

    by_category: dict[int | None, Decimal] = defaultdict(Decimal)
    by_merchant: dict[str, dict] = defaultdict(lambda: {"total": Decimal(0), "count": 0})
    for tx in txs:
        gbp = -to_gbp(tx.amount, tx.account.currency)
        by_category[tx.category_id] += gbp
        m = by_merchant[tx.merchant or tx.description]
        m["total"] += gbp
        m["count"] += 1

    return {
        "month": month,
        "categories": sorted(
            (
                {
                    "id": cid if cid is not None else 0,
                    "name": categories.get(cid, "Uncategorized"),
                    "total": float(total),
                }
                for cid, total in by_category.items()
            ),
            key=lambda c: -c["total"],
        ),
        "merchants": sorted(
            (
                {"merchant": name, "total": float(v["total"]), "count": v["count"]}
                for name, v in by_merchant.items()
            ),
            key=lambda x: -x["total"],
        )[:15],
    }


def category_merchants(db: Session, category_id: int, months: int = 12) -> dict:
    """Top merchants within one category (id 0 = uncategorized) across the
    last N months, GBP-converted, spending only."""
    txs = [t for t in _spending_transactions(db) if t.amount < 0]
    month_keys = sorted({t.date.strftime("%Y-%m") for t in txs})[-months:]
    window = set(month_keys)
    wanted = None if category_id == 0 else category_id

    by_merchant: dict[str, dict] = defaultdict(lambda: {"total": Decimal(0), "count": 0})
    for tx in txs:
        if tx.category_id != wanted or tx.date.strftime("%Y-%m") not in window:
            continue
        m = by_merchant[tx.merchant or tx.description]
        m["total"] += -to_gbp(tx.amount, tx.account.currency)
        m["count"] += 1

    return {
        "merchants": sorted(
            (
                {"merchant": name, "total": float(v["total"]), "count": v["count"]}
                for name, v in by_merchant.items()
            ),
            key=lambda x: -x["total"],
        )[:15]
    }


CADENCES = [
    ("weekly", 5, 9),
    ("monthly", 24, 38),
    ("quarterly", 80, 100),
    ("yearly", 330, 400),
]


def recurring(db: Session) -> list[dict]:
    """Detect recurring payments: merchants with >= 3 charges at a steady
    interval and steady amount."""
    txs = [t for t in _spending_transactions(db) if t.amount < 0 and t.merchant]
    categories = {c.id: c.name for c in db.scalars(select(Category))}

    by_merchant: dict[str, list[Transaction]] = defaultdict(list)
    for t in txs:
        by_merchant[t.merchant].append(t)

    results = []
    today = date.today()
    for merchant, group in by_merchant.items():
        if len(group) < 3:
            continue
        group.sort(key=lambda t: t.date)
        # Collapse same-day charges (split payments) into one occurrence.
        dates = sorted({t.date for t in group})
        if len(dates) < 3:
            continue
        intervals = [(b - a).days for a, b in zip(dates, dates[1:])]
        med = median(intervals)
        cadence = next((name for name, lo, hi in CADENCES if lo <= med <= hi), None)
        if cadence is None:
            continue
        # Steady schedule: most gaps close to the median.
        tolerance = max(3, med * 0.25)
        regular = sum(1 for i in intervals if abs(i - med) <= tolerance)
        if regular / len(intervals) < 0.7:
            continue
        amounts = [-to_gbp(t.amount, t.account.currency) for t in group]
        typical = median(amounts)
        # Steady amount: recurring bills shouldn't swing wildly.
        if typical == 0 or (max(amounts) - min(amounts)) / typical > 0.6:
            continue
        last_tx = group[-1]
        last_amount = -to_gbp(last_tx.amount, last_tx.account.currency)
        active = (today - dates[-1]).days <= med * 1.6
        results.append(
            {
                "merchant": merchant,
                "category": categories.get(group[-1].category_id, "Uncategorized"),
                "cadence": cadence,
                "typical_amount": float(typical),
                "last_amount": float(last_amount),
                "price_change": float(last_amount - typical) if abs(last_amount - typical) / typical > 0.05 else 0.0,
                "last_date": dates[-1].isoformat(),
                "next_expected": (dates[-1] + timedelta(days=round(med))).isoformat() if active else None,
                "occurrences": len(dates),
                "active": active,
                "monthly_equivalent": float(typical * Decimal(30) / Decimal(med)),
            }
        )
    results.sort(key=lambda r: (not r["active"], -r["monthly_equivalent"]))
    return results
