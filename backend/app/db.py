import os
import sqlite3
from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

DATA_DIR = Path(os.environ.get("DATA_DIR") or Path(__file__).resolve().parents[2] / "data")
DATA_DIR.mkdir(exist_ok=True)


@event.listens_for(Engine, "connect")
def _enable_sqlite_foreign_keys(dbapi_connection, _connection_record) -> None:
    """SQLite ships with foreign key enforcement OFF per connection; turn it
    on everywhere (registered on the Engine class so test engines get it
    too) so dangling category/account/batch references can't be written."""
    if isinstance(dbapi_connection, sqlite3.Connection):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

engine = create_engine(
    f"sqlite:///{DATA_DIR / 'budget.sqlite3'}",
    # timeout: wait for a concurrent writer (e.g. a sync overlapping a manual
    # edit in another threadpool worker) instead of "database is locked".
    connect_args={"check_same_thread": False, "timeout": 30},
)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


def get_db():
    db: Session = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def ensure_columns() -> None:
    """Minimal forward-only migration: add columns create_all won't add to
    existing tables. Replace with Alembic once the schema stabilizes."""
    from sqlalchemy import text

    added = {
        "transactions": {
            "merchant": "VARCHAR",
            "category_source": "VARCHAR",
            "transfer_peer_id": "INTEGER REFERENCES transactions(id)",
            "trip_id": "INTEGER REFERENCES trips(id)",
        }
    }
    with engine.begin() as conn:
        for table, columns in added.items():
            existing = {
                row[1] for row in conn.execute(text(f"PRAGMA table_info({table})"))
            }
            for name, ddl_type in columns.items():
                if name not in existing:
                    conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {ddl_type}"))
