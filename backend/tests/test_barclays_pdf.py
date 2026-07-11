import pytest

from app.importers.barclays_pdf import StatementDecodeError, _decode, _validate, _year_for
from app.importers.base import ParsedRow
from datetime import date
from decimal import Decimal


def test_cipher_decodes_known_strings():
    assert _decode("™Ž†„Ž š†ˆ†‹‡\x8f") == "Start balance"
    assert _decode("’“ ”ƒ‹ · ’– ”ƒˆ “’“—") == "02 Jun - 01 Jul 2026"
    assert _decode("&\x8f‡\x8fâ.\x8fá ¢„\x7f\x90") == "Received From"
    # The decimal-point glyph extracts as a space; amounts arrive as a
    # pounds word plus a pence word, reassembled positionally.
    assert _decode("œ\x9džäê žà") == "4,398 35"


def test_year_rollover():
    # Dec -> Jan statement dated 2027: December rows belong to 2026
    period = (12, 1, 2027)
    assert _year_for(12, period) == 2026
    assert _year_for(1, period) == 2027
    # Same-year period
    assert _year_for(6, (6, 7, 2026)) == 2026


def test_validation_rejects_mismatched_totals():
    rows = [ParsedRow(date=date(2026, 6, 1), description="x", amount=Decimal("-10"))]
    with pytest.raises(StatementDecodeError, match="reconcile"):
        _validate(rows, {"in": Decimal(0), "out": Decimal("10")}, {"out": Decimal("99")})


def test_validation_accepts_matching_totals():
    rows = [ParsedRow(date=date(2026, 6, 1), description="x", amount=Decimal("-10"))]
    _validate(rows, {"in": Decimal(0), "out": Decimal("10")}, {"out": Decimal("10")})
