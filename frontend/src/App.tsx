import { useCallback, useEffect, useState } from 'react'
import Dashboard from './Dashboard'
import './App.css'

type Account = {
  id: number
  name: string
  provider: string
  kind: string
  currency: string
  transaction_count: number
  latest_transaction: string | null
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
    fmt = new Intl.NumberFormat('en-GB', { style: 'currency', currency })
    formatters.set(currency, fmt)
  }
  return fmt.format(amount)
}

function daysAgo(iso: string | null): string {
  if (!iso) return 'no data'
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
  const [tab, setTab] = useState<'dashboard' | 'transactions' | 'review'>('dashboard')
  const [accounts, setAccounts] = useState<Account[]>([])
  const [categories, setCategories] = useState<Category[]>([])
  const [txs, setTxs] = useState<Tx[]>([])
  const [total, setTotal] = useState(0)
  const [page, setPage] = useState(0)
  const [search, setSearch] = useState('')
  const [accountFilter, setAccountFilter] = useState<number | ''>('')
  const [categoryFilter, setCategoryFilter] = useState<number | ''>('')
  const [onlyUncategorized, setOnlyUncategorized] = useState(false)
  const [review, setReview] = useState<ReviewGroup[]>([])
  const [reviewTotal, setReviewTotal] = useState(0)
  const [imports, setImports] = useState<ImportResult[]>([])
  const [dragging, setDragging] = useState(false)
  const [transferMsg, setTransferMsg] = useState('')

  async function detectTransfers() {
    const res = await (await fetch('/api/transfers/detect', { method: 'POST' })).json()
    setTransferMsg(`${res.pairs} new transfer pair${res.pairs === 1 ? '' : 's'} linked`)
    await Promise.all([loadTxs(), loadReview()])
  }

  const loadStatic = useCallback(async () => {
    const [acc, cats] = await Promise.all([
      fetch('/api/accounts').then((r) => r.json()),
      fetch('/api/categories').then((r) => r.json()),
    ])
    setAccounts(acc)
    setCategories(cats)
  }, [])

  const loadTxs = useCallback(async () => {
    const params = new URLSearchParams({
      limit: String(PAGE_SIZE),
      offset: String(page * PAGE_SIZE),
    })
    if (search) params.set('search', search)
    if (accountFilter !== '') params.set('account_id', String(accountFilter))
    if (categoryFilter !== '') params.set('category_id', String(categoryFilter))
    if (onlyUncategorized) params.set('uncategorized', 'true')
    const data = await (await fetch(`/api/transactions?${params}`)).json()
    setTxs(data.items)
    setTotal(data.total)
  }, [page, search, accountFilter, categoryFilter, onlyUncategorized])

  const loadReview = useCallback(async () => {
    const data = await (await fetch('/api/review')).json()
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

  async function uploadFiles(files: FileList | File[]) {
    const results: ImportResult[] = []
    for (const file of Array.from(files)) {
      const body = new FormData()
      body.append('file', file)
      const res = await fetch('/api/imports', { method: 'POST', body })
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
  }

  async function categorizeTx(tx: Tx, categoryId: number | null) {
    await fetch(`/api/transactions/${tx.id}`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ category_id: categoryId }),
    })
    await Promise.all([loadTxs(), loadReview()])
  }

  async function assignGroup(group: ReviewGroup, categoryId: number | null, createRule: boolean) {
    if (categoryId === null) return
    await fetch('/api/review/assign', {
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

  return (
    <main className="app">
      <h1>Budget Tracker</h1>

      <section
        className={`dropzone ${dragging ? 'dragging' : ''}`}
        onDragOver={(e) => {
          e.preventDefault()
          setDragging(true)
        }}
        onDragLeave={() => setDragging(false)}
        onDrop={(e) => {
          e.preventDefault()
          setDragging(false)
          uploadFiles(e.dataTransfer.files)
        }}
      >
        <p>Drop bank exports here (CSV or Excel), or</p>
        <label className="filepick">
          choose files
          <input
            type="file"
            accept=".csv,.xlsx"
            multiple
            hidden
            onChange={(e) => e.target.files && uploadFiles(e.target.files)}
          />
        </label>
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

      {accounts.length > 0 && (
        <section className="accounts">
          {accounts.map((a) => (
            <div key={a.id} className="account-card">
              <strong>{a.name}</strong>
              <span>
                {a.transaction_count} transactions · {a.currency}
              </span>
              <span className="coverage">latest: {daysAgo(a.latest_transaction)}</span>
            </div>
          ))}
        </section>
      )}

      <nav className="tabs">
        <button className={tab === 'dashboard' ? 'active' : ''} onClick={() => setTab('dashboard')}>
          Dashboard
        </button>
        <button
          className={tab === 'transactions' ? 'active' : ''}
          onClick={() => setTab('transactions')}
        >
          Transactions
        </button>
        <button className={tab === 'review' ? 'active' : ''} onClick={() => setTab('review')}>
          Review{reviewTotal > 0 ? ` (${reviewTotal})` : ''}
        </button>
      </nav>

      {tab === 'dashboard' && <Dashboard />}

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
                setAccountFilter(e.target.value === '' ? '' : Number(e.target.value))
                setPage(0)
              }}
            >
              <option value="">All accounts</option>
              {accounts.map((a) => (
                <option key={a.id} value={a.id}>
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
            <button onClick={detectTransfers} title="Link transfers between your own accounts">
              ⇄ Detect transfers
            </button>
          </section>
          {transferMsg && <p className="review-intro">{transferMsg}</p>}

          <table className="tx-table">
            <thead>
              <tr>
                <th>Date</th>
                <th>Description</th>
                <th>Category</th>
                <th className="num">Amount</th>
              </tr>
            </thead>
            <tbody>
              {txs.map((t) => (
                <tr key={t.id}>
                  <td>{t.date}</td>
                  <td>
                    {t.description}
                    {t.transfer_peer_id !== null && (
                      <span className="source-badge" title="Linked transfer between your accounts">
                        ⇄ transfer
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
                        <span className="source-badge">{t.category_source}</span>
                      )}
                    </span>
                  </td>
                  <td className={`num ${t.amount < 0 ? 'out' : 'in'}`}>
                    {money(t.amount, accounts.find((a) => a.id === t.account_id)?.currency ?? 'GBP')}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>

          <footer className="pager">
            <button disabled={page === 0} onClick={() => setPage(page - 1)}>
              ← Prev
            </button>
            <span>
              Page {page + 1} of {pageCount} ({total} transactions)
            </span>
            <button disabled={page + 1 >= pageCount} onClick={() => setPage(page + 1)}>
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
            <TrainModel onDone={() => Promise.all([loadTxs(), loadReview()])} />
            <AskClaude onDone={() => Promise.all([loadTxs(), loadReview()])} />
          </div>
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

function AddCategory({ onAdded }: { onAdded: () => void }) {
  const [name, setName] = useState('')
  async function add() {
    const trimmed = name.trim()
    if (!trimmed) return
    await fetch('/api/categories', {
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
      <button onClick={add} disabled={!name.trim()}>
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
      const res = await fetch('/api/model/train', { method: 'POST' })
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
      <button onClick={trainNow} disabled={busy}>
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
      const res = await fetch('/api/llm/categorize', { method: 'POST' })
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
    } finally {
      setBusy(false)
    }
  }
  return (
    <div className="train-model">
      <button onClick={run} disabled={busy}>
        {busy ? 'Asking Claude…' : '✨ Ask Claude'}
      </button>
      {result && <span className="review-meta">{result}</span>}
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
        <label className="checkbox" title="Create a rule so future imports auto-categorize">
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
