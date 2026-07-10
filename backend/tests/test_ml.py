from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app import ml
from app.db import Base
from app.models import Account, Category, Transaction


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(ml, "MODEL_PATH", tmp_path / "model.joblib")
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def seed(db):
    acc = Account(name="Amex", provider="amex", kind="credit", currency="GBP")
    groceries = Category(name="Groceries")
    coffee = Category(name="Coffee")
    db.add_all([acc, groceries, coffee])
    db.flush()
    n = 0
    for i in range(30):
        for desc, cat in [
            (f"TESCO STORE {1000+i} LONDON", groceries),
            (f"SAINSBURYS SMKT {i} LONDON", groceries),
            (f"PRET A MANGER {i} LONDON", coffee),
            (f"COSTA COFFEE {i} LONDON", coffee),
        ]:
            n += 1
            db.add(Transaction(
                account_id=acc.id, date=date(2026, 1, 1), description=desc,
                merchant=desc, amount=Decimal("-5"), category_id=cat.id,
                category_source="human", fingerprint=f"fp{n}",
            ))
    db.commit()
    return acc, groceries, coffee


def test_train_and_apply_confident(db):
    acc, groceries, coffee = seed(db)
    result = ml.train(db)
    assert result.trained_on == 120
    assert result.classes == 2
    assert result.holdout_accuracy is None or result.holdout_accuracy > 0.9

    tx = Transaction(
        account_id=acc.id, date=date(2026, 2, 1), description="TESCO STORE 9999 LEEDS",
        merchant="TESCO STORE LEEDS", amount=Decimal("-12"), fingerprint="new1",
    )
    db.add(tx)
    db.commit()
    stats = ml.apply_model(db)
    assert stats["applied"] == 1
    assert tx.category_id == groceries.id
    assert tx.category_source == "model"


def test_unfamiliar_merchant_left_for_review(db):
    seed(db)
    ml.train(db)
    tx = Transaction(
        account_id=1, date=date(2026, 2, 1), description="ZXQ 77 UNKNOWN THING",
        merchant="ZXQ UNKNOWN THING", amount=Decimal("-12"), fingerprint="new2",
    )
    db.add(tx)
    db.commit()
    ml.apply_model(db, threshold=0.95)
    assert tx.category_id is None


def test_model_never_trains_on_its_own_output(db):
    acc, groceries, _ = seed(db)
    db.add(Transaction(
        account_id=acc.id, date=date(2026, 1, 2), description="MODEL LABELED",
        merchant="MODEL LABELED", amount=Decimal("-1"), category_id=groceries.id,
        category_source="model", fingerprint="modelfp",
    ))
    db.commit()
    texts, _ = ml._training_data(db)
    assert not any("MODEL LABELED" in t for t in texts)
