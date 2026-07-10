"""Splitwise integration.

Bank transactions stay gross; every Splitwise expense imports one signed
*correction* into a virtual Splitwise account:

    correction = paid_share - owed_share   (in our sign convention: positive
    reduces spending — you fronted money; negative is consumption your bank
    never saw — a friend paid.)

Item granularity never has to match the bank side. Settle-up payments are
matched against bank transactions by exact amount within a date window and
linked through the existing transfer machinery, so they never count as
income or spending. Idempotent: each Splitwise expense/payment is imported
at most once (fingerprinted by Splitwise id).
"""

import hashlib
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from .categorize import normalize_merchant
from .models import Account, Category, ImportBatch, Transaction
from .secrets_env import get_secret
from .stats import GBP_RATES

API = "https://secure.splitwise.com/api/v3.0"
ACCOUNT_NAME = "Splitwise"
SETTLE_WINDOW_DAYS = 5

# Splitwise category names -> this app's category names (best effort; anything
# unmapped lands in the review queue).
CATEGORY_MAP = {
    "Dining out": "Eating Out",
    "Groceries": "Groceries",
    "Liquor": "Eating Out",
    "Food and drink": "Food",
    "Plane": "Travel",
    "Hotel": "Travel",
    "Bus/train": "Travel",
    "Taxi": "Transport",
    "Car": "Transport",
    "Gas/fuel": "Transport",
    "Parking": "Transport",
    "Bicycle": "Transport",
    "Transportation": "Transport",
    "Movies": "Entertainment",
    "Games": "Entertainment",
    "Music": "Entertainment",
    "Sports": "Entertainment",
    "Entertainment": "Entertainment",
    "Rent": "Housing",
    "Mortgage": "Housing",
    "Household supplies": "Shopping",
    "Furniture": "Shopping",
    "Electronics": "Shopping",
    "Clothing": "Shopping",
    "Gifts": "Gifts & Donations",
    "Medical expenses": "Health",
    "Electricity": "Utilities & Bills",
    "Heat/gas": "Utilities & Bills",
    "Water": "Utilities & Bills",
    "TV/Phone/Internet": "Utilities & Bills",
    "Utilities": "Utilities & Bills",
    "Insurance": "Utilities & Bills",
    "Education": "Education",
}


class SplitwiseClient:
    """Thin HTTP client; swapped out in tests."""

    def __init__(self, api_key: str):
        import httpx

        self._http = httpx.Client(
            base_url=API, headers={"Authorization": f"Bearer {api_key}"}, timeout=30
        )

    def current_user_id(self) -> int:
        r = self._http.get("/get_current_user")
        r.raise_for_status()
        return r.json()["user"]["id"]

    def expenses(self):
        offset = 0
        while True:
            r = self._http.get("/get_expenses", params={"limit": 100, "offset": offset})
            r.raise_for_status()
            page = r.json()["expenses"]
            if not page:
                return
            yield from page
            offset += len(page)


def make_client() -> SplitwiseClient:
    api_key = get_secret("SPLITWISE_API_KEY")
    if not api_key:
        raise RuntimeError("SPLITWISE_API_KEY is not configured (env, Keychain, or secrets.env)")
    return SplitwiseClient(api_key)


def _fingerprint(kind: str, splitwise_id) -> str:
    return hashlib.sha256(f"splitwise|{kind}|{splitwise_id}".encode()).hexdigest()


def _get_or_create_account(db: Session) -> Account:
    account = db.scalar(select(Account).where(Account.name == ACCOUNT_NAME))
    if account is None:
        account = Account(name=ACCOUNT_NAME, provider="splitwise", kind="clearing", currency="GBP")
        db.add(account)
        db.flush()
    return account


def _to_gbp(value: Decimal, currency: str) -> Decimal | None:
    rate = GBP_RATES.get(currency)
    return None if rate is None else (value * rate).quantize(Decimal("0.01"))


def _my_shares(expense: dict, user_id: int) -> tuple[Decimal, Decimal] | None:
    for u in expense.get("users", []):
        uid = u.get("user_id") or u.get("user", {}).get("id")
        if uid == user_id:
            return Decimal(u.get("paid_share") or "0"), Decimal(u.get("owed_share") or "0")
    return None


