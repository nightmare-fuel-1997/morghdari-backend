// Tiny SVG sparkline — zero dependencies, matches the "no chart library" rule.

export default function Sparkline({ points, width = 220, height = 48, color = '#38bdf8', threshold = null }) {
  const vals = points.map((p) => p.y).filter((v) => v != null)
  if (vals.length === 0) {
    return <div className="spark-empty">—</div>
  }
  let min = Math.min(...vals)
  let max = Math.max(...vals)
  const span = max - min || 1
  min -= span * 0.25
  max += span * 0.25
  if (threshold != null) {
    if (threshold < min) min = threshold - span * 0.15
    if (threshold > max) max = threshold + span * 0.15
  }
  const W = width
  const H = height
  const x = (i) => 2 + (i / Math.max(points.length - 1, 1)) * (W - 4)
  const y = (v) => H - 3 - ((v - min) / (max - min)) * (H - 6)
  const d = points.map((p, i) => `${i === 0 ? 'M' : 'L'}${x(i).toFixed(1)},${y(p.y).toFixed(1)}`).join(' ')
  const tY = threshold != null ? y(threshold) : null
  return (
    <svg width={W} height={H} viewBox={`0 0 ${W} ${H}`} className="spark" aria-hidden="true" focusable="false">
      {threshold != null && (
        <line x1="0" x2={W} y1={tY} y2={tY} className="spark-threshold" />
      )}
      <path d={d} fill="none" stroke={color} strokeWidth="2" strokeLinejoin="round" strokeLinecap="round" />
    </svg>
  )
}
