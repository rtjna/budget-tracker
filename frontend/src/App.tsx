import { useCallback, useEffect, useState } from 'react'
import './App.css'

type Account = {
  id: number
  name: string
  provider: string
  kind: string
  transaction_count: number
  latest_transaction: string | null
}

type Tx = {
  id: number
  account_id: number
  date: string
  description: string
  amount: number
  category_id: number | null
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

const gbp = new Intl.NumberFormat('en-GB', { style: 'currency', currency: 'GBP' })

function daysAgo(iso: string | null): string {
  if (!iso) return 'no data'
  const days = Math.floor((Date.now() - new Date(iso).getTime()) / 86_400_000)
  if (days <= 0) return 'today'
  if (days === 1) return 'yesterday'
  return `${days} days ago`
}

export default function App() {
  const [accounts, setAccounts] = useState<Account[]>([])
  const [txs, setTxs] = useState<Tx[]>([])
  const [total, setTotal] = useState(0)
  const [page, setPage] = useState(0)
  const [search, setSearch] = useState('')
  const [accountFilter, setAccountFilter] = useState<number | ''>('')
  const [imports, setImports] = useState<ImportResult[]>([])
  const [dragging, setDragging] = useState(false)

  const loadAccounts = useCallback(async () => {
    setAccounts(await (await fetch('/api/accounts')).json())
  }, [])

  const loadTxs = useCallback(async () => {
    const params = new URLSearchParams({
      limit: String(PAGE_SIZE),
      offset: String(page * PAGE_SIZE),
    })
    if (search) params.set('search', search)
    if (accountFilter !== '') params.set('account_id', String(accountFilter))
    const data = await (await fetch(`/api/transactions?${params}`)).json()
    setTxs(data.items)
    setTotal(data.total)
  }, [page, search, accountFilter])

  useEffect(() => {
    loadAccounts()
  }, [loadAccounts])

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
    await Promise.all([loadAccounts(), loadTxs()])
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
              <span>{a.transaction_count} transactions</span>
              <span className="coverage">latest: {daysAgo(a.latest_transaction)}</span>
            </div>
          ))}
        </section>
      )}

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
      </section>

      <table className="tx-table">
        <thead>
          <tr>
            <th>Date</th>
            <th>Description</th>
            <th className="num">Amount</th>
          </tr>
        </thead>
        <tbody>
          {txs.map((t) => (
            <tr key={t.id}>
              <td>{t.date}</td>
              <td>{t.description}</td>
              <td className={`num ${t.amount < 0 ? 'out' : 'in'}`}>{gbp.format(t.amount)}</td>
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
    </main>
  )
}
