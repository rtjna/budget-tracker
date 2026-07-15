"""Dashboard aggregates.

All amounts are converted to GBP with approximate, hardcoded rates — good
enough for trend dashboards, not for accounting. Transfers between own
accounts (linked pairs and the Transfers category) are excluded everywhere.
"""

from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal
from statistics import median

from sqlalchemy import func, select
from sqlalchemy.orm import Session, joinedload

from .models import Account, Category, Transaction

# Approximate GBP conversion rates (mid-2026). Update occasionally by hand.
GBP_RATES = {
    "GBP": Decimal("1"),
    "USD": Decimal("0.74"),
    "EUR": Decimal("0.85"),
    "CHF": Decimal("0.92"),
    "JPY": Decimal("0.0050"),
    "THB": Decimal("0.023"),
    # Travel/Splitwise currencies — trend-grade accuracy, not accounting.
    "AED": Decimal("0.20"),
    "CNY": Decimal("0.10"),
    "COP": Decimal("0.00018"),
    "DKK": Decimal("0.11"),
    "HUF": Decimal("0.0022"),
    "MXN": Decimal("0.040"),
    "MYR": Decimal("0.17"),
    "OMR": Decimal("1.92"),
    "SGD": Decimal("0.56"),
}

TRANSFER_CATEGORY = "Transfers"
# Asset movements, not consumption: buys aren't spending and sells aren't
# income. Excluded from every spending/income aggregate and reported as a
# separate net-invested series instead.
INVESTING_CATEGORY = "Investing"

# Spending that is committed rather than discretionary: these categories plus
# anything from a detected recurring merchant. The split answers "how much of
# my spending could I actually change?"
COMMITTED_CATEGORIES = ("Housing", "Utilities & Bills", "Subscriptions")


def _is_income(tx, income_id) -> bool:
    """A positive amount counts as income when it's explicitly categorized as
    Income, or uncategorized in a real bank account. Positive amounts with a
    spending category — and anything in the Splitwise clearing account — are
    refunds/corrections that offset spending instead."""
    if tx.account.provider == "splitwise":
        return False
    return tx.category_id == income_id or tx.category_id is None


def _income_category(tx, income_id) -> bool:
    """Explicitly categorized Income in a real bank account — such rows hit
    the income total with their sign (outflows like back-paid tax reduce it),
    never the spending side."""
    return (
        income_id is not None
        and tx.category_id == income_id
        and tx.account.provider != "splitwise"
    )


def to_gbp(amount: Decimal, currency: str) -> Decimal:
    """Convert to GBP. Refuses unknown currencies loudly — a silent 1:1
    fallback would misstate totals by an order of magnitude (audit H1).
    Callers filter with GBP_RATES first and report exclusions."""
    rate = GBP_RATES.get(currency)
    if rate is None:
        raise ValueError(f"No GBP rate configured for {currency!r} — add it to GBP_RATES")
    return Decimal(amount) * rate


def _excluded_currencies(db: Session) -> dict:
    """Transactions whose account currency has no GBP rate: excluded from all
    GBP aggregates, reported so the dashboard can warn instead of lying."""
    rows = db.execute(
        select(Account.currency, func.count(Transaction.id))
        .join(Transaction, Transaction.account_id == Account.id)
        .where(Account.currency.not_in(list(GBP_RATES)))
        .group_by(Account.currency)
    ).all()
    return {
        "currencies": sorted(c for c, _ in rows),
        "transactions": sum(n for _, n in rows),
    }


def _spending_transactions(db: Session) -> list[Transaction]:
    """All transactions that count as consumption or income: transfer pairs,
    the Transfers category, and the Investing category (asset movements)
    excluded."""
    excluded_ids = list(
        db.scalars(
            select(Category.id).where(Category.name.in_((TRANSFER_CATEGORY, INVESTING_CATEGORY)))
        )
    )
    query = (
        select(Transaction)
        .options(joinedload(Transaction.account))
        .where(Transaction.transfer_peer_id.is_(None))
    )
    if excluded_ids:
        query = query.where(
            Transaction.category_id.not_in(excluded_ids) | Transaction.category_id.is_(None)
        )
    # Unknown-currency accounts can't be converted honestly; they are excluded
    # here and surfaced via _excluded_currencies rather than converted 1:1.
    return [t for t in db.scalars(query) if t.account.currency in GBP_RATES]


