"""Monzo personal API integration.

OAuth confidential client: /api/monzo/connect returns the consent URL,
Monzo redirects back to /api/monzo/callback, tokens live in
DATA_DIR/monzo_tokens.json (with the financial data, outside the repo).

Monzo quirks handled here:
- After OAuth the user must ALSO approve data access in the Monzo app
  (Strong Customer Authentication). Until then the transactions endpoint
  returns 403 verification_required.
- Full history is only served within ~5 minutes of that in-app approval;
  afterwards the API limits to the last 90 days. First sync tries full
  history and falls back to the 90-day window with a warning.
- Transactions are fingerprinted by Monzo's own transaction id, so syncs
  are idempotent regardless of settlement-time amendments.
"""

import json
import secrets
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from .categorize import apply_rules, normalize_merchant
from .db import DATA_DIR
from .models import Account, ImportBatch, Transaction
from .secrets_env import get_secret

API = "https://api.monzo.com"
AUTH_URL = "https://auth.monzo.com/"
REDIRECT_URI = "http://localhost:8000/api/monzo/callback"
TOKENS_PATH = DATA_DIR / "monzo_tokens.json"
HISTORY_START = "2024-07-01T00:00:00Z"  # matches the rest of the data window
SCA_WINDOW_DAYS = 89

_pending_states: set[str] = set()


class MonzoError(RuntimeError):
    pass


class NotConnected(MonzoError):
    pass


def _creds() -> tuple[str, str]:
    client_id = get_secret("MONZO_CLIENT_ID")
    client_secret = get_secret("MONZO_CLIENT_SECRET")
    if not client_id or not client_secret:
        raise MonzoError(
            "MONZO_CLIENT_ID / MONZO_CLIENT_SECRET are not configured "
            "(register an OAuth client at developers.monzo.com)"
        )
    return client_id, client_secret


def _load_tokens() -> dict | None:
    if TOKENS_PATH.exists():
        return json.loads(TOKENS_PATH.read_text())
    return None


def _save_tokens(tokens: dict) -> None:
    tokens["obtained_at"] = datetime.now(timezone.utc).isoformat()
    TOKENS_PATH.write_text(json.dumps(tokens))
    TOKENS_PATH.chmod(0o600)


def status() -> dict:
    configured = bool(get_secret("MONZO_CLIENT_ID") and get_secret("MONZO_CLIENT_SECRET"))
    return {"configured": configured, "connected": _load_tokens() is not None}


def connect_url() -> str:
    client_id, _ = _creds()
    state = secrets.token_urlsafe(24)
    _pending_states.add(state)
    from urllib.parse import urlencode

    return AUTH_URL + "?" + urlencode(
        {
            "client_id": client_id,
            "redirect_uri": REDIRECT_URI,
            "response_type": "code",
            "state": state,
        }
    )


def handle_callback(code: str, state: str) -> None:
    import httpx

    if state not in _pending_states:
        raise MonzoError("Unknown OAuth state — start the connection again from the app")
    _pending_states.discard(state)
    client_id, client_secret = _creds()
    response = httpx.post(
        f"{API}/oauth2/token",
        data={
            "grant_type": "authorization_code",
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uri": REDIRECT_URI,
            "code": code,
        },
        timeout=30,
    )
    response.raise_for_status()
    _save_tokens(response.json())


def _access_token() -> str:
    import httpx

    tokens = _load_tokens()
    if tokens is None:
        raise NotConnected("Monzo is not connected yet")
    obtained = datetime.fromisoformat(tokens.get("obtained_at"))
    expires_in = int(tokens.get("expires_in", 0))
    if datetime.now(timezone.utc) < obtained + timedelta(seconds=max(expires_in - 300, 60)):
        return tokens["access_token"]

    client_id, client_secret = _creds()
    response = httpx.post(
        f"{API}/oauth2/token",
        data={
            "grant_type": "refresh_token",
            "client_id": client_id,
            "client_secret": client_secret,
            "refresh_token": tokens["refresh_token"],
        },
        timeout=30,
    )
    if response.status_code != 200:
        TOKENS_PATH.unlink(missing_ok=True)
        raise NotConnected("Monzo session expired — reconnect from the app")
    _save_tokens(response.json())
    return _load_tokens()["access_token"]


