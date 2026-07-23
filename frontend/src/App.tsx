import { useCallback, useEffect, useRef, useState } from 'react'
import { api } from './api'
import Coverage from './Coverage'
import Dashboard from './Dashboard'
import './App.css'

// Hover explanations for the badge showing who categorized a transaction.
const SOURCE_TIPS: Record<string, string> = {
  rule: 'Categorized automatically by one of your rules',
  model: 'Categorized by the local ML model (≥75% confident)',
  llm: 'Categorized by Claude based on the merchant name',
  splitwise: 'Created from Splitwise as a shared-expense correction',
}

type Account = {
  id: number
  name: string
  provider: string
  kind: string
  currency: string
  transaction_count: number
  latest_transaction: string | null
  last_imported: string | null
}

// One card per data source: a provider's currency accounts (e.g. the five
// Revolut ones) are one source, imported from the same exports.
function accountGroups(accounts: Account[]) {
  const byProvider = new Map<string, Account[]>()
  for (const a of accounts) {
    byProvider.set(a.provider, [...(byProvider.get(a.provider) ?? []), a])
  }
  return [...byProvider.entries()].map(([provider, accs]) => {
    const currencies = [...new Set(accs.map((a) => a.currency))]
    const latest = accs.map((a) => a.latest_transaction).filter(Boolean).sort().at(-1) ?? null
    const imported = accs.map((a) => a.last_imported).filter(Boolean).sort().at(-1) ?? null
    return {
      key: provider,
      name:
        accs.length === 1 ? accs[0].name : provider.charAt(0).toUpperCase() + provider.slice(1),
      filter: accs.length === 1 ? String(accs[0].id) : `p:${provider}`,
      count: accs.reduce((s, a) => s + a.transaction_count, 0),
      currencyLabel: currencies.length === 1 ? currencies[0] : `${currencies.length} currencies`,
      latest,
      imported,
      stale:
        imported !== null && (Date.now() - new Date(imported).getTime()) / 86_400_000 > 30,
    }
  })
}

type Category = { id: number; name: string; parent_id: number | null }

type Tx = {
  id: number
  account_id: number
  date: string
  description: string
  merchant: string | null
  amount: number
  category_id: number | null
  category_source: string | null
  transfer_peer_id: number | null
  manual: boolean
}

type ReviewGroup = {
  merchant: string
  count: number
  sample_description: string
  latest: string
}

type ImportResult = {
  filename: string
  source: string
  new: number
  duplicates: number
  date_min: string | null
  date_max: string | null
  error?: string
}

const PAGE_SIZE = 50

const formatters = new Map<string, Intl.NumberFormat>()

function money(amount: number, currency: string): string {
  let fmt = formatters.get(currency)
  if (!fmt) {
    try {
      fmt = new Intl.NumberFormat('en-GB', { style: 'currency', currency })
    } catch {
      // Not a valid ISO-4217 code — plain number beats a crash.
      fmt = new Intl.NumberFormat('en-GB', { maximumFractionDigits: 2 })
    }
    formatters.set(currency, fmt)
  }
  return fmt.format(amount)
}

function daysAgo(iso: string | null): string {
  if (!iso) return 'never'
  const days = Math.floor((Date.now() - new Date(iso).getTime()) / 86_400_000)
  if (days <= 0) return 'today'
  if (days === 1) return 'yesterday'
  return `${days} days ago`
}

function CategorySelect({
  categories,
  value,
  onChange,
  placeholder = 'Pick category…',
}: {
  categories: Category[]
  value: number | ''
  onChange: (id: number | null) => void
  placeholder?: string
}) {
  return (
    <select
      value={value}
      onChange={(e) => onChange(e.target.value === '' ? null : Number(e.target.value))}
    >
      <option value="">{placeholder}</option>
      {categories.map((c) => (
        <option key={c.id} value={c.id}>
          {c.name}
        </option>
      ))}
    </select>
  )
}

