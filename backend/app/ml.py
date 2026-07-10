"""Local ML categorizer: TF-IDF character n-grams + logistic regression.

Trains on human- and rule-labeled transactions (never on its own or LLM
output, to avoid feedback loops) and only auto-categorizes above a
confidence threshold — everything else stays in the review queue.
"""

from dataclasses import dataclass

import joblib
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.linear_model import LogisticRegression
from sklearn.model_selection import train_test_split
from sklearn.pipeline import Pipeline
from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from .db import DATA_DIR
from .models import Transaction

MODEL_PATH = DATA_DIR / "categorizer.joblib"
CONFIDENCE_THRESHOLD = 0.75
TRAIN_SOURCES = ("human", "rule")


def features(tx: Transaction) -> str:
    """Single text feature: merchant + description plus categorical tokens
    the char n-grams can latch onto (direction and provider)."""
    direction = "__IN__" if tx.amount > 0 else "__OUT__"
    provider = f"__{tx.account.provider.upper()}__" if tx.account else ""
    return f"{tx.merchant or ''} {tx.description} {direction} {provider}"


@dataclass
class TrainResult:
    trained_on: int
    classes: int
    holdout_accuracy: float | None


def _training_data(db: Session) -> tuple[list[str], list[int]]:
    txs = db.scalars(
        select(Transaction)
        .options(joinedload(Transaction.account))
        .where(
            Transaction.category_id.isnot(None),
            Transaction.transfer_peer_id.is_(None),
            Transaction.category_source.in_(TRAIN_SOURCES),
        )
    ).all()
    return [features(t) for t in txs], [t.category_id for t in txs]


def train(db: Session) -> TrainResult:
    texts, labels = _training_data(db)
    if len(set(labels)) < 2:
        raise ValueError("Need labeled transactions in at least 2 categories to train")

    pipeline = Pipeline(
        [
            ("tfidf", TfidfVectorizer(analyzer="char_wb", ngram_range=(2, 4), min_df=2)),
            ("clf", LogisticRegression(max_iter=2000, class_weight="balanced")),
        ]
    )

    holdout_accuracy = None
    # Honest generalization estimate on a stratified holdout when the data
    # allows it, then retrain on everything for the model that gets saved.
    label_counts = {c: labels.count(c) for c in set(labels)}
    if len(labels) >= 100 and min(label_counts.values()) >= 2:
        x_train, x_test, y_train, y_test = train_test_split(
            texts, labels, test_size=0.2, random_state=42, stratify=labels
        )
        pipeline.fit(x_train, y_train)
        holdout_accuracy = float(pipeline.score(x_test, y_test))

    pipeline.fit(texts, labels)
    joblib.dump(pipeline, MODEL_PATH)
    return TrainResult(
        trained_on=len(labels), classes=len(set(labels)), holdout_accuracy=holdout_accuracy
    )


def load_model():
    if MODEL_PATH.exists():
        return joblib.load(MODEL_PATH)
    return None


def apply_model(
    db: Session,
    transactions: list[Transaction] | None = None,
    threshold: float = CONFIDENCE_THRESHOLD,
) -> dict:
    """Categorize uncategorized transactions the model is confident about.
    Returns counts. Never touches categorized or transfer-linked rows."""
    model = load_model()
    if model is None:
        return {"applied": 0, "low_confidence": 0}

    if transactions is None:
        transactions = list(
            db.scalars(
                select(Transaction)
                .options(joinedload(Transaction.account))
                .where(
                    Transaction.category_id.is_(None),
                    Transaction.transfer_peer_id.is_(None),
                )
            )
        )
    candidates = [t for t in transactions if t.category_id is None and t.transfer_peer_id is None]
    if not candidates:
        return {"applied": 0, "low_confidence": 0}

    probabilities = model.predict_proba([features(t) for t in candidates])
    applied = 0
    for tx, probs in zip(candidates, probabilities):
        best = probs.argmax()
        if probs[best] >= threshold:
            tx.category_id = int(model.classes_[best])
            tx.category_source = "model"
            applied += 1
    return {"applied": applied, "low_confidence": len(candidates) - applied}
