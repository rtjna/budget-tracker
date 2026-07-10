# Personal Budgeting App — Roadmap

A self-hosted spending tracker with automatic UK Open Banking sync and a
categorization engine that learns from your corrections.

## Decisions (from interview, 2026-07-10)

| Area | Decision |
|---|---|
| Platform | Self-hosted web app; develop on Mac, Dockerized from day one |
| Stack | Python (FastAPI) backend + React SPA frontend |
| Database | SQLite (single user; trivially portable and backed up as one file) |
| Bank data | CSV import with per-bank parsers (Barclays, Amex, Revolut) is the primary pipeline; Monzo personal API optional (Monzo is only used as an aggregation view today — and connected-account data cannot be exported from Monzo by API, Sheets, or CSV). No self-serve UK aggregator exists for individuals in 2026: GoCardless closed to new signups July 2025, Enable Banking is EEA-only, Plaid's free tier is US/CA-only, TrueLayer/Yapily/Salt Edge are sales-gated. Sync layer stays pluggable in case one appears. |
| Categorization | Hybrid: deterministic rules → local ML → Claude API fallback for low-confidence |
| LLM privacy | Only merchant/description strings sent to Claude — never balances or account IDs |
| Budget style | Tracking & insights first; budgets are a later phase |
| Accounts | 1–2 current accounts, credit card(s), savings/ISA — transfer & CC-payment matching required |
| Pace | Built rapidly with Claude Code, iterating in sessions |

## Before Phase 1 — your action items

These need you personally (accounts in your name):

1. **CSV exports** from each bank — the primary data pipeline (no personal
   APIs exist, and connected-account data cannot be exported out of Monzo):
   - **Barclays**: online banking → statements → CSV/OFX export.
   - **Amex**: online account → statements → CSV (includes Amex's own categories).
   - **Revolut**: app → statement → CSV.
   Grab the longest history each offers upfront for ML training data and trends.
2. **Anthropic API key** for the LLM fallback (Phase 4).
3. *(Optional, later)* **Monzo developer access** at developers.monzo.com if
   meaningful money moves through the Monzo account itself. Quirk: under SCA
   rules the API returns full history only within ~5 minutes of authorising;
   after that, only the last 90 days. Exposes native Monzo accounts/pots only.
4. The Enable Banking account created earlier is unused (EEA-only, no UK
   coverage) — safe to delete.

---

## Phase 0 — Scaffold (first session)

- Git repo, FastAPI backend, React (Vite) frontend, SQLite via SQLAlchemy + Alembic migrations.
- `docker-compose.yml` from the start so moving to an always-on box later is a copy job.
- Config/secrets via `.env` (Anthropic key; Monzo credentials if/when added).

**Done when:** `docker compose up` serves a hello-world dashboard at localhost.

## Phase 1 — Data foundation & bank sync

The core value: transactions flow in automatically.

- Schema: `accounts`, `transactions`, `categories` (user-defined tree), `transaction_splits`, `tags`, `merchants` (normalized), `sync_log`.
- Pluggable sync layer: a `BankSource` interface so each provider (CSV parsers,
  Monzo API, any future aggregator) is an interchangeable plugin.
- CSV import pipeline — the primary path, so the UX gets the investment:
  - Drag-and-drop upload of multiple files at once; per-bank parsers (Barclays, Amex, Revolut) auto-detected from the file shape.
  - Import preview + summary (n new, n duplicates skipped, date range covered).
  - "Coverage" indicator per account: how recent the newest imported transaction is, nudging when an account is stale.
  - Amex's own category column captured as a categorization hint.
- Idempotent dedup across all sources (pending→booked changes, overlapping CSV exports; match on amount/date/reference).
- *(Optional)* Monzo integration: OAuth flow against the personal API; full history
  pulled in the 5-minute post-authorization window; incremental sync + webhook thereafter.
- Basic transaction list UI: filter, search, sort.

**Done when:** dropping a month of Barclays/Amex/Revolut exports into the app
takes under a minute and lands in one clean, deduplicated transaction list.

## Phase 2 — Categories, rules & the review workflow

Foundation for the ML: clean labels come from a workflow you actually enjoy using.

- Seed category tree (editable, nestable, archivable); split transactions across categories.
- Merchant normalization ("AMZN Mktp UK*2K4..." → "Amazon") — this single step does more for accuracy than any model choice.
- Rules engine: merchant/regex/amount-range → category, evaluated first, always wins. "Always categorize X as Y" one-click rule creation from any transaction.
- **Review queue**: inbox of uncategorized/low-confidence transactions with fast keyboard-driven triage. Every correction is stored as labeled training data with provenance (rule / model / LLM / human).

**Done when:** you can triage a week of spending in under a minute, and corrections accumulate as training data.

## Phase 3 — Multi-account intelligence

- Transfer matching: opposite-sign pairs across your accounts within a date window, auto-marked as transfers (excluded from spending).
- Credit card payment matching so statement payments aren't double-counted.
- Account overview / simple net-worth view from balances.

**Done when:** monthly "spending" numbers are trustworthy despite CC payments and savings shuffles.

## Phase 4 — The learning engine

Layered categorizer, each layer only firing when the previous abstains:

1. **Rules** (Phase 2) — deterministic, always win.
2. **Local ML** — TF-IDF character n-grams on normalized merchant + amount bucket, day-of-week, account as features; logistic regression or LightGBM. Calibrated confidence; below threshold → next layer. Retrains automatically from the corrections table (nightly or on-demand).
3. **Claude API fallback** — merchant string + your category tree + a few of your own past examples (few-shot from your corrections). Result cached per merchant so each merchant is asked at most once.
4. **Review queue** — anything still low-confidence lands with a human.

- Metrics page: accuracy per layer, % auto-categorized, corrections-over-time (should trend down).

**Done when:** >90% of new transactions auto-categorize correctly and the queue keeps shrinking.

## Phase 5 — Dashboards & insights

- Monthly breakdown by category, category trends over time, income vs. spending, merchant leaderboards.
- **Recurring detection**: same merchant at regular intervals → subscriptions/bills view; flag price increases and newly appeared subscriptions.
- Month-in-review summary.

**Done when:** you open the app to *learn* something, not just to file transactions.

## Phase 6 — Budgets (deliberately last)

By now there are months of clean data to set realistic limits against.

- Monthly category budgets with rollover option; budget-vs-actual on the dashboard; "safe to spend" figure.

## Phase 7 — Hardening & always-on

- Move the container to a home server/Pi/VPS when ready; add simple auth + HTTPS if it leaves localhost.
- Automated SQLite backups; data export (CSV/JSON) so you're never locked in.

---

## Suggested session plan

| Session | Target |
|---|---|
| 1 | Phase 0 + schema + first CSV importer working end-to-end (start with your real exports) |
| 2 | Remaining importers (Barclays, Amex, Revolut), dedup, import UX, transaction list UI |
| 3 | Categories, rules engine, review queue |
| 4 | Transfer/CC matching + first dashboards |
| 5 | ML layer + Claude fallback |
| 6+ | Recurring detection, insights, polish, budgets |
