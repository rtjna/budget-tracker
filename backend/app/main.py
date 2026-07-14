import os
from datetime import date
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Query, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from . import models
from .categorize import apply_rules, normalize_merchant, seed_categories
from .db import Base, SessionLocal, engine, ensure_columns, get_db
from .importing import CrossFormatOverlapError, UnrecognizedFileError, import_file
from .ml import apply_model, train
from .transfers import detect_transfers
from .xlsx import XlsxError, is_xlsx, xlsx_to_csv_text

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

# Single-user app served on loopback only. Rejecting other Host headers
# mitigates DNS rebinding, where a hostile page resolves its own hostname to
# 127.0.0.1 to reach this API from a browser. If you deliberately expose the
# app behind a reverse proxy, add its hostname here.
app.add_middleware(
    TrustedHostMiddleware,
    allowed_hosts=["localhost", "127.0.0.1"],
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# CSRF protection: every state-changing request must carry X-Budget-App: 1.
# HTML forms cannot set custom headers, and cross-origin fetch() with one
# forces a CORS preflight (which the CORS policy above only grants to the
# Vite dev origin), so a hostile page can't fire POSTs at this API. GET,
# HEAD and OPTIONS are exempt: reads are safe, and OPTIONS preflights by
# definition can't carry the header. The Monzo OAuth callback is a GET, so
# it stays reachable from a plain browser redirect.
CSRF_HEADER = "x-budget-app"


@app.middleware("http")
async def require_custom_header(request, call_next):
    if request.method not in ("GET", "HEAD", "OPTIONS") and request.headers.get(CSRF_HEADER) != "1":
        from fastapi.responses import JSONResponse

        return JSONResponse(
            status_code=403,
            content={"detail": "Missing X-Budget-App header (CSRF protection)"},
        )
    return await call_next(request)


@app.get("/api/health")
def health():
    return {"status": "ok"}


@app.get("/api/monzo/status")
def monzo_status():
    from . import monzo

    return monzo.status()


@app.get("/api/monzo/connect")
def monzo_connect():
    from . import monzo

    try:
        return {"url": monzo.connect_url()}
    except monzo.MonzoError as e:
        raise HTTPException(status_code=503, detail=str(e))


@app.get("/api/monzo/callback")
def monzo_callback(code: str, state: str):
    from fastapi.responses import HTMLResponse

    from . import monzo

    try:
        monzo.handle_callback(code, state)
    except monzo.MonzoError as e:
        return HTMLResponse(f"<h3>Monzo connection failed</h3><p>{e}</p>", status_code=400)
    return HTMLResponse(
        "<h3>Monzo connected ✓</h3>"
        "<p>Now open the <strong>Monzo app</strong> and approve access "
        "(Settings → Privacy &amp; security, or the prompt it shows you), then go back "
        "to Budget Tracker and press <strong>Sync Monzo</strong> within 5 minutes "
        "to capture your full history.</p>"
    )


@app.post("/api/monzo/sync")
def monzo_sync(db: Session = Depends(get_db)):
    from . import monzo

    try:
        result = monzo.sync(db)
    except monzo.NotConnected as e:
        raise HTTPException(status_code=409, detail=str(e))
    except monzo.MonzoError as e:
        raise HTTPException(status_code=503, detail=str(e))
    result["transfers"] = detect_transfers(db)
    result["model_categorized"] = apply_model(db)["applied"]
    db.commit()
    return result


@app.get("/api/splitwise/status")
def splitwise_status():
    from .secrets_env import get_secret

    return {"configured": get_secret("SPLITWISE_API_KEY") is not None}


@app.post("/api/splitwise/sync")
def splitwise_sync(db: Session = Depends(get_db)):
    from .splitwise import sync

    try:
        return sync(db)
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))


@app.get("/api/llm/status")
def llm_status():
    from .secrets_env import get_secret

    # Reports only presence, never the value.
    return {"configured": get_secret("ANTHROPIC_API_KEY") is not None}


