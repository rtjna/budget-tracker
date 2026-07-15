# Advisory reports — July 2026

Two advisory subagent reports commissioned 2026-07-15: a personal-finance
advisor on what further insight features the app should gain, and a design
consultant on layout/IA improvements. Advisory only — nothing here has been
implemented yet. Reports reproduced verbatim.

---

# Report 1: Financial-Insight Roadmap

**Scope reviewed:** `backend/app/stats.py`, `main.py`, `trips.py`, `models.py`, `categorize.py`, `splitwise.py`, `transfers.py`, importers; `frontend/src/Dashboard.tsx`, `App.tsx`, `Coverage.tsx`.

**Note on existing features:** beyond the brief's list, the app *already* has recurring-payment detection with price-change flags, lapsed/active status, next-expected dates and a subscriptions-per-month tile (`stats.recurring`), month-level top merchants (`month_detail`), a per-category drill-down with merchant table and monthly trend (`CategoryView` + `category_merchants`), and a data-coverage view. None of those are re-proposed below.

## Top 5

### 1. Category budgets with mid-month pace tracking — Priority: Highest, Effort: M
**The question it answers:** "Am I on track this month, or do I find out I overspent three weeks after the fact?" This is the single biggest gap: the app is entirely retrospective — it describes spending but never compares it to an intention. Everything else is reporting; this is budgeting.
**How:** New `Budget` table (`category_id`, `monthly_amount`, `effective_from date`) — the effective-date column matters so historical months are judged against the budget that applied *then*, not today's. A `/api/stats/budget/{month}` endpoint joins onto the aggregation already in `monthly_overview` (same refund semantics, same GBP conversion). Frontend: a per-category bullet bar (spent vs budget) on the month view, with a pace marker — "day 15 of 31: you've used 85% of Eating Out." The "Average month (last 12)" pseudo-month is a ready-made seed: "set budgets from your averages" one-click. Unbudgeted categories roll into an implicit "everything else" line so the total reconciles with the month's spending figure.

### 2. Savings rate + fixed-vs-discretionary split — Priority: High, Effort: S
**The question it answers:** "What fraction of my income do I keep, and how much of my spending could I actually change?" These are the two numbers a human advisor computes first, and all the data already exists.
**How:** Savings rate = `(income − spending) / income`, with invested shown as its component (the net-invested series already exists) — one extra tile plus a 3-month rolling line so a single bonus month doesn't whipsaw it. Fixed-vs-discretionary: classify spending as *committed* (active recurring merchants from `recurring()` + a small set of categories: Housing, Utilities & Bills, Subscriptions) vs everything else. Two stacked numbers: "Committed £X (41% of income) / Discretionary £Y." This reframes every "can I afford…?" question, and it is nearly free — pure derivation in `stats.py`, no schema change.

### 3. "What changed this month" anomaly panel — Priority: High, Effort: M
**The question it answers:** "Why was March expensive?" Today the user must eyeball the stacked bars and diff mentally. An advisor's monthly review is exactly this diff, and it's mechanical.
**How:** New `month_insights(db, month)` in `stats.py`, comparing the month against the trailing-12 median per category (data already computed in `monthly_overview`). Emit a ranked list of plain-English findings: categories > ~1.5× their median (with the £ delta), the month's largest single transactions, merchants seen for the first time ever (cheap: min date per merchant), recurring price rises (already detected — just not surfaced as an event), and lapsed subscriptions that stopped charging. Render as a short bulleted "This month" card above the charts. Optional cherry: a "narrate this month" button through the existing `llm.py` plumbing turning the findings into two sentences — but the deterministic list is the real value.

