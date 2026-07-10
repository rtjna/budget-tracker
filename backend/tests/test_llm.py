from datetime import date
from decimal import Decimal

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.llm import BatchResult, MerchantAssignment, categorize_merchants
from app.models import Account, Category, LlmMerchantCache, Transaction


class FakeResponse:
    def __init__(self, parsed):
        self.parsed_output = parsed


class FakeMessages:
    def __init__(self, result):
        self.result = result
        self.calls = []

    def parse(self, **kwargs):
        self.calls.append(kwargs)
        return FakeResponse(self.result)


class FakeClient:
    def __init__(self, result):
        self.messages = FakeMessages(result)


def make_db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    acc = Account(name="Amex", provider="amex", kind="credit", currency="GBP")
    groceries = Category(name="Groceries")
    coffee = Category(name="Coffee")
    db.add_all([acc, groceries, coffee])
    db.flush()
    for i, desc in enumerate(["OCADO RETAIL LTD", "MYSTERY SHOP 42", "BLANK STREET LONDON"]):
        db.add(Transaction(
            account_id=acc.id, date=date(2026, 7, 1), description=desc,
            merchant=desc, amount=Decimal("-5"), fingerprint=f"fp{i}",
        ))
    db.commit()
    return db, groceries, coffee


def test_applies_confident_and_caches_unsure():
    db, groceries, coffee = make_db()
    client = FakeClient(BatchResult(assignments=[
        MerchantAssignment(merchant="OCADO RETAIL LTD", category="Groceries"),
        MerchantAssignment(merchant="BLANK STREET LONDON", category="Coffee"),
        MerchantAssignment(merchant="MYSTERY SHOP 42", category="UNSURE"),
    ]))
    stats = categorize_merchants(db, client=client)
    assert stats == {"asked": 3, "categorized": 2, "unsure": 1, "transactions": 2}

    txs = {t.merchant: t for t in db.scalars(select(Transaction))}
    assert txs["OCADO RETAIL LTD"].category_id == groceries.id
    assert txs["OCADO RETAIL LTD"].category_source == "llm"
    assert txs["MYSTERY SHOP 42"].category_id is None

    # Second run: everything cached, no API calls made
    stats2 = categorize_merchants(db, client=client)
    assert stats2["asked"] == 0
    assert len(client.messages.calls) == 1


def test_hallucinated_merchant_or_category_ignored():
    db, groceries, _ = make_db()
    client = FakeClient(BatchResult(assignments=[
        MerchantAssignment(merchant="NOT A REAL MERCHANT", category="Groceries"),
        MerchantAssignment(merchant="OCADO RETAIL LTD", category="Made Up Category"),
    ]))
    stats = categorize_merchants(db, client=client)
    # Invented merchant dropped; invented category treated as unsure
    assert stats["categorized"] == 0
    assert stats["unsure"] == 1
    cached = db.scalars(select(LlmMerchantCache)).all()
    assert {c.merchant for c in cached} == {"OCADO RETAIL LTD"}


def test_prompt_contains_no_amounts():
    db, _, _ = make_db()
    client = FakeClient(BatchResult(assignments=[]))
    categorize_merchants(db, client=client)
    sent = str(client.messages.calls[0])
    assert "-5" not in sent and "amount" not in sent.lower()
