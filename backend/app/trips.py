"""Trip cost tracking.

A Trip groups transactions across categories, accounts, currencies, and
Splitwise corrections; membership is orthogonal to categorization, so trip
totals never distort the regular stats.

Candidates come from the trip window plus 4 months before and 1 month after
(prepaid flights and hotels live before departure; late charges and refunds
trail it). Each candidate is reviewed by Claude — does this payment belong to
this trip? — and every verdict is only a pre-ticked suggestion: assignment is
always confirmed by the user.

Privacy note (documented in PRIVACY.md): unlike the merchants-only
categorization flow, the trip review deliberately sends each candidate's
date, amount, currency, category, and description to the Anthropic API. The
user opted into this per-transaction review explicitly.
"""

from datetime import timedelta
from decimal import Decimal

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from .llm import MODEL, _make_client, _parse_batch
from .models import Category, Transaction, Trip, TripReviewVerdict
from .stats import GBP_RATES, load_rate_book, to_gbp

BEFORE_DAYS = 120  # flights are often booked ~4 months out
AFTER_DAYS = 30
BATCH_SIZE = 100

# Categories that can never be trip spending.
NEVER_TRIP = ("Transfers", "Investing", "Income")


class TripVerdict(BaseModel):
    id: int
    belongs: bool


class TripReview(BaseModel):
    verdicts: list[TripVerdict]


def candidates(db: Session, trip: Trip) -> list[Transaction]:
    """Unassigned (or already this trip's), non-transfer transactions in the
    extended window, excluding categories that can't be trip spending."""
    excluded = set(
        db.scalars(select(Category.id).where(Category.name.in_(NEVER_TRIP)))
    )
    lo = trip.start_date - timedelta(days=BEFORE_DAYS)
    hi = trip.end_date + timedelta(days=AFTER_DAYS)
    txs = db.scalars(
        select(Transaction)
        .options(joinedload(Transaction.account))
        .where(
            Transaction.date >= lo,
            Transaction.date <= hi,
            Transaction.transfer_peer_id.is_(None),
            (Transaction.trip_id.is_(None)) | (Transaction.trip_id == trip.id),
        )
        .order_by(Transaction.date)
    ).all()
    return [t for t in txs if t.category_id not in excluded]


def _heuristic(trip: Trip, tx: Transaction, travel_id: int | None) -> bool:
    """Fallback when no API key is configured: in-window foreign-currency or
    Travel rows; outside the window only Travel-category rows."""
    in_window = trip.start_date <= tx.date <= trip.end_date
    if in_window:
        return tx.account.currency != "GBP" or tx.category_id == travel_id
    return tx.category_id == travel_id


def review(db: Session, trip: Trip, client=None) -> dict:
    """Suggestions for the trip: every candidate with a belongs verdict from
    Claude (or the heuristic when no key is configured). Changes nothing."""
    txs = candidates(db, trip)
    categories = {c.id: c.name for c in db.scalars(select(Category))}
    travel_id = next((cid for cid, n in categories.items() if n == "Travel"), None)

    verdicts: dict[int, bool] = {}
    llm_used = False
    try:
        if client is None:
            client = _make_client()
        llm_used = True
    except RuntimeError:
        client = None

    if client is not None:
        system = (
            "You review personal bank transactions and decide whether each one "
            "belongs to a specific trip. Prepaid bookings (flights, hotels, "
            "trains, travel insurance, visas) commonly appear up to 4 months "
            "BEFORE the trip window; late charges and refunds up to a month "
            "after. Everyday home-country spending (rent, subscriptions, "
            "groceries near home, gym) does NOT belong even when dated inside "
            "the window. When genuinely unsure, answer belongs=false."
        )
        for start in range(0, len(txs), BATCH_SIZE):
            batch = txs[start : start + BATCH_SIZE]
            lines = []
            for t in batch:
                window = (
                    "IN-WINDOW"
                    if trip.start_date <= t.date <= trip.end_date
                    else ("BEFORE" if t.date < trip.start_date else "AFTER")
                )
                lines.append(
                    f"- id {t.id} | {t.date} ({window}) | {t.account.currency} "
                    f"{t.amount} | {categories.get(t.category_id, 'uncategorized')} "
                    f"| {t.description}"
                )
            prompt = (
                f"Trip: {trip.name!r}, {trip.start_date} to {trip.end_date}.\n"
                "For each transaction, does it belong to this trip?\n\n"
                + "\n".join(lines)
                + "\n\nReturn a verdict for every id, exactly once each."
            )
            response = _parse_batch(
                client,
                model=MODEL,
                max_tokens=16000,
                system=system,
                messages=[{"role": "user", "content": prompt}],
                output_format=TripReview,
            )
            result = response.parsed_output
            if result is not None:
                batch_ids = {t.id for t in batch}
                for v in result.verdicts:
                    if v.id in batch_ids:
                        verdicts[v.id] = v.belongs

    suggestions = []
    for t in txs:
        belongs = verdicts.get(t.id) if client is not None else None
        if belongs is None:
            belongs = _heuristic(trip, t, travel_id)
        suggestions.append(_suggestion(trip, t, belongs))

    # Persist the verdicts so the checklist survives navigation and can be
    # reopened instantly without another API pass.
    for old in db.scalars(
        select(TripReviewVerdict).where(TripReviewVerdict.trip_id == trip.id)
    ):
        db.delete(old)
    db.flush()  # deletes must hit before the re-inserts of the same keys
    for s in suggestions:
        db.add(
            TripReviewVerdict(trip_id=trip.id, transaction_id=s["id"], belongs=s["belongs"])
        )
    db.commit()
    return {"llm_used": llm_used, "stored": False, "suggestions": suggestions}


