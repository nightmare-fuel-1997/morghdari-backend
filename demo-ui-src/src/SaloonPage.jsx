// Dedicated per-saloon page: live chart, history with range picker,
// window averages, and the full sensor inventory with health states.
import { useEffect, useMemo, useState } from 'react'
import LineChart from './LineChart.jsx'
import AttentionTicker, { groupSaloons, saloonGroups } from './AttentionTicker.jsx'
import FlockDial from './FlockDial.jsx'
import { fetchSaloonSensors, fetchSaloonStats, fetchSaloonTelemetry } from './api.js'

const WINDOWS = ['1h', '6h', '24h', '7d']
const METRIC_COLORS = { temp: '#f5a94b', nh3: '#e2574c', humidity: '#7fb4c9', thi: '#9dc88d' }
const METRIC_LABELS = { temp: 'Temp °C', nh3: 'NH3 ppm', humidity: 'Humidity %', thi: 'THI' }
const THRESHOLDS = { nh3: 25, temp: 30 }

function sinceISO(hours) {
  return new Date(Date.now() - hours * 3600_000).toISOString().slice(0, 19)
}

function fmtTime(ts) {
  if (!ts) return '—'
  const d = new Date(ts.endsWith('Z') ? ts : ts + 'Z')
  return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
}

export default function SaloonPage({ session, saloonId, farm, tr, lang }) {
  // This saloon's own attention items, from the global farm poll — the same
  // plain-language ticker as the overview, narrowed to the saloon you're on.
  const saloonAttention = useMemo(
    () => saloonGroups(groupSaloons(farm), saloonId),
    [farm, saloonId],
  )

  // This saloon's flock instrument: day-of-cycle + placement count, from the
  // same per-saloon flock block the boss board reads (no extra fetch).
  const myFlock = useMemo(() => {
    for (const g of groupSaloons(farm)) {
      const s = (g.saloons || []).find((x) => x.id === saloonId)
      if (s) return s.flock || null
    }
    return null
  }, [farm, saloonId])
  const [sensors, setSensors] = useState(null)
  const [stats, setStats] = useState(null)
  const [window, setWindow] = useState('1h')
  const [telemetry, setTelemetry] = useState(null)
  const [focusMetric, setFocusMetric] = useState('nh3')

  useEffect(() => {
    let alive = true
    const load = async () => {
      const [s, st] = await Promise.all([
        fetchSaloonSensors(session.access, saloonId),
        fetchSaloonStats(session.access, saloonId, { window }),
      ])
      if (!alive) return
      if (s.ok) setSensors(s.data)
      if (st.ok) setStats(st.data)
    }
    load()
    const id = setInterval(() => { if (!document.hidden) load() }, 10_000)
    return () => { alive = false; clearInterval(id) }
  }, [session.access, saloonId, window])

  useEffect(() => {
    let alive = true
    fetchSaloonTelemetry(session.access, saloonId, { since: sinceISO(WINDOW_HOURS[window]) })
      .then((r) => { if (alive && r.ok) setTelemetry(r.data?.results || []) })
    return () => { alive = false }
  }, [session.access, saloonId, window])

  const series = useMemo(() => {
    if (!telemetry) return []
    const rows = telemetry.slice()
    const bySlot = new Map()
    for (const row of rows) {
      if (!bySlot.has(row.slot_id)) bySlot.set(row.slot_id, [])
      bySlot.get(row.slot_id).push(row)
    }
    const out = []
    for (const [slotId, slotRows] of bySlot) {
      out.push({
        name: slotId,
        color: METRIC_COLORS[focusMetric] || '#a89a85',
        points: slotRows.map((r, i) => ({
          x: i,
          y: r.metrics?.[focusMetric]?.avg ?? null,
        })),
      })
    }
    return out
  }, [telemetry, focusMetric])

  const healthBadge = (h) => h === 'online' ? 'ok' : h === 'stale' ? 'warn' : 'off'

  return (
    <div className="page">
      <nav className="crumbs">
        <a href="#/">← {tr('overviewTitle')}</a>
        <span>{tr('saloon')} {saloonId}</span>
      </nav>
      {/* Non-link here: the item's destination is this very page. */}
      <AttentionTicker groups={saloonAttention} tr={tr} />
      <h2 className="page-title">{tr('saloon')} {saloonId}</h2>

      <section className="card">
        <div className="card-head">
          <h3>{tr('liveChartTitle')}</h3>
          <div className="metric-tabs">
            {['nh3', 'temp', 'humidity', 'thi'].map((m) => (
              <button key={m} className={`tab ${focusMetric === m ? 'active' : ''}`}
                style={focusMetric === m ? { borderColor: METRIC_COLORS[m], color: METRIC_COLORS[m] } : {}}
                onClick={() => setFocusMetric(m)}>
                {METRIC_LABELS[m]}
              </button>
            ))}
          </div>
        </div>
        <LineChart
          series={series}
          width={900} height={280}
          threshold={THRESHOLDS[focusMetric] != null ? { value: THRESHOLDS[focusMetric], label: tr('threshold') } : null}
          yLabel={METRIC_LABELS[focusMetric]}
          xFormat={(v) => fmtTime(telemetry?.[v]?.device_ts)}
          yFormat={(v) => v.toFixed(1)}
        />
        <p className="hint">{tr('liveChartSub')}</p>
      </section>

      <section className="card">
        <div className="card-head">
          <h3>{tr('historyTitle')}</h3>
          <div className="metric-tabs">
            {WINDOWS.map((w) => (
              <button key={w} className={`tab ${window === w ? 'active' : ''}`} onClick={() => setWindow(w)}>{w}</button>
            ))}
          </div>
        </div>
        {stats && (
          <div className="stats-grid">
            {Object.entries(stats.stats || {}).map(([metric, s]) => (
              <div key={metric} className="stat-block">
                <span className="stat-metric">{METRIC_LABELS[metric] || metric}</span>
                <span className="stat-row"><span>{tr('avg')}</span><b>{s.avg ?? '—'}</b></span>
                <span className="stat-row"><span>{tr('min')}</span><b>{s.min ?? '—'}</b></span>
                <span className="stat-row"><span>{tr('max')}</span><b>{s.max ?? '—'}</b></span>
                <span className="stat-row"><span>{tr('count')}</span><b>{s.count}</b></span>
              </div>
            ))}
            {!Object.keys(stats.stats || {}).length && <p className="hint">{tr('noData')}</p>}
          </div>
        )}
      </section>

      <section className="card">
        <div className="card-head">
          <h3>{tr('sensorsTitle')}</h3>
          {sensors && (
            <span className="health-summary">
              {sensors.health_counts.online} ● / {sensors.health_counts.stale} ◐ / {sensors.health_counts.offline} ○
            </span>
          )}
        </div>
        {myFlock && myFlock.day > 0 && (
          <div className="saloon-flock">
            <FlockDial day={myFlock.day} cycleDays={myFlock.cycle_days || 45} tr={tr} />
            <p className="saloon-flock-line">
              {tr('farmBirds')
                .replace('{n}', (myFlock.placement_count || 0).toLocaleString('en-US'))
                .replace('{d}', myFlock.day)}
            </p>
          </div>
        )}
        {sensors && sensors.sensors.length > 0 ? (
          <table className="sensor-table">
            <thead>
              <tr>
                <th>{tr('sensor')}</th><th>{tr('pack')}</th><th>{tr('health')}</th>
                <th>{tr('temp')}</th><th>{tr('nh3')}</th><th>{tr('lastSeen')}</th><th></th>
              </tr>
            </thead>
            <tbody>
              {sensors.sensors.map((s) => (
                <tr key={s.slot_id} className={s.breaching.length ? 'breach-row' : ''}>
                  <td className="mono">{s.slot_id}</td>
                  <td>{s.pack_id || '—'}</td>
                  <td><span className={`status ${healthBadge(s.health)}`}>
                    {s.health === 'online' ? '●' : s.health === 'stale' ? '◐' : '○'} {tr(s.health)}
                  </span></td>
                  <td className="mono">{s.last_metrics?.temp?.avg ?? '—'}</td>
                  <td className={`mono ${s.breaching.includes('nh3') ? 'danger' : ''}`}>
                    {s.last_metrics?.nh3?.avg ?? '—'}
                  </td>
                  <td className="mono">{s.last_device_ts ? fmtTime(s.last_device_ts) : '—'}</td>
                  <td><a className="btn ghost btn-sm" href={`#/saloon/${saloonId}/sensor/${encodeURIComponent(s.slot_id)}`}>
                    {tr('sensorPage')}
                  </a></td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : (
          <p className="hint">{tr('noData')}</p>
        )}
      </section>
    </div>
  )
}

const WINDOW_HOURS = { '1h': 1, '6h': 6, '24h': 24, '7d': 168 }
