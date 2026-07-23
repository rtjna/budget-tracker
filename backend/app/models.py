from datetime import date, datetime, timezone

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Account(Base):
    __tablename__ = "accounts"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String, unique=True)
    provider: Mapped[str] = mapped_column(String)  # amex | barclays | revolut | monzo
    kind: Mapped[str] = mapped_column(String)  # current | credit | savings
    currency: Mapped[str] = mapped_column(String, default="GBP")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    transactions: Mapped[list["Transaction"]] = relationship(back_populates="account")


class Category(Base):
    __tablename__ = "categories"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String)
    parent_id: Mapped[int | None] = mapped_column(ForeignKey("categories.id"), nullable=True)

    __table_args__ = (UniqueConstraint("name", "parent_id"),)


class ImportBatch(Base):
    __tablename__ = "import_batches"

    id: Mapped[int] = mapped_column(primary_key=True)
    source: Mapped[str] = mapped_column(String)  # importer name, e.g. "amex"
    filename: Mapped[str] = mapped_column(String)
    imported_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    new_count: Mapped[int] = mapped_column(Integer, default=0)
    duplicate_count: Mapped[int] = mapped_column(Integer, default=0)
    date_min: Mapped[date | None] = mapped_column(Date, nullable=True)
    date_max: Mapped[date | None] = mapped_column(Date, nullable=True)


class Trip(Base):
    """A named, date-bounded trip whose cost is tracked across categories,
    accounts, currencies, and Splitwise corrections."""

    __tablename__ = "trips"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String, unique=True)
    start_date: Mapped[date] = mapped_column(Date)
    end_date: Mapped[date] = mapped_column(Date)


class TripReviewVerdict(Base):
    """Persisted outcome of the last suggestion review for a trip, so the
    confirmation checklist survives navigation without re-asking Claude."""

    __tablename__ = "trip_review_verdicts"

    id: Mapped[int] = mapped_column(primary_key=True)
    trip_id: Mapped[int] = mapped_column(ForeignKey("trips.id"), index=True)
    transaction_id: Mapped[int] = mapped_column(ForeignKey("transactions.id"))
    belongs: Mapped[bool] = mapped_column(Boolean)

    __table_args__ = (UniqueConstraint("trip_id", "transaction_id"),)


class Budget(Base):
    """A monthly spending limit for one category, effective from a given month
    onward. Keeping effective_from means a historical month is judged against
    the budget that applied *then*, and changing a limit never rewrites the
    past. The latest budget with effective_from <= a month's start applies to
    that month; category_id NULL is the overall (all-categories) budget."""

    __tablename__ = "budgets"

    id: Mapped[int] = mapped_column(primary_key=True)
    category_id: Mapped[int | None] = mapped_column(
        ForeignKey("categories.id"), nullable=True
    )
    amount: Mapped[float] = mapped_column(Numeric(12, 2))
    # First month the limit applies, stored as the first of that month.
    effective_from: Mapped[date] = mapped_column(Date)

    __table_args__ = (UniqueConstraint("category_id", "effective_from"),)


class BalanceSnapshot(Base):
    """A manually entered account balance on a date. The app tracks flows, not
    stock; snapshots give a net-worth line and double as a reconciliation check
    (two snapshots should differ by the sum of transactions between them)."""

    __tablename__ = "balance_snapshots"

    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"), index=True)
    date: Mapped[date] = mapped_column(Date, index=True)
    balance: Mapped[float] = mapped_column(Numeric(12, 2))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    __table_args__ = (UniqueConstraint("account_id", "date"),)


class MonthlyRate(Base):
    """A currency's GBP conversion rate for one month. When present it overrides
    the static GBP_RATES fallback, so historical foreign-currency transactions
    convert at a period-appropriate rate instead of today's."""

    __tablename__ = "monthly_rates"

    id: Mapped[int] = mapped_column(primary_key=True)
    currency: Mapped[str] = mapped_column(String, index=True)
    month: Mapped[str] = mapped_column(String)  # "YYYY-MM"
    rate: Mapped[float] = mapped_column(Numeric(18, 8))

    __table_args__ = (UniqueConstraint("currency", "month"),)


class Transaction(Base):
    __tablename__ = "transactions"

    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"), index=True)
    date: Mapped[date] = mapped_column(Date, index=True)
    description: Mapped[str] = mapped_column(String)
    # Normalized merchant key for grouping/rules/ML features.
    merchant: Mapped[str | None] = mapped_column(String, index=True, nullable=True)
    # Signed: negative = money out, positive = money in, on every account type.
    amount: Mapped[float] = mapped_column(Numeric(12, 2))
    category_id: Mapped[int | None] = mapped_column(ForeignKey("categories.id"), nullable=True)
    # Provenance of the categorization: human | rule | model | llm.
    category_source: Mapped[str | None] = mapped_column(String, nullable=True)
    import_batch_id: Mapped[int | None] = mapped_column(ForeignKey("import_batches.id"), nullable=True)
    # The opposite leg when this transaction is a transfer between own
    # accounts (set on both legs); transfers are excluded from spending.
    transfer_peer_id: Mapped[int | None] = mapped_column(
        ForeignKey("transactions.id"), nullable=True, index=True
    )
    fingerprint: Mapped[str] = mapped_column(String, unique=True, index=True)
    # Optional trip membership — orthogonal to category: a dinner in Tokyo is
    # still Eating Out AND part of the Japan trip.
    trip_id: Mapped[int | None] = mapped_column(ForeignKey("trips.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    account: Mapped[Account] = relationship(back_populates="transactions")


class LlmMerchantCache(Base):
    """One row per merchant ever sent to the LLM, so each merchant is asked
    at most once. category_id NULL means the LLM was unsure — the merchant
    stays in the review queue and is not re-asked."""

    __tablename__ = "llm_merchant_cache"

    id: Mapped[int] = mapped_column(primary_key=True)
    merchant: Mapped[str] = mapped_column(String, unique=True, index=True)
    category_id: Mapped[int | None] = mapped_column(ForeignKey("categories.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Rule(Base):
    __tablename__ = "rules"

    id: Mapped[int] = mapped_column(primary_key=True)
    # contains = case-insensitive substring on description; regex = re.search.
    match: Mapped[str] = mapped_column(String, default="contains")
    pattern: Mapped[str] = mapped_column(String)
    category_id: Mapped[int] = mapped_column(ForeignKey("categories.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    __table_args__ = (UniqueConstraint("match", "pattern"),)