def _suggestion(trip: Trip, t: Transaction, belongs: bool) -> dict:
    return {
        "id": t.id,
        "date": t.date,
        "description": t.description,
        "amount": float(t.amount),
        "currency": t.account.currency,
        "category_id": t.category_id,
        "in_window": trip.start_date <= t.date <= trip.end_date,
        "assigned": t.trip_id == trip.id,
        "belongs": belongs,
    }


def stored_suggestions(db: Session, trip: Trip) -> dict | None:
    """The last review's checklist, rebuilt from persisted verdicts — no LLM
    call. None when this trip has never been reviewed. Candidates that
    appeared since the review (new imports) fall back to the heuristic."""
    stored = {
        v.transaction_id: v.belongs
        for v in db.scalars(
            select(TripReviewVerdict).where(TripReviewVerdict.trip_id == trip.id)
        )
    }
    if not stored:
        return None
    categories = {c.id: c.name for c in db.scalars(select(Category))}
    travel_id = next((cid for cid, n in categories.items() if n == "Travel"), None)
    suggestions = [
        _suggestion(trip, t, stored.get(t.id, _heuristic(trip, t, travel_id)))
        for t in candidates(db, trip)
    ]
    return {"llm_used": None, "stored": True, "suggestions": suggestions}


def trip_stats(db: Session, trip: Trip) -> dict:
    """Total and per-category cost of the trip, GBP-converted, refunds
    offsetting (a cancelled hotel reduces the trip's cost)."""
    txs = db.scalars(
        select(Transaction)
        .options(joinedload(Transaction.account))
        .where(Transaction.trip_id == trip.id, Transaction.transfer_peer_id.is_(None))
    ).all()
    categories = {c.id: c.name for c in db.scalars(select(Category))}
    rates = load_rate_book(db)
    total = Decimal(0)
    by_category: dict[int | None, Decimal] = {}
    for t in txs:
        if t.account.currency not in GBP_RATES:
            continue
        gbp = -to_gbp(t.amount, t.account.currency, t.date, rates)
        total += gbp
        by_category[t.category_id] = by_category.get(t.category_id, Decimal(0)) + gbp
    days = (trip.end_date - trip.start_date).days + 1
    return {
        "id": trip.id,
        "name": trip.name,
        "start_date": trip.start_date,
        "end_date": trip.end_date,
        "days": days,
        "transactions": len(txs),
        "total": float(total),
        "per_day": float(total / days) if days else 0.0,
        "by_category": sorted(
            (
                {
                    "id": cid if cid is not None else 0,
                    "name": categories.get(cid, "Uncategorized"),
                    "total": float(v),
                }
                for cid, v in by_category.items()
            ),
            key=lambda c: -c["total"],
        ),
    }
