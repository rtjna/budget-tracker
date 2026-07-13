from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.main as main
from app.db import Base


def make_client(monkeypatch):
    # StaticPool: a plain :memory: engine gives every pooled connection its
    # own empty database.
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    maker = sessionmaker(bind=engine)

    def get_db():
        db = maker()
        try:
            yield db
        finally:
            db.close()

    main.app.dependency_overrides[main.get_db] = get_db
    # base_url: TrustedHostMiddleware only allows localhost/127.0.0.1.
    # X-Budget-App: required on state-changing requests (CSRF protection).
    return TestClient(main.app, base_url="http://localhost", headers={"X-Budget-App": "1"})


def test_manual_create_and_delete(monkeypatch):
    client = make_client(monkeypatch)
    r = client.post("/api/transactions", json={
        "account_id": 0, "date": "2026-07-10", "description": "Farmers market",
        "amount": -12.5, "category_id": None,
    })
    assert r.status_code == 200
    tx_id = r.json()["id"]

    listed = client.get("/api/transactions").json()
    assert listed["total"] == 1
    assert listed["items"][0]["manual"] is True
    accounts = client.get("/api/accounts").json()
    assert any(a["name"] == "Cash" and a["provider"] == "manual" for a in accounts)

    assert client.delete(f"/api/transactions/{tx_id}").status_code == 200
    assert client.get("/api/transactions").json()["total"] == 0


def test_rejects_empty_and_zero(monkeypatch):
    client = make_client(monkeypatch)
    bad = {"account_id": 0, "date": "2026-07-10", "description": "  ", "amount": -5}
    assert client.post("/api/transactions", json=bad).status_code == 422
    bad2 = {"account_id": 0, "date": "2026-07-10", "description": "x", "amount": 0}
    assert client.post("/api/transactions", json=bad2).status_code == 422


def test_accounts_report_last_imported_and_provider_filter():
    from tests.test_categories import make_client

    client = make_client()
    csv_text = "Date,Description,Amount\n01/07/2026,TESCO,10.00\n"
    r = client.post("/api/imports", files={"file": ("amex.csv", csv_text, "text/csv")})
    assert r.status_code == 200

    accounts = client.get("/api/accounts").json()
    amex = next(a for a in accounts if a["provider"] == "amex")
    assert amex["last_imported"] is not None

    # Manual account has no import batches.
    body = {"account_id": 0, "date": "2026-07-01", "description": "Cash thing", "amount": -1.0}
    assert client.post("/api/transactions", json=body).status_code == 200
    accounts = client.get("/api/accounts").json()
    cash = next(a for a in accounts if a["provider"] == "manual")
    assert cash["last_imported"] is None

    # Provider filter returns only that provider's transactions.
    data = client.get("/api/transactions?provider=amex").json()
    assert data["total"] == 1 and data["items"][0]["description"] == "TESCO"
    assert client.get("/api/transactions?provider=manual").json()["total"] == 1


def test_transactions_month_filter_and_order():
    from tests.test_categories import make_client

    client = make_client()
    for d, desc in [("2026-06-15", "JUNE THING"), ("2026-07-01", "JULY FIRST"),
                    ("2026-07-20", "JULY LAST")]:
        body = {"account_id": 0, "date": d, "description": desc, "amount": -1.0}
        assert client.post("/api/transactions", json=body).status_code == 200

    july = client.get("/api/transactions?month=2026-07").json()
    assert july["total"] == 2
    assert [t["description"] for t in july["items"]] == ["JULY LAST", "JULY FIRST"]

    asc = client.get("/api/transactions?month=2026-07&order=date_asc").json()
    assert [t["description"] for t in asc["items"]] == ["JULY FIRST", "JULY LAST"]

    assert client.get("/api/transactions?month=07/2026").status_code == 422
    assert client.get("/api/transactions?order=amount").status_code == 422
