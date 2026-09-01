import { useEffect, useMemo, useRef, useState } from 'react'
import { T } from './i18n.js'
import {
  loadSession, saveSession, login, fetchFarm, fetchIdentity, fetchAlerts, fetchQuality, fetchStateMachines, simulate,
  sensorOff, acknowledgeAlert,
} from './api.js'
import Sparkline from './Sparkline.jsx'
import { useHashRoute } from './router.js'
import SaloonPage from './SaloonPage.jsx'
import SensorPage from './SensorPage.jsx'
import EmqxPage from './EmqxPage.jsx'
import BossBoard from './BossBoard.jsx'
import AttentionTicker, { groupSaloons } from './AttentionTicker.jsx'

const POLL_MS = 5000
const WINDOW_MIN = 30

// NH3 threshold per the seeded lifecycle rules (nh3_max = 25).
const NH3_MAX = 25
const TEMP_MAX = 30

// Alert triggered_at arrives as naive-UTC ISO from the API — treat it as UTC
// (append Z) so the "last hour" cutoff and the shown clock are both correct.
function parseIso(iso) {
  if (!iso) return NaN
  const s = iso.endsWith('Z') || iso.includes('+') ? iso : iso + 'Z'
  return new Date(s).getTime()
}

function useInterval(fn, ms) {
  const ref = useRef(fn)
  ref.current = fn
  useEffect(() => {
    const id = setInterval(() => {
      if (!document.hidden) ref.current()
    }, ms)
    return () => clearInterval(id)
  }, [ms])
}

