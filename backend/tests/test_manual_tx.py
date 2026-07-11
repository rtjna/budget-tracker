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
    return TestClient(main.app)


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
