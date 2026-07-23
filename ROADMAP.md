# Personal Budgeting App — Decisions & remaining work

A self-hosted spending tracker with a categorization engine that learns from
your corrections. The original phase-by-phase build plan (scaffold → data
foundation → categories/rules/review → multi-account intelligence → learning
engine → dashboards) is done; see [README.md](README.md) for what the app does
today. This file now keeps the durable design decisions and the work that
remains.

## Decisions (from interview, 2026-07-10)

| Area | Decision |
|---|---|
| Platform | Self-hosted web app; develop on Mac, Dockerized from day one |
| Stack | Python (FastAPI) backend + React SPA frontend |
| Database | SQLite (single user; trivially portable and backed up as one file) |
| Bank data | CSV import with per-bank parsers (Barclays, Amex, Revolut) is the primary pipeline; Monzo personal API optional (Monzo is only used as an aggregation view today — and connected-account data cannot be exported from Monzo by API, Sheets, or CSV). No self-serve UK aggregator exists for individuals in 2026: GoCardless closed to new signups July 2025, Enable Banking is EEA-only, Plaid's free tier is US/CA-only, TrueLayer/Yapily/Salt Edge are sales-gated. Sync layer stays pluggable in case one appears. |
| Categorization | Hybrid: deterministic rules → local ML → Claude API fallback for low-confidence |
| LLM privacy | Only merchant/description strings sent to Claude — never balances or account IDs. (Exception since added, opt-in: the trip review sends each candidate's date/amount/currency/category/description; see [PRIVACY.md](PRIVACY.md).) |
| Budget style | Tracking & insights first; budgets are a later phase |
| Accounts | 1–2 current accounts, credit card(s), savings/ISA — transfer & CC-payment matching required |
| Pace | Built rapidly with Claude Code, iterating in sessions |

### Bank export quirks worth remembering

- **Monzo personal API**: under SCA rules the API returns full history only
  within ~5 minutes of authorising; after that, only the last 90 days. Exposes
  native Monzo accounts/pots only — connected-account (aggregation) data cannot
  be exported.
- **Enable Banking** account created early on is unused (EEA-only, no UK
  coverage) — safe to delete.

## Remaining work

The forward-looking backlog now lives in
[docs/advisory-2026-07.md](docs/advisory-2026-07.md) (a finance-insight and a
design review). Most of the roadmap themes are now built:

- **Budgets** — *done*: per-category monthly limits with mid-month pace
  tracking, budget-vs-actual, seed-from-averages, plus savings rate and the
  committed-vs-discretionary split. (A "safe to spend" figure and budget
  rollover remain as possible refinements.)
- **Net worth** — *done*: manual balance snapshots give a net-worth line and
  double as an import-gap reconciliation check.
- **Dated FX rates** — *done*: per-month rate overrides supersede the static
  table, so historical foreign-currency months convert at a period-accurate
  rate. Only the mechanism ships; historical rates are entered by hand.
- **Data export & backups** — *done*: CSV / full-JSON export from the Data
  tab, and `scripts/backup.sh` for timestamped SQLite backups.

Still open:

- **Always-on deployment** — move the container to a home server / Pi / VPS,
  and add simple auth + HTTPS if it ever leaves localhost. Deliberately not
  built yet: it depends on where (if anywhere) this gets hosted. Today it
  assumes a trusted single user on localhost.
- Refinements from the advisory report (income sub-breakdown, YoY /
  seasonality, tax-year view + ISA tracking, anomaly narration, etc.).
