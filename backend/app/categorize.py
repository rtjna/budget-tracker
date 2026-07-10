import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import Category, Rule, Transaction

SEED_CATEGORIES = [
    "Groceries",
    "Eating Out",
    "Coffee",
    "Transport",
    "Travel",
    "Shopping",
    "Subscriptions",
    "Utilities & Bills",
    "Housing",
    "Health",
    "Entertainment",
    "Personal Care",
    "Gifts & Donations",
    "Income",
    "Transfers",
    "Fees & Charges",
    "Cash",
    "Other",
]


def normalize_merchant(description: str) -> str:
    """Collapse a raw bank description to a stable merchant key:
    'TESCO STORE 6545 6545TE LONDON' -> 'TESCO STORE LONDON'
    'AMAZON.CO.UK*8C3NF0TI5 AMAZON.CO.UK' -> 'AMAZON CO UK'
    """
    s = description.upper()
    s = re.sub(r"[*#][A-Z0-9]+", " ", s)
    s = re.sub(r"[^A-Z0-9&' ]", " ", s)
    tokens = [t for t in s.split() if not any(c.isdigit() for c in t)]
    return " ".join(tokens[:4]) or description.upper().strip()


def seed_categories(db: Session) -> None:
    if db.scalar(select(Category).limit(1)) is None:
        db.add_all(Category(name=name) for name in SEED_CATEGORIES)
        db.commit()


def rule_matches(rule: Rule, transaction: Transaction) -> bool:
    if rule.match == "regex":
        try:
            return re.search(rule.pattern, transaction.description, re.IGNORECASE) is not None
        except re.error:
            return False
    if rule.match == "merchant":
        return transaction.merchant == rule.pattern
    return rule.pattern.upper() in transaction.description.upper()


def apply_rules(db: Session, transactions: list[Transaction]) -> int:
    """Apply rules to uncategorized transactions. Human categorizations are
    never overwritten. Returns how many transactions were categorized."""
    rules = list(db.scalars(select(Rule).order_by(Rule.id)))
    if not rules:
        return 0
    applied = 0
    for tx in transactions:
        if tx.category_id is not None:
            continue
        for rule in rules:
            if rule_matches(rule, tx):
                tx.category_id = rule.category_id
                tx.category_source = "rule"
                applied += 1
                break
    return applied
