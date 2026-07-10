# Personal Budgeting App — Roadmap

A self-hosted spending tracker with automatic UK Open Banking sync and a
categorization engine that learns from your corrections.

## Decisions (from interview, 2026-07-10)

| Area | Decision |
|---|---|
| Platform | Self-hosted web app; develop on Mac, Dockerized from day one |
| Stack | Python (FastAPI) backend + React SPA frontend |
| Database | SQLite (single user; trivially portable and backed up as one file) |
| Bank data | Monzo personal API (free, automatic) + CSV import with per-bank parsers for Barclays, Amex, and Revolut. No self-serve UK aggregator exists for individuals in 2026: GoCardless closed to new signups July 2025, Enable Banking is EEA-only, Plaid's free tier is US/CA-only, TrueLayer/Yapily/Salt Edge are sales-gated. Design the sync layer as pluggable so an aggregator can slot in if one appears. |
| Categorization | Hybrid: deterministic rules → local ML → Claude API fallback for low-confidence |
| LLM privacy | Only merchant/description strings sent to Claude — never balances or account IDs |
| Budget style | Tracking & insights first; budgets are a later phase |
| Accounts | 1–2 current accounts, credit card(s), savings/ISA — transfer & CC-payment matching required |
| Pace | Built rapidly with Claude Code, iterating in sessions |

## Before Phase 1 — your action items

These need you personally (accounts in your name):

1. **Monzo developer access**: sign in at developers.monzo.com with your Monzo
   account and create an OAuth client (confidential). Note the client ID/secret.
   Quirk to know: under SCA rules the API returns **all** transaction history
   only within ~5 minutes of authorising in the Monzo app; after that, only the
   last 90 days. The app will pull full history immediately on first connect.
   The API exposes Monzo accounts/pots only — **not** connected accounts.
2. **CSV exports** for the others (no personal APIs exist):
   - **Barclays**: online banking → statements → CSV/OFX export.
   - **Amex**: online account → statements → CSV (includes Amex's own categories).
   - **Revolut**: app → statement → CSV.
3. **Anthropic API key** for the LLM fallback (Phase 4).
4. The Enable Banking account created earlier is unused (EEA-only, no UK
   coverage) — safe to delete.

---

## Phase 0 — Scaffold (first session)

- Git repo, FastAPI backend, React (Vite) frontend, SQLite via SQLAlchemy + Alembic migrations.
- `docker-compose.yml` from the start so moving to an always-on box later is a copy job.
- Config/secrets via `.env` (Monzo client ID/secret, Anthropic key).

**Done when:** `docker compose up` serves a hello-world dashboard at localhost.

## Phase 1 — Data foundation & bank sync

The core value: transactions flow in automatically.

- Schema: `accounts`, `transactions`, `categories` (user-defined tree), `transaction_splits`, `tags`, `merchants` (normalized), `sync_log`.
- Pluggable sync layer: a `BankSource` interface so each provider (Monzo API,
  CSV parsers, any future aggregator) is an interchangeable plugin.
- Monzo integration:
  - OAuth flow against the personal API; webhook for real-time transactions later.
  - Full history pulled in the 5-minute post-authorization window; incremental sync thereafter.
- CSV import pipeline:
  - Drag-and-drop upload with per-bank parsers (Barclays, Amex, Revolut) auto-detected from the file shape.
  - Amex's own category column captured as a categorization hint.
- Idempotent dedup across all sources (pending→booked changes, overlapping CSV exports; match on amount/date/reference).
- Manual sync button first; scheduled background sync for Monzo (APScheduler) once stable.
- Basic transaction list UI: filter, search, sort.

**Done when:** Monzo syncs automatically and a month of Barclays/Amex/Revolut
CSVs imports cleanly into one deduplicated transaction list.

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
| 1 | Phase 0 + schema + Monzo OAuth connection |
| 2 | Monzo sync + CSV importers (Barclays, Amex, Revolut), dedup, transaction list UI |
| 3 | Categories, rules engine, review queue |
| 4 | Transfer/CC matching + first dashboards |
| 5 | ML layer + Claude fallback |
| 6+ | Recurring detection, insights, polish, budgets |