@app.post("/api/llm/categorize")
def llm_categorize(db: Session = Depends(get_db), max_merchants: int = Query(default=200, le=1000)):
    import anthropic

    from .llm import categorize_merchants

    # Link transfer pairs first so Claude is never asked about a payment leg
    # that simply hadn't been matched yet.
    detect_transfers(db)
    try:
        return categorize_merchants(db, max_merchants=max_merchants)
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))
    except anthropic.APIError as e:
        # Progress is committed per batch, so a mid-run failure loses nothing.
        raise HTTPException(status_code=502, detail=f"Claude API error: {e}")


MAX_UPLOAD_BYTES = 25 * 1024 * 1024  # far above any real bank export


@app.post("/api/imports")
async def create_import(file: UploadFile, db: Session = Depends(get_db)):
    data = await file.read(MAX_UPLOAD_BYTES + 1)
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="File is larger than 25 MB — not a bank export")
    try:
        if data[:5] == b"%PDF-":
            from .importers import barclaycard_pdf, barclays_pdf
            from .importing import import_rows

            rows = None
            source = account = provider = kind = ""
            errors: list[str] = []
            for module, src, acct, prov, knd in (
                (barclays_pdf, "barclays_pdf", "Barclays", "barclays", "current"),
                (barclaycard_pdf, "barclaycard_pdf", "Barclaycard", "barclaycard", "credit"),
            ):
                try:
                    rows = module.parse_pdf(data)
                    source, account, provider, kind = src, acct, prov, knd
                    break
                except barclays_pdf.StatementDecodeError as e:
                    errors.append(f"{acct}: {e}")
                except Exception:
                    # pdfplumber/pdfminer raise a zoo of exceptions on corrupt,
                    # truncated or password-protected PDFs.
                    errors.append(f"{acct}: unreadable PDF (corrupt or password-protected?)")
            if rows is None:
                raise HTTPException(
                    status_code=422,
                    detail="Could not parse this PDF as a bank statement — " + " / ".join(errors),
                )
            batch = import_rows(
                db,
                source=source,
                filename=file.filename or "statement.pdf",
                rows=rows,
                provider=provider,
                kind=kind,
                default_account_name=account,
            )
        else:
            if is_xlsx(data):
                try:
                    text = xlsx_to_csv_text(data)
                except XlsxError as e:
                    raise HTTPException(status_code=422, detail=str(e))
            else:
                try:
                    text = data.decode("utf-8-sig")
                except UnicodeDecodeError:
                    # Some bank exports arrive in a legacy 8-bit encoding
                    # (e.g. latin-1 £ signs); fall back before giving up.
                    try:
                        text = data.decode("latin-1")
                    except UnicodeDecodeError:
                        raise HTTPException(
                            status_code=422,
                            detail="File is not readable as text (expected UTF-8 or Latin-1 CSV)",
                        )
            batch = import_file(db, file.filename or "upload.csv", text)
    except (CrossFormatOverlapError, UnrecognizedFileError) as e:
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


@app.get("/api/model/audit")
def model_audit(db: Session = Depends(get_db)):
    """Second opinions: machine-categorized rows (rule/model/llm) where the
    trained model confidently disagrees, grouped by merchant and change."""
    from .ml import audit

    groups: dict[tuple, dict] = {}
    for tx, suggested, confidence in audit(db):
        key = (tx.merchant, tx.category_id, suggested)
        group = groups.setdefault(
            key,
            {
                "merchant": tx.merchant,
                "current_category_id": tx.category_id,
                "suggested_category_id": suggested,
                "source": tx.category_source,
                "sample_description": tx.description,
                "transaction_ids": [],
                "confidence": 0.0,
            },
        )
        group["transaction_ids"].append(tx.id)
        group["confidence"] = max(group["confidence"], confidence)
    return {
        "groups": sorted(
            groups.values(), key=lambda g: (-len(g["transaction_ids"]), -g["confidence"])
        )
    }


class AuditResolveBody(BaseModel):
    transaction_ids: list[int]
    category_id: int


