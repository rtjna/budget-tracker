from decimal import Decimal

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.categorize import apply_rules, normalize_merchant, seed_categories
from app.db import Base
from app.importing import import_file
from app.models import Category, Rule, Transaction


def make_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


AMEX_CSV = """Date,Description,Amount
09/07/2026,TESCO STORE 6545 6545TE LONDON,4.60
03/07/2026,TESCO STORE 6787 6787TE LONDON,30.67
08/07/2026,SOFU. COFFEE            London,5.00
"""


def test_normalize_merchant():
    assert normalize_merchant("TESCO STORE 6545 6545TE LONDON") == "TESCO STORE LONDON"
    assert normalize_merchant("TESCO STORE 6787 6787TE LONDON") == "TESCO STORE LONDON"
    assert normalize_merchant("AMAZON.CO.UK*8C3NF0TI5  AMAZON.CO.UK") == "AMAZON CO UK AMAZON"
    assert normalize_merchant("SOFU. COFFEE            London") == "SOFU COFFEE LONDON"


def test_seed_categories_idempotent():
    db = make_session()
    seed_categories(db)
    count = len(db.scalars(select(Category)).all())
    seed_categories(db)
    assert len(db.scalars(select(Category)).all()) == count > 0


def test_rules_apply_on_import_and_never_overwrite_human():
    db = make_session()
    seed_categories(db)
    groceries = db.scalar(select(Category).where(Category.name == "Groceries"))
    coffee = db.scalar(select(Category).where(Category.name == "Coffee"))
    db.add(Rule(match="merchant", pattern="TESCO STORE LONDON", category_id=groceries.id))
    db.commit()

    import_file(db, "amex.csv", AMEX_CSV)
    txs = {t.description: t for t in db.scalars(select(Transaction))}
    tesco = txs["TESCO STORE 6545 6545TE LONDON"]
    assert tesco.category_id == groceries.id
    assert tesco.category_source == "rule"
    sofu = txs["SOFU. COFFEE London"]
    assert sofu.category_id is None

    # Human overrides, then a conflicting rule must not clobber it
    sofu.category_id = coffee.id
    sofu.category_source = "human"
    db.commit()
    db.add(Rule(match="contains", pattern="SOFU", category_id=groceries.id))
    db.commit()
    apply_rules(db, list(db.scalars(select(Transaction))))
    db.commit()
    assert sofu.category_id == coffee.id
    assert sofu.category_source == "human"
