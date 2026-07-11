from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.models import Account, Category, LlmMerchantCache, Rule, Transaction

from tests.test_security import make_client as _make_client


def make_client():
    client = _make_client()
    client.headers.update({"X-Budget-App": "1"})  # CSRF header (H2)
    return client


def make_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


# --- M6: SQLite foreign key enforcement ---


def test_foreign_keys_enforced():
    db = make_session()
    db.add(
        Transaction(
            account_id=999,  # no such account
            date=date(2026, 7, 1),
            description="Dangling",
            amount=Decimal("-1.00"),
            fingerprint="dangling",
        )
    )
    with pytest.raises(IntegrityError):
        db.commit()


def test_foreign_keys_enforced_for_category_reference():
    db = make_session()
    account = Account(name="A", provider="manual", kind="cash", currency="GBP")
    db.add(account)
    db.flush()
    db.add(
        Transaction(
            account_id=account.id,
            date=date(2026, 7, 1),
            description="Bad category",
            amount=Decimal("-1.00"),
            category_id=12345,  # no such category
            fingerprint="badcat",
        )
    )
    with pytest.raises(IntegrityError):
        db.commit()


# --- M6: delete_category blocks on every referencing table ---


def _create_category(client, name="Doomed"):
    return client.post("/api/categories", json={"name": name}).json()["id"]


def test_delete_unused_category_ok():
    client = make_client()
    cid = _create_category(client)
    assert client.delete(f"/api/categories/{cid}").status_code == 200


def test_delete_category_blocked_by_rule():
    client = make_client()
    cid = _create_category(client)
    r = client.post("/api/rules", json={"pattern": "TESCO", "match": "contains", "category_id": cid})
    assert r.status_code == 200
    resp = client.delete(f"/api/categories/{cid}")
    assert resp.status_code == 409
    assert "1 rules" in resp.json()["detail"]


def test_delete_category_blocked_by_llm_cache(monkeypatch):
    client = make_client()
    cid = _create_category(client)
    # Insert a cache row directly through the overridden session factory.
    import app.main as main

    db = next(main.app.dependency_overrides[main.get_db]())
    db.add(LlmMerchantCache(merchant="TESCO", category_id=cid))
    db.commit()

    resp = client.delete(f"/api/categories/{cid}")
    assert resp.status_code == 409
    assert "LLM merchant cache" in resp.json()["detail"]


def test_delete_category_blocked_by_transactions_and_subcategory():
    client = make_client()
    cid = _create_category(client, "Parent")
    child = client.post("/api/categories", json={"name": "Child", "parent_id": cid}).json()["id"]
    body = {"account_id": 0, "date": "2026-07-01", "description": "Tesco",
            "amount": -5.0, "category_id": cid}
    assert client.post("/api/transactions", json=body).status_code == 200

    resp = client.delete(f"/api/categories/{cid}")
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert "1 transactions" in detail and "1 subcategories" in detail

    # Missing category is a 404, not a silent success.
    assert client.delete("/api/categories/98765").status_code == 404
    # The child alone deletes fine.
    tx_id = client.get("/api/transactions").json()["items"][0]["id"]
    assert client.delete(f"/api/transactions/{tx_id}").status_code == 200
    assert client.delete(f"/api/categories/{child}").status_code == 200