@app.post("/api/model/audit/resolve")
def model_audit_resolve(body: AuditResolveBody, db: Session = Depends(get_db)):
    """Human verdict on a second opinion. Marks the rows as human-labeled
    whether the suggestion was accepted or the current category kept, so the
    decision feeds training and the group is never flagged again."""
    if db.get(models.Category, body.category_id) is None:
        raise HTTPException(status_code=422, detail="Unknown category")
    txs = db.scalars(
        select(models.Transaction).where(models.Transaction.id.in_(body.transaction_ids))
    ).all()
    for tx in txs:
        tx.category_id = body.category_id
        tx.category_source = "human"
    db.commit()
    return {"updated": len(txs)}


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


@app.get("/api/stats/overview")
def stats_overview(db: Session = Depends(get_db), months: int = Query(default=12, le=60)):
    from .stats import monthly_overview

    return monthly_overview(db, months=months)


class TripBody(BaseModel):
    name: str
    start_date: date
    end_date: date


class TripAssignBody(BaseModel):
    add: list[int] = []
    remove: list[int] = []


@app.get("/api/trips")
def list_trips(db: Session = Depends(get_db)):
    from .trips import trip_stats

    return [
        trip_stats(db, trip)
        for trip in db.scalars(select(models.Trip).order_by(models.Trip.start_date.desc()))
    ]


@app.post("/api/trips")
def create_trip(body: TripBody, db: Session = Depends(get_db)):
    name = body.name.strip()
    if not name or body.end_date < body.start_date:
        raise HTTPException(status_code=422, detail="A trip needs a name and start <= end")
    if db.scalar(select(models.Trip).where(models.Trip.name == name)):
        raise HTTPException(status_code=409, detail=f"Trip {name!r} already exists")
    trip = models.Trip(name=name, start_date=body.start_date, end_date=body.end_date)
    db.add(trip)
    db.commit()
    return {"id": trip.id, "name": trip.name, "start_date": trip.start_date, "end_date": trip.end_date}


@app.delete("/api/trips/{trip_id}")
def delete_trip(trip_id: int, db: Session = Depends(get_db)):
    trip = db.get(models.Trip, trip_id)
    if trip is None:
        raise HTTPException(status_code=404, detail="Trip not found")
    for tx in db.scalars(select(models.Transaction).where(models.Transaction.trip_id == trip_id)):
        tx.trip_id = None
    db.delete(trip)
    db.commit()
    return {"deleted": trip_id}


@app.post("/api/trips/{trip_id}/suggest")
def suggest_trip(trip_id: int, db: Session = Depends(get_db)):
    """LLM (or heuristic fallback) review of the trip's candidate window —
    3 months before through 1 month after. Suggestions only; nothing is
    assigned until /assign."""
    import anthropic

    from .trips import review

    trip = db.get(models.Trip, trip_id)
    if trip is None:
        raise HTTPException(status_code=404, detail="Trip not found")
    try:
        return review(db, trip)
    except anthropic.APIError as e:
        raise HTTPException(status_code=502, detail=f"Claude API error: {e}")


@app.post("/api/trips/{trip_id}/assign")
def assign_trip(trip_id: int, body: TripAssignBody, db: Session = Depends(get_db)):
    trip = db.get(models.Trip, trip_id)
    if trip is None:
        raise HTTPException(status_code=404, detail="Trip not found")
    added = removed = 0
    for tx in db.scalars(select(models.Transaction).where(models.Transaction.id.in_(body.add))):
        if tx.trip_id not in (None, trip_id):
            raise HTTPException(
                status_code=409, detail=f"Transaction {tx.id} already belongs to another trip"
            )
        tx.trip_id = trip_id
        added += 1
    for tx in db.scalars(
        select(models.Transaction).where(
            models.Transaction.id.in_(body.remove), models.Transaction.trip_id == trip_id
        )
    ):
        tx.trip_id = None
        removed += 1
    db.commit()
    from .trips import trip_stats

    return {"added": added, "removed": removed, "trip": trip_stats(db, trip)}


@app.get("/api/stats/year")
def stats_year(db: Session = Depends(get_db), year: int | None = Query(default=None, ge=1900, le=2200)):
    from .stats import year_summary

    return year_summary(db, year=year)


@app.get("/api/stats/month/{month}")
def stats_month(month: str, db: Session = Depends(get_db)):
    from .stats import month_detail

    return month_detail(db, month)