export default function App() {
  const [lang, setLang] = useState(localStorage.getItem('morghdari-lang') || 'fa')
  const [session, setSession] = useState(loadSession())
  const [loginError, setLoginError] = useState(false)
  const [farm, setFarm] = useState(null)
  const [alerts, setAlerts] = useState(null)
  const [quality, setQuality] = useState(null)
  const [stateMachines, setStateMachines] = useState({})
  const [history, setHistory] = useState({}) // saloonId -> [{ts, temp, nh3}]
  const [busyBtn, setBusyBtn] = useState(null)
  const [loggingIn, setLoggingIn] = useState(false)
  const [identity, setIdentity] = useState(null)
  const [toast, setToast] = useState(null)
  const toastTimer = useRef(null)
  const errorRef = useRef(null)
  const route = useHashRoute()

  const tr = (key) => T[key]?.[lang] ?? key
  const dir = lang === 'fa' ? 'rtl' : 'ltr'

  useEffect(() => {
    document.documentElement.lang = lang
    document.documentElement.dir = dir
    document.title = `${T.brand[lang]} — ${T.heroKicker[lang]}`
    localStorage.setItem('morghdari-lang', lang)
  }, [lang, dir])

  const refresh = async (sess = session) => {
    if (!sess) return
    const [farmRes, alertRes, qualityRes, stateRes] = await Promise.all([
      fetchFarm(sess.access),
      fetchAlerts(sess.access, { limit: 100 }),
      fetchQuality(sess.access),
      fetchStateMachines(sess.access),
    ])
    if (farmRes.status === 401 || alertRes.status === 401) {
      setSession(null)
      saveSession(null)
      return
    }
    if (farmRes.ok && farmRes.data) {
      setFarm(farmRes.data)
      setHistory((prev) => {
        const next = { ...prev }
        const since = new Date(Date.now() - WINDOW_MIN * 60_000).toISOString().slice(0, 19)
        for (const saloon of farmRes.data.saloons) {
          fetch(`/api/v1/saloons/${saloon.id}/telemetry/?since=${encodeURIComponent(since)}`, {
            headers: { Authorization: `Bearer ${sess.access}` },
          })
            .then((r) => (r.ok ? r.json() : null))
            .then((data) => {
              if (!data) return
              const rows = (data.results || []).map((row) => ({
                ts: row.device_ts,
                temp: row.metrics?.temp?.avg ?? null,
                nh3: row.metrics?.nh3?.avg ?? null,
              }))
              next[saloon.id] = rows
              setHistory((h) => ({ ...h, [saloon.id]: rows }))
            })
        }
        return next
      })
    }
    if (alertRes.ok && alertRes.data) {
      setAlerts(alertRes.data.alerts || [])
    }
    if (qualityRes.ok && qualityRes.data) {
      setQuality(qualityRes.data)
    }
    if (stateRes.ok && stateRes.data) {
      setStateMachines(stateRes.data)
    }
  }

  useEffect(() => {
    if (session) refresh(session)
  }, [session?.access])

  useInterval(() => refresh(), POLL_MS)

  // Barn identity for the login backdrop — public endpoint, gentle 30s
  // heartbeat so the etched plaque stays honest while the page sits open.
  useEffect(() => {
    if (session) return
    let alive = true
    const load = () => fetchIdentity().then((r) => { if (alive && r.ok) setIdentity(r.data) })
    load()
    const id = setInterval(load, 30_000)
    return () => { alive = false; clearInterval(id) }
  }, [session])

  const doLogin = async (e) => {
    e.preventDefault()
    setLoginError(false)
    const username = e.target.username.value.trim()
    const password = e.target.password.value
    setLoggingIn(true)
    const res = await login(username, password)
    setLoggingIn(false)
    if (!res.ok) {
      setLoginError(true)
      // Move focus to the error so screen readers announce it (WCAG focus-management).
      requestAnimationFrame(() => errorRef.current?.focus())
      return
    }
    const sess = {
      access: res.data.access_token,
      refresh: res.data.refresh_token,
      profile: res.data.user,
    }
    saveSession(sess)
    setSession(sess)
  }

  const signOut = () => {
    saveSession(null)
    setSession(null)
    setFarm(null)
    setAlerts(null)
  }

  const press = async (key, action) => {
    setBusyBtn(key)
    try {
      const res = await action()
      if (res && !res.ok) {
        setToast({ kind: 'err', text: tr('requestFailed').replace('{code}', res.status) })
      } else if (key === 'spike') {
        setToast({ kind: 'ok', text: lang === 'fa' ? 'هشدار فوری ارسال شد' : 'Emergency alert sent' })
      } else if (key === 'recover') {
        setToast({ kind: 'ok', text: lang === 'fa' ? 'دستور بازیابی ارسال شد' : 'Recovery sent' })
      } else if (key === 'sensoroff') {
        setToast({ kind: 'ok', text: lang === 'fa' ? 'حسگر خاموش شد — هشدارِ قطع تا یک دقیقهٔ دیگر' : 'Sensor switched off — offline alert within a minute' })
      } else if (key === 'ack') {
        setToast({ kind: 'ok', text: lang === 'fa' ? 'هشدار تأیید شد — ارتقا متوقف شد' : 'Alert acknowledged — escalation paused' })
      }
      setTimeout(() => refresh(), 800)
    } catch {
      setToast({ kind: 'err', text: tr('requestFailed').replace('{code}', '—') })
    } finally {
      setBusyBtn(null)
      // Auto-dismiss after the press completes; a fresh press restarts the timer.
      clearTimeout(toastTimer.current)
      toastTimer.current = setTimeout(() => setToast(null), 3200)
    }
  }

  const onSpike = () => press('spike', () => simulate(session.access, { dangerous: true }))
  const onRecover = () => press('recover', () => simulate(session.access, {}))
  const onSensorOff = () => press('sensoroff', () => sensorOff(session.access))
  const onAck = () => {
    const target = (alerts || []).find((a) => !a.resolved_at)
    if (!target) {
      setToast({ kind: 'err', text: lang === 'fa' ? 'هیچ هشدار بازی برای تأیید' : 'No open alert to acknowledge' })
      clearTimeout(toastTimer.current)
      toastTimer.current = setTimeout(() => setToast(null), 3200)
      return
    }
    press('ack', () => acknowledgeAlert(session.access, target.id))
  }

  const openAlerts = useMemo(
    () => (alerts || []).filter((a) => !a.resolved_at),
    [alerts],
  )
  const pastAlerts = useMemo(
    () => (alerts || []).filter((a) => a.resolved_at),
    [alerts],
  )

  // ── recently-noticed log ──
  // Saloon/farm names for alert rows, from the same farm payload the strip uses.
  const saloonNameOf = useMemo(() => {
    const m = new Map()
    for (const g of groupSaloons(farm)) {
      for (const s of g.saloons) m.set(s.id, { saloon: s.name, farm: g.name })
    }
    return m
  }, [farm])

  // Engine-confirmed alerts that fired within the last hour.
  const noticed = useMemo(() => {
    const cutoff = Date.now() - 3600_000
    return (alerts || [])
      .filter((a) => a.triggered_at && parseIso(a.triggered_at) >= cutoff)
      .slice(0, 8)
  }, [alerts])

  const fmtHhmm = (iso) => {
    const d = new Date(parseIso(iso))
    return Number.isNaN(d.getTime()) ? '—' : d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
  }

  // Live escalation countdown data from /demo/api/farm/ (alert_state block).
  const alertState = farm?.alert_state || {}
  const escalationConfig = farm?.escalation || { tier2_min: 15, tier3_min: 30, ack_pause_min: 10 }
  const escalationFor = (a) => {
    const entry = alertState[String(a.saloon_id)]
    if (!entry) return null
    const active = entry.active_alerts?.find((x) => x.id === String(a.id))
    if (active) return active
    const breach = entry.breaches?.find((x) => x.metric === a.metric_type)
    if (breach) return { ...breach, isBreach: true }
    return null
  }

  if (!session) {
    return (
      <div className="shell" dir={dir}>
        <a className="skip-link" href="#main">{tr('skipToContent')}</a>
        <Header lang={lang} setLang={setLang} tr={tr} onLogout={null} />
        <section className="login-hero">
          <h1>{tr('heroTitle')}</h1>
          <p className="hero-sub">{tr('heroSub')}</p>
          <form className="login-card" onSubmit={doLogin}>
            <h2>{tr('loginTitle')}</h2>
            <p className="hint">{tr('loginHint')}</p>
            <label htmlFor="login-username">{tr('username')}</label>
            <input id="login-username" name="username" defaultValue="demo-owner" autoComplete="username" required />
            <label htmlFor="login-password">{tr('password')}</label>
            <input id="login-password" name="password" type="password" defaultValue="demo-pass-123" autoComplete="current-password" required />
            {loginError && <p className="err" ref={errorRef} tabIndex={-1} role="alert">{tr('loginError')}</p>}
            <button className="btn primary" type="submit" disabled={loggingIn}>
              {loggingIn ? tr('busy') : tr('loginBtn')}
            </button>
          </form>
          {identity && identity.farms > 0 && (
            <div className="plaque etched" aria-label={tr('mainTitle')}>
              <span className="plaque-item"><b>{identity.farms}</b>{tr('statFarms')}</span>
              <span className="plaque-sep" aria-hidden="true">·</span>
              <span className="plaque-item"><b>{identity.saloons}</b>{tr('statSaloons')}</span>
              <span className="plaque-sep" aria-hidden="true">·</span>
              <span className="plaque-item"><b>{identity.sensors_online}</b>{tr('statSensors')}</span>
              <span className={`live-dot ${identity.sensors_online > 0 ? '' : 'off'}`} aria-hidden="true" />
            </div>
          )}
        </section>
        <Promises tr={tr} />
        <Footer tr={tr} />
      </div>
    )
  }

  const saloons = farm?.saloons || []

  const chrome = (children) => (
    <div className="shell wide" dir={dir}>
      <a className="skip-link" href="#main">{tr('skipToContent')}</a>
      <Header lang={lang} setLang={setLang} tr={tr} onLogout={signOut} session={session} />
      <main id="main">{children}</main>
      <Footer tr={tr} />
    </div>
  )
  if (route.page === 'saloon') {
    return chrome(<SaloonPage session={session} saloonId={route.saloonId} farm={farm} tr={tr} lang={lang} />)
  }
  if (route.page === 'sensor') {
    return chrome(<SensorPage session={session} saloonId={route.saloonId} slotId={route.slotId} farm={farm} tr={tr} lang={lang} />)
  }
  if (route.page === 'emqx') {
    return chrome(<EmqxPage session={session} tr={tr} lang={lang} />)
  }
  if (route.page === 'farms') {
    return chrome(<BossBoard farm={farm} tr={tr} lang={lang} />)
  }

  return (
    <div className="shell" dir={dir}>
      <a className="skip-link" href="#main">{tr('skipToContent')}</a>
      <Header lang={lang} setLang={setLang} tr={tr} onLogout={signOut} session={session} />

      {/* Screen-reader page title — the visible headline is the section h2. */}
      <h1 className="sr-only">{tr('mainTitle')}</h1>

      {/* ── live strip ── */}
      <main id="main">
      <AttentionTicker groups={groupSaloons(farm)} tr={tr} link />
      <section className="strip">
        <div className="strip-head">
          <h2>{tr('stripTitle')}</h2>
          <span className="live-dot" />
          <span className="live-label">{tr('liveBadge')}</span>
        </div>
        <p className="strip-sub">{tr('stripSub')}</p>
        <div className="saloon-grid">
          {saloons.map((s) => {
            const rows = history[s.id] || []
            const tempPts = rows.filter((r) => r.temp != null).map((r) => ({ y: r.temp }))
            const nh3Pts = rows.filter((r) => r.nh3 != null).map((r) => ({ y: r.nh3 }))
            const latest = rows[rows.length - 1]
            const allOnline = s.slots.every((sl) => sl.online)
            const anyStale = s.slots.some((sl) => !sl.online)
            const breaching = (latest?.nh3 ?? 0) >= NH3_MAX
            return (
              <a key={s.id} href={`#/saloon/${s.id}`} className={`saloon-card link ${breaching ? 'breach' : ''}`}>
                <div className="saloon-head">
                  <span className="saloon-name">{tr('saloon')} {s.id}</span>
                  <span className={`status ${allOnline ? 'ok' : anyStale ? 'warn' : 'off'}`}>
                    {allOnline ? '●' : anyStale ? '◐' : '○'} {allOnline ? tr('online') : tr('stale')}
                  </span>
                </div>
                <div className="metrics">
                  <div className="metric">
                    <span className="metric-label">{tr('temp')}</span>
                    <span className="metric-val">{latest?.temp != null ? `${latest.temp}°` : '—'}</span>
                    <Sparkline points={tempPts} color="#7fb4c9" threshold={TEMP_MAX} />
                  </div>
                  <div className="metric">
                    <span className="metric-label">{tr('nh3')}</span>
                    <span className={`metric-val ${breaching ? 'danger' : ''}`}>
                      {latest?.nh3 != null ? latest.nh3 : '—'}
                    </span>
                    {breaching && <span className="over-limit">{tr('overLimit')}</span>}
                    <Sparkline points={nh3Pts} color={breaching ? '#e2574c' : '#f5a94b'} threshold={NH3_MAX} />
                  </div>
                </div>
                <span className="saloon-cta">{tr('openSaloon')} →</span>
              </a>
            )
          })}
          {saloons.length === 0 && <p className="hint">{tr('noData')}</p>}
        </div>
      </section>

      {/* ── recently noticed: last-hour confirmed alerts ── */}
      <section className="noticed">
        <div className="noticed-head">
          <h2>{tr('noticedTitle')}</h2>
          <span className="hint">{tr('noticedSub')}</span>
        </div>
        {noticed.length === 0 ? (
          <p className="hint noticed-empty">{tr('noticedEmpty')}</p>
        ) : (
          <ul className="noticed-list">
            {noticed.map((a) => {
              const where = saloonNameOf.get(a.saloon_id)
              const tier = (a.current_status || '').match(/ACTIVE_TIER_(\d)/)
              const chip = tier
                ? { text: tr('tierActive').replace('{n}', tier[1]), cls: tier[1] === '1' ? 'warn' : 'danger' }
                : a.current_status === 'RESOLVED'
                  ? { text: tr('resolved'), cls: 'ok' }
                  : { text: a.current_status || '—', cls: 'off' }
              return (
                <li key={a.id} className="notice-row">
                  <span className="notice-time mono">{fmtHhmm(a.triggered_at)}</span>
                  <span className="notice-what">{tr(a.metric_type)} {tr('overLimit')}</span>
                  {where && <span className="notice-where">{where.saloon} · {where.farm}</span>}
                  <span className="notice-value mono">{a.current_value != null ? parseFloat(a.current_value) : '—'}</span>
                  <span className={`notice-chip ${chip.cls}`}>{chip.text}</span>
                </li>
              )
            })}
          </ul>
        )}
      </section>

      {/* ── alert inbox ── */}
      <section className="inbox">
        <h2>{tr('inboxTitle')}</h2>
        {openAlerts.length === 0 && pastAlerts.length === 0 && (
          <p className="inbox-empty">{tr('inboxEmpty')}</p>
        )}
        {openAlerts.map((a) => (
          <AlertRow key={a.id} a={a} tr={tr} lang={lang} open escalation={escalationFor(a)} escalationConfig={escalationConfig} onAck={() => {
            press('ack', () => acknowledgeAlert(session.access, a.id))
          }} />
        ))}
        {pastAlerts.slice(0, 3).map((a) => (
          <AlertRow key={a.id} a={a} tr={tr} lang={lang} open={false} />
        ))}
      </section>

      {/* ── reliability (MET-ALERT-QUALITY) ── */}
      <QualityStrip quality={quality} tr={tr} />

      {/* ── alert engine: live state machines (Redis) ── */}
      <StateMachinesStrip stateMachines={stateMachines} tr={tr} />

      {/* ── promises ── */}
      <Promises tr={tr} />

      {/* ── try-it ── */}
      <section className="try">
        <h2>{tr('tryTitle')}</h2>
        <p className="strip-sub">{tr('trySub')}</p>
        <div className="try-grid">
          <button className="try-card danger" onClick={onSpike} disabled={busyBtn !== null}>
            <span className="try-title">{tr('btnSpike')}</span>
            <span className="try-note">{tr('btnSpikeNote')}</span>
            {busyBtn === 'spike' && <span className="busy">{tr('busy')}</span>}
          </button>
          <button className="try-card" onClick={onSensorOff} disabled={busyBtn !== null}>
            <span className="try-title">{tr('btnSensorOff')}</span>
            <span className="try-note">{tr('btnSensorOffNote')}</span>
            {busyBtn === 'sensoroff' && <span className="busy">{tr('busy')}</span>}
          </button>
          <button className="try-card" onClick={onAck} disabled={busyBtn !== null}>
            <span className="try-title">{tr('btnAck')}</span>
            <span className="try-note">{tr('btnAckNote')}</span>
            {busyBtn === 'ack' && <span className="busy">{tr('busy')}</span>}
          </button>
          <button className="try-card" onClick={onRecover} disabled={busyBtn !== null}>
            <span className="try-title">{tr('btnRecover')}</span>
            <span className="try-note">{tr('btnRecoverNote')}</span>
            {busyBtn === 'recover' && <span className="busy">{tr('busy')}</span>}
          </button>
        </div>
      </section>

      {toast && <div className={`toast ${toast.kind}`} role="status" aria-live="polite">{toast.text}</div>}
      </main>
      <Footer tr={tr} />
    </div>
  )
}