export default function App() {
  const [tab, setTab] = useState<'dashboard' | 'transactions' | 'review' | 'data'>('dashboard')
  const [accounts, setAccounts] = useState<Account[]>([])
  const [categories, setCategories] = useState<Category[]>([])
  const [txs, setTxs] = useState<Tx[]>([])
  const [total, setTotal] = useState(0)
  const [page, setPage] = useState(0)
  const [search, setSearch] = useState('')
  // '' = all, a numeric string = one account, 'p:<provider>' = all of a
  // provider's accounts (e.g. every Revolut currency at once).
  const [accountFilter, setAccountFilter] = useState<string>('')
  const [categoryFilter, setCategoryFilter] = useState<number | ''>('')
  const [monthFilter, setMonthFilter] = useState('')
  // Set only by dashboard drill-downs (there is no merchant dropdown);
  // rendered as a removable chip in the filter row.
  const [merchantFilter, setMerchantFilter] = useState('')
  const [months, setMonths] = useState<string[]>([])
  const [sortOrder, setSortOrder] = useState('date_desc')
  const [onlyUncategorized, setOnlyUncategorized] = useState(false)
  const [review, setReview] = useState<ReviewGroup[]>([])
  const [reviewTotal, setReviewTotal] = useState(0)
  const [imports, setImports] = useState<ImportResult[]>([])
  const [dragging, setDragging] = useState(false)
  const [transferMsg, setTransferMsg] = useState('')
  const [monzoUrl, setMonzoUrl] = useState('')
  const [showAdd, setShowAdd] = useState(false)
  const fileInput = useRef<HTMLInputElement>(null)

  async function detectTransfers() {
    const res = await (await api('/api/transfers/detect', { method: 'POST' })).json()
    setTransferMsg(`${res.pairs} new transfer pair${res.pairs === 1 ? '' : 's'} linked`)
    await Promise.all([loadTxs(), loadReview()])
  }

  async function syncMonzo() {
    setTransferMsg('Syncing Monzo…')
    const res = await api('/api/monzo/sync', { method: 'POST' })
    const data = await res.json()
    if (res.status === 409) {
      const conn = await api('/api/monzo/connect')
      const connData = await conn.json()
      if (!conn.ok) {
        setTransferMsg(`⚠ ${connData.detail ?? 'Monzo is not configured'}`)
        return
      }
      setMonzoUrl(connData.url)
      setTransferMsg(
        'Monzo needs authorizing — use the link below, approve in the Monzo app, then press Sync Monzo again (within 5 minutes for full history).',
      )
      return
    }
    setMonzoUrl('')
    if (!res.ok) {
      setTransferMsg(`⚠ ${data.detail ?? 'Monzo sync failed'}`)
      return
    }
    setTransferMsg(
      `Monzo: ${data.new} new transactions across ${data.accounts} account(s), ` +
        `${data.duplicates} duplicates, ${data.transfers} transfers linked` +
        (data.window_limited ? ' — note: only the last 90 days were available (SCA window)' : ''),
    )
    await Promise.all([loadStatic(), loadTxs(), loadReview()])
  }

  async function syncSplitwise() {
    setTransferMsg('Syncing Splitwise…')
    const res = await api('/api/splitwise/sync', { method: 'POST' })
    const data = await res.json()
    if (!res.ok) {
      setTransferMsg(`⚠ ${data.detail ?? 'Splitwise sync failed'}`)
      return
    }
    setTransferMsg(
      `Splitwise: ${data.corrections} corrections imported (${data.uncategorized} to review), ` +
        `${data.settlements_linked} settle-ups linked, ${data.settlements_pending} awaiting bank data` +
        (data.unknown_currency > 0
          ? ` — ⚠ ${data.unknown_currency} items in ${(data.unknown_currencies ?? []).join(', ')} ` +
            'skipped (no exchange rate); they will import once a rate is added'
          : ''),
    )
    await Promise.all([loadStatic(), loadTxs(), loadReview()])
  }

  const loadStatic = useCallback(async () => {
    const [acc, cats, cov] = await Promise.all([
      api('/api/accounts').then((r) => r.json()),
      api('/api/categories').then((r) => r.json()),
      api('/api/stats/coverage').then((r) => r.json()),
    ])
    setAccounts(acc)
    setCategories(cats)
    setMonths(cov.months ?? [])
  }, [])

  const loadTxs = useCallback(async () => {
    const params = new URLSearchParams({
      limit: String(PAGE_SIZE),
      offset: String(page * PAGE_SIZE),
    })
    if (search) params.set('search', search)
    if (accountFilter.startsWith('p:')) params.set('provider', accountFilter.slice(2))
    else if (accountFilter !== '') params.set('account_id', accountFilter)
    if (monthFilter) params.set('month', monthFilter)
    if (merchantFilter) params.set('merchant', merchantFilter)
    if (sortOrder !== 'date_desc') params.set('order', sortOrder)
    if (categoryFilter !== '') params.set('category_id', String(categoryFilter))
    if (onlyUncategorized) params.set('uncategorized', 'true')
    const data = await (await api(`/api/transactions?${params}`)).json()
    setTxs(data.items)
    setTotal(data.total)
  }, [page, search, accountFilter, categoryFilter, monthFilter, merchantFilter, sortOrder, onlyUncategorized])

  const loadReview = useCallback(async () => {
    const data = await (await api('/api/review')).json()
    setReview(data.groups)
    setReviewTotal(data.total_uncategorized)
  }, [])

  useEffect(() => {
    loadStatic()
    loadReview()
  }, [loadStatic, loadReview])

  useEffect(() => {
    loadTxs()
  }, [loadTxs])

  const uploadFiles = useCallback(
    async (files: FileList | File[]) => {
      const results: ImportResult[] = []
      for (const file of Array.from(files)) {
        const body = new FormData()
        body.append('file', file)
        const res = await api('/api/imports', { method: 'POST', body })
        if (res.ok) {
          results.push(await res.json())
        } else {
          const detail = (await res.json()).detail ?? res.statusText
          results.push({
            filename: file.name,
            source: '?',
            new: 0,
            duplicates: 0,
            date_min: null,
            date_max: null,
            error: detail,
          })
        }
      }
      setImports(results)
      setPage(0)
      await Promise.all([loadStatic(), loadTxs(), loadReview()])
    },
    [loadStatic, loadTxs, loadReview],
  )

  // Files can be dropped anywhere, on any tab: a window-level listener shows
  // a full-viewport overlay and routes the drop to the importer (the drop
  // itself is handled here too — the overlay is purely visual).
  useEffect(() => {
    let depth = 0
    const hasFiles = (e: DragEvent) => e.dataTransfer?.types.includes('Files') ?? false
    const enter = (e: DragEvent) => {
      if (!hasFiles(e)) return
      depth += 1
      setDragging(true)
    }
    const leave = (e: DragEvent) => {
      if (!hasFiles(e)) return
      depth = Math.max(0, depth - 1)
      if (depth === 0) setDragging(false)
    }
    const over = (e: DragEvent) => {
      if (hasFiles(e)) e.preventDefault()
    }
    const drop = (e: DragEvent) => {
      depth = 0
      setDragging(false)
      if (!hasFiles(e)) return
      e.preventDefault()
      if (e.dataTransfer?.files.length) {
        setTab('data') // land where the import results appear
        uploadFiles(e.dataTransfer.files)
      }
    }
    window.addEventListener('dragenter', enter)
    window.addEventListener('dragleave', leave)
    window.addEventListener('dragover', over)
    window.addEventListener('drop', drop)
    return () => {
      window.removeEventListener('dragenter', enter)
      window.removeEventListener('dragleave', leave)
      window.removeEventListener('dragover', over)
      window.removeEventListener('drop', drop)
    }
  }, [uploadFiles])

  // Dashboard → Transactions drill-down: land on the transactions tab with
  // exactly the filters that reproduce the number that was clicked.
  function drill(f: { categoryId?: number; month?: string; merchant?: string }) {
    setSearch('')
    setAccountFilter('')
    setOnlyUncategorized(f.categoryId === 0)
    setCategoryFilter(f.categoryId !== undefined && f.categoryId !== 0 ? f.categoryId : '')
    setMonthFilter(f.month ?? '')
    setMerchantFilter(f.merchant ?? '')
    setSortOrder('date_desc')
    setPage(0)
    setTab('transactions')
  }

  async function categorizeTx(tx: Tx, categoryId: number | null) {
    await api(`/api/transactions/${tx.id}`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ category_id: categoryId }),
    })
    await Promise.all([loadTxs(), loadReview()])
  }

  async function assignGroup(group: ReviewGroup, categoryId: number | null, createRule: boolean) {
    if (categoryId === null) return
    await api('/api/review/assign', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        merchant: group.merchant,
        category_id: categoryId,
        create_rule: createRule,
      }),
    })
    await Promise.all([loadTxs(), loadReview()])
  }

  const pageCount = Math.max(1, Math.ceil(total / PAGE_SIZE))
  const anyStale = accountGroups(accounts).some((g) => g.stale)

  return (
    <main className="app">
      <h1>Budget Tracker</h1>

      {dragging && (
        <div className="drop-overlay" aria-hidden="true">
          <span>Drop to import</span>
        </div>
      )}

      <nav className="tabs">
        <button
          className={tab === 'dashboard' ? 'active' : ''}
          aria-current={tab === 'dashboard' ? 'page' : undefined}
          onClick={() => setTab('dashboard')}
          data-tip="Monthly spending charts and category breakdowns"
        >
          Dashboard
        </button>
        <button
          className={tab === 'transactions' ? 'active' : ''}
          aria-current={tab === 'transactions' ? 'page' : undefined}
          onClick={() => setTab('transactions')}
          data-tip="Browse, search, and filter every transaction"
        >
          Transactions
        </button>
        <button
          className={tab === 'review' ? 'active' : ''}
          aria-current={tab === 'review' ? 'page' : undefined}
          onClick={() => setTab('review')}
          data-tip="Categorize what's still uncategorized, grouped by merchant"
        >
          Review{reviewTotal > 0 ? ` (${reviewTotal})` : ''}
        </button>
        <button
          className={tab === 'data' ? 'active' : ''}
          aria-current={tab === 'data' ? 'page' : undefined}
          onClick={() => setTab('data')}
          data-tip="Import files, sync accounts, and check which months have data — files can also be dropped anywhere in the app"
        >
          Data
          {anyStale && (
            <span className="stale-flag" data-tip="An account hasn't been fed data in over 30 days">
              {' '}
              ⚠<span className="sr-only"> — an account hasn't been fed data in over 30 days</span>
            </span>
          )}
        </button>
      </nav>

      {tab === 'dashboard' && <Dashboard onDrill={drill} />}

      {tab === 'data' && (
        <>
          <section className="dropzone">
            <p>Drop bank exports anywhere in the app (CSV, Excel, or Barclays PDF statements), or</p>
            <button
              className="filepick"
              data-tip="Pick bank export files to import — the format and bank are detected automatically, and re-importing the same file is safe (duplicates are skipped)"
              onClick={() => fileInput.current?.click()}
            >
              choose files
            </button>
            <input
              ref={fileInput}
              type="file"
              accept=".csv,.xlsx,.pdf"
              multiple
              hidden
              onChange={(e) => e.target.files && uploadFiles(e.target.files)}
            />
          </section>

          {imports.length > 0 && (
            <section className="import-results">
              {imports.map((r, i) => (
                <p key={i} className={r.error ? 'error' : ''}>
                  {r.error
                    ? `${r.filename}: ${r.error}`
                    : `${r.filename} → ${r.source}: ${r.new} new, ${r.duplicates} duplicates ` +
                      `(${r.date_min} to ${r.date_max})`}
                </p>
              ))}
            </section>
          )}

          <section className="data-actions">
            <button
              onClick={detectTransfers}
              data-tip="Find matching money movements between your own accounts (card payments, top-ups) and link them so they don't count as spending or income"
            >
              ⇄ Detect transfers
            </button>
            <button
              onClick={syncSplitwise}
              data-tip="Pull your Splitwise balances and apply corrections so shared expenses only count your share"
            >
              ⚖ Sync Splitwise
            </button>
            <button
              onClick={syncMonzo}
              data-tip="Pull recent transactions straight from Monzo via its API (asks you to re-authorize when the connection has expired)"
            >
              ⚡ Sync Monzo
            </button>
          </section>
          {transferMsg && (
            <p className="review-intro">
              {transferMsg}
              {monzoUrl && (
                <>
                  {' '}
                  <a href={monzoUrl} target="_blank" rel="noreferrer">
                    → Authorize with Monzo
                  </a>
                </>
              )}
            </p>
          )}

          {accounts.length > 0 && (
            <section className="accounts">
              {accountGroups(accounts).map((g) => (
                <div
                  key={g.key}
                  className="account-card clickable"
                  data-tip={`Show all ${g.name} transactions`}
                  role="button"
                  tabIndex={0}
                  onClick={() => {
                    setAccountFilter(g.filter)
                    setPage(0)
                    setTab('transactions')
                  }}
                  onKeyDown={(e) => {
                    if (e.key === 'Enter' || e.key === ' ') {
                      e.preventDefault()
                      setAccountFilter(g.filter)
                      setPage(0)
                      setTab('transactions')
                    }
                  }}
                >
                  <strong>
                    {g.name}
                    {g.stale && <span className="stale-flag"> ⚠ stale</span>}
                  </strong>
                  <span>
                    {g.count} transactions · {g.currencyLabel}
                  </span>
                  <span className="coverage">
                    activity {daysAgo(g.latest)}
                    {g.imported ? ` · imported ${daysAgo(g.imported)}` : ' · manual entries'}
                  </span>
                </div>
              ))}
            </section>
          )}

          <Coverage />

          <section className="data-export">
            <span className="review-meta">Export your data — you're never locked in:</span>
            <a className="export-link" href="/api/export/transactions.csv" download>
              ⬇ Transactions (CSV)
            </a>
            <a className="export-link" href="/api/export/all.json" download>
              ⬇ Full backup (JSON)
            </a>
          </section>

          <RatesEditor />
        </>
      )}

      {tab === 'transactions' && (
        <>
          <section className="filters">
            <input
              placeholder="Search description…"
              value={search}
              onChange={(e) => {
                setSearch(e.target.value)
                setPage(0)
              }}
            />
            <select
              value={accountFilter}
              onChange={(e) => {
                setAccountFilter(e.target.value)
                setPage(0)
              }}
            >
              <option value="">All accounts</option>
              {accountGroups(accounts)
                .filter((g) => g.filter.startsWith('p:'))
                .map((g) => (
                  <option key={g.filter} value={g.filter}>
                    {g.name} (all currencies)
                  </option>
                ))}
              {accounts.map((a) => (
                <option key={a.id} value={String(a.id)}>
                  {a.name}
                </option>
              ))}
            </select>
            <CategorySelect
              categories={categories}
              value={categoryFilter}
              onChange={(id) => {
                setCategoryFilter(id ?? '')
                setPage(0)
              }}
              placeholder="All categories"
            />
            <select
              value={monthFilter}
              onChange={(e) => {
                setMonthFilter(e.target.value)
                setPage(0)
              }}
            >
              <option value="">All months</option>
              {[...months].reverse().map((m) => (
                <option key={m} value={m}>
                  {new Date(m + '-01').toLocaleString('en-GB', { month: 'long', year: 'numeric' })}
                </option>
              ))}
            </select>
            <select
              value={sortOrder}
              onChange={(e) => {
                setSortOrder(e.target.value)
                setPage(0)
              }}
              data-tip="Order the list by date or by size (largest = biggest amount, in or out)"
            >
              <option value="date_desc">↓ Newest first</option>
              <option value="date_asc">↑ Oldest first</option>
              <option value="amount_desc">↓ Largest first</option>
              <option value="amount_asc">↑ Smallest first</option>
            </select>
            <label className="checkbox">
              <input
                type="checkbox"
                checked={onlyUncategorized}
                onChange={(e) => {
                  setOnlyUncategorized(e.target.checked)
                  setPage(0)
                }}
              />
              uncategorized
            </label>
            {merchantFilter && (
              <span className="filter-chip">
                {merchantFilter}
                <button
                  className="delete-btn"
                  data-tip="Stop filtering by this merchant"
                  onClick={() => {
                    setMerchantFilter('')
                    setPage(0)
                  }}
                >
                  ×<span className="sr-only"> clear merchant filter</span>
                </button>
              </span>
            )}
            <button
              className={`add-toggle ${showAdd ? '' : 'btn-primary'}`}
              onClick={() => setShowAdd(!showAdd)}
              data-tip="Enter a transaction by hand — e.g. cash spending no bank export will ever contain"
            >
              {showAdd ? '× Close' : '+ Add transaction'}
            </button>
          </section>
          {showAdd && (
            <AddTransaction
              accounts={accounts}
              categories={categories}
              onAdded={async () => {
                setShowAdd(false)
                await Promise.all([loadStatic(), loadTxs(), loadReview()])
              }}
            />
          )}

          <div className="tx-scroll">
          <table className="tx-table">
            <thead>
              <tr>
                <th>Date</th>
                <th>Description</th>
                <th>Category</th>
                <th className="num">Amount</th>
                <th></th>
              </tr>
            </thead>
            <tbody>
              {txs.map((t) => (
                <tr key={t.id}>
                  <td>{t.date}</td>
                  <td>
                    {t.description}
                    {t.transfer_peer_id !== null && (
                      <span
                        className="source-badge"
                        data-tip="Linked to its counterpart in another of your accounts — excluded from spending and income"
                      >
                        ⇄ transfer
                        <span className="sr-only">
                          — linked to its counterpart in another of your accounts, excluded from
                          spending and income
                        </span>
                      </span>
                    )}
                  </td>
                  <td>
                    <span className="cat-cell">
                      <CategorySelect
                        categories={categories}
                        value={t.category_id ?? ''}
                        onChange={(id) => categorizeTx(t, id)}
                        placeholder="—"
                      />
                      {t.category_source && t.category_source !== 'human' && (
                        <span
                          className="source-badge"
                          data-tip={SOURCE_TIPS[t.category_source] ?? 'Categorized automatically'}
                        >
                          {t.category_source}
                          <span className="sr-only">
                            — {SOURCE_TIPS[t.category_source] ?? 'categorized automatically'}
                          </span>
                        </span>
                      )}
                    </span>
                  </td>
                  <td className={`num ${t.amount < 0 ? 'out' : 'in'}`}>
                    {money(t.amount, accounts.find((a) => a.id === t.account_id)?.currency ?? 'GBP')}
                  </td>
                  <td className="row-actions">
                    {t.manual && (
                      <button
                        className="delete-btn"
                        data-tip="Delete this manually entered transaction"
                        onClick={async () => {
                          const cur = accounts.find((a) => a.id === t.account_id)?.currency ?? 'GBP'
                          if (!confirm(`Delete "${t.description}" (${money(t.amount, cur)})?`)) return
                          await api(`/api/transactions/${t.id}`, { method: 'DELETE' })
                          await Promise.all([loadStatic(), loadTxs(), loadReview()])
                        }}
                      >
                        ×
                      </button>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          </div>

          <footer className="pager">
            <button disabled={page === 0} onClick={() => setPage(page - 1)} data-tip="Previous page">
              ← Prev
            </button>
            <span>
              Page {page + 1} of {pageCount} ({total} transactions)
            </span>
            <button
              disabled={page + 1 >= pageCount}
              onClick={() => setPage(page + 1)}
              data-tip="Next page"
            >
              Next →
            </button>
          </footer>
        </>
      )}

      {tab === 'review' && (
        <section className="review">
          <p className="review-intro">
            {reviewTotal === 0
              ? 'Nothing to review — everything is categorized. 🎉'
              : `${reviewTotal} uncategorized transactions, grouped by merchant. ` +
                'Assigning also creates a rule so future imports categorize themselves.'}
          </p>
          <div className="review-tools">
            <AddCategory onAdded={loadStatic} />
            <ManageCategories
              categories={categories}
              onChanged={() => Promise.all([loadStatic(), loadTxs(), loadReview()])}
            />
            <TrainModel onDone={() => Promise.all([loadTxs(), loadReview()])} />
            <AskClaude onDone={() => Promise.all([loadTxs(), loadReview()])} />
          </div>
          <SecondOpinions
            categories={categories}
            onChanged={() => Promise.all([loadTxs(), loadReview()])}
          />
          {review.map((g) => (
            <ReviewRow
              key={g.merchant}
              group={g}
              categories={categories}
              onAssign={assignGroup}
            />
          ))}
        </section>
      )}
    </main>
  )
}

function AddTransaction({
  accounts,
  categories,
  onAdded,
}: {
  accounts: Account[]
  categories: Category[]
  onAdded: () => void
}) {
  const today = new Date().toISOString().slice(0, 10)
  const [form, setForm] = useState({
    date: today,
    description: '',
    amount: '',
    kind: 'expense' as 'expense' | 'income',
    accountId: 0, // 0 = Cash (manual)
    categoryId: '' as number | '',
  })
  const [error, setError] = useState('')

  async function submit() {
    const value = Number(form.amount)
    if (!form.description.trim() || !value || value <= 0) {
      setError('Enter a description and a positive amount.')
      return
    }
    const res = await api('/api/transactions', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        account_id: form.accountId,
        date: form.date,
        description: form.description.trim(),
        amount: form.kind === 'expense' ? -value : value,
        category_id: form.categoryId === '' ? null : form.categoryId,
      }),
    })
    if (!res.ok) {
      setError((await res.json()).detail ?? 'Failed to add transaction')
      return
    }
    onAdded()
  }

  const currency = accounts.find((a) => a.id === form.accountId)?.currency ?? 'GBP'

  return (
    <div className="add-tx">
      <label className="field">
        <span className="field-label">Date</span>
        <input
          type="date"
          value={form.date}
          max={today}
          onChange={(e) => setForm({ ...form, date: e.target.value })}
        />
      </label>
      <label className="field add-tx-desc">
        <span className="field-label">Description</span>
        <input
          autoFocus
          placeholder="e.g. Farmers market"
          value={form.description}
          onChange={(e) => setForm({ ...form, description: e.target.value })}
          onKeyDown={(e) => e.key === 'Enter' && submit()}
        />
      </label>
      <label className="field">
        <span className="field-label">Type</span>
        <select
          value={form.kind}
          onChange={(e) => setForm({ ...form, kind: e.target.value as 'expense' | 'income' })}
        >
          <option value="expense">Expense</option>
          <option value="income">Income</option>
        </select>
      </label>
      <label className="field">
        <span className="field-label">Amount ({currency})</span>
        <input
          className="add-tx-amount"
          type="number"
          min="0.01"
          step="0.01"
          placeholder="0.00"
          value={form.amount}
          onChange={(e) => setForm({ ...form, amount: e.target.value })}
          onKeyDown={(e) => e.key === 'Enter' && submit()}
        />
      </label>
      <label className="field">
        <span className="field-label">Account</span>
        <select
          value={form.accountId}
          onChange={(e) => setForm({ ...form, accountId: Number(e.target.value) })}
        >
          <option value={0}>Cash (manual)</option>
          {accounts
            .filter((a) => a.provider !== 'splitwise')
            .map((a) => (
              <option key={a.id} value={a.id}>
                {a.name}
              </option>
            ))}
        </select>
      </label>
      <label className="field">
        <span className="field-label">Category</span>
        <CategorySelect
          categories={categories}
          value={form.categoryId}
          onChange={(id) => setForm({ ...form, categoryId: id ?? '' })}
        />
      </label>
      <button className="btn-primary" onClick={submit} data-tip="Save this transaction to the selected account">
        Add
      </button>
      {error && <span className="error">{error}</span>}
    </div>
  )
}

type RateData = {
  static: Record<string, number>
  overrides: { id: number; currency: string; month: string; rate: number }[]
}

// Manage per-month exchange-rate overrides. The static table converts every
// month by default; an override pins a specific month to a period-accurate
// rate so historical foreign-currency trends aren't distorted by today's.
function RatesEditor() {
  const [open, setOpen] = useState(false)
  const [data, setData] = useState<RateData | null>(null)
  const [form, setForm] = useState({ currency: '', month: '', rate: '' })

  const load = () =>
    api('/api/rates')
      .then((r) => r.json())
      .then(setData)
      .catch(() => {})
  useEffect(() => {
    if (open && !data) load()
  }, [open])

  async function save(currency: string, month: string, rate: number) {
    await api('/api/rates', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ currency, month, rate }),
    })
    load()
  }

  return (
    <section className="rates-editor">
      <button
        className="table-toggle"
        onClick={() => setOpen(!open)}
        data-tip="Pin a specific month's conversion rate for a currency, so old foreign-currency transactions convert at a period-accurate rate instead of today's"
      >
        {open ? '× Close exchange rates' : '💱 Exchange rate overrides'}
      </button>
      {open && data && (
        <div className="rates-body">
          <p className="review-meta">
            Without an override, every month uses the built-in rate table. Add one to correct a
            specific month.
          </p>
          <div className="rates-form">
            <input
              placeholder="Currency (e.g. JPY)"
              value={form.currency}
              maxLength={3}
              onChange={(e) => setForm({ ...form, currency: e.target.value.toUpperCase() })}
            />
            <input
              placeholder="Month (YYYY-MM)"
              value={form.month}
              onChange={(e) => setForm({ ...form, month: e.target.value })}
            />
            <input
              placeholder="GBP per unit (e.g. 0.006)"
              type="number"
              step="0.0001"
              value={form.rate}
              onChange={(e) => setForm({ ...form, rate: e.target.value })}
            />
            <button
              className="btn-primary"
              disabled={!/^[A-Z]{3}$/.test(form.currency) || !/^\d{4}-\d{2}$/.test(form.month) || !form.rate}
              onClick={async () => {
                await save(form.currency, form.month, Number(form.rate))
                setForm({ currency: '', month: '', rate: '' })
              }}
            >
              Add override
            </button>
          </div>
          {data.overrides.length > 0 && (
            <table className="mini-table">
              <tbody>
                {data.overrides.map((o) => (
                  <tr key={o.id}>
                    <td>{o.currency}</td>
                    <td className="muted">{o.month}</td>
                    <td className="num">{o.rate}</td>
                    <td className="row-actions">
                      <button
                        className="delete-btn"
                        data-tip="Remove this override (revert to the built-in rate)"
                        onClick={() => save(o.currency, o.month, 0)}
                      >
                        ×
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      )}
    </section>
  )
}

function AddCategory({ onAdded }: { onAdded: () => void }) {
  const [name, setName] = useState('')
  async function add() {
    const trimmed = name.trim()
    if (!trimmed) return
    await api('/api/categories', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name: trimmed }),
    })
    setName('')
    onAdded()
  }
  return (
    <div className="add-category">
      <input
        placeholder="New category…"
        value={name}
        onChange={(e) => setName(e.target.value)}
        onKeyDown={(e) => e.key === 'Enter' && add()}
      />
      <button
        onClick={add}
        disabled={!name.trim()}
        data-tip="Create a new category to use when categorizing transactions"
      >
        + Add category
      </button>
    </div>
  )
}

function TrainModel({ onDone }: { onDone: () => void }) {
  const [busy, setBusy] = useState(false)
  const [result, setResult] = useState('')
  async function trainNow() {
    setBusy(true)
    try {
      const res = await api('/api/model/train', { method: 'POST' })
      const data = await res.json()
      if (!res.ok) {
        setResult(data.detail ?? 'Training failed')
        return
      }
      const acc =
        data.holdout_accuracy !== null ? `${(data.holdout_accuracy * 100).toFixed(1)}% holdout accuracy, ` : ''
      setResult(
        `Trained on ${data.trained_on} labels (${data.classes} categories): ${acc}` +
          `${data.applied} auto-categorized, ${data.low_confidence} left for review`,
      )
      onDone()
    } finally {
      setBusy(false)
    }
  }
  return (
    <div className="train-model">
      <button
        onClick={trainNow}
        disabled={busy}
        data-tip="Train the local ML model on your own categorizations, then auto-fill any uncategorized transactions it is at least 75% sure about. Runs entirely on this machine — free and private."
      >
        {busy ? 'Training…' : '🧠 Train model & auto-categorize'}
      </button>
      {result && <span className="review-meta">{result}</span>}
    </div>
  )
}

function AskClaude({ onDone }: { onDone: () => void }) {
  const [busy, setBusy] = useState(false)
  const [result, setResult] = useState('')
  async function run() {
    setBusy(true)
    setResult('Asking Claude — this can take a few minutes…')
    try {
      const res = await api('/api/llm/categorize', { method: 'POST' })
      const data = await res.json()
      if (!res.ok) {
        setResult(data.detail ?? 'LLM categorization failed')
        return
      }
      setResult(
        `Claude reviewed ${data.asked} merchants: ${data.categorized} categorized ` +
          `(${data.transactions} transactions), ${data.unsure} left for you`,
      )
      onDone()
    } catch {
      setResult('LLM categorization failed — check the server log and try again')
    } finally {
      setBusy(false)
    }
  }
  return (
    <div className="train-model">
      <button
        onClick={run}
        disabled={busy}
        data-tip="Send still-uncategorized merchant names (never amounts, dates, or balances) to the Claude API and apply its suggestions. Each merchant is only ever asked once. Uses your API key — costs a little."
      >
        {busy ? 'Asking Claude…' : '✨ Ask Claude'}
      </button>
      {result && <span className="review-meta">{result}</span>}
    </div>
  )
}

type AuditGroup = {
  merchant: string
  current_category_id: number
  suggested_category_id: number
  source: string
  sample_description: string
  transaction_ids: number[]
  confidence: number
}

function SecondOpinions({
  categories,
  onChanged,
}: {
  categories: Category[]
  onChanged: () => void
}) {
  const [groups, setGroups] = useState<AuditGroup[] | null>(null)
  const [busy, setBusy] = useState(false)
  const catName = (id: number) => categories.find((c) => c.id === id)?.name ?? '?'

  async function scan() {
    setBusy(true)
    try {
      const res = await api('/api/model/audit')
      const data = await res.json()
      setGroups(res.ok ? data.groups : [])
    } finally {
      setBusy(false)
    }
  }

  async function resolve(group: AuditGroup, categoryId: number) {
    await api('/api/model/audit/resolve', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ transaction_ids: group.transaction_ids, category_id: categoryId }),
    })
    setGroups((gs) => (gs ?? []).filter((g) => g !== group))
    onChanged()
  }

  return (
    <div className="second-opinions">
      <button
        onClick={scan}
        disabled={busy}
        data-tip="Have the local model re-check everything that was categorized automatically (by rules, the model itself, or Claude) and flag rows it confidently disagrees with. Your own labels are never questioned."
      >
        {busy ? 'Checking…' : '🔎 Second opinions'}
      </button>
      {groups !== null && (
        <>
          <span className="review-meta">
            {groups.length === 0
              ? 'The model agrees with every automatic categorization.'
              : `${groups.length} merchants where the model disagrees — your call:`}
          </span>
          {groups.map((g) => (
            <div className="review-row audit-row" key={`${g.merchant}|${g.suggested_category_id}`}>
              <div className="review-merchant">
                <strong>{g.merchant}</strong>
                <span className="review-meta">
                  {g.transaction_ids.length}× · currently “{catName(g.current_category_id)}” (
                  {g.source}) · e.g. “{g.sample_description}”
                </span>
              </div>
              <div className="review-actions">
                <button
                  onClick={() => resolve(g, g.suggested_category_id)}
                  data-tip={`Recategorize these ${g.transaction_ids.length} as “${catName(g.suggested_category_id)}”`}
                >
                  → {catName(g.suggested_category_id)} ({Math.round(g.confidence * 100)}%)
                </button>
                <button
                  onClick={() => resolve(g, g.current_category_id)}
                  data-tip={`Keep “${catName(g.current_category_id)}” and never flag this again`}
                >
                  keep {catName(g.current_category_id)}
                </button>
              </div>
            </div>
          ))}
        </>
      )}
    </div>
  )
}

