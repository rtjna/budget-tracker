from datetime import date
from decimal import Decimal

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.models import Account, Transaction
from app.transfers import detect_transfers


def make_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def add_tx(db, account, day, description, amount, **kw):
    tx = Transaction(
        account_id=account.id,
        date=day,
        description=description,
        merchant=description.upper(),
        amount=Decimal(amount),
        fingerprint=f"{account.id}|{day}|{description}|{amount}",
        **kw,
    )
    db.add(tx)
    db.flush()
    return tx


def make_accounts(db):
    amex = Account(name="Amex", provider="amex", kind="credit", currency="GBP")
    rev = Account(name="Revolut", provider="revolut", kind="current", currency="GBP")
    jpy = Account(name="Revolut JPY", provider="revolut", kind="current", currency="JPY")
    db.add_all([amex, rev, jpy])
    db.flush()
    return amex, rev, jpy


def test_cc_payment_matched():
    db = make_session()
    amex, rev, _ = make_accounts(db)
    pay_in = add_tx(db, amex, date(2026, 6, 26), "PAYMENT RECEIVED - THANK YOU", "4398.35")
    pay_out = add_tx(db, rev, date(2026, 6, 25), "To American Express", "-4398.35")
    db.commit()

    assert detect_transfers(db) == 1
    assert pay_in.transfer_peer_id == pay_out.id
    assert pay_out.transfer_peer_id == pay_in.id


def test_small_identical_purchases_not_matched():
    db = make_session()
    amex, rev, _ = make_accounts(db)
    add_tx(db, amex, date(2026, 6, 10), "PRET A MANGER LONDON", "-4.60")
    add_tx(db, rev, date(2026, 6, 11), "Boots", "4.60")  # refund, coincidence
    db.commit()

    assert detect_transfers(db) == 0


def test_large_pair_needs_transferish_keyword_on_one_leg():
    # One transfer-ish leg ("savings") is enough; the other can be anything.
    db = make_session()
    _, rev, _ = make_accounts(db)
    other = Account(name="Barclays", provider="barclays", kind="current", currency="GBP")
    db.add(other)
    db.flush()
    a = add_tx(db, other, date(2026, 6, 1), "R TJONAMEEUW", "-500.00")
    b = add_tx(db, rev, date(2026, 6, 1), "From savings pot", "500.00")
    db.commit()

    assert detect_transfers(db) == 1
    assert a.transfer_peer_id == b.id


def test_salary_vs_rent_same_amount_not_linked():
    # Regression (M1): a £1,200 salary and a £1,200 rent payment in the same
    # week are not a transfer — the old large-amount bypass linked them.
    db = make_session()
    _, rev, _ = make_accounts(db)
    barclays = Account(name="Barclays", provider="barclays", kind="current", currency="GBP")
    db.add(barclays)
    db.flush()
    salary = add_tx(db, barclays, date(2026, 6, 25), "ACME LTD SALARY", "1200.00")
    rent = add_tx(db, rev, date(2026, 6, 26), "RENT J SMITH LANDLORD", "-1200.00")
    db.commit()

    assert detect_transfers(db) == 0
    assert salary.transfer_peer_id is None and rent.transfer_peer_id is None


def test_small_refund_vs_card_payment_not_linked():
    # Regression (M1): bare "PAYMENT" in a card purchase description used to
    # satisfy the transfer-ish requirement and link it to a coincidental
    # same-amount refund.
    db = make_session()
    amex, rev, _ = make_accounts(db)
    add_tx(db, amex, date(2026, 6, 10), "CARD PAYMENT PRET A MANGER", "-4.60")
    add_tx(db, rev, date(2026, 6, 11), "Boots refund", "4.60")
    db.commit()

    assert detect_transfers(db) == 0


def test_amex_bill_payment_still_links():
    # Real data relies on this pairing: Amex statement credit vs. the bank's
    # "Bill Payment to American Exp".
    db = make_session()
    amex, _, _ = make_accounts(db)
    barclays = Account(name="Barclays", provider="barclays", kind="current", currency="GBP")
    db.add(barclays)
    db.flush()
    credit = add_tx(db, amex, date(2026, 6, 27), "PAYMENT RECEIVED - THANK YOU", "823.10")
    debit = add_tx(db, barclays, date(2026, 6, 26), "Bill Payment to American Exp", "-823.10")
    db.commit()

    assert detect_transfers(db) == 1
    assert credit.transfer_peer_id == debit.id


def test_fx_exchange_matched_across_currencies():
    db = make_session()
    _, rev, jpy = make_accounts(db)
    out = add_tx(db, rev, date(2026, 5, 1), "Exchanged to JPY", "-20.00")
    inn = add_tx(db, jpy, date(2026, 5, 1), "Exchanged to JPY", "3000")
    db.commit()

    assert detect_transfers(db) == 1
    assert out.transfer_peer_id == inn.id


def test_idempotent():
    db = make_session()
    amex, rev, _ = make_accounts(db)
    add_tx(db, amex, date(2026, 6, 26), "PAYMENT RECEIVED - THANK YOU", "100.00")
    add_tx(db, rev, date(2026, 6, 26), "To American Express", "-100.00")
    db.commit()

    assert detect_transfers(db) == 1
    assert detect_transfers(db) == 0
    linked = [t for t in db.scalars(select(Transaction)) if t.transfer_peer_id is not None]
    assert len(linked) == 2


def test_fx_exchange_to_missing_pocket_does_not_steal_other_leg():
    db = make_session()
    _, rev, jpy = make_accounts(db)
    # Same day: an exchange into a pocket that's absent from the export (THB)
    # and a legitimate JPY exchange. THB must stay unmatched.
    thb_out = add_tx(db, rev, date(2026, 5, 1), "Exchanged to THB", "-616.11")
    jpy_out = add_tx(db, rev, date(2026, 5, 1), "Exchanged to JPY", "-20.00")
    jpy_in = add_tx(db, jpy, date(2026, 5, 1), "Exchanged to JPY", "3000")
    db.commit()

    assert detect_transfers(db) == 1
    assert thb_out.transfer_peer_id is None
    assert jpy_out.transfer_peer_id == jpy_in.id