function Header({ lang, setLang, tr, onLogout, session }) {
  return (
    <header className="topbar">
      <a className="brand" href="#/">
        <svg viewBox="0 0 24 24" className="brand-icon" role="img" aria-label={tr('brand')}>
          <path d="M12 2l8 4v6c0 5-3.4 8.6-8 10-4.6-1.4-8-5-8-10V6l8-4z" />
        </svg>
        <div>
          <strong>{tr('brand')}</strong>
          <span className="kicker">{tr('heroKicker')}</span>
        </div>
      </a>
      <nav className="main-nav" aria-label="Pages">
        <a href="#/farms" className="nav-link">{tr('navFarms')}</a>
        <a href="#/overview" className="nav-link">{tr('overviewTitle')}</a>
        <a href="#/emqx" className="nav-link">{tr('navEmqx')}</a>
      </nav>
      <div className="top-actions">
        <button className="btn ghost" onClick={() => setLang(lang === 'fa' ? 'en' : 'fa')}>
          {tr('langBtn')}
        </button>
        {onLogout && (
          <>
            <span className="who">{session?.profile?.username}</span>
            <button className="btn ghost" onClick={onLogout}>{tr('logout')}</button>
          </>
        )}
      </div>
    </header>
  )
}

function Promises({ tr }) {
  const items = ['p1', 'p2', 'p3', 'p4']
  return (
    <section className="promises">
      <h2>{tr('promisesTitle')}</h2>
      <div className="promise-grid">
        {items.map((k) => (
          <article key={k} className="promise-card">
            <h3>{tr(`${k}Title`)}</h3>
            <p>{tr(`${k}Body`)}</p>
          </article>
        ))}
      </div>
    </section>
  )
}

