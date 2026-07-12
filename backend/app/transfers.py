import re
from datetime import timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from .models import Transaction

# Descriptions that suggest a movement between own accounts rather than a
# purchase. One leg matching this is required for every same-currency pair —
# regardless of size — so that e.g. two genuine £4.60 coffees, or a salary
# and a same-amount rent payment, never pair up. Bare "PAYMENT" is
# deliberately not enough: card purchases ("CARD PAYMENT PRET") and rent
# ("RENT PAYMENT") contain it; only specific shapes like "PAYMENT RECEIVED",
# "BILL PAYMENT" and "PAYMENT TO/FROM" count.
TRANSFERISH = re.compile(
    r"PAYMENT RECEIVED|BILL PAYMENT|PAYMENT (TO|FROM)\b|TRANSFER|EXCHANGE|TOP ?UP"
    r"|THANK YOU|REVOLUT|AMEX|AMERICAN EXPRESS"
    r"|MONZO|BARCLAY|B/CARD|SAVING|STANDING ORDER|DIRECT DEBIT RECEIVED"
    r"|PAYMENT (\?|BY) DIRECT DEBIT",  # Barclaycard credit-side leg ('B' glyph unmapped)
    re.IGNORECASE,
)

EXCHANGE = re.compile(r"\bEXCHANGED? (TO|FROM)\b", re.IGNORECASE)

MAX_DATE_DIFF = timedelta(days=3)


def _unmatched(db: Session) -> list[Transaction]:
    return list(
        db.scalars(
            select(Transaction)
            .options(joinedload(Transaction.account))
            .where(Transaction.transfer_peer_id.is_(None))
        )
    )


def _link(a: Transaction, b: Transaction) -> None:
    a.transfer_peer_id = b.id
    b.transfer_peer_id = a.id


def _match_same_currency(txs: list[Transaction]) -> int:
    outs = [t for t in txs if t.amount < 0]
    ins_by_key: dict[tuple, list[Transaction]] = {}
    for t in txs:
        if t.amount > 0:
            ins_by_key.setdefault((t.account.currency, Decimal(t.amount)), []).append(t)

    linked = 0
    # Most recent first so partial matches favour recent, well-ordered data.
    for out in sorted(outs, key=lambda t: t.date, reverse=True):
        key = (out.account.currency, -Decimal(out.amount))
        candidates = [
            t
            for t in ins_by_key.get(key, [])
            if t.transfer_peer_id is None
            and t.account_id != out.account_id
            and abs(t.date - out.date) <= MAX_DATE_DIFF
        ]
        # At least one leg must look transfer-ish, whatever the amount:
        # matching on amount+date alone links coincidences like salary vs.
        # same-amount rent.
        if not TRANSFERISH.search(out.description):
            candidates = [c for c in candidates if TRANSFERISH.search(c.description)]
        if not candidates:
            continue
        best = min(candidates, key=lambda t: abs(t.date - out.date))
        _link(out, best)
        linked += 1
    return linked


def _match_fx_exchanges(txs: list[Transaction]) -> int:
    """Revolut currency exchanges: both legs appear on the same day with an
    'Exchanged to/from …' description but different currencies and amounts."""
    legs = [t for t in txs if t.transfer_peer_id is None and EXCHANGE.search(t.description)]
    outs = [t for t in legs if t.amount < 0]
    # Both legs of a Revolut exchange carry the identical description
    # ("Exchanged to JPY"), so pair on date + description. This stops an
    # exchange into a pocket missing from the export (e.g. THB) from
    # stealing another same-day exchange's leg.
    ins_by_key: dict[tuple, list[Transaction]] = {}
    for t in legs:
        if t.amount > 0:
            ins_by_key.setdefault((t.date, t.description.upper()), []).append(t)

    linked = 0
    for out in outs:
        candidates = [
            t
            for t in ins_by_key.get((out.date, out.description.upper()), [])
            if t.transfer_peer_id is None
            and t.account_id != out.account_id
            and t.account.currency != out.account.currency
        ]
        if not candidates:
            continue
        _link(out, candidates[0])
        linked += 1
    return linked


def detect_transfers(db: Session) -> int:
    """Pair up transfers between own accounts. Idempotent: already-linked
    transactions are never touched. Returns the number of new pairs."""
    txs = _unmatched(db)
    pairs = _match_same_currency(txs)
    pairs += _match_fx_exchanges(txs)
    db.commit()
    return pairs
