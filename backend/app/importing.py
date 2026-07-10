import csv
import hashlib
import io
from collections import defaultdict

from sqlalchemy import select
from sqlalchemy.orm import Session

from .importers import detect_importer
from .importers.base import ParsedRow
from .models import Account, ImportBatch, Transaction


class UnrecognizedFileError(Exception):
    pass


def fingerprint(account_id: int, row: ParsedRow, ordinal: int) -> str:
    key = f"{account_id}|{row.date.isoformat()}|{row.amount}|{row.description.lower()}|{ordinal}"
    return hashlib.sha256(key.encode()).hexdigest()


def get_or_create_account(db: Session, importer) -> Account:
    account = db.scalar(select(Account).where(Account.name == importer.default_account_name))
    if account is None:
        account = Account(
            name=importer.default_account_name,
            provider=importer.provider,
            kind=importer.account_kind,
        )
        db.add(account)
        db.flush()
    return account


def import_file(db: Session, filename: str, text: str) -> ImportBatch:
    reader = csv.reader(io.StringIO(text))
    header = next(reader, None)
    if header is None:
        raise UnrecognizedFileError("File is empty")
    sample = [row for _, row in zip(range(5), reader)]
    importer = detect_importer(header, sample)
    if importer is None:
        raise UnrecognizedFileError(f"No importer recognizes the format of {filename!r}")

    rows = importer.parse(text)
    account = get_or_create_account(db, importer)

    # Identical rows (same date/description/amount) are legitimate — e.g. two
    # identical coffees in one day — so each occurrence gets an ordinal, making
    # fingerprints stable across overlapping export files.
    groups: dict[tuple, list[ParsedRow]] = defaultdict(list)
    for row in rows:
        groups[(row.date, row.description.lower(), row.amount)].append(row)

    candidates: list[tuple[str, ParsedRow]] = []
    for group in groups.values():
        for ordinal, row in enumerate(group):
            candidates.append((fingerprint(account.id, row, ordinal), row))

    existing = set(
        db.scalars(
            select(Transaction.fingerprint).where(
                Transaction.fingerprint.in_([fp for fp, _ in candidates])
            )
        )
    )

    batch = ImportBatch(
        source=importer.name,
        filename=filename,
        new_count=0,
        duplicate_count=0,
        date_min=min((r.date for r in rows), default=None),
        date_max=max((r.date for r in rows), default=None),
    )
    db.add(batch)
    db.flush()

    for fp, row in candidates:
        if fp in existing:
            batch.duplicate_count += 1
            continue
        db.add(
            Transaction(
                account_id=account.id,
                date=row.date,
                description=row.description,
                amount=row.amount,
                import_batch_id=batch.id,
                fingerprint=fp,
            )
        )
        batch.new_count += 1

    db.commit()
    return batch
