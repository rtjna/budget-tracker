import { useEffect, useMemo, useState } from 'react'
import './Dashboard.css'

type MonthRow = {
  month: string
  spending: number
  income: number
  net: number
  by_category: Record<string, number>
}

type CategoryTotal = { id: number; name: string; total: number }

type Overview = { months: MonthRow[]; categories: CategoryTotal[] }

type MonthDetail = {
  month: string
  categories: CategoryTotal[]
  merchants: { merchant: string; total: number; count: number }[]
}

type RecurringItem = {
  merchant: string
  category: string
  cadence: string
  typical_amount: number
  last_amount: number
  price_change: number
  last_date: string
  next_expected: string | null
  occurrences: number
  active: boolean
  monthly_equivalent: number
}

const SERIES_VARS = [1, 2, 3, 4, 5, 6, 7, 8].map((i) => `var(--series-${i})`)
const MAX_SLOTS = 7 // top 7 categories get their own hue; the tail folds into Other

const gbp = (n: number, digits = 0) =>
  n.toLocaleString('en-GB', {
    style: 'currency',
    currency: 'GBP',
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  })

const monthLabel = (m: string) => {
  const d = new Date(m + '-01')
  const name = d.toLocaleString('en-GB', { month: 'short' })
  return d.getMonth() === 0 ? `${name} ’${String(d.getFullYear()).slice(2)}` : name
}

const monthLong = (m: string) =>
  new Date(m + '-01').toLocaleString('en-GB', { month: 'long', year: 'numeric' })

function niceTicks(max: number): number[] {
  if (max <= 0) return [0]
  const raw = max / 4
  const mag = Math.pow(10, Math.floor(Math.log10(raw)))
  const step = [1, 2, 2.5, 5, 10].map((s) => s * mag).find((s) => s >= raw) ?? mag * 10
  const ticks = []
  for (let v = 0; v <= max + step * 0.001; v += step) ticks.push(v)
  return ticks
}

