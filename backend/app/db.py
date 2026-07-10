import os
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

DATA_DIR = Path(os.environ.get("DATA_DIR") or Path(__file__).resolve().parents[2] / "data")
DATA_DIR.mkdir(exist_ok=True)

engine = create_engine(
    f"sqlite:///{DATA_DIR / 'budget.sqlite3'}",
    connect_args={"check_same_thread": False},
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