function ManageCategories({
  categories,
  onChanged,
}: {
  categories: Category[]
  onChanged: () => void
}) {
  const [open, setOpen] = useState(false)
  const [error, setError] = useState('')

  async function remove(c: Category) {
    setError('')
    let res = await api(`/api/categories/${c.id}`, { method: 'DELETE' })
    if (res.status === 409) {
      const data = await res.json()
      const usage = (data.detail ?? '').replace('Category is used by ', '')
      const warning =
        `“${c.name}” is still in use: ${usage}.\n\n` +
        'Delete anyway? Its transactions go back to the review queue, and its rules ' +
        'and cached Claude answers are removed.'
      if (!confirm(warning)) return
      res = await api(`/api/categories/${c.id}?force=true`, { method: 'DELETE' })
    }
    if (!res.ok) {
      const data = await res.json().catch(() => null)
      setError(data?.detail ?? 'Delete failed')
      return
    }
    onChanged()
  }

  return (
    <div className="manage-categories">
      <button
        onClick={() => setOpen(!open)}
        data-tip="Show all categories with the option to delete them"
      >
        {open ? '× Close' : '🗂 Manage categories'}
      </button>
      {open && (
        <span className="category-chips">
          {categories.map((c) => (
            <span className="category-chip" key={c.id}>
              {c.name}
              <button
                className="delete-btn"
                onClick={() => remove(c)}
                data-tip={`Delete “${c.name}”`}
              >
                ×
              </button>
            </span>
          ))}
        </span>
      )}
      {error && <span className="error">{error}</span>}
    </div>
  )
}

function ReviewRow({
  group,
  categories,
  onAssign,
}: {
  group: ReviewGroup
  categories: Category[]
  onAssign: (g: ReviewGroup, categoryId: number | null, createRule: boolean) => void
}) {
  const [createRule, setCreateRule] = useState(true)
  return (
    <div className="review-row">
      <div className="review-merchant">
        <strong>{group.merchant}</strong>
        <span className="review-meta">
          {group.count}× · last {group.latest} · e.g. “{group.sample_description}”
        </span>
      </div>
      <div className="review-actions">
        <CategorySelect
          categories={categories}
          value={''}
          onChange={(id) => onAssign(group, id, createRule)}
        />
        <label
          className="checkbox"
          data-tip="Also create a rule, so this merchant is categorized automatically in every future import"
        >
          <input
            type="checkbox"
            checked={createRule}
            onChange={(e) => setCreateRule(e.target.checked)}
          />
          always
        </label>
      </div>
    </div>
  )
}
