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
design review). The larger unbuilt themes:

- **Budgets** — partially started (savings rate, committed-vs-discretionary
  split). Still to do: monthly category budgets with mid-month pace tracking,
  budget-vs-actual on the dashboard, a "safe to spend" figure. Now that there
  are months of clean data, limits can be set realistically.
- **Net worth** — the app tracks flows, not balances. Manual balance snapshots
  (which double as import-gap reconciliation) would give a net-worth line; see
  the advisory report.
- **Data quality** — dated FX rates: conversion currently uses one static
  rate table applied to all history, which distorts trends and trip totals for
  older foreign-currency transactions.
- **Hardening & always-on** — move the container to a home server / Pi / VPS;
  add simple auth + HTTPS if it ever leaves localhost; automated SQLite
  backups; data export (CSV/JSON) so you're never locked in.
