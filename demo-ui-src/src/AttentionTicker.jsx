// Attention ticker — the plain-language strip that appears ONLY when a
// reading is over its safe limit. Shared by the boss board (calm landing)
// and the engineering Overview; both consume the same /demo/api/farm/
// payload and compute attention identically.
//
// It feels like brief notifications, not a permanent list: each item lives
// TICKER_MS, then cross-fades out. It also fades out early when the breach
// resolves (the strip mirrors the real air, without a stale list). With
// `link`, each item navigates to its saloon page.
import { useEffect, useRef, useState } from 'react'

const NH3_MAX = 25
const TEMP_MAX = 30
const TICKER_MS = 8000
const TICKER_FADE_MS = 450

// One reading per saloon: mean of the latest real readings across slots, but
// the WORST slot's value when that slot is breaching — a single bad sensor
// must stay visible, never averaged away.
export function saloonReading(saloon) {
  const temps = []
  const nh3s = []
  let anyData = false
  let anyOnline = false
  let tempMax = null
  let nh3Max = null
  for (const slot of saloon.slots || []) {
    if (slot.online) anyOnline = true
    const m = slot.last_metrics
    if (!m) continue
    anyData = true
    if (m.temp && m.temp.avg != null) {
      temps.push(m.temp.avg)
      tempMax = tempMax == null ? m.temp.avg : Math.max(tempMax, m.temp.avg)
    }
    if (m.nh3 && m.nh3.avg != null) {
      nh3s.push(m.nh3.avg)
      nh3Max = nh3Max == null ? m.nh3.avg : Math.max(nh3Max, m.nh3.avg)
    }
  }
  const mean = (a) => (a.length ? a.reduce((x, y) => x + y, 0) / a.length : null)
  const tempBreach = tempMax != null && tempMax >= TEMP_MAX
  const nh3Breach = nh3Max != null && nh3Max >= NH3_MAX
  const attention = tempBreach || nh3Breach
  return {
    temp: attention && tempBreach ? tempMax : mean(temps),
    nh3: attention && nh3Breach ? nh3Max : mean(nh3s),
    tempMax,
    nh3Max,
    anyData,
    anyOnline,
    attention,
    tempBreach,
    nh3Breach,
  }
}

export function groupSaloons(farm) {
  if (Array.isArray(farm?.farms) && farm.farms.length) return farm.farms
  // Fallback: derive the grouping from the flat list (older backend).
  const map = new Map()
  for (const s of farm?.saloons || []) {
    const key = s.farm_id ?? 0
    const g = map.get(key) || { id: key, name: s.farm_name || '—', saloon_count: 0, online_slots: 0, dead_packs: 0, saloons: [] }
    g.saloon_count += 1
    g.online_slots += (s.slots || []).filter((x) => x.online).length
    g.dead_packs += (s.slots || []).filter((x) => x.lwt === 'offline').length
    g.saloons.push(s)
    map.set(key, g)
  }
  return [...map.values()].sort((a, b) => (a.id ?? 0) - (b.id ?? 0))
}

// Narrow the groups down to ONE saloon's own attention items (used by the
// per-saloon and per-sensor pages: same ticker, filtered view).
export function saloonGroups(groups, saloonId) {
  const out = []
  for (const g of groups || []) {
    const sals = (g.saloons || []).filter((s) => s.id === saloonId)
    if (sals.length) out.push({ ...g, saloons: sals })
  }
  return out
}

// Plain-language attention list — appears ONLY when something is over limit.
export function attentionItems(groups) {
  const items = []
  for (const g of groups) {
    for (const s of g.saloons) {
      const r = saloonReading(s)
      if (!r.anyData) continue
      if (r.tempBreach) items.push({ id: s.id, farm: g.name, saloon: s.name, metric: 'temp', value: r.tempMax })
      if (r.nh3Breach) items.push({ id: s.id, farm: g.name, saloon: s.name, metric: 'nh3', value: r.nh3Max })
    }
  }
  return items
}

