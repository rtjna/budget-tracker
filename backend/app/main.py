import os
from datetime import date
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Query, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from . import models
from .db import Base, engine, get_db
from .importing import UnrecognizedFileError, import_file
from .xlsx import is_xlsx, xlsx_to_csv_text

Base.metadata.create_all(engine)

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
    return {
        "source": batch.source,
        "filename": batch.filename,
        "new": batch.new_count,
        "duplicates": batch.duplicate_count,
        "date_min": batch.date_min,
        "date_max": batch.date_max,
    }


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
                "amount": float(t.amount),
                "category_id": t.category_id,
            }
            for t in txs
        ],
    }


# In Docker the built frontend is served from STATIC_DIR; in dev, Vite serves it.
_static = Path(os.environ.get("STATIC_DIR", ""))
if _static.is_dir():
    app.mount("/", StaticFiles(directory=_static, html=True), name="frontend")
