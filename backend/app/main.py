import os
from datetime import date
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Query, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from . import models
from .categorize import apply_rules, normalize_merchant, seed_categories
from .db import Base, SessionLocal, engine, ensure_columns, get_db
from .importing import UnrecognizedFileError, import_file
from .ml import apply_model, train
from .transfers import detect_transfers
from .xlsx import is_xlsx, xlsx_to_csv_text

from .secrets_env import load_secrets

load_secrets()
Base.metadata.create_all(engine)
ensure_columns()
with SessionLocal() as _db:
    seed_categories(_db)
    for _tx in _db.scalars(
        select(models.Transaction).where(models.Transaction.merchant.is_(None))
    ):
        _tx.merchant = normalize_merchant(_tx.description)
    _db.commit()

app = FastAPI(title="Budget Tracker")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/api/health")
def health():
    return {"status": "ok"}


@app.get("/api/llm/status")
def llm_status():
    from .secrets_env import get_secret

    # Reports only presence, never the value.
    return {"configured": get_secret("ANTHROPIC_API_KEY") is not None}


@app.post("/api/imports")
async def create_import(file: UploadFile, db: Session = Depends(get_db)):
    data = await file.read()
    if is_xlsx(data):
        text = xlsx_to_csv_text(data)
    else:
        text = data.decode("utf-8-sig")
    try:
        batch = import_file(db, file.filename or "upload.csv", text)
    except UnrecognizedFileError as e:
        raise HTTPException(status_code=422, detail=str(e))
    transfers = detect_transfers(db)
    model_result = apply_model(db)
    db.commit()
    return {
        "source": batch.source,
        "filename": batch.filename,
        "new": batch.new_count,
        "duplicates": batch.duplicate_count,
        "date_min": batch.date_min,
        "date_max": batch.date_max,
        "transfers": transfers,
        "model_categorized": model_result["applied"],
    }


@app.post("/api/model/train")
def train_model(db: Session = Depends(get_db)):
    try:
        result = train(db)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    applied = apply_model(db)
    db.commit()
    return {
        "trained_on": result.trained_on,
        "classes": result.classes,
        "holdout_accuracy": result.holdout_accuracy,
        "applied": applied["applied"],
        "low_confidence": applied["low_confidence"],
    }


@app.post("/api/transfers/detect")
def run_transfer_detection(db: Session = Depends(get_db)):
    return {"pairs": detect_transfers(db)}


@app.post("/api/transfers/unlink/{tx_id}")
def unlink_transfer(tx_id: int, db: Session = Depends(get_db)):
    tx = db.get(models.Transaction, tx_id)
    if tx is None or tx.transfer_peer_id is None:
        raise HTTPException(status_code=404, detail="No transfer link on this transaction")
    peer = db.get(models.Transaction, tx.transfer_peer_id)
    tx.transfer_peer_id = None
    if peer is not None:
        peer.transfer_peer_id = None
    db.commit()
    return {"unlinked": tx_id}


@app.get("/api/accounts")
def list_accounts(db: Session = Depends(get_db)):
    rows = db.execute(
        select(
            models.Account,
            func.count(models.Transaction.id),
            func.max(models.Transaction.date),
        )
        .outerjoin(models.Transaction)
        .group_by(models.Account.id)
    ).all()
    return [
        {
            "id": account.id,
            "name": account.name,
            "provider": account.provider,
            "kind": account.kind,
            "currency": account.currency,
            "transaction_count": count,
            "latest_transaction": latest,
        }
        for account, count, latest in rows
    ]


@app.get("/api/transactions")
def list_transactions(
    db: Session = Depends(get_db),
    account_id: int | None = None,
    search: str | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    uncategorized: bool = False,
    limit: int = Query(default=50, le=500),
    offset: int = 0,
):
    query = select(models.Transaction)
    if account_id is not None:
        query = query.where(models.Transaction.account_id == account_id)
    if search:
        query = query.where(or_(models.Transaction.description.icontains(search)))
    if date_from is not None:
        query = query.where(models.Transaction.date >= date_from)
    if date_to is not None:
        query = query.where(models.Transaction.date <= date_to)
    if uncategorized:
        query = query.where(models.Transaction.category_id.is_(None))

    total = db.scalar(select(func.count()).select_from(query.subquery()))
    txs = db.scalars(
        query.order_by(models.Transaction.date.desc(), models.Transaction.id.desc())
        .limit(limit)
        .offset(offset)
    ).all()
    return {
        "total": total,
        "items": [
            {
                "id": t.id,
                "account_id": t.account_id,
                "date": t.date,
                "description": t.description,
                "merchant": t.merchant,
                "amount": float(t.amount),
                "category_id": t.category_id,
                "category_source": t.category_source,
                "transfer_peer_id": t.transfer_peer_id,
            }
            for t in txs
        ],
    }


class CategorizeBody(BaseModel):
    category_id: int | None


@app.patch("/api/transactions/{tx_id}")
def categorize_transaction(tx_id: int, body: CategorizeBody, db: Session = Depends(get_db)):
    tx = db.get(models.Transaction, tx_id)
    if tx is None:
        raise HTTPException(status_code=404, detail="Transaction not found")
    tx.category_id = body.category_id
    tx.category_source = "human" if body.category_id is not None else None
    db.commit()
    return {"id": tx.id, "category_id": tx.category_id, "category_source": tx.category_source}


