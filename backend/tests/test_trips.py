from datetime import date
from decimal import Decimal

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.models import Account, Category, Transaction, Trip
from app.trips import TripReview, TripVerdict, candidates, review, trip_stats

from tests.test_categories import make_client


def make_db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    gbp = Account(name="Amex", provider="amex", kind="credit", currency="GBP")
    jpy = Account(name="Revolut JPY", provider="revolut", kind="current", currency="JPY")
    travel = Category(name="Travel")
    eating = Category(name="Eating Out")
    transfers = Category(name="Transfers")
    db.add_all([gbp, jpy, travel, eating, transfers])
    db.flush()
    trip = Trip(name="Japan", start_date=date(2026, 4, 12), end_date=date(2026, 4, 26))
    db.add(trip)
    db.flush()
    rows = [
        (gbp, date(2026, 2, 1), "BA FLIGHTS", "-600.00", travel.id),      # prepaid, BEFORE
        (jpy, date(2026, 4, 15), "RAMEN TOKYO", "-2000", eating.id),      # in window, JPY
        (gbp, date(2026, 4, 16), "NETFLIX", "-9.99", eating.id),          # in window, home life
        (gbp, date(2026, 5, 10), "HOTEL REFUND", "120.00", travel.id),    # AFTER, refund
        (gbp, date(2026, 4, 14), "CARD PAYMENT", "-500.00", transfers.id),  # excluded category
    ]
    for i, (acc, d, desc, amt, cat) in enumerate(rows):
        db.add(Transaction(
            account_id=acc.id, date=d, description=desc, merchant=desc,
            amount=Decimal(amt), category_id=cat, fingerprint=f"trip{i}",
        ))
    db.commit()
    return db, trip


class FakeMessages:
    def __init__(self, result):
        self.result = result

    def parse(self, **kwargs):
        class R:
            parsed_output = self.result
        return R()


class FakeClient:
    def __init__(self, result):
        self.messages = FakeMessages(result)


def test_candidates_cover_extended_window_and_skip_excluded():
    db, trip = make_db()
    cands = candidates(db, trip)
    descs = {t.description for t in cands}
    assert descs == {"BA FLIGHTS", "RAMEN TOKYO", "NETFLIX", "HOTEL REFUND"}


def test_review_uses_llm_verdicts_and_assignment_flow():
    db, trip = make_db()
    cands = {t.description: t.id for t in candidates(db, trip)}
    result = review(db, trip, client=FakeClient(TripReview(verdicts=[
        TripVerdict(id=cands["BA FLIGHTS"], belongs=True),
        TripVerdict(id=cands["RAMEN TOKYO"], belongs=True),
        TripVerdict(id=cands["NETFLIX"], belongs=False),
        TripVerdict(id=cands["HOTEL REFUND"], belongs=True),
    ])))
    by_id = {s["id"]: s for s in result["suggestions"]}
    assert by_id[cands["BA FLIGHTS"]]["belongs"] is True
    assert by_id[cands["NETFLIX"]]["belongs"] is False
    assert by_id[cands["BA FLIGHTS"]]["in_window"] is False

    # Assign the accepted ones and check the stats.
    for desc in ("BA FLIGHTS", "RAMEN TOKYO", "HOTEL REFUND"):
        db.get(Transaction, cands[desc]).trip_id = trip.id
    db.commit()
    stats = trip_stats(db, trip)
    assert stats["transactions"] == 3
    assert stats["total"] == 490.0  # 600 + 10 (JPY) - 120 refund
    assert stats["days"] == 15
    names = [c["name"] for c in stats["by_category"]]
    assert names == ["Travel", "Eating Out"]  # 480 travel net, 10 eating


def test_trip_api_crud_and_assign():
    client = make_client()
    r = client.post("/api/trips", json={"name": "Japan", "start_date": "2026-04-12",
                                        "end_date": "2026-04-26"})
    assert r.status_code == 200
    tid = r.json()["id"]
    assert client.post("/api/trips", json={"name": "Japan", "start_date": "2026-04-12",
                                           "end_date": "2026-04-26"}).status_code == 409

    body = {"account_id": 0, "date": "2026-04-15", "description": "Sushi", "amount": -30.0}
    assert client.post("/api/transactions", json=body).status_code == 200
    tx = client.get("/api/transactions").json()["items"][0]

    r = client.post(f"/api/trips/{tid}/assign", json={"add": [tx["id"]]})
    assert r.status_code == 200 and r.json()["trip"]["total"] == 30.0

    # A second trip can't steal an assigned transaction.
    r2 = client.post("/api/trips", json={"name": "Peru", "start_date": "2026-04-10",
                                         "end_date": "2026-04-20"})
    assert client.post(f"/api/trips/{r2.json()['id']}/assign",
                       json={"add": [tx["id"]]}).status_code == 409

    assert client.delete(f"/api/trips/{tid}").status_code == 200
    assert all(t["name"] != "Japan" for t in client.get("/api/trips").json())


def test_review_verdicts_persist_and_reopen_without_llm():
    from app.trips import stored_suggestions

    db, trip = make_db()
    assert stored_suggestions(db, trip) is None  # never reviewed

    cands = {t.description: t.id for t in candidates(db, trip)}
    review(db, trip, client=FakeClient(TripReview(verdicts=[
        TripVerdict(id=cands["BA FLIGHTS"], belongs=True),
        TripVerdict(id=cands["RAMEN TOKYO"], belongs=True),
        TripVerdict(id=cands["NETFLIX"], belongs=False),
        TripVerdict(id=cands["HOTEL REFUND"], belongs=True),
    ])))

    saved = stored_suggestions(db, trip)  # no client involved
    assert saved is not None and saved["stored"] is True
    by_id = {s["id"]: s["belongs"] for s in saved["suggestions"]}
    assert by_id[cands["BA FLIGHTS"]] is True
    assert by_id[cands["NETFLIX"]] is False

    # A re-review replaces the verdicts rather than duplicating them.
    review(db, trip, client=FakeClient(TripReview(verdicts=[
        TripVerdict(id=cands["NETFLIX"], belongs=True),
    ])))
    saved = stored_suggestions(db, trip)
    assert {s["id"] for s in saved["suggestions"]} == set(cands.values())
