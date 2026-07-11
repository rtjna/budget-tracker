import csv
import hashlib
import io
from collections import defaultdict

from sqlalchemy import select
from sqlalchemy.orm import Session

from .categorize import apply_rules, normalize_merchant
from .importers import detect_importer
from .importers.base import ParsedRow
from .models import Account, ImportBatch, Transaction


class UnrecognizedFileError(Exception):
    pass


def fingerprint(account_id: int, row: ParsedRow, ordinal: int) -> str:
    key = f"{account_id}|{row.date.isoformat()}|{row.amount}|{row.description.lower()}|{ordinal}"
    return hashlib.sha256(key.encode()).hexdigest()


def get_or_create_account(db: Session, importer, name: str, currency: str) -> Account:
    account = db.scalar(select(Account).where(Account.name == name))
    if account is None:
        account = Account(
            name=name,
            provider=importer.provider,
            kind=importer.account_kind,
            currency=currency,
        )
        db.add(account)
        db.flush()
    return account


def import_file(db: Session, filename: str, text: str) -> ImportBatch:
    # Continental exports (e.g. ZKB) are semicolon-delimited.
    first_line = text.splitlines()[0] if text.splitlines() else ""
    delimiter = ";" if first_line.count(";") > first_line.count(",") else ","
    reader = csv.reader(io.StringIO(text), delimiter=delimiter)
    header = next(reader, None)
    if header is None:
        raise UnrecognizedFileError("File is empty")
    sample = [row for _, row in zip(range(5), reader)]
    importer = detect_importer(header, sample)
    if importer is None:
        raise UnrecognizedFileError(f"No importer recognizes the format of {filename!r}")

    return import_rows(
        db,
        source=importer.name,
        filename=filename,
        rows=importer.parse(text),
        provider=importer.provider,
        kind=importer.account_kind,
        default_account_name=importer.default_account_name,
    )


class _ImporterMeta:
    def __init__(self, provider: str, kind: str):
        self.provider = provider
        self.account_kind = kind


def import_rows(
    db: Session,
    *,
    source: str,
    filename: str,
    rows: list[ParsedRow],
    provider: str,
    kind: str,
    default_account_name: str,
) -> ImportBatch:
    meta = _ImporterMeta(provider, kind)

    accounts: dict[str, Account] = {}
    for row in rows:
        name = row.account or default_account_name
        if name not in accounts:
            accounts[name] = get_or_create_account(db, meta, name, row.currency)

    # Identical rows (same account/date/description/amount) are legitimate —
    # e.g. two identical coffees in one day — so each occurrence gets an
    # ordinal, making fingerprints stable across overlapping export files.
    groups: dict[tuple, list[ParsedRow]] = defaultdict(list)
    for row in rows:
        name = row.account or default_account_name
        groups[(name, row.date, row.description.lower(), row.amount)].append(row)

    candidates: list[tuple[str, int, ParsedRow]] = []
    for (name, *_), group in groups.items():
        account_id = accounts[name].id
        for ordinal, row in enumerate(group):
            candidates.append((fingerprint(account_id, row, ordinal), account_id, row))

    existing = set(
        db.scalars(
            select(Transaction.fingerprint).where(
                Transaction.fingerprint.in_([fp for fp, _, _ in candidates])
            )
        )
    )

    batch = ImportBatch(
        source=source,
        filename=filename,
        new_count=0,
        duplicate_count=0,
        date_min=min((r.date for r in rows), default=None),
        date_max=max((r.date for r in rows), default=None),
    )
    db.add(batch)
    db.flush()

    created: list[Transaction] = []
    for fp, account_id, row in candidates:
        if fp in existing:
            batch.duplicate_count += 1
            continue
        tx = Transaction(
            account_id=account_id,
            date=row.date,
            description=row.description,
            merchant=normalize_merchant(row.description),
            amount=row.amount,
            import_batch_id=batch.id,
            fingerprint=fp,
        )
        db.add(tx)
        created.append(tx)
        batch.new_count += 1

    apply_rules(db, created)
    db.commit()
    return batch
