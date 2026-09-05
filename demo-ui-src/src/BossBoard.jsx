// Boss Board — the calm, farmer-facing landing: farms, saloons, live numbers.
// Consumes the SAME /demo/api/farm/ payload the engineering pages use; the
// only thing special here is what we choose to show (and deliberately not
// show: no tiers, no state machines, no try-it buttons).
import { useEffect, useRef, useState } from 'react'
import Sparkline from './Sparkline.jsx'
import AttentionTicker, { saloonReading, groupSaloons } from './AttentionTicker.jsx'
import FlockDial from './FlockDial.jsx'

const NH3_MAX = 25
const TEMP_MAX = 30
const MAX_POINTS = 60

const TEMP_COLOR = '#7fb4c9'
const NH3_COLOR = '#f5a94b'
const DANGER_COLOR = '#e2574c'

export default function BossBoard({ farm, tr, lang }) {
  const groups = groupSaloons(farm)
  const historiesRef = useRef({})
  const [lastUpdate, setLastUpdate] = useState(null)
  const [now, setNow] = useState(Date.now())

  // Rolling per-saloon buffers fed by the same 5s poll — no extra fetches.
  useEffect(() => {
    for (const s of farm?.saloons || []) {
      const r = saloonReading(s)
      const arr = historiesRef.current[s.id] || []
      arr.push({ temp: r.temp, nh3: r.nh3 })
      if (arr.length > MAX_POINTS) arr.shift()
      historiesRef.current[s.id] = arr
    }
    setLastUpdate(Date.now())
  }, [farm])

  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), 1000)
    return () => clearInterval(id)
  }, [])

  const totalSaloons = groups.reduce((n, g) => n + g.saloon_count, 0)
  const onlineSlots = groups.reduce((n, g) => n + (g.online_slots || 0), 0)
  const updatedLabel = (() => {
    if (!lastUpdate) return null
    const secs = Math.max(0, Math.round((now - lastUpdate) / 1000))
    if (secs < 2) return tr('justNow')
    if (secs < 60) return tr('updatedAgoSec').replace('{s}', secs)
    return tr('updatedAgoMin').replace('{s}', Math.round(secs / 60))
  })()

  return (
    <div className="board">
      <header className="board-head">
        <h2 className="board-title">{tr('farmsTitle')}</h2>
        <p className="strip-sub">
          {tr('farmsSub')}
          {updatedLabel && <span className="updated"> · {updatedLabel}</span>}
        </p>
      </header>

      <div className="plaque">
        <span className="plaque-item"><b>{groups.length}</b>{tr('statFarms')}</span>
        <span className="plaque-sep" aria-hidden="true">·</span>
        <span className="plaque-item"><b>{totalSaloons}</b>{tr('statSaloons')}</span>
        <span className="plaque-sep" aria-hidden="true">·</span>
        <span className="plaque-item"><b>{onlineSlots}</b>{tr('statSensors')}</span>
        <span className={`live-dot ${onlineSlots > 0 ? '' : 'off'}`} aria-hidden="true" />
      </div>

      <AttentionTicker groups={groups} tr={tr} />

      {groups.length === 0 ? (
        <p className="inbox-empty">{tr('noData')}</p>
      ) : (
        <div className="farm-grid">
          {groups.map((g, i) => (
            <FarmCard key={g.id} group={g} index={i} tr={tr} histories={historiesRef.current} />
          ))}
        </div>
      )}
    </div>
  )
}

function FarmCard({ group, index, tr, histories }) {
  const anyLive = (group.online_slots || 0) > 0
  const birds = group.saloons.reduce((n, s) => n + (s.flock?.placement_count || 0), 0)
  const day = Math.max(0, ...group.saloons.map((s) => s.flock?.day || 0))
  const cycleDays = Math.max(0, ...group.saloons.map((s) => s.flock?.cycle_days || 0)) || 45
  const deadPacks = group.dead_packs || 0
  const flockLine = birds > 0 ? tr('farmBirds').replace('{n}', birds.toLocaleString('en-US')).replace('{d}', day) : null
  return (
    <section className="farm-card" style={{ animationDelay: `${index * 80}ms` }}>
      <div className="farm-head">
        <div className="farm-title">
          <h3 className="farm-name">{group.name}</h3>
          {flockLine && <p className="farm-sub">{flockLine}</p>}
          {deadPacks > 0 && (
            <p className="farm-sub farm-dead">{tr('packsOffline').replace('{n}', deadPacks)}</p>
          )}
        </div>
        {day > 0 && <FlockDial day={day} cycleDays={cycleDays} tr={tr} index={index} />}
        <div className="farm-meta">
          <span className={`live-dot ${anyLive ? '' : 'off'}`} aria-hidden="true" />
          <span className="live-label">{anyLive ? tr('liveNow') : tr('noSignal')}</span>
        </div>
      </div>
      <div className="farm-saloon-list">
        {group.saloons.map((s) => (
          <SaloonRow key={s.id} saloon={s} tr={tr} history={histories[s.id] || []} />
        ))}
      </div>
    </section>
  )
}

function SaloonRow({ saloon, tr, history }) {
  const r = saloonReading(saloon)
  const status = !r.anyData ? 'off' : r.attention ? 'warn' : 'ok'
  const tempPts = history.map((h) => ({ y: h.temp }))
  const nh3Pts = history.map((h) => ({ y: h.nh3 }))
  return (
    <div className={`saloon-row ${status}`}>
      <div className="sr-name">
        <span className="sr-dot" aria-hidden="true" />
        <span className="sr-title">{saloon.name}</span>
        <a className="sr-link" href={`#/saloon/${saloon.id}`}>{tr('details')}</a>
      </div>
      <div className="sr-metric">
        <span className="sr-label">{tr('temp')}</span>
        <LiveVal value={r.temp} suffix="°" danger={r.tempBreach} />
        <Sparkline points={tempPts} color={TEMP_COLOR} threshold={TEMP_MAX} width={150} height={40} />
      </div>
      <div className="sr-metric">
        <span className="sr-label">{tr('nh3')}</span>
        <LiveVal value={r.nh3} danger={r.nh3Breach} />
        <Sparkline points={nh3Pts} color={r.nh3Breach ? DANGER_COLOR : NH3_COLOR} threshold={NH3_MAX} width={150} height={40} />
      </div>
      <span className={`sr-status ${status}`}>
        {status === 'warn' ? tr('attention') : status === 'ok' ? tr('liveNow') : tr('noSignal')}
      </span>
    </div>
  )
}

// Quiet tick: values cross-fade on change (keyed remount), never bounce.
function LiveVal({ value, suffix = '', danger }) {
  if (value == null) return <span className="lv muted">—</span>
  const rounded = Math.round(value * 10) / 10
  return (
    <span key={rounded} className={`lv ${danger ? 'danger' : ''}`}>
      {rounded}{suffix}
    </span>
  )
}