export default function Dashboard() {
  const [overview, setOverview] = useState<Overview | null>(null)
  const [selected, setSelected] = useState('')
  const [detail, setDetail] = useState<MonthDetail | null>(null)
  const [recurring, setRecurring] = useState<RecurringItem[]>([])

  useEffect(() => {
    fetch('/api/stats/overview?months=12')
      .then((r) => r.json())
      .then((data: Overview) => {
        setOverview(data)
        if (data.months.length) setSelected(data.months[data.months.length - 1].month)
      })
    fetch('/api/stats/recurring')
      .then((r) => r.json())
      .then((d) => setRecurring(d.items))
  }, [])

  useEffect(() => {
    if (selected) {
      fetch(`/api/stats/month/${selected}`)
        .then((r) => r.json())
        .then(setDetail)
    }
  }, [selected])

  // Fixed slot assignment over the whole window: color follows the category,
  // never the current month's ranking.
  const slots = useMemo(() => {
    const map = new Map<number, number>()
    overview?.categories.slice(0, MAX_SLOTS).forEach((c, i) => map.set(c.id, i))
    return map
  }, [overview])

  if (!overview || !overview.months.length) {
    return <p className="review-intro">No data yet — import some transactions first.</p>
  }

  const months = overview.months
  const current = months.find((m) => m.month === selected) ?? months[months.length - 1]
  const prevIdx = months.indexOf(current) - 1
  const prev = prevIdx >= 0 ? months[prevIdx] : null
  const activeSubs = recurring.filter((r) => r.active)
  const subsMonthly = activeSubs.reduce((s, r) => s + r.monthly_equivalent, 0)

  const seriesName = (id: number) =>
    overview.categories.find((c) => c.id === id)?.name ?? 'Uncategorized'
  const slotFill = (categoryId: number) => {
    const slot = slots.get(categoryId)
    return SERIES_VARS[slot !== undefined ? slot : MAX_SLOTS]
  }

  return (
    <section className="dash viz-root">
      <div className="dash-filters">
        <label>
          Month{' '}
          <select value={selected} onChange={(e) => setSelected(e.target.value)}>
            {[...months].reverse().map((m) => (
              <option key={m.month} value={m.month}>
                {monthLong(m.month)}
              </option>
            ))}
          </select>
        </label>
        <span className="dash-note">
          Foreign-currency accounts converted at approximate rates · transfers excluded
        </span>
      </div>

      <div className="kpi-row">
        <StatTile
          label={`Spending — ${monthLong(current.month)}`}
          value={gbp(current.spending)}
          delta={prev ? current.spending - prev.spending : null}
          downIsGood
        />
        <StatTile
          label="Income"
          value={gbp(current.income)}
          delta={prev ? current.income - prev.income : null}
        />
        <StatTile label="Net" value={gbp(current.net)} delta={prev ? current.net - prev.net : null} />
        <StatTile
          label="Subscriptions / month"
          value={gbp(subsMonthly)}
          sub={`${activeSubs.length} active`}
        />
      </div>

      <ChartCard title="Spending by month">
        <StackedColumns
          months={months}
          slots={slots}
          seriesName={seriesName}
          selected={current.month}
          onSelect={setSelected}
        />
      </ChartCard>

      <ChartCard title="Income vs spending">
        <IncomeSpending months={months} selected={current.month} onSelect={setSelected} />
      </ChartCard>

      <div className="dash-split">
        <ChartCard title={`Categories — ${monthLong(current.month)}`}>
          {detail && <BarList rows={detail.categories} fill={slotFill} />}
        </ChartCard>
        <div className="chart-card">
          <h3>Top merchants — {monthLong(current.month)}</h3>
          <table className="mini-table">
            <tbody>
              {detail?.merchants.slice(0, 10).map((m) => (
                <tr key={m.merchant}>
                  <td className="merchant-name">{m.merchant}</td>
                  <td className="num muted">{m.count}×</td>
                  <td className="num">{gbp(m.total, 2)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>

      <div className="chart-card">
        <h3>Recurring payments</h3>
        <table className="mini-table subs-table">
          <thead>
            <tr>
              <th>Merchant</th>
              <th>Category</th>
              <th>Cadence</th>
              <th className="num">Typical</th>
              <th className="num">Last</th>
              <th>Next expected</th>
              <th>Status</th>
            </tr>
          </thead>
          <tbody>
            {recurring.map((r) => (
              <tr key={r.merchant} className={r.active ? '' : 'lapsed'}>
                <td className="merchant-name">{r.merchant}</td>
                <td>{r.category}</td>
                <td>{r.cadence}</td>
                <td className="num">{gbp(r.typical_amount, 2)}</td>
                <td className="num">
                  {gbp(r.last_amount, 2)}
                  {r.price_change > 0 && (
                    <span className="price-up"> ▲ {gbp(r.price_change, 2)} rise</span>
                  )}
                  {r.price_change < 0 && (
                    <span className="price-down"> ▼ {gbp(-r.price_change, 2)} drop</span>
                  )}
                </td>
                <td>{r.next_expected ?? '—'}</td>
                <td>{r.active ? 'active' : 'lapsed'}</td>
              </tr>
            ))}
            {recurring.length === 0 && (
              <tr>
                <td colSpan={7} className="muted">
                  Nothing recurring detected yet.
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
    </section>
  )
}

function StatTile({
  label,
  value,
  delta,
  sub,
  downIsGood = false,
}: {
  label: string
  value: string
  delta?: number | null
  sub?: string
  downIsGood?: boolean
}) {
  const good = delta != null && (downIsGood ? delta < 0 : delta > 0)
  return (
    <div className="stat-tile">
      <span className="stat-label">{label}</span>
      <span className="stat-value">{value}</span>
      {delta != null && Math.abs(delta) >= 1 && (
        <span className={`stat-delta ${good ? 'good' : 'bad'}`}>
          {delta > 0 ? '▲' : '▼'} {gbp(Math.abs(delta))} vs previous month
        </span>
      )}
      {sub && <span className="stat-delta muted">{sub}</span>}
    </div>
  )
}

function ChartCard({ title, children }: { title: string; children: React.ReactNode }) {
  const [table, setTable] = useState(false)
  return (
    <div className="chart-card">
      <div className="chart-card-head">
        <h3>{title}</h3>
        <button className="table-toggle" onClick={() => setTable(!table)}>
          {table ? 'chart' : 'table'}
        </button>
      </div>
      <div data-view={table ? 'table' : 'chart'} className="chart-card-body">
        {children}
      </div>
    </div>
  )
}

const W = 860
const H = 240
const PAD = { left: 52, right: 12, top: 12, bottom: 26 }

function StackedColumns({
  months,
  slots,
  seriesName,
  selected,
  onSelect,
}: {
  months: MonthRow[]
  slots: Map<number, number>
  seriesName: (id: number) => string
  selected: string
  onSelect: (m: string) => void
}) {
  const [hover, setHover] = useState<string | null>(null)
  const maxSpend = Math.max(...months.map((m) => m.spending))
  const ticks = niceTicks(maxSpend)
  const top = ticks[ticks.length - 1]
  const plotW = W - PAD.left - PAD.right
  const plotH = H - PAD.top - PAD.bottom
  const band = plotW / months.length
  const barW = Math.min(24, band * 0.6)
  const y = (v: number) => PAD.top + plotH - (v / top) * plotH

  // Legend order = slot order; Other last.
  const legendIds = [...slots.keys()]
  const stackFor = (m: MonthRow) => {
    const parts = legendIds.map((id) => ({ id, value: m.by_category[String(id)] ?? 0 }))
    const other = m.spending - parts.reduce((s, p) => s + p.value, 0)
    if (other > 0.005) parts.push({ id: -1, value: other })
    return parts.filter((p) => p.value > 0)
  }

  const hovered = hover ? months.find((m) => m.month === hover) : null

  return (
    <>
      <div className="legend">
        {legendIds.map((id, i) => (
          <span key={id} className="legend-item">
            <span className="swatch" style={{ background: SERIES_VARS[i] }} />
            {seriesName(id)}
          </span>
        ))}
        <span className="legend-item">
          <span className="swatch" style={{ background: SERIES_VARS[7] }} />
          Other
        </span>
      </div>
      <div className="chart-wrap">
        <svg viewBox={`0 0 ${W} ${H}`} className="chart-svg" role="img" aria-label="Monthly spending stacked by category">
          {ticks.map((t) => (
            <g key={t}>
              <line x1={PAD.left} x2={W - PAD.right} y1={y(t)} y2={y(t)} className="gridline" />
              <text x={PAD.left - 6} y={y(t) + 3} className="axis-text" textAnchor="end">
                {t >= 1000 ? `${(t / 1000).toLocaleString()}k` : t.toLocaleString()}
              </text>
            </g>
          ))}
          {months.map((m, mi) => {
            const x = PAD.left + band * mi + (band - barW) / 2
            let acc = 0
            const segs = stackFor(m)
            return (
              <g
                key={m.month}
                tabIndex={0}
                onPointerEnter={() => setHover(m.month)}
                onPointerLeave={() => setHover(null)}
                onFocus={() => setHover(m.month)}
                onBlur={() => setHover(null)}
                onClick={() => onSelect(m.month)}
                style={{ cursor: 'pointer' }}
              >
                <rect x={PAD.left + band * mi} y={PAD.top} width={band} height={plotH + PAD.bottom} fill="transparent" />
                {segs.map((seg, si) => {
                  const y1 = y(acc + seg.value)
                  const h = y(acc) - y1
                  acc += seg.value
                  const isTop = si === segs.length - 1
                  const fill = seg.id === -1 ? SERIES_VARS[7] : SERIES_VARS[slots.get(seg.id) ?? 7]
                  return (
                    <rect
                      key={seg.id}
                      x={x}
                      y={y1}
                      width={barW}
                      height={Math.max(h - 2, 1)} /* 2px surface gap between segments */
                      rx={isTop ? 4 : 0}
                      fill={fill}
                      opacity={hover && hover !== m.month ? 0.45 : 1}
                    />
                  )
                })}
                <text
                  x={x + barW / 2}
                  y={H - 8}
                  textAnchor="middle"
                  className={`axis-text ${m.month === selected ? 'axis-selected' : ''}`}
                >
                  {monthLabel(m.month)}
                </text>
              </g>
            )
          })}
        </svg>
        {hovered && (
          <div className="viz-tooltip">
            <strong>{monthLong(hovered.month)}</strong>
            {stackFor(hovered)
              .slice()
              .reverse()
              .map((seg) => (
                <div key={seg.id} className="tt-row">
                  <span
                    className="tt-key"
                    style={{ background: seg.id === -1 ? SERIES_VARS[7] : SERIES_VARS[slots.get(seg.id) ?? 7] }}
                  />
                  <span className="tt-value">{gbp(seg.value)}</span>
                  <span className="tt-label">{seg.id === -1 ? 'Other' : seriesName(seg.id)}</span>
                </div>
              ))}
            <div className="tt-row tt-total">
              <span className="tt-value">{gbp(hovered.spending)}</span>
              <span className="tt-label">total</span>
            </div>
          </div>
        )}
      </div>
      <table className="mini-table chart-table">
        <thead>
          <tr>
            <th>Month</th>
            {legendIds.map((id) => (
              <th key={id} className="num">
                {seriesName(id)}
              </th>
            ))}
            <th className="num">Other</th>
            <th className="num">Total</th>
          </tr>
        </thead>
        <tbody>
          {months.map((m) => {
            const known = legendIds.reduce((s, id) => s + (m.by_category[String(id)] ?? 0), 0)
            return (
              <tr key={m.month}>
                <td>{monthLong(m.month)}</td>
                {legendIds.map((id) => (
                  <td key={id} className="num">
                    {gbp(m.by_category[String(id)] ?? 0)}
                  </td>
                ))}
                <td className="num">{gbp(Math.max(m.spending - known, 0))}</td>
                <td className="num">{gbp(m.spending)}</td>
              </tr>
            )
          })}
        </tbody>
      </table>
    </>
  )
}

function IncomeSpending({
  months,
  selected,
  onSelect,
}: {
  months: MonthRow[]
  selected: string
  onSelect: (m: string) => void
}) {
  const [hover, setHover] = useState<string | null>(null)
  const maxV = Math.max(...months.map((m) => Math.max(m.income, m.spending)))
  const ticks = niceTicks(maxV)
  const top = ticks[ticks.length - 1]
  const plotW = W - PAD.left - PAD.right
  const plotH = H - PAD.top - PAD.bottom
  const band = plotW / months.length
  const barW = Math.min(18, band * 0.28)
  const y = (v: number) => PAD.top + plotH - (v / top) * plotH
  const hovered = hover ? months.find((m) => m.month === hover) : null

  return (
    <>
      <div className="legend">
        <span className="legend-item">
          <span className="swatch" style={{ background: SERIES_VARS[0] }} />
          Spending
        </span>
        <span className="legend-item">
          <span className="swatch" style={{ background: SERIES_VARS[1] }} />
          Income
        </span>
      </div>
      <div className="chart-wrap">
        <svg viewBox={`0 0 ${W} ${H}`} className="chart-svg" role="img" aria-label="Monthly income versus spending">
          {ticks.map((t) => (
            <g key={t}>
              <line x1={PAD.left} x2={W - PAD.right} y1={y(t)} y2={y(t)} className="gridline" />
              <text x={PAD.left - 6} y={y(t) + 3} className="axis-text" textAnchor="end">
                {t >= 1000 ? `${(t / 1000).toLocaleString()}k` : t.toLocaleString()}
              </text>
            </g>
          ))}
          {months.map((m, mi) => {
            const cx = PAD.left + band * mi + band / 2
            return (
              <g
                key={m.month}
                tabIndex={0}
                onPointerEnter={() => setHover(m.month)}
                onPointerLeave={() => setHover(null)}
                onFocus={() => setHover(m.month)}
                onBlur={() => setHover(null)}
                onClick={() => onSelect(m.month)}
                style={{ cursor: 'pointer' }}
              >
                <rect x={PAD.left + band * mi} y={PAD.top} width={band} height={plotH + PAD.bottom} fill="transparent" />
                <rect
                  x={cx - barW - 1}
                  y={y(m.spending)}
                  width={barW}
                  height={Math.max(y(0) - y(m.spending), 1)}
                  rx={4}
                  fill={SERIES_VARS[0]}
                  opacity={hover && hover !== m.month ? 0.45 : 1}
                />
                <rect
                  x={cx + 1}
                  y={y(m.income)}
                  width={barW}
                  height={Math.max(y(0) - y(m.income), 1)}
                  rx={4}
                  fill={SERIES_VARS[1]}
                  opacity={hover && hover !== m.month ? 0.45 : 1}
                />
                <text
                  x={cx}
                  y={H - 8}
                  textAnchor="middle"
                  className={`axis-text ${m.month === selected ? 'axis-selected' : ''}`}
                >
                  {monthLabel(m.month)}
                </text>
              </g>
            )
          })}
        </svg>
        {hovered && (
          <div className="viz-tooltip">
            <strong>{monthLong(hovered.month)}</strong>
            <div className="tt-row">
              <span className="tt-key" style={{ background: SERIES_VARS[1] }} />
              <span className="tt-value">{gbp(hovered.income)}</span>
              <span className="tt-label">income</span>
            </div>
            <div className="tt-row">
              <span className="tt-key" style={{ background: SERIES_VARS[0] }} />
              <span className="tt-value">{gbp(hovered.spending)}</span>
              <span className="tt-label">spending</span>
            </div>
            <div className="tt-row tt-total">
              <span className="tt-value">{gbp(hovered.net)}</span>
              <span className="tt-label">net</span>
            </div>
          </div>
        )}
      </div>
      <table className="mini-table chart-table">
        <thead>
          <tr>
            <th>Month</th>
            <th className="num">Income</th>
            <th className="num">Spending</th>
            <th className="num">Net</th>
          </tr>
        </thead>
        <tbody>
          {months.map((m) => (
            <tr key={m.month}>
              <td>{monthLong(m.month)}</td>
              <td className="num">{gbp(m.income)}</td>
              <td className="num">{gbp(m.spending)}</td>
              <td className="num">{gbp(m.net)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </>
  )
}

function BarList({ rows, fill }: { rows: CategoryTotal[]; fill: (id: number) => string }) {
  const max = Math.max(...rows.map((r) => r.total), 1)
  return (
    <div className="bar-list">
      {rows.map((r) => (
        <div key={r.id} className="bar-row" title={`${r.name}: ${gbp(r.total, 2)}`}>
          <span className="bar-name">{r.name}</span>
          <span className="bar-track">
            <span
              className="bar-fill"
              style={{ width: `${(r.total / max) * 100}%`, background: fill(r.id) }}
            />
          </span>
          <span className="bar-value">{gbp(r.total)}</span>
        </div>
      ))}
    </div>
  )
}
