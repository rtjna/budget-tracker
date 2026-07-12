import pytest

from app.importers import barclays_pdf
from app.importers.barclays_pdf import StatementDecodeError, _decode, _parse, _validate, _year_for
from app.importers.base import ParsedRow
from datetime import date
from decimal import Decimal

from tests.synthetic_statements import barclays_pages


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


@pytest.mark.parametrize("seed", range(8))
def test_autoderives_rescrambled_statement(seed):
    """The parser self-calibrates on a freshly re-scrambled font and reconciles:
    letters from anchors, digits from the running balance / FSR number."""
    rows = _parse(barclays_pages(seed))
    assert [r.amount for r in rows] == [Decimal("500.00"), Decimal("-40.00"), Decimal("-200.00")]
    assert [r.date for r in rows] == [date(2026, 6, 2), date(2026, 6, 5), date(2026, 7, 10)]
    # capital-B (newline sentinel) restored; unknown description letters -> '?'
    assert rows[0].description == "Payment Received"
    assert rows[2].description.startswith("Rent Bakshi")


def test_rejects_when_totals_do_not_reconcile(monkeypatch):
    """A corrupted glance total makes every candidate cipher fail to reconcile,
    so the import is refused loudly rather than writing garbage."""
    pages = barclays_pages(0)
    # Break the hardcoded fast path so derivation runs, then break reconciliation
    # by forcing the glance 'in' total to a wrong value.
    real = barclays_pdf._collect_glance

    def bad_glance(page_text, glance):
        real(page_text, glance)
        if "in" in glance:
            glance["in"] += Decimal("1.00")

    monkeypatch.setattr(barclays_pdf, "_collect_glance", bad_glance)
    with pytest.raises(StatementDecodeError):
        _parse(pages)