def _invested_by_month(db: Session) -> dict[str, Decimal]:
    """Net invested per month: Investing outflows minus inflows (a sell that
    returns money reduces the month's net investment), GBP-converted."""
    investing_id = db.scalar(select(Category.id).where(Category.name == INVESTING_CATEGORY))
    invested: dict[str, Decimal] = defaultdict(Decimal)
    if investing_id is None:
        return invested
    txs = db.scalars(
        select(Transaction)
        .options(joinedload(Transaction.account))
        .where(
            Transaction.category_id == investing_id,
            Transaction.transfer_peer_id.is_(None),
        )
    )
    for tx in txs:
        if tx.account.currency not in GBP_RATES:
            continue
        invested[tx.date.strftime("%Y-%m")] += -to_gbp(tx.amount, tx.account.currency)
    return invested


def _calendar_window(all_keys: list[str], months: int) -> list[str]:
    """A true calendar window of `months` months ending at the latest month
    with data — not "the last N keys that happen to contain data", which
    lets stray old months in. `all_keys` must be sorted "YYYY-MM" strings."""
    if not all_keys:
        return []
    last_y, last_m = map(int, all_keys[-1].split("-"))
    start_index = (last_y * 12 + (last_m - 1)) - (months - 1)
    cutoff = f"{start_index // 12:04d}-{start_index % 12 + 1:02d}"
    return [k for k in all_keys if k >= cutoff]