function AlertRow({ a, tr, lang, open, escalation, escalationConfig, onAck }) {
  const tier = a.highest_tier_reached || 1
  const when = new Date(a.triggered_at)
  const mins = Math.max(0, Math.round((Date.now() - when.getTime()) / 60000))
  const timeLabel = mins < 1 ? tr('justNow') : `${mins} ${tr('minAgo')}`
  // Escalation countdown: seconds until the next tier promotion. Tier times
  // come from SystemConfiguration via /demo/api/farm/ (escalation block) —
  // demo seed tunes 1/2 min, production seed 15/30; never hardcoded here.
  const tier2Min = escalationConfig?.tier2_min ?? 15
  const tier3Min = escalationConfig?.tier3_min ?? 30
  let escLabel = null
  if (open && escalation) {
    const fmt = (s) => {
      const m = Math.floor(s / 60)
      const sec = Math.round(s % 60)
      return `${m}:${String(sec).padStart(2, '0')}`
    }
    if (escalation.isBreach && escalation.remaining_sec != null) {
      escLabel = tr('tier1In').replace('{t}', fmt(escalation.remaining_sec))
    } else if (escalation.elapsed_sec != null && escalation.tier < 3) {
      const nextAt = escalation.tier === 1 ? tier2Min : tier3Min
      escLabel = tr('nextTierIn').replace('{t}', fmt(Math.max(0, nextAt * 60 - escalation.elapsed_sec)))
    }
  }
  // The fuse: fraction of time left before the next escalation tier.
  // Breach phase = full fuse burning toward Tier 1; later tiers burn
  // toward the next promotion. Red flare when < 20% remains.
  let fuseInfo = null
  if (open && escalation) {
    if (escalation.isBreach && escalation.remaining_sec != null) {
      const window = Math.max(1, escalationConfig.tier1_min != null ? escalationConfig.tier1_min * 60 : 900)
      fuseInfo = { fraction: Math.min(1, escalation.remaining_sec / window), critical: escalation.remaining_sec / window < 0.2 }
    } else if (escalation.elapsed_sec != null && escalation.tier < 3) {
      const nextAt = (escalation.tier === 1 ? tier2Min : tier3Min) * 60
      fuseInfo = { fraction: Math.min(1, Math.max(0, (nextAt - escalation.elapsed_sec) / nextAt)), critical: (nextAt - escalation.elapsed_sec) / nextAt < 0.2 }
    }
  }
  return (
    <div className={`alert-row ${open ? 'open' : 'closed'}`}>
      <span className={`pill tier-${tier}`}>{tr(`tier${tier}`)}</span>
      <span className="alert-metric">{a.metric_type === 'nh3' ? tr('nh3') : a.metric_type}</span>
      <span className="alert-val">{a.current_value != null ? `${a.current_value}` : ''}</span>
      <a className="alert-saloon" href={`#/saloon/${a.saloon_id}`}>{tr('saloon')} {a.saloon_id}</a>
      {escLabel && <span className="alert-esc">{escLabel}</span>}
      <span className="alert-time">{open ? timeLabel : tr('resolved')}</span>
      {open && escalation && fuseInfo && (
        <div className="fuse" role="presentation">
          <div
            className={`fuse-bar ${fuseInfo.critical ? 'critical' : ''}`}
            style={{ width: `${Math.round(fuseInfo.fraction * 100)}%` }}
          />
        </div>
      )}
      {open && onAck && (
        <button className="btn ghost ack-btn" onClick={onAck}>{tr('ackBtn')}</button>
      )}
    </div>
  )
}