@app.get("/api/stats/category/{category_id}/merchants")
def stats_category_merchants(
    category_id: int, db: Session = Depends(get_db), months: int = Query(default=12, le=60)
):
    from .stats import category_merchants

    return category_merchants(db, category_id, months=months)


@app.get("/api/stats/coverage")
def stats_coverage(db: Session = Depends(get_db)):
    from .stats import coverage

    return coverage(db)


@app.get("/api/stats/recurring")
def stats_recurring(db: Session = Depends(get_db)):
    from .stats import recurring

    return {"items": recurring(db)}


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
    # When the account was last fed data (file import or API sync) — distinct
    # from the latest transaction date, which also moves with mere inactivity.
    last_imports = dict(
        db.execute(
            select(models.Transaction.account_id, func.max(models.ImportBatch.imported_at))
            .join(models.ImportBatch, models.Transaction.import_batch_id == models.ImportBatch.id)
            .group_by(models.Transaction.account_id)
        ).all()
    )
    return [
        {
            "id": account.id,
            "name": account.name,
            "provider": account.provider,
            "kind": account.kind,
            "currency": account.currency,
            "transaction_count": count,
            "latest_transaction": latest,
            "last_imported": last_imports.get(account.id),
        }
        for account, count, latest in rows
    ]


@app.get("/api/transactions")
def list_transactions(
    db: Session = Depends(get_db),
    account_id: int | None = None,
    provider: str | None = None,
    category_id: int | None = None,
    search: str | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    month: str | None = Query(default=None, pattern=r"^\d{4}-\d{2}$"),
    uncategorized: bool = False,
    order: str = Query(default="date_desc", pattern="^(date|amount)_(asc|desc)$"),
    limit: int = Query(default=50, le=500),
    offset: int = 0,
):
    query = select(models.Transaction)
    if account_id is not None:
        query = query.where(models.Transaction.account_id == account_id)
    if provider is not None:
        # All of a provider's accounts at once (e.g. every Revolut currency).
        query = query.where(
            models.Transaction.account_id.in_(
                select(models.Account.id).where(models.Account.provider == provider)
            )
        )
    if category_id is not None:
        query = query.where(models.Transaction.category_id == category_id)
    if search:
        query = query.where(or_(models.Transaction.description.icontains(search)))
    if date_from is not None:
        query = query.where(models.Transaction.date >= date_from)
    if date_to is not None:
        query = query.where(models.Transaction.date <= date_to)
    if month is not None:
        query = query.where(func.strftime("%Y-%m", models.Transaction.date) == month)
    if uncategorized:
        query = query.where(models.Transaction.category_id.is_(None))

    total = db.scalar(select(func.count()).select_from(query.subquery()))
    # "Size" is magnitude: a £3,000 salary and a £3,000 rent payment are both
    # big, regardless of sign. Date breaks ties either way.
    ordering = {
        "date_asc": (models.Transaction.date.asc(), models.Transaction.id.asc()),
        "date_desc": (models.Transaction.date.desc(), models.Transaction.id.desc()),
        "amount_desc": (func.abs(models.Transaction.amount).desc(), models.Transaction.date.desc()),
        "amount_asc": (func.abs(models.Transaction.amount).asc(), models.Transaction.date.desc()),
    }[order]
    txs = db.scalars(query.order_by(*ordering).limit(limit).offset(offset)).all()
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
                "manual": t.import_batch_id is None,
            }
            for t in txs
        ],
    }


class ManualTransactionBody(BaseModel):
    account_id: int  # 0 = the manual "Cash" account (created on demand)
    date: date
    description: str
    amount: float  # signed: negative = money out
    category_id: int | None = None


