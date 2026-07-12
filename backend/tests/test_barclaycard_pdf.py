from decimal import Decimal

import pytest

from app.importers.barclaycard_pdf import (
    _amount_from,
    _month_from_tail,
    _validate,
    decode_co,
    decode_row,
)
from app.importers.barclays_pdf import StatementDecodeError
from app.importers.base import ParsedRow
from datetime import date


def test_row_cipher_decodes_known_strings():
    assert decode_row("ä‡\x9fž·™\x9dà") != ""  # smoke: no crash on unknowns
    assert decode_row("ñ·–‘˜’\x9d¢¢ç\x8f››¢à") == "Revolut**1788*,"
    assert decode_row("¸ƒà\x81\x81\x81\x7f\x81\x81") == "£2,000.00"


def test_co_cipher_decodes_known_strings():
    assert decode_co("àˆ\x90Š‰‹‡†") == "Payments"
    assert decode_co("\x8dá¢š——’——") == "£7,500.00"


def test_unknown_digit_glyph_is_three():
    # Every digit except 3 is mapped, so ? in a digit position must be 3.
    assert _amount_from("£1,?45.6?") == Decimal("1345.63")
    assert _amount_from("no amount here") is None


def test_month_tails():
    assert _month_from_tail("?un") == 6      # Jun with unknown J
    assert _month_from_tail("?uly") == 7     # July in the Co font (y present)
    assert _month_from_tail("?anuary") == 1  # Jan beats May despite shared 'a'
    assert _month_from_tail("?a") == 5       # May with dropped y
    assert _month_from_tail("?ebruary") == 2
    assert _month_from_tail("???") is None


def test_validation_rejects_unbalanced():
    rows = [ParsedRow(date=date(2026, 6, 29), description="x", amount=Decimal("2200"))]
    anchors = {
        "previous": (0, Decimal("500.00")),
        "payments": (0, Decimal("2700.00")),
        "spend": (0, Decimal("2700.00")),
        "new": (0, Decimal("500.00")),
    }
    with pytest.raises(StatementDecodeError, match="reconcile"):
        _validate(rows, {"in": Decimal("2200"), "out": Decimal(0)}, anchors)