function QualityStrip({ quality, tr }) {
  if (!quality) return null
  const mttr = quality.mttr_seconds_by_tier || {}
  const ack = quality.ack_to_resolve_seconds || {}
  const flocks = quality.alerts_per_flock || {}
  const fmt = (s) => {
    if (s == null) return '—'
    const m = Math.floor(s / 60)
    const sec = Math.round(s % 60)
    return m > 0 ? `${m}m ${sec}s` : `${sec}s`
  }
  const tierRow = (tier) => mttr[tier] != null ? (
    <span key={tier} className="q-chip">{tr(`tier${tier}`)}: {fmt(mttr[tier]?.mean_seconds ?? mttr[tier])}</span>
  ) : null
  const ackMean = ack.acked?.mean_seconds ?? ack.acked ?? null
  const unackedMean = ack.not_acked?.mean_seconds ?? ack.not_acked ?? null
  const flockCount = Array.isArray(flocks) ? flocks.length : (flocks.count ?? null)
  return (
    <section className="quality">
      <h2>{tr('qualityTitle')}</h2>
      <div className="quality-grid">
        <div className="quality-card">
          <h3>{tr('qMttr')}</h3>
          <div className="q-chips">{[1, 2, 3].map(tierRow)}</div>
        </div>
        <div className="quality-card">
          <h3>{tr('qAck')}</h3>
          <p>{tr('qAckAcked')}: {fmt(ackMean)} · {tr('qAckNot')}: {fmt(unackedMean)}</p>
        </div>
        <div className="quality-card">
          <h3>{tr('qFlocks')}</h3>
          <p>{flockCount != null ? `${flockCount}` : '—'}</p>
        </div>
      </div>
    </section>
  )
}