@app.post("/api/transactions")
def create_transaction(body: ManualTransactionBody, db: Session = Depends(get_db)):
    import uuid

    from .categorize import normalize_merchant

    if body.account_id == 0:
        account = db.scalar(select(models.Account).where(models.Account.name == "Cash"))
        if account is None:
            account = models.Account(name="Cash", provider="manual", kind="cash", currency="GBP")
            db.add(account)
            db.flush()
    else:
        account = db.get(models.Account, body.account_id)
        if account is None:
            raise HTTPException(status_code=404, detail="Account not found")

    description = body.description.strip()
    if not description or body.amount == 0:
        raise HTTPException(status_code=422, detail="Description and a non-zero amount are required")

    tx = models.Transaction(
        account_id=account.id,
        date=body.date,
        description=description,
        merchant=normalize_merchant(description),
        amount=body.amount,
        category_id=body.category_id,
        category_source="human" if body.category_id is not None else None,
        # Manual entries deliberately bypass import dedup.
        fingerprint=f"manual|{uuid.uuid4()}",
    )
    db.add(tx)
    db.commit()
    return {"id": tx.id, "account_id": account.id}


@app.delete("/api/transactions/{tx_id}")
def delete_transaction(tx_id: int, db: Session = Depends(get_db)):
    tx = db.get(models.Transaction, tx_id)
    if tx is None:
        raise HTTPException(status_code=404, detail="Transaction not found")
    if tx.import_batch_id is not None:
        raise HTTPException(status_code=409, detail="Only manually entered transactions can be deleted")
    if tx.transfer_peer_id is not None:
        peer = db.get(models.Transaction, tx.transfer_peer_id)
        if peer is not None:
            peer.transfer_peer_id = None
    db.delete(tx)
    db.commit()
    return {"deleted": tx_id}


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
    name = body.name.strip()
    # Explicit duplicate check: the (name, parent_id) unique constraint does
    # not catch top-level duplicates because SQLite treats NULLs as distinct.
    parent_matches = (
        models.Category.parent_id.is_(None)
        if body.parent_id is None
        else models.Category.parent_id == body.parent_id
    )
    if db.scalar(select(models.Category).where(models.Category.name == name, parent_matches)):
        raise HTTPException(status_code=409, detail=f"Category {name!r} already exists")
    category = models.Category(name=name, parent_id=body.parent_id)
    db.add(category)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail=f"Category {name!r} already exists")
    return {"id": category.id, "name": category.name, "parent_id": category.parent_id}


@app.delete("/api/categories/{category_id}")
def delete_category(category_id: int, force: bool = False, db: Session = Depends(get_db)):
    category = db.get(models.Category, category_id)
    if category is None:
        raise HTTPException(status_code=404, detail="Category not found")
    # Without force, block deletion while anything still references the
    # category; with SQLite FK enforcement on, deleting anyway would fail at
    # commit time with an opaque IntegrityError. The 409 detail tells the
    # frontend what force would unwind.
    references = [
        ("transactions", models.Transaction, models.Transaction.category_id),
        ("rules", models.Rule, models.Rule.category_id),
        ("LLM merchant cache entries", models.LlmMerchantCache, models.LlmMerchantCache.category_id),
        ("subcategories", models.Category, models.Category.parent_id),
    ]
    used_by = []
    for label, model, column in references:
        count = db.scalar(select(func.count()).select_from(model).where(column == category_id))
        if count:
            used_by.append(f"{count} {label}")
    if used_by and not force:
        raise HTTPException(status_code=409, detail=f"Category is used by {', '.join(used_by)}")
    if force:
        # Transactions return to the review queue; rules and cached LLM
        # answers for the category are dropped (so merchants can be re-asked);
        # subcategories become top-level.
        for tx in db.scalars(
            select(models.Transaction).where(models.Transaction.category_id == category_id)
        ):
            tx.category_id = None
            tx.category_source = None
        for rule in db.scalars(select(models.Rule).where(models.Rule.category_id == category_id)):
            db.delete(rule)
        for entry in db.scalars(
            select(models.LlmMerchantCache).where(models.LlmMerchantCache.category_id == category_id)
        ):
            db.delete(entry)
        for child in db.scalars(
            select(models.Category).where(models.Category.parent_id == category_id)
        ):
            child.parent_id = None
    db.delete(category)
    db.commit()
    return {"deleted": category_id, "unwound": used_by}


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
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status_code=409, detail=f"A {body.match} rule for {body.pattern.strip()!r} already exists"
        )
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