def map_transaction(t: dict) -> tuple[str, Decimal] | None:
    """Monzo transaction -> (description, signed amount in pounds), or None
    if it shouldn't be imported (declined / zero-amount)."""
    if t.get("decline_reason"):
        return None
    amount = Decimal(t["amount"]) / 100
    if amount == 0:
        return None
    merchant = t.get("merchant") or {}
    counterparty = t.get("counterparty") or {}
    description = (
        (merchant.get("name") if isinstance(merchant, dict) else None)
        or counterparty.get("name")
        or t.get("description")
        or "Monzo transaction"
    )
    return description, amount


def _account_name(account: dict) -> str:
    return "Monzo Joint" if account.get("type") == "uk_retail_joint" else "Monzo"


def sync(db: Session) -> dict:
    import httpx

    token = _access_token()
    client = httpx.Client(base_url=API, headers={"Authorization": f"Bearer {token}"}, timeout=30)

    accounts_response = client.get("/accounts")
    if accounts_response.status_code == 403:
        raise MonzoError(
            "Monzo says access is not approved yet — open the Monzo app, approve "
            "access, then sync again within 5 minutes for full history."
        )
    accounts_response.raise_for_status()
    monzo_accounts = [a for a in accounts_response.json()["accounts"] if not a.get("closed")]

    stats = {"accounts": 0, "new": 0, "duplicates": 0, "window_limited": False}
    batch = ImportBatch(source="monzo", filename="monzo-api")
    db.add(batch)
    db.flush()

    for monzo_account in monzo_accounts:
        created: list[Transaction] = []
        name = _account_name(monzo_account)
        account = db.scalar(select(Account).where(Account.name == name))
        if account is None:
            account = Account(name=name, provider="monzo", kind="current", currency="GBP")
            db.add(account)
            db.flush()
        stats["accounts"] += 1

        since = HISTORY_START
        while True:
            response = client.get(
                "/transactions",
                params={
                    "account_id": monzo_account["id"],
                    "limit": 100,
                    "since": since,
                    "expand[]": "merchant",
                },
            )
            if response.status_code in (400, 403) and since == HISTORY_START:
                # Outside the post-approval SCA window Monzo refuses history
                # older than 90 days (observed as 400 or 403 depending on
                # endpoint mood). Fall back to the permitted window.
                stats["window_limited"] = True
                since = (datetime.now(timezone.utc) - timedelta(days=SCA_WINDOW_DAYS)).strftime(
                    "%Y-%m-%dT%H:%M:%SZ"
                )
                continue
            if response.status_code in (400, 403):
                try:
                    detail = response.json().get("message") or response.json().get("code", "")
                except Exception:
                    detail = response.text[:200]
                raise MonzoError(
                    "Monzo refused transaction access — approve access in the Monzo app "
                    f"and sync again (within 5 minutes for full history). Monzo said: {detail}"
                )
            response.raise_for_status()
            page = response.json()["transactions"]
            if not page:
                break

            for t in page:
                mapped = map_transaction(t)
                if mapped is None:
                    continue
                fingerprint = f"monzo|{t['id']}"
                if db.scalar(select(Transaction.id).where(Transaction.fingerprint == fingerprint)):
                    stats["duplicates"] += 1
                    continue
                description, amount = mapped
                tx = Transaction(
                    account_id=account.id,
                    date=datetime.fromisoformat(t["created"].replace("Z", "+00:00")).date(),
                    description=description,
                    merchant=normalize_merchant(description),
                    amount=amount,
                    import_batch_id=batch.id,
                    fingerprint=fingerprint,
                )
                db.add(tx)
                created.append(tx)
                stats["new"] += 1

            since = page[-1]["id"]  # cursor pagination
            if len(page) < 100:
                break

        apply_rules(db, created)
        batch.new_count += len(created)

    batch.duplicate_count = stats["duplicates"]
    db.commit()
    return stats