### 4. Trip-adjusted baseline ("what does a normal month cost me?") — Priority: High, Effort: S
**The question it answers:** "Ignoring Japan, is my day-to-day spending under control?" Trips are already meticulously tracked, but monthly stats still include trip spending, so any travel month looks like a blowout and pollutes the 12-month average — the exact average the user would base budgets on.
**How:** `trip_id` is already on every transaction. Add `?exclude_trips=true` to `/api/stats/overview` (one extra `where` in `_spending_transactions`) and a dashboard toggle. Show trip spending as its own series/segment (like Invested) rather than deleting it, so totals still reconcile. This makes proposals 1–3 dramatically more accurate for a frequent traveller — averages, budgets, and anomaly baselines should all run on the trip-excluded series.

### 5. End-of-month cash-flow forecast — Priority: High, Effort: M
**The question it answers:** "Where will this month land?" — turning the dashboard from a rear-view mirror into a windscreen.
**How:** The hard part is already built: `recurring()` produces `next_expected` dates and typical amounts. Forecast = actual spend to date + recurring commitments still due this month + (median daily *discretionary* run-rate from the last 3 months × days remaining, excluding trip-tagged spend). Show as a projected extension on the current month's column plus a tile: "Projected: £2,450 (vs £2,210 typical)." Pairs naturally with budgets (#1): "on pace to exceed Groceries by £40."

## Second tier

### 6. Net worth via balance anchors — Priority: Medium-high, Effort: L
The app tracks flows, never stock: it cannot answer "am I getting richer?" The `Transaction` model has no balance and importers discard the running-balance columns that Barclays statements (and the Monzo API) provide. Two-step approach: (a) a `BalanceSnapshot` table (`account_id`, `date`, `balance`) with manual entry per account — S effort, immediately gives a net-worth line and doubles as a *reconciliation check* (snapshot vs snapshot should equal the sum of imported transactions between them, catching import gaps the coverage view can't see); (b) later, capture balances automatically at import/Monzo-sync time. Investment account *value* would stay manual-snapshot-only, which is fine at monthly cadence. Large but it's the one number a financial advisor tracks above all others.

### 7. UK tax-year view and ISA-allowance tracking — Priority: Medium, Effort: S
The user is UK-based; the year view is calendar-only. Add an April-6-to-April-5 toggle to `year_summary`, and — since Investing is already isolated as its own series — a "£X of £20,000 ISA allowance used this tax year" tile (perhaps a per-account flag on `Account.kind` for which investing accounts are ISAs). Cheap and directly actionable every March.

### 8. Year-over-year and seasonality comparison — Priority: Medium, Effort: S
The 12-month window can't answer "is this December worse than last December?" or "is Groceries drifting up year on year?" Add same-month-last-year deltas to the month tiles (data is one `months=24` fetch away) and a YoY per-category delta column in the year view. Trivial backend change; mostly presentation.

### 9. Hierarchy-aware categories + income breakdown — Priority: Medium, Effort: M
`Category.parent_id` exists in the schema and the create-category API, but every aggregate in `stats.py` treats categories as flat — subcategories currently *fragment* insight instead of adding it. Roll subcategories into their parent in `monthly_overview`/`year_summary`, expanding on click. Then use it on the income side: Income is one undifferentiated bucket today; subcategories (Salary, Interest, Refunds, Other) would show whether savings-rate changes come from earning or spending, and surface interest earned — which currently vanishes into Income.

### 10. Fees and interest tracker — Priority: Medium, Effort: S
A "Fees & Charges" category already exists but gets no dedicated treatment. A small annual tile — "£X in fees and interest this year (£Y Barclaycard interest, £Z FX/ATM fees)" via the existing category-merchant machinery — is the classic advisor quick win, since fees are the most fixable spending there is. Mostly needs rules/LLM prompting to reliably route interest lines and Revolut fee lines into the category, then one widget.

### 11. Grocery/merchant price-inflation view — Priority: Low-medium, Effort: M
Recurring detection already flags subscription price rises; extend the idea to *variable* frequent merchants: average transaction size and monthly total at the top 5–10 merchants, YoY ("your average Tesco shop is up 11%"). Distinguishes inflation from lifestyle creep — a question the user genuinely cannot answer today.

### 12. Data-quality: dated FX rates — Priority: Medium (it underpins everything), Effort: S-M
All multi-currency insight quality hangs on the hardcoded mid-2026 `GBP_RATES`, applied to *historical* transactions — a Revolut-heavy month from 2024 is converted at 2026 rates, which quietly distorts trends, trip totals, and YoY comparisons. A monthly-rate table (currency, month, rate) loaded from a periodically hand-updated file (or a free ECB CSV pulled on demand) keeps the self-hosted, no-cloud character while making the trend data honest. `to_gbp` already centralizes conversion, so the change is contained.

## Considered and rejected
- **Peer/population benchmarks** ("you spend more on eating out than the average Briton") — needs external data of dubious comparability; wrong fit for a private, single-user app.
- **Open-banking live aggregation** — large surface area and credential risk; the CSV/API import discipline already works and the coverage view guards against gaps.
- **Push/email alerts** — data only updates at import time, so "alerts" would fire on stale data; the "What changed" panel (#3) at import time delivers the same value honestly.
- **Portfolio performance tracking** — the data model sees investment *cash flows* only, not holdings or prices; net-invested plus manual snapshots (#6) is the honest ceiling here.

**Suggested build order:** 2 → 4 → 1 → 3 → 5 (the two S items first — they compound the value of budgets, anomalies, and forecasting that follow), then 7/8/12 as small wins, with 6 as the one larger investment worth planning properly.

---

# Report 2: Design & Layout Advisory

Scope: `/frontend/src/` (App.tsx, Dashboard.tsx, Coverage.tsx, App.css, Dashboard.css, Coverage.css, index.css). This deliberately does not repeat docs/audit-2026-07.md; where a finding touches a prior audit item, it is because the fix was partial and the residue is new work.

## Top 5

1. **(P1, M) Get the import dropzone and account cards out of the permanent shell.** They render above the tabs on *every* view, so the thing a daily user actually came for (the dashboard) starts ~300px down the page. Move both into a "Data" tab (merged with Coverage), keep a window-level drag-drop overlay so files can still be dropped from anywhere.
2. **(P1, M) Add drill-down from Dashboard to Transactions.** The month/category/merchant views are dead ends — you can see "Groceries £412 in June" but there is no path to the underlying rows. Category-by-month drill-down is pure frontend (the filters already exist); merchant drill-down needs one backend param.
3. **(P1, S) Fix the month-view reading order.** Clicking a bar updates the KPI row *above* the chart and the category/merchant detail *two full-width charts below* it — the response to a selection is split across both ends of the page. Reorder so month detail sits directly under the stacked chart.
4. **(P1, S–M) Finish the design-token migration and establish a button hierarchy.** App.css still runs on `#8883–#888a` borders and hardcoded `#d33`/`#d97706` while Dashboard.css uses tokens — the audit-H1 fix moved tokens to `:root` but never migrated the shell to consume them. Separately, every button looks identical: "Save trip assignment" and "Cancel" carry equal visual weight.
5. **(P1, S) Give forms real labels.** AddTransaction and the trip form are placeholder-only; the trip form has two naked `<input type="date">` fields whose meaning (start vs end) is only discoverable via hover tooltip.

---

## A. Information architecture & navigation

### A1. The shell carries import UI as permanent chrome — P1, effort M
**Problem as experienced:** Every visit to any tab starts with "Drop bank exports here…" and a row of account cards. Importing happens maybe weekly; looking at the dashboard happens daily. The app leads with its least-used feature, and on a laptop the dashboard KPIs start near or below the fold.
**Change:**
- Create a **Data** tab (or "Accounts") that absorbs: the dropzone + import results, the account cards, the Coverage heatmap, and the three sync actions (Detect transfers, Sync Splitwise, Sync Monzo). These are all one mental job — "get data in and check it's complete" — currently smeared across the shell, the Transactions filter bar, and the Coverage tab.
- Keep drag-and-drop global: listen for `dragenter` on `window`, show a full-viewport drop overlay ("Drop to import"), route to the same `uploadFiles`. Nothing is lost; the dropzone just stops being furniture.
- The account cards' click-to-filter behavior moves with them and still deep-links to Transactions.
- Tab row becomes: **Dashboard · Transactions · Review (n) · Data**. The stale-account warning (`⚠ stale`) deserves promotion: surface it as a small notice chip next to the Data tab label so it's still seen without the cards being omnipresent.
**Backend:** none.

### A2. Sync actions are mixed into the Transactions *filter* bar — P1 (falls out of A1), effort S
**Problem:** `.filters` contains search/account/category/month/sort/uncategorized — and then four buttons, three of which mutate data ("Detect transfers", "Sync Splitwise", "Sync Monzo") and one that opens a form. Users scanning for a filter parse eight controls; the destructive/network actions look like filters. It also makes the wrap behavior ugly on mid-width windows: buttons interleave with selects.
**Change:** Keep only query controls in `.filters`. Move syncs to the Data tab (A1); keep "+ Add transaction" but visually separate it (right-aligned, primary style — see D1). If you want sync reachable from Transactions, a small "⟳ Sync" menu button is enough.

### A3. Dashboard → Transactions drill-down is missing — P1, effort M
**Problem:** The account cards already prove the pattern (click → filtered Transactions), but nothing on the Dashboard does it. From "Categories — June 2026" or "Top merchants" there is no way to answer "which transactions are these?" without manually re-creating the filter on the Transactions tab.
**Change:** Lift `accountFilter/categoryFilter/monthFilter` setters into a callback passed to `<Dashboard onDrill={(f) => …}>` (they live in App.tsx already). Then:
- Month-detail BarList rows → Transactions filtered to that category + month (both filters exist in the API — frontend only).
- Year view rows → category + that year's months (needs either a `year` param or month-range; the API takes a single `month`, so flag: **small backend change** — a `year=` filter on `/api/transactions` — or skip).
- Merchant rows (Top merchants, Recurring) → **backend change**: `/api/transactions` currently has `search` (description) but no `merchant` param; add one, since merchant is a first-class column. Using `search=` as a stopgap works imperfectly.
Add a chevron or "view →" affordance on hover so rows read as links, and make the rows real buttons (the CategoryView rows already are).

### A4. Trips is a workflow hiding inside a chart-view switcher — P2, effort S
**Problem:** The ViewSwitch pill says "By month / By category / By year / Trips". The first three are lenses on the same data; Trips is a destination with a creation form, an LLM review checklist, and delete actions. Users looking for trips won't think to look inside the Dashboard, and the pill's label grammar ("By month… Trips") signals the mismatch.
**Change:** Either promote Trips to the top-level tab row, or keep it but rename the switcher segments to noun-consistent labels ("Months / Categories / Years / Trips") and accept the tradeoff. Promotion is cleaner: Dashboard keeps three true views, and Trips stops resetting when you navigate away (see A6).
**Backend:** none.

### A5. The Review tab is a queue with a toolbox dumped on top — P2, effort M
**Problem:** Review's `review-tools` row holds AddCategory (input+button), ManageCategories (expanding chip cloud), TrainModel, and AskClaude, then SecondOpinions renders *its* results inline, and only then the actual queue. A user whose job is "categorize these 37 merchants" wades through model-management UI first, and result strings (`Trained on 412 labels…`) appear as tiny `review-meta` spans squeezed next to buttons, wrapping unpredictably.
**Change:**
- Lead with the queue: intro line, then ReviewRows.
- Group the automation into a bordered "Auto-categorize" card above or beside it: Train model, Ask Claude, Second opinions — with a shared status line beneath the buttons (one place for results, full width, not a squeezed span).
- Move AddCategory/ManageCategories into a "Categories" disclosure or to the Data tab — category CRUD is settings, not review.
- Second-opinions results should visually match ReviewRows (they nearly do) but appear under their own heading so "queue" and "disagreements" don't interleave.
**Backend:** none.

### A6. No URL state — every refresh and tab-switch loses your place — P2, effort M
**Problem:** Tab, dashboard view, selected month, selected category, and Transactions filters are all component state. Refresh mid-review → back to Dashboard/month view. Switch Dashboard→Transactions→Dashboard: App keeps Dashboard mounted only while tab==='dashboard', so it *unmounts* — selected month and view reset, and the overview refetches.
**Change:** Mirror `tab`, dashboard `view`, `selected` month, and the Transactions filter set into the URL hash (`#/dashboard/2026-06`, `#/transactions?cat=4&month=2026-06`) with a tiny hand-rolled hash router — no dependency needed. This also makes A3's drill-downs shareable/bookmarkable and gives you browser back-button flow (dashboard → month → transactions → back), which currently doesn't exist at all.
**Backend:** none.

## B. Layout, hierarchy, density

### B1. Month-selection response is spatially split — P1, effort S
**Problem:** On the default dashboard the order is: filters → KPI row → "Spending by month" → "Income vs spending" → month categories/merchants split → recurring. Clicking a June bar changes the KPIs (above where you clicked) and the detail cards (below two 240px charts). The eye has nowhere to land; most users will miss that the bottom half changed.
**Change:** Reorder to: KPI row → Spending by month → **Categories/Top merchants for the selected month** (the dash-split) → Income vs spending → Recurring. The stacked chart and its month detail then form one visual unit: pick a column, read its breakdown immediately below. Also add a stronger selected-column cue than the bold axis label — e.g. a `--accent` underline rect beneath the selected column or a slightly darker surface band behind it; the current `.axis-selected` bold-only cue is easy to miss.
**Backend:** none.

### B2. 900px shell is too tight for what the dashboard has become — P2, effort S
**Problem:** `.app { max-width: 900px }` was right for a transaction list; now the KPI row has five tiles (`minmax(180px,1fr)` → at ~868px content width you get 4+1, with a lone orphan tile on row two), the stacked chart squeezes 13 columns into ~860px, and the recurring table has 7 columns.
**Change:** Raise to `max-width: 1100px` (or `min(1100px, 100% - 2rem)`), and let the dash-split stay 2-up. If you keep 900, drop the KPI row to four tiles by folding "Subscriptions / month" into the Recurring card header (it duplicates that card's content anyway). Either way, prevent the orphan-tile row: `grid-template-columns: repeat(auto-fit, minmax(160px, 1fr))` with five tiles at 1100px gives a clean single row.
**Backend:** none.

### B3. Typography is a cloud of ad-hoc sizes — P2, effort S
**Problem:** The CSS uses at least 13 distinct font sizes between 0.68rem and 0.95rem. Adjacent components sit one imperceptible notch apart (tx-table 0.92 vs review-intro 0.95 vs account-card 0.9), which reads as slightly "off" without being diagnosable, and makes future work guesswork.
**Change:** Define a scale in `:root` and consume it everywhere: `--fs-xs: 0.72rem` (badges, axis, table-toggle), `--fs-sm: 0.8rem` (meta, mini-table, legends), `--fs-base: 0.9rem` (tables, forms, intros), `--fs-lg: 1rem` (card titles), `--fs-xl: 1.65rem` (stat values). Mapping each existing rule to the nearest step is a mechanical one-pass edit.
**Backend:** none.

### B4. The header spends a full `<h1>` on the app's name, on every view — P3, effort S
**Problem:** "Budget Tracker" at default h1 size (~2.1rem + margins) buys nothing for a single-user app and costs vertical space daily.
**Change:** Compact top bar: app name at ~1rem/600 weight on the left, tabs inline on the same row (this pairs naturally with A1 removing the dropzone). Total chrome above content drops to ~48px. Keep it an `<h1>` semantically, just styled small.
**Backend:** none.

## C. States: loading, empty, feedback

### C1. Transactions has no loading, empty, or "no matches" state — P1, effort S
**Problem:** The Dashboard got the three-state treatment (loading/empty/error) but Transactions didn't: before the first fetch resolves you see headers over an empty table; with a filter that matches nothing you see the identical empty table plus "Page 1 of 1 (0 transactions)" — no hint whether it's loading, broken, or genuinely empty; and fetch failures in `loadTxs` are unhandled (silent empty table).
**Change:** Track loading/error in App (or migrate loadTxs to the same pattern Dashboard uses). Render: a lightweight "Loading…" row; on zero results with active filters, a row saying "No transactions match — [clear filters]" with a one-click reset button; on zero results with no filters, "No transactions yet — import files on the Data tab"; on error, a visible failure line. The clear-filters affordance matters: six filter controls, and today the only recovery is resetting each by hand.
**Backend:** none.

### C2. TripsView renders literally nothing while loading — P1, effort S
**Problem:** In `TripsView`, `trips` starts `null` and the list only renders when `trips` is truthy. Between mount and fetch resolution the area below the form is blank — on a slow backend the tab looks broken. Same class of bug the prior audit fixed on Dashboard (H6), newly reintroduced in this newer view.
**Change:** `{trips === null && !error && <p className="review-intro">Loading trips…</p>}`. Two minutes.
**Backend:** none.

### C3. Status messages are unowned, undismissable, and immortal — P2, effort M
**Problem:** Three separate ad-hoc feedback channels: `imports` (paragraph list that persists until the *next* import, forever otherwise), `transferMsg` (a single string reused by three different actions — a Splitwise result overwrites a Monzo auth prompt, including the `monzoUrl` flow), and the per-button `result` spans in Review. None can be dismissed; none distinguish success from failure except a "⚠" prefix; a stale "3 transfers linked" from yesterday still sits there today.
**Change:** One `<StatusBanner>` component: icon + tinted border by kind (success `--money-in`-tinted, warning amber, error `--money-out`-tinted), dismiss ×, used by imports, syncs, and the Review tools. Give the Monzo-auth prompt its own persistent slot so a subsequent action can't eat the authorize link mid-flow. Import results belong inside it as a collapsible list ("3 files imported — details").
**Backend:** none.

### C4. First-run experience is a bare dropzone — P3, effort S
**Problem:** With zero accounts the shell shows the dropzone, four tabs, and "No data yet — import some transactions first." There's no orientation for what the app will do or which formats/banks work (that knowledge is hidden in a hover tooltip on "choose files").
**Change:** When `accounts.length === 0`, replace the dashboard with a short welcome card: supported sources (CSV/XLSX/Barclays PDF, Monzo API, Splitwise), the three steps (import → review categories → read the dashboard), and one primary "Choose files" button. All copy already exists in tooltips; surface it.
**Backend:** none.

## D. Component consistency

### D1. No primary/secondary button hierarchy — P1, effort S
**Problem:** Every button is the same 1px-border ghost style. In the trip checklist, "✓ Save trip assignment", "↻ Re-ask Claude" (costs money), and "Cancel" are visually interchangeable. In AddTransaction, "Add" doesn't stand out from the field cluster. Users rely on reading, not recognition.
**Change:** Add `.btn-primary { background: var(--accent); color: #fff; border-color: var(--accent); }` and apply to exactly one action per form/dialog: Add, Save trip assignment, + New trip, choose files (first-run). Keep default ghost for secondary; keep `.delete-btn` for destructive. Consider a `.btn-quiet` (borderless, muted) for Cancel. This is the single cheapest "feels designed" win available.
**Backend:** none.

### D2. Finish migrating the shell to tokens — P1, effort S
**Problem:** Residue of audit H1: the tokens moved to `:root`, but App.css still hardcodes `#8883/#8884/#8885/#8886/#888a` borders (dropzone, cards, tables, tabs, chips, selects), `#d33` errors (three places), and `#d97706` stale-flag; Dashboard.css hardcodes the warning ambers (`#d9770666/#b45309/#fbbf24`). Result: card borders on the Transactions tab visibly differ from chart-card borders on the Dashboard in both themes, and error/warning colors can't be tuned per theme.
**Change:** Mechanical replace: `#8883/#8884` → `var(--gridline)`, `#8885/#8886/#888a` → `var(--baseline)`; add `--danger` (use for `.error`, `.delete-btn`) and `--warning`/`--warning-border` (dash-warning, stale-flag) to the `:root` block with dark variants. Zero layout risk.
**Backend:** none.

### D3. One card recipe — P2, effort S
**Problem:** Four "card" styles coexist: `.account-card` (radius 8, `#8884`), `.stat-tile` (10, tokens, surface fill), `.chart-card` (10, tokens, surface fill), `.add-tx` (10, `#8884`, no fill). On the Transactions tab, cards have no background fill; on the Dashboard they do — the tabs feel like different apps at the surface level.
**Change:** A `.card` base class (surface-1 fill, `--gridline` border, one radius, one padding rhythm) composed by all four. Account cards and add-tx just inherit it.
**Backend:** none.

### D4. Emoji as icon system — P3, effort S–M
**Problem:** ⇄ ⚖ ⚡ 🧠 ✨ 🔎 🗂 ↻ ✓ × mix monochrome glyphs with full-color emoji; rendering varies by platform/font, colored emoji fight the otherwise restrained palette, and at button sizes 🗂/⚖ are mud.
**Change:** Either commit to text-only buttons (fine at this density), or inline a ~10-glyph monochrome SVG set (`currentColor`) for sync/train/sparkle/search/etc. Keep × and ⇄ text if you like — they're typographically safe.
**Backend:** none.

### D5. Small table inconsistencies — P3, effort S
- `.tx-table` rows have no hover highlight, but CategoryView bar rows do; add `tr:hover { background: color-mix(in srgb, var(--gridline) 40%, transparent) }` to both table families for scanability.
- With 50-row pages, the tx-table header scrolls away: `thead th { position: sticky; top: 0; background: var(--surface-1); }` (works since the scroll container is the page; give it a z-index).
- The month BarList shows no share-of-total while the Year view does (`.bar-share`). Add the % to the month card too — it's a client-side division and makes the two views consistent.
- Recurring table: sort active-first (currently API order), and consider folding lapsed rows behind a "show 4 lapsed" toggle; a long half-transparent tail dilutes the card. The "Status" text column duplicates the row opacity — replace with a small dot + `active`/`lapsed` in the merchant cell to save a column at 900px.

## E. Forms

### E1. Placeholder-only fields, unlabeled dates — P1, effort S
**Problem:** AddTransaction: the date input has no label or placeholder at all; description/amount rely on placeholders that vanish on input; the amount field gives no currency cue even though the chosen account fixes the currency. Trip form: "Trip name" placeholder, then *two identical bare date inputs* — which is start? The answer lives in a hover tooltip, invisible on touch and to anyone who doesn't hover.
**Change:** Visible mini-labels (0.72rem, `--text-secondary`, stacked above each field): Date, Description, Type, Amount (with a prefix span showing the selected account's currency symbol), Account, Category. Trip form: "From" / "To" labels, or a single labeled pair "Dates: [start] → [end]". Also give AddTransaction autofocus on Description when opened.
**Backend:** none.

### E2. Trip checklist ergonomics — P2, effort S
**Problem:** The suggestion checklist can be long (± booking window), every row is manual, and unchecked rows use the same `.lapsed` half-opacity as lapsed subscriptions — dimming rows the user may still need to read to decide. There's also no count of what's in-window vs outside, and no select-all.
**Change:** Keep full opacity on unchecked rows (use the checkbox as the state, maybe a subtle strikethrough on excluded); add "select all / none / in-window only" links above the table; show a running total of the checked amount next to "(n selected)" — the number the user is actually curating toward. Sort in-window first, outside-window after a divider row labeled "possible bookings / late charges".
**Backend:** none.

### E3. Native `confirm()` dialogs — P3, effort M
Delete transaction, delete category (with a two-step force flow), delete trip all use `window.confirm`, which blocks, looks foreign in both themes, and truncates the carefully written category-deletion warning awkwardly. Fine to keep for now; if touched, a small in-app `<dialog>` styled on the card recipe covers all three. Note the category force-delete flow is genuinely good UX logic — it just deserves better dress.

## F. Charts

### F1. The "Avg" column masquerades as a 13th month — P2, effort S
**Problem:** `chartMonths = [...months, avgMonth]` appends the average as one more column, same fill, same width, labeled "Avg". At a glance it reads as next month, and it inflates the visual total of the year (13 columns of ink for 12 months of data). Hover/tooltip treat it identically.
**Change:** Separate it visually: a gap of one band width before it, and/or render its segments at reduced opacity or with a hatched/outlined treatment, and a divider gridline. Same for IncomeSpending. The `monthLabel` already special-cases it; the geometry should too.
**Backend:** none.

### F2. Legend order vs stack order vs tooltip order — P3, effort S
**Problem:** The stack builds bottom-up (slot 0 at the bottom), the legend reads left-to-right in slot order, and the tooltip lists top-down (reversed). So the legend's first item is the *bottom* segment while the tooltip's first row is the *top* segment.
**Change:** Order the legend to match the tooltip (top segment first) — cheapest is reversing the legend arrays so legend and tooltip agree with the visual top of the bar.
**Backend:** none.

### F3. Income vs spending chart could carry "net" visually — P3, effort S–M
The net figure only exists in the tooltip and table. A small marker per month (dot or dash at `y(net)` when positive, below baseline styling when negative) would make the "did I come out ahead" question answerable at a glance. Keep the paired bars; just add the marker + a legend entry using `--accent`.
**Backend:** none.

### F4. Chart-tables need a sticky first column — P3, effort S
In **table** mode the chart-card collapses the legend, leaving the toggle pill as the only header context for stacked columns with 10 columns of numbers at 0.8rem inside a `display:block` scroller. Add a sticky first column (`Month`) like Coverage's `.coverage-account` sticky treatment so wide chart-tables stay readable while scrolled.
**Backend:** none.

## G. Narrow-viewport notes (secondary for desktop-first, all S)

- `.bar-row { grid-template-columns: 8.5rem 1fr 4.5rem }` — below ~420px the track is slivers; add a ~560px media query dropping the name column to `6rem` and font to `--fs-xs`. Same for `.year-row`'s 4-column variant.
- The `.review-row` flexbox will squeeze the select at narrow widths since `.review-actions` is `flex-shrink: 0`; allow wrap so actions drop below the merchant on phones.
- The recurring table (7 columns) has no scroll wrapper unlike tx-table/coverage — wrap it in `.tx-scroll` or equivalent.
- KPI tiles at `minmax(180px,1fr)` go single-column on phones — fine, but the 1.65rem values could step down via `clamp()`.

---

## Backend-change flags (everything else above is frontend-only)
- **A3 merchant drill-down:** add a `merchant=` filter to `GET /api/transactions`.
- **A3 year drill-down (optional):** a `year=` filter (or month-range) on the same endpoint.

## Suggested sequencing
1. **Week-one polish (all S):** D1 primary buttons, D2 token migration, E1 form labels, C1/C2 missing states, B1 dashboard reorder, F1 Avg column treatment.
2. **Structural (M):** A1/A2 Data tab + global drop overlay, A3 drill-downs (frontend part), C3 status banner, A5 Review regrouping.
3. **Foundational (M):** A6 hash routing (unlocks back-button flow and shareable drill-downs), B2 width bump, B3 type scale, A4 Trips promotion.

The overall verdict: the visualization layer is genuinely strong post-audit (tokens, semantic money colors, table fallbacks, three-state dashboard). The remaining gap is architectural, not cosmetic — the shell still reflects the app's origin as "an importer with a table," while the product has become "a dashboard with workflows." Items A1, A3, and B1 close most of that gap.
