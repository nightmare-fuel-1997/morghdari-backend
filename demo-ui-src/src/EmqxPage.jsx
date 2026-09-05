// EMQX page: embedded broker dashboard + live reachability + pipeline health.
import { useEffect, useState } from 'react'
import { fetchEmqxStatus, fetchPipeline } from './api.js'

export default function EmqxPage({ session, tr }) {
  const [status, setStatus] = useState(null)
  const [pipeline, setPipeline] = useState(null)

  useEffect(() => {
    let alive = true
    const load = async () => {
      const [s, p] = await Promise.all([
        fetchEmqxStatus(session.access),
        fetchPipeline(session.access),
      ])
      if (!alive) return
      if (s.ok) setStatus(s.data)
      if (p.ok) setPipeline(p.data)
    }
    load()
    const id = setInterval(() => { if (!document.hidden) load() }, 15_000)
    return () => { alive = false; clearInterval(id) }
  }, [session.access])

  const dot = (ok) => ok == null ? '◐' : ok ? '●' : '○'
  const cls = (ok) => ok == null ? 'warn' : ok ? 'ok' : 'off'

  return (
    <div className="page">
      <nav className="crumbs">
        <a href="#/">← {tr('overviewTitle')}</a>
        <span>EMQX</span>
      </nav>
      <h2 className="page-title">EMQX {tr('brokerDashboard')}</h2>

      <section className="card">
        <div className="card-head"><h3>{tr('brokerStatus')}</h3></div>
        {status ? (
          <div className="stats-grid">
            <div className="stat-block">
              <span className="stat-metric">MQTT :{status.mqtt_port}</span>
              <span className={`status ${cls(status.mqtt_reachable)}`}>
                {dot(status.mqtt_reachable)} {status.mqtt_reachable ? tr('reachable') : tr('unreachable')}
              </span>
            </div>
            <div className="stat-block">
              <span className="stat-metric">Dashboard :{status.dashboard_port}</span>
              <span className={`status ${cls(status.dashboard_reachable)}`}>
                {dot(status.dashboard_reachable)} {status.dashboard_reachable ? tr('reachable') : tr('unreachable')}
              </span>
            </div>
          </div>
        ) : <p className="hint">…</p>}
      </section>

      {status?.dashboard_reachable && (
        <section className="card">
          <div className="card-head">
            <h3>{tr('emqxEmbed')}</h3>
            <a className="btn ghost btn-sm" href={status.dashboard_url} target="_blank" rel="noreferrer">
              {tr('openNewTab')} ↗
            </a>
          </div>
          <iframe
            src={status.dashboard_url}
            title="EMQX Dashboard"
            className="emqx-frame"
            referrerPolicy="no-referrer"
          />
          <p className="hint">{tr('emqxEmbedHint')}</p>
        </section>
      )}

      {pipeline && (
        <section className="card">
          <div className="card-head"><h3>{tr('pipelineTitle')}</h3></div>
          <div className="stats-grid">
            {Object.entries(pipeline.streams || {}).map(([k, v]) => (
              <div key={k} className="stat-block">
                <span className="stat-metric">{k}</span>
                <span className="stat-row"><b className="mono">{v}</b></span>
              </div>
            ))}
            {Object.entries(pipeline.groups || {}).map(([k, g]) => (
              <div key={k} className="stat-block">
                <span className="stat-metric">{k} group</span>
                <span className="stat-row"><span>pending</span><b className="mono">{g.pending}</b></span>
                <span className="stat-row"><span>lag</span><b className="mono">{g.lag ?? '—'}</b></span>
              </div>
            ))}
          </div>
        </section>
      )}
    </div>
  )
}