def monthly_overview(db: Session, months: int = 12) -> dict:
    txs = _spending_transactions(db)
    categories = {c.id: c.name for c in db.scalars(select(Category))}
    income_id = db.scalar(select(Category.id).where(Category.name == "Income"))
    committed_ids = set(
        db.scalars(select(Category.id).where(Category.name.in_(COMMITTED_CATEGORIES)))
    )
    # Recurring merchants count as committed wherever they're categorized —
    # a gym membership is committed even though Sports isn't.
    recurring_merchants = {r["merchant"] for r in recurring(db)}

    def _is_committed(tx) -> bool:
        return tx.category_id in committed_ids or tx.merchant in recurring_merchants

    by_month: dict[str, dict] = defaultdict(
        lambda: {
            "spending": Decimal(0),
            "income": Decimal(0),
            "committed": Decimal(0),
            "by_category": defaultdict(Decimal),
        }
    )

    for tx in txs:
        gbp = to_gbp(tx.amount, tx.account.currency)
        month = tx.date.strftime("%Y-%m")
        bucket = by_month[month]
        if _income_category(tx, income_id):
            # Signed on purpose: the mirror of refund semantics. An outflow
            # categorized Income (e.g. a back-paid income tax bill) reduces
            # income rather than counting as spending.
            bucket["income"] += gbp
        elif gbp < 0:
            bucket["spending"] += -gbp
            bucket["by_category"][tx.category_id] += -gbp
            if _is_committed(tx):
                bucket["committed"] += -gbp
        elif _is_income(tx, income_id):
            bucket["income"] += gbp
        else:
            # Refund semantics: a categorized inflow (refund, reimbursement,
            # Splitwise correction) offsets its category rather than counting
            # as income.
            bucket["spending"] -= gbp
            bucket["by_category"][tx.category_id] -= gbp
            if _is_committed(tx):
                bucket["committed"] -= gbp

    invested = _invested_by_month(db)
    for month in invested:
        by_month[month]  # a month with only investing activity still appears

    keys = _calendar_window(sorted(by_month), months)
    # Category totals honor the same window as the months list — never
    # all-time, or the ranked list disagrees with the monthly columns.
    category_totals: dict[int | None, Decimal] = defaultdict(Decimal)
    for k in keys:
        for cid, v in by_month[k]["by_category"].items():
            category_totals[cid] += v
    return {
        "excluded_currencies": _excluded_currencies(db),
        "months": [
            {
                "month": m,
                "spending": float(by_month[m]["spending"]),
                "income": float(by_month[m]["income"]),
                "committed": float(by_month[m]["committed"]),
                "invested": float(invested.get(m, 0)),
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


def year_summary(db: Session, year: int | None = None) -> dict:
    """One calendar year: total spent/earned/invested and per-category spend
    with each category's share of the year's total spend. Same conventions as
    monthly_overview (refund semantics, transfers/investing/unknown-currency
    exclusions)."""
    txs = _spending_transactions(db)
    years = sorted({t.date.year for t in txs})
    if not years:
        return {"year": None, "years": [], "spending": 0, "income": 0, "net": 0,
                "invested": 0, "categories": [], "merchants": []}
    if year is None or year not in years:
        year = years[-1]

    categories = {c.id: c.name for c in db.scalars(select(Category))}
    income_id = db.scalar(select(Category.id).where(Category.name == "Income"))
    spending = income = Decimal(0)
    by_category: dict[int | None, Decimal] = defaultdict(Decimal)
    by_merchant: dict[str, dict] = defaultdict(lambda: {"total": Decimal(0), "count": 0})

    def _spend(tx, gbp: Decimal) -> None:
        m = by_merchant[tx.merchant or tx.description]
        m["total"] += gbp
        m["count"] += 1

    for tx in txs:
        if tx.date.year != year:
            continue
        gbp = to_gbp(tx.amount, tx.account.currency)
        if _income_category(tx, income_id):
            income += gbp  # signed: back-paid tax reduces income
        elif gbp < 0:
            spending += -gbp
            by_category[tx.category_id] += -gbp
            _spend(tx, -gbp)
        elif _is_income(tx, income_id):
            income += gbp
        else:
            spending -= gbp
            by_category[tx.category_id] -= gbp
            _spend(tx, -gbp)  # refunds subtract from their merchant

    invested = sum(
        (v for month, v in _invested_by_month(db).items() if month.startswith(f"{year:04d}-")),
        Decimal(0),
    )
    return {
        "year": year,
        "years": years,
        "spending": float(spending),
        "income": float(income),
        "net": float(income - spending),
        "invested": float(invested),
        "categories": sorted(
            (
                {
                    "id": cid if cid is not None else 0,
                    "name": categories.get(cid, "Uncategorized"),
                    "total": float(total),
                    # Share of the year's net spend; a refund-heavy category
                    # can legitimately be negative.
                    "share": float(total / spending * 100) if spending else 0.0,
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


def month_detail(db: Session, month: str) -> dict:
    income_id = db.scalar(select(Category.id).where(Category.name == "Income"))
    txs = [
        t
        for t in _spending_transactions(db)
        if t.date.strftime("%Y-%m") == month
        and not _income_category(t, income_id)  # income-side rows, both signs
        and (t.amount < 0 or not _is_income(t, income_id))
    ]
    categories = {c.id: c.name for c in db.scalars(select(Category))}

    by_category: dict[int | None, Decimal] = defaultdict(Decimal)
    by_merchant: dict[str, dict] = defaultdict(lambda: {"total": Decimal(0), "count": 0})
    for tx in txs:
        gbp = -to_gbp(tx.amount, tx.account.currency)  # refunds subtract
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


def coverage(db: Session) -> dict:
    """Which accounts have data in which months — every transaction counts,
    transfers included, because this is about data presence, not spending."""
    from sqlalchemy import func

    rows = db.execute(
        select(
            Transaction.account_id,
            func.strftime("%Y-%m", Transaction.date).label("month"),
            func.count(Transaction.id),
            func.max(Transaction.date),
        ).group_by(Transaction.account_id, "month")
    ).all()

    accounts = {a.id: a for a in db.scalars(select(Account))}
    by_account: dict[int, dict] = {}
    all_months: set[str] = set()
    for account_id, month, count, latest in rows:
        entry = by_account.setdefault(
            account_id, {"months": {}, "latest": None}
        )
        entry["months"][month] = count
        entry["latest"] = max(entry["latest"] or latest, latest)
        all_months.add(month)

    return {
        "months": sorted(all_months),
        "accounts": sorted(
            (
                {
                    "id": account_id,
                    "name": accounts[account_id].name,
                    "provider": accounts[account_id].provider,
                    "kind": accounts[account_id].kind,
                    "latest": str(entry["latest"]),
                    "total": sum(entry["months"].values()),
                    "months": entry["months"],
                }
                for account_id, entry in by_account.items()
                if account_id in accounts
            ),
            key=lambda a: a["name"],
        ),
    }


def category_merchants(db: Session, category_id: int, months: int = 12) -> dict:
    """Top merchants within one category (id 0 = uncategorized) across the
    same calendar window as monthly_overview, GBP-converted, with refund
    semantics: positive categorized amounts subtract from their merchant."""
    txs = _spending_transactions(db)
    income_id = db.scalar(select(Category.id).where(Category.name == "Income"))
    # Same window anchor as monthly_overview: latest month with any activity.
    all_keys = sorted({t.date.strftime("%Y-%m") for t in txs})
    window = set(_calendar_window(all_keys, months))
    wanted = None if category_id == 0 else category_id

    by_merchant: dict[str, dict] = defaultdict(lambda: {"total": Decimal(0), "count": 0})
    for tx in txs:
        if tx.category_id != wanted or tx.date.strftime("%Y-%m") not in window:
            continue
        if tx.amount > 0 and _is_income(tx, income_id):
            continue  # income, not a refund
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


# month_insights thresholds: a category is a spike when it beats its
# trailing-12 median by both this ratio and this floor (the floor keeps a
# £4 → £8 coffee month out of the report).
SPIKE_RATIO = 1.5
SPIKE_MIN_DELTA = Decimal(25)
NEW_CATEGORY_MIN = Decimal(50)  # spend in a category with a £0 median
NEW_MERCHANT_MIN = Decimal(20)
LARGE_TX_MIN = Decimal(75)
CADENCE_DAYS = {"weekly": 7, "monthly": 30, "quarterly": 91, "yearly": 365}


def month_insights(db: Session, month: str) -> dict:
    """What changed this month: a ranked, plain-English diff of the month
    against the trailing 12 calendar months — category spikes vs the median,
    subscription price changes, recently lapsed subscriptions, first-ever
    merchants, and the largest single payments. Deterministic; no LLM."""
    txs = _spending_transactions(db)
    categories = {c.id: c.name for c in db.scalars(select(Category))}
    income_id = db.scalar(select(Category.id).where(Category.name == "Income"))

    by_month_cat: dict[str, dict] = defaultdict(lambda: defaultdict(Decimal))
    merchant_first: dict[str, str] = {}
    merchant_month_total: dict[str, Decimal] = defaultdict(Decimal)
    month_spending: list[tuple] = []
    for tx in txs:
        if _income_category(tx, income_id):
            continue
        if tx.amount > 0 and _is_income(tx, income_id):
            continue
        key = tx.date.strftime("%Y-%m")
        gbp = -to_gbp(tx.amount, tx.account.currency)  # refunds subtract
        by_month_cat[key][tx.category_id] += gbp
        name = tx.merchant or tx.description
        if name not in merchant_first or key < merchant_first[name]:
            merchant_first[name] = key
        if key == month:
            merchant_month_total[name] += gbp
            if gbp > 0:
                month_spending.append((gbp, tx.date, tx.description, name))

    # Baseline: the 12 calendar months immediately before `month` that have
    # any data at all; a category absent from one of them counts as £0 there.
    baseline_keys = _calendar_window(
        sorted(k for k in by_month_cat if k < month), 12
    )
    findings: list[dict] = []

    if baseline_keys:
        spikes = []
        for cid, cur in by_month_cat.get(month, {}).items():
            base = median(by_month_cat[k].get(cid, Decimal(0)) for k in baseline_keys)
            name = categories.get(cid, "Uncategorized")
            if base <= 0:
                if cur >= NEW_CATEGORY_MIN:
                    spikes.append((cur, f"{name}: {_g(cur)} — usually nothing"))
            elif cur >= base * Decimal(SPIKE_RATIO) and cur - base >= SPIKE_MIN_DELTA:
                spikes.append(
                    (cur - base, f"{name}: {_g(cur)}, {_g(cur - base)} over its typical {_g(base)}")
                )
        spikes.sort(key=lambda s: -s[0])
        findings += [{"kind": "category_spike", "text": t} for _, t in spikes[:5]]

    recurring_items = recurring(db)
    recurring_merchants = {r["merchant"] for r in recurring_items}
    for r in recurring_items:
        if r["price_change"] != 0 and r["last_date"].startswith(month):
            arrow = "rose" if r["price_change"] > 0 else "dropped"
            findings.append(
                {
                    "kind": "price_change",
                    "text": f"{r['merchant']} {arrow} to {_g(Decimal(str(r['last_amount'])))} "
                    f"(typically {_g(Decimal(str(r['typical_amount'])))})",
                }
            )
        if not r["active"]:
            # Recently lapsed: the charge that was due this month never came.
            expected = date.fromisoformat(r["last_date"]) + timedelta(
                days=CADENCE_DAYS.get(r["cadence"], 30)
            )
            if expected.strftime("%Y-%m") == month:
                findings.append(
                    {
                        "kind": "lapsed",
                        "text": f"{r['merchant']} ({_g(Decimal(str(r['typical_amount'])))} "
                        f"{r['cadence']}) stopped charging — last seen {r['last_date']}",
                    }
                )

    if baseline_keys:  # without history, every merchant is trivially "new"
        new_merchants = sorted(
            (
                (total, name)
                for name, total in merchant_month_total.items()
                if merchant_first.get(name) == month and total >= NEW_MERCHANT_MIN
            ),
            reverse=True,
        )
        findings += [
            {"kind": "new_merchant", "text": f"First time at {name}: {_g(total)}"}
            for total, name in new_merchants[:5]
        ]

    # Notable single payments: routine recurring charges are excluded — the
    # rent being large every month is not news.
    notable = [
        s for s in month_spending if s[0] >= LARGE_TX_MIN and s[3] not in recurring_merchants
    ]
    notable.sort(reverse=True)
    findings += [
        {"kind": "large_transaction", "text": f"{desc} — {_g(amount)} on {day}"}
        for amount, day, desc, _name in notable[:3]
    ]

    return {"month": month, "findings": findings}


def _g(v: Decimal) -> str:
    """£-formatted whole pounds for insight sentences."""
    return f"£{float(v):,.0f}"