def _match_settlement(db: Session, account: Account, amount_gbp: Decimal, when: date) -> Transaction | None:
    """Find an unlinked bank transaction that looks like this settle-up:
    exact amount, within the window, not in the Splitwise account."""
    lo, hi = when - timedelta(days=SETTLE_WINDOW_DAYS), when + timedelta(days=SETTLE_WINDOW_DAYS)
    candidates = db.scalars(
        select(Transaction)
        .options(joinedload(Transaction.account))
        .where(
            Transaction.account_id != account.id,
            Transaction.transfer_peer_id.is_(None),
            Transaction.date >= lo,
            Transaction.date <= hi,
        )
    ).all()
    for tx in candidates:
        if tx.account.currency == "GBP" and Decimal(tx.amount) == amount_gbp:
            return tx
    return None


def sync(db: Session, client: SplitwiseClient | None = None) -> dict:
    if client is None:
        client = make_client()
    user_id = client.current_user_id()
    account = _get_or_create_account(db)
    categories = {c.name: c.id for c in db.scalars(select(Category))}

    stats = {"corrections": 0, "settlements_linked": 0, "settlements_pending": 0,
             "skipped": 0, "uncategorized": 0}
    batch = ImportBatch(source="splitwise", filename="splitwise-api")
    db.add(batch)
    db.flush()

    for expense in client.expenses():
        if expense.get("deleted_at"):
            continue
        shares = _my_shares(expense, user_id)
        if shares is None:
            continue  # expense doesn't involve me
        paid, owed = shares
        when = date.fromisoformat(expense["date"][:10])
        currency = expense.get("currency_code", "GBP")

        if expense.get("payment"):
            # Settle-up: net_balance > 0 means money coming to me.
            net = paid - owed
            if net == 0:
                continue
            fp = _fingerprint("payment", expense["id"])
            if db.scalar(select(Transaction.id).where(Transaction.fingerprint == fp)):
                stats["skipped"] += 1
                continue
            net_gbp = _to_gbp(net, currency)
            if net_gbp is None:
                continue
            # net > 0: I paid a friend -> bank shows -net. net < 0: friend paid
            # me -> bank shows -net (positive). Bank side is always -net; the
            # mirror leg is +net so the pair sums to zero.
            bank_tx = _match_settlement(db, account, -net_gbp, when)
            if bank_tx is None:
                stats["settlements_pending"] += 1  # retried on next sync
                continue
            mirror = Transaction(
                account_id=account.id,
                date=when,
                description=f"Settle up ({expense.get('description') or 'Splitwise'})",
                merchant="SPLITWISE SETTLE UP",
                amount=net_gbp,
                import_batch_id=batch.id,
                fingerprint=fp,
            )
            db.add(mirror)
            db.flush()
            mirror.transfer_peer_id = bank_tx.id
            bank_tx.transfer_peer_id = mirror.id
            stats["settlements_linked"] += 1
            batch.new_count += 1
            continue

        correction = paid - owed
        if correction == 0:
            continue  # fully personal or perfectly netted item
        fp = _fingerprint("expense", expense["id"])
        if db.scalar(select(Transaction.id).where(Transaction.fingerprint == fp)):
            stats["skipped"] += 1
            continue
        amount_gbp = _to_gbp(correction, currency)
        if amount_gbp is None or amount_gbp == 0:
            continue
        sw_category = (expense.get("category") or {}).get("name", "")
        category_id = categories.get(CATEGORY_MAP.get(sw_category, sw_category))
        description = expense.get("description") or "Splitwise expense"
        db.add(
            Transaction(
                account_id=account.id,
                date=when,
                description=f"{description} (Splitwise share)",
                merchant=normalize_merchant(description),
                amount=amount_gbp,
                category_id=category_id,
                category_source="splitwise" if category_id else None,
                import_batch_id=batch.id,
                fingerprint=fp,
            )
        )
        stats["corrections"] += 1
        if category_id is None:
            stats["uncategorized"] += 1
        batch.new_count += 1

    batch.duplicate_count = stats["skipped"]
    db.commit()
    return stats