function StateMachinesStrip({ stateMachines, tr }) {
  const families = Object.entries(stateMachines || {})
  const hasAny = families.some(([, entries]) => entries.length > 0)
  return (
    <section className="quality">
      <h2>{tr('stateTitle')}</h2>
      <p className="strip-sub">{tr('stateSub')}</p>
      {!hasAny ? (
        <p className="inbox-empty">{tr('stateEmpty')}</p>
      ) : (
        <div className="quality-grid">
          {families.map(([family, entries]) =>
            entries.length > 0 ? (
              <div key={family} className="quality-card">
                <h3>{family}</h3>
                <div className="q-chips">
                  {entries.slice(0, 6).map((e) => (
                    <span key={e.key} className="q-chip" title={e.value}>
                      {e.key.split(':').slice(-2).join(':')}: {e.value.length > 24 ? e.value.slice(0, 24) + '…' : e.value}
                    </span>
                  ))}
                  {entries.length > 6 && <span className="q-chip">+{entries.length - 6}</span>}
                </div>
              </div>
            ) : null,
          )}
        </div>
      )}
    </section>
  )
}

function Footer({ tr }) {
  return (
    <footer className="foot">
      <span>{tr('footerNote')}</span>
      <a href="/demo/console/">{tr('footerTech')}</a>
    </footer>
  )
}
