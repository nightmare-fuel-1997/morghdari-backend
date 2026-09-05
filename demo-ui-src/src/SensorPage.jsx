// Individual sensor page: its own history chart, stats, and health.
import { useEffect, useMemo, useState } from 'react'
import LineChart from './LineChart.jsx'
import AttentionTicker, { groupSaloons, saloonGroups } from './AttentionTicker.jsx'
import { fetchSaloonSensors, fetchSaloonStats, fetchSaloonTelemetry } from './api.js'

const METRIC_COLORS = { temp: '#f5a94b', nh3: '#e2574c', humidity: '#7fb4c9', thi: '#9dc88d' }
const METRIC_LABELS = { temp: 'Temp °C', nh3: 'NH3 ppm', humidity: 'Humidity %', thi: 'THI' }
const THRESHOLDS = { nh3: 25, temp: 30 }

function sinceISO(hours) {
  return new Date(Date.now() - hours * 3600_000).toISOString().slice(0, 19)
}

export default function SensorPage({ session, saloonId, slotId, farm, tr }) {
  // The parent saloon's attention items — from this page, the item link is a
  // useful jump back to the saloon's own view.
  const saloonAttention = useMemo(
    () => saloonGroups(groupSaloons(farm), saloonId),
    [farm, saloonId],
  )
  const [sensor, setSensor] = useState(null)
  const [rows, setRows] = useState([])
  const [stats, setStats] = useState(null)
  const [focusMetric, setFocusMetric] = useState('nh3')

  useEffect(() => {
    let alive = true
    const load = async () => {
      const s = await fetchSaloonSensors(session.access, saloonId)
      if (alive && s.ok) {
        setSensor((s.data.sensors || []).find((x) => x.slot_id === slotId) || null)
      }
      const st = await fetchSaloonStats(session.access, saloonId, { window: '24h', slotId })
      if (alive && st.ok) setStats(st.data)
    }
    load()
    const id = setInterval(() => { if (!document.hidden) load() }, 10_000)
    return () => { alive = false; clearInterval(id) }
  }, [session.access, saloonId, slotId])

  useEffect(() => {
    let alive = true
    fetchSaloonTelemetry(session.access, saloonId, { since: sinceISO(24), slotId })
      .then((r) => { if (alive && r.ok) setRows(r.data?.results || []) })
    return () => { alive = false }
  }, [session.access, saloonId, slotId])

  const points = useMemo(() => rows.map((r, i) => ({
    x: i, y: r.metrics?.[focusMetric]?.avg ?? null,
  })), [rows, focusMetric])

  const health = sensor?.health || 'offline'
  const badge = health === 'online' ? 'ok' : health === 'stale' ? 'warn' : 'off'

  return (
    <div className="page">
      <nav className="crumbs">
        <a href="#/">← {tr('overviewTitle')}</a>
        <a href={`#/saloon/${saloonId}`}>{tr('saloon')} {saloonId}</a>
        <span className="mono">{slotId}</span>
      </nav>
      <AttentionTicker groups={saloonAttention} tr={tr} link />
      <h2 className="page-title">
        {slotId}
        <span className={`status ${badge}`} style={{ marginLeft: 12 }}>
          {health === 'online' ? '●' : health === 'stale' ? '◐' : '○'} {tr(health)}
        </span>
        {sensor?.breaching?.length > 0 && (
          <span className="over-limit" style={{ marginLeft: 10 }}>
            {tr('breaching')}: {sensor.breaching.join(', ')}
          </span>
        )}
      </h2>

      <section className="card">
        <div className="card-head">
          <h3>{tr('sensorHistoryTitle')}</h3>
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
          series={[{ name: slotId, color: METRIC_COLORS[focusMetric] || '#a89a85', points }]}
          width={900} height={280}
          threshold={THRESHOLDS[focusMetric] != null ? { value: THRESHOLDS[focusMetric], label: tr('threshold') } : null}
          yLabel={METRIC_LABELS[focusMetric]}
          xFormat={(v) => {
            const r = rows[v]
            if (!r) return ''
            const d = new Date(r.device_ts.endsWith('Z') ? r.device_ts : r.device_ts + 'Z')
            return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
          }}
          yFormat={(v) => v.toFixed(1)}
        />
        <p className="hint">{tr('sensorHistorySub')}</p>
      </section>

      {stats && (
        <section className="card">
          <div className="card-head"><h3>{tr('historyTitle')} · 24h</h3></div>
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
        </section>
      )}

      <section className="card">
        <div className="card-head"><h3>{tr('recentReadings')}</h3></div>
        {rows.length ? (
          <table className="sensor-table">
            <thead><tr><th>{tr('deviceTs')}</th><th>temp</th><th>nh3</th><th>humidity</th></tr></thead>
            <tbody>
              {rows.slice(-12).reverse().map((r, i) => (
                <tr key={i}>
                  <td className="mono">{r.device_ts}</td>
                  <td className="mono">{r.metrics?.temp?.avg ?? '—'}</td>
                  <td className="mono">{r.metrics?.nh3?.avg ?? '—'}</td>
                  <td className="mono">{r.metrics?.humidity?.avg ?? '—'}</td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : <p className="hint">{tr('noData')}</p>}
      </section>
    </div>
  )
}
