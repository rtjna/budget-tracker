from decimal import Decimal

from app.monzo import map_transaction, _account_name


def test_map_prefers_merchant_name():
    t = {"amount": -460, "merchant": {"name": "Pret A Manger"},
         "counterparty": {}, "description": "PRET A MANGER LONDON GBR"}
    assert map_transaction(t) == ("Pret A Manger", Decimal("-4.60"))


def test_map_counterparty_for_p2p():
    t = {"amount": 20000, "merchant": None,
         "counterparty": {"name": "Ruben Tjon-A-Meeuw"}, "description": "Faster payment"}
    assert map_transaction(t) == ("Ruben Tjon-A-Meeuw", Decimal("200.00"))


def test_map_skips_declined_and_zero():
    assert map_transaction({"amount": -100, "decline_reason": "INSUFFICIENT_FUNDS"}) is None
    assert map_transaction({"amount": 0, "merchant": None, "counterparty": {}}) is None


def test_account_naming():
    assert _account_name({"type": "uk_retail"}) == "Monzo"
    assert _account_name({"type": "uk_retail_joint"}) == "Monzo Joint"