// Converts the (recomputed-every-poll) attention list into a self-dismissing
// toast list keyed by saloon+metric, so a saloon is never shown twice in a
// row while it stays breaching — one notification per breach episode. The
// effect is keyed on a stable signature, so the 5s poll and the 1s clock
// never re-run it; only a real attention change (new breach / resolved
// breach) does.
function useTicker(attention) {
  const [toasts, setToasts] = useState([])
  const timersRef = useRef(new Map())
  const activeRef = useRef(new Set())
  const attentionRef = useRef(attention)
  attentionRef.current = attention

  const sig = attention
    .map((it) => `${it.id}|${it.metric}`)
    .sort()
    .join(';')

  useEffect(() => {
    const items = attentionRef.current
    const active = new Set(items.map((it) => `${it.id}|${it.metric}`))
    const activeSet = activeRef.current

    // Mark toasts whose breach has ended as leaving (early dismiss), and
    // forget them as "active" so a NEW episode can notify again later.
    setToasts((prev) => {
      const next = []
      for (const t of prev) {
        if (t.leaving) {
          // Still fading out; free the key once the breach is gone so a
          // future episode of the same saloon can notify again.
          if (!active.has(t.key)) activeSet.delete(t.key)
          next.push(t)
          continue
        }
        if (active.has(t.key)) {
          next.push(t)
        } else {
          activeSet.delete(t.key)
          clearTimeout(timersRef.current.get(t.key))
          timersRef.current.delete(t.key)
          const leave = { ...t, leaving: true }
          next.push(leave)
          const rm = setTimeout(() => {
            setToasts((cur) => cur.filter((x) => x !== leave))
            timersRef.current.delete(t.key)
          }, TICKER_FADE_MS)
          timersRef.current.set(t.key, rm)
        }
      }
      return next
    })

    // New breaches enter the ticker, then auto-dismiss after TICKER_MS.
    for (const it of items) {
      const key = `${it.id}|${it.metric}`
      if (activeSet.has(key)) continue
      activeSet.add(key)
      const toast = { key, leaving: false, ...it }
      setToasts((prev) => [...prev, toast])
      const t = setTimeout(() => {
        setToasts((cur) => {
          const target = cur.find((x) => x.key === key && !x.leaving)
          if (!target) return cur
          const leavingToast = { ...target, leaving: true }
          const rm = setTimeout(() => {
            setToasts((c) => c.filter((x) => x !== leavingToast))
            timersRef.current.delete(key)
          }, TICKER_FADE_MS)
          timersRef.current.set(key, rm)
          return cur.map((x) => (x === target ? leavingToast : x))
        })
      }, TICKER_MS)
      timersRef.current.set(key, t)
    }
  }, [sig])

  useEffect(() => () => {
    for (const t of timersRef.current.values()) clearTimeout(t)
    timersRef.current.clear()
  }, [])

  return toasts
}

export default function AttentionTicker({ groups, tr, link = false }) {
  const toasts = useTicker(attentionItems(groups))
  if (toasts.length === 0) return null
  return (
    <div className="attention-strip" role="status" aria-live="polite">
      {toasts.map((it, i) => {
        const inner = (
          <>
            <span className="attention-dot" aria-hidden="true" />
            <span>
              {tr(it.metric)} {tr('overLimit')} — {it.saloon} · {it.farm} ({it.value})
            </span>
          </>
        )
        const delay = { '--enter-delay': `${Math.min(i * 90, 360)}ms` }
        // Accessible target: the link's label names the saloon BEFORE the
        // issue so keyboard/screen-reader users know the destination, and
        // the title gives pointer users the same preview on hover.
        const targetLabel = `${it.saloon} · ${it.farm}: ${tr(it.metric)} ${tr('overLimit')} (${it.value})`
        return link ? (
          <a
            key={it.key}
            href={`#/saloon/${it.id}`}
            className={`attention-item attention-link${it.leaving ? ' leaving' : ''}`}
            style={delay}
            aria-label={targetLabel}
            title={targetLabel}
          >
            {inner}
          </a>
        ) : (
          <div key={it.key} className={`attention-item${it.leaving ? ' leaving' : ''}`} style={delay}>
            {inner}
          </div>
        )
      })}
    </div>
  )
}