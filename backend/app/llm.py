"""Claude fallback categorizer for merchants the rules and local model
can't handle.

Privacy: only merchant keys and one sample transaction description per
merchant are sent — never amounts, dates, balances, or account details.
Each merchant is asked at most once (LlmMerchantCache); answers the model
is unsure about leave the merchant in the human review queue.
"""

import os
import time

from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .models import Category, LlmMerchantCache, Transaction
from .secrets_env import get_secret

MODEL = os.environ.get("LLM_MODEL", "claude-opus-4-8")
BATCH_SIZE = 40
FEW_SHOT_EXAMPLES = 60
UNSURE = "UNSURE"

# The first structured-output request compiles the schema's grammar
# server-side and can time out; the compiled grammar is cached, so a
# short-delay retry normally succeeds.
GRAMMAR_TIMEOUT = "Grammar compilation timed out"
GRAMMAR_RETRIES = 2


def _parse_batch(client, **kwargs):
    import anthropic

    for attempt in range(GRAMMAR_RETRIES + 1):
        try:
            return client.messages.parse(**kwargs)
        except anthropic.BadRequestError as e:
            if GRAMMAR_TIMEOUT not in str(e) or attempt == GRAMMAR_RETRIES:
                raise
            time.sleep(2 * (attempt + 1))


class MerchantAssignment(BaseModel):
    merchant: str
    category: str


class BatchResult(BaseModel):
    assignments: list[MerchantAssignment]


def _make_client():
    import anthropic

    api_key = get_secret("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY is not configured (env, Keychain, or secrets.env)")
    return anthropic.Anthropic(api_key=api_key)


def _few_shot(db: Session) -> list[tuple[str, str]]:
    """Sample of the user's own (merchant, category) decisions, spread across
    categories, used as in-context examples."""
    rows = db.execute(
        select(Transaction.merchant, Category.name)
        .join(Category, Transaction.category_id == Category.id)
        .where(
            Transaction.category_source == "human",
            Transaction.merchant.isnot(None),
        )
        .group_by(Transaction.merchant, Category.name)
        .order_by(func.random())
        .limit(FEW_SHOT_EXAMPLES)
    ).all()
    return [(merchant, category) for merchant, category in rows]


def _pending_merchants(db: Session, max_merchants: int) -> list[tuple[str, str]]:
    """Uncategorized, non-transfer merchants not yet asked, with one sample
    description each, biggest transaction count first."""
    asked = select(LlmMerchantCache.merchant)
    rows = db.execute(
        select(Transaction.merchant, func.max(Transaction.description))
        .where(
            Transaction.category_id.is_(None),
            Transaction.transfer_peer_id.is_(None),
            Transaction.merchant.isnot(None),
            Transaction.merchant.not_in(asked),
        )
        .group_by(Transaction.merchant)
        .order_by(func.count(Transaction.id).desc())
        .limit(max_merchants)
    ).all()
    return [(merchant, sample) for merchant, sample in rows]


def _prompt(categories: list[str], examples: list[tuple[str, str]], batch: list[tuple[str, str]]) -> str:
    example_lines = "\n".join(f"- {m} -> {c}" for m, c in examples)
    merchant_lines = "\n".join(f"- merchant: {m} (sample transaction text: {d})" for m, d in batch)
    return f"""Here are examples of how this user has categorized merchants themselves:

{example_lines}

Assign exactly one category to each of the following merchants from a UK bank statement.

{merchant_lines}

Rules:
- Use only categories from the allowed list.
- Follow the user's own examples above when a merchant is similar.
- If a merchant is genuinely ambiguous or unrecognizable, use {UNSURE} rather than guessing.
- Return every merchant exactly as written, once each."""


def categorize_merchants(db: Session, max_merchants: int = 200, client=None) -> dict:
    categories = {c.name: c.id for c in db.scalars(select(Category))}
    pending = _pending_merchants(db, max_merchants)
    if not pending:
        return {"asked": 0, "categorized": 0, "unsure": 0, "transactions": 0}

    if client is None:
        client = _make_client()
    examples = _few_shot(db)
    system = (
        "You categorize bank-statement merchants for a personal budgeting app. "
        "Allowed categories: " + ", ".join(sorted(categories)) + f", {UNSURE}."
    )

    stats = {"asked": 0, "categorized": 0, "unsure": 0, "transactions": 0}
    for start in range(0, len(pending), BATCH_SIZE):
        batch = pending[start : start + BATCH_SIZE]
        batch_merchants = {m for m, _ in batch}
        response = _parse_batch(
            client,
            model=MODEL,
            max_tokens=16000,
            system=system,
            messages=[{"role": "user", "content": _prompt(sorted(categories), examples, batch)}],
            output_format=BatchResult,
        )
        result = response.parsed_output
        if result is None:
            continue

        seen: set[str] = set()
        for item in result.assignments:
            merchant = item.merchant.strip()
            if merchant not in batch_merchants or merchant in seen:
                continue
            seen.add(merchant)
            stats["asked"] += 1
            category_id = categories.get(item.category.strip())
            db.add(LlmMerchantCache(merchant=merchant, category_id=category_id))
            if category_id is None:
                stats["unsure"] += 1
                continue
            stats["categorized"] += 1
            txs = db.scalars(
                select(Transaction).where(
                    Transaction.merchant == merchant,
                    Transaction.category_id.is_(None),
                    Transaction.transfer_peer_id.is_(None),
                )
            ).all()
            for tx in txs:
                tx.category_id = category_id
                tx.category_source = "llm"
            stats["transactions"] += len(txs)
        db.commit()

    return stats
