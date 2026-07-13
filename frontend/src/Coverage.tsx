import { useEffect, useState } from 'react'
import { api } from './api'
import './Dashboard.css'
import './Coverage.css'

type CoverageAccount = {
  id: number
  name: string
  provider: string
  kind: string
  latest: string
  total: number
  months: Record<string, number>
}

type CoverageData = { months: string[]; accounts: CoverageAccount[] }

// Sequential ramp buckets: more transactions = stronger cell.
const BUCKETS: { min: number; cls: string }[] = [
  { min: 40, cls: 'bucket-4' },
  { min: 15, cls: 'bucket-3' },
  { min: 5, cls: 'bucket-2' },
  { min: 1, cls: 'bucket-1' },
]

const monthLabel = (m: string) => {
  const d = new Date(m + '-01')
  const name = d.toLocaleString('en-GB', { month: 'short' })
  return d.getMonth() === 0 ? `${name} ’${String(d.getFullYear()).slice(2)}` : name
}

export default function Coverage() {
  const [data, setData] = useState<CoverageData | null>(null)
  const [windowMonths, setWindowMonths] = useState<number | 'all'>(24)

  useEffect(() => {
    api('/api/stats/coverage')
      .then((r) => r.json())
      .then(setData)
  }, [])

  if (!data) return <p className="review-intro">Loading…</p>
  if (!data.months.length) return <p className="review-intro">No data yet.</p>

  const months =
    windowMonths === 'all' ? data.months : data.months.slice(-windowMonths)

  return (
    <section className="dash viz-root">
      <div className="dash-filters">
        <label>
          Window{' '}
          <select
            value={String(windowMonths)}
            onChange={(e) =>
              setWindowMonths(e.target.value === 'all' ? 'all' : Number(e.target.value))
            }
          >
            <option value="12">Last 12 months</option>
            <option value="24">Last 24 months</option>
            <option value="all">Everything ({data.months.length} months)</option>
          </select>
        </label>
        <span className="dash-note">
          Transactions per account per month — gaps show where statements are missing
        </span>
      </div>

      <div className="chart-card">
        <h3>Data coverage</h3>
        <div className="coverage-scroll">
          <table className="coverage-grid">
            <thead>
              <tr>
                <th className="coverage-account">Account</th>
                {months.map((m) => (
                  <th key={m} className="coverage-month">
                    {monthLabel(m)}
                  </th>
                ))}
                <th className="num">Total</th>
                <th>Latest</th>
              </tr>
            </thead>
            <tbody>
              {data.accounts.map((a) => (
                <tr key={a.id}>
                  <td className="coverage-account">
                    {a.name}
                    <span className="coverage-kind"> {a.kind}</span>
                  </td>
                  {months.map((m) => {
                    const count = a.months[m]
                    const bucket = count ? BUCKETS.find((b) => count >= b.min) : undefined
                    return (
                      <td
                        key={m}
                        className={`coverage-cell ${bucket ? bucket.cls : 'coverage-empty'}`}
                        title={`${a.name} — ${monthLabel(m)}: ${count ?? 0} transactions`}
                      >
                        {count ?? ''}
                      </td>
                    )
                  })}
                  <td className="num">{a.total.toLocaleString()}</td>
                  <td className="coverage-latest">{a.latest}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        <div className="legend coverage-legend">
          <span className="legend-item">
            <span className="swatch coverage-empty" /> none
          </span>
          {[
            ['bucket-1', '1–4'],
            ['bucket-2', '5–14'],
            ['bucket-3', '15–39'],
            ['bucket-4', '40+'],
          ].map(([cls, label]) => (
            <span key={cls} className="legend-item">
              <span className={`swatch ${cls}`} /> {label}
            </span>
          ))}
          <span className="legend-item">transactions / month</span>
        </div>
        <p className="dash-note coverage-note">
          Empty cells mean no data for that account in that month. For statement-based
          accounts (Barclays, Barclaycard) that usually means a statement hasn't been
          imported; for API accounts it may just mean no activity.
        </p>
      </div>
    </section>
  )
}