class CategoryBody(BaseModel):
    name: str
    parent_id: int | None = None


@app.get("/api/categories")
def list_categories(db: Session = Depends(get_db)):
    return [
        {"id": c.id, "name": c.name, "parent_id": c.parent_id}
        for c in db.scalars(select(models.Category).order_by(models.Category.name))
    ]


@app.post("/api/categories")
def create_category(body: CategoryBody, db: Session = Depends(get_db)):
    category = models.Category(name=body.name.strip(), parent_id=body.parent_id)
    db.add(category)
    db.commit()
    return {"id": category.id, "name": category.name, "parent_id": category.parent_id}


@app.delete("/api/categories/{category_id}")
def delete_category(category_id: int, db: Session = Depends(get_db)):
    used = db.scalar(
        select(func.count())
        .select_from(models.Transaction)
        .where(models.Transaction.category_id == category_id)
    )
    if used:
        raise HTTPException(status_code=409, detail=f"Category is used by {used} transactions")
    category = db.get(models.Category, category_id)
    if category is None:
        raise HTTPException(status_code=404, detail="Category not found")
    db.delete(category)
    db.commit()
    return {"deleted": category_id}


class RuleBody(BaseModel):
    pattern: str
    match: str = "contains"  # contains | merchant | regex
    category_id: int


@app.get("/api/rules")
def list_rules(db: Session = Depends(get_db)):
    return [
        {
            "id": r.id,
            "match": r.match,
            "pattern": r.pattern,
            "category_id": r.category_id,
        }
        for r in db.scalars(select(models.Rule).order_by(models.Rule.id))
    ]


@app.post("/api/rules")
def create_rule(body: RuleBody, db: Session = Depends(get_db)):
    if body.match not in ("contains", "merchant", "regex"):
        raise HTTPException(status_code=422, detail="match must be contains, merchant, or regex")
    rule = models.Rule(match=body.match, pattern=body.pattern.strip(), category_id=body.category_id)
    db.add(rule)
    db.flush()
    uncategorized = list(
        db.scalars(select(models.Transaction).where(models.Transaction.category_id.is_(None)))
    )
    applied = apply_rules(db, uncategorized)
    db.commit()
    return {"id": rule.id, "applied": applied}


@app.delete("/api/rules/{rule_id}")
def delete_rule(rule_id: int, db: Session = Depends(get_db)):
    rule = db.get(models.Rule, rule_id)
    if rule is None:
        raise HTTPException(status_code=404, detail="Rule not found")
    db.delete(rule)
    db.commit()
    return {"deleted": rule_id}


@app.get("/api/review")
def review_queue(db: Session = Depends(get_db), limit: int = Query(default=50, le=200)):
    rows = db.execute(
        select(
            models.Transaction.merchant,
            func.count(models.Transaction.id),
            func.max(models.Transaction.description),
            func.max(models.Transaction.date),
        )
        .where(
            models.Transaction.category_id.is_(None),
            models.Transaction.transfer_peer_id.is_(None),
        )
        .group_by(models.Transaction.merchant)
        .order_by(func.count(models.Transaction.id).desc())
        .limit(limit)
    ).all()
    total = db.scalar(
        select(func.count())
        .select_from(models.Transaction)
        .where(
            models.Transaction.category_id.is_(None),
            models.Transaction.transfer_peer_id.is_(None),
        )
    )
    return {
        "total_uncategorized": total,
        "groups": [
            {
                "merchant": merchant,
                "count": count,
                "sample_description": sample,
                "latest": latest,
            }
            for merchant, count, sample, latest in rows
        ],
    }


class AssignBody(BaseModel):
    merchant: str
    category_id: int
    create_rule: bool = True


@app.post("/api/review/assign")
def review_assign(body: AssignBody, db: Session = Depends(get_db)):
    txs = list(
        db.scalars(
            select(models.Transaction).where(
                models.Transaction.merchant == body.merchant,
                models.Transaction.category_id.is_(None),
                models.Transaction.transfer_peer_id.is_(None),
            )
        )
    )
    for tx in txs:
        tx.category_id = body.category_id
        tx.category_source = "human"
    rule_id = None
    if body.create_rule:
        existing = db.scalar(
            select(models.Rule).where(
                models.Rule.match == "merchant", models.Rule.pattern == body.merchant
            )
        )
        if existing is None:
            rule = models.Rule(match="merchant", pattern=body.merchant, category_id=body.category_id)
            db.add(rule)
            db.flush()
            rule_id = rule.id
        else:
            existing.category_id = body.category_id
            rule_id = existing.id
    db.commit()
    return {"categorized": len(txs), "rule_id": rule_id}


# In Docker the built frontend is served from STATIC_DIR; in dev, Vite serves it.
_static = Path(os.environ.get("STATIC_DIR", ""))
if _static.is_dir():
    app.mount("/", StaticFiles(directory=_static, html=True), name="frontend")
