// Full SVG line chart — zero dependencies, matches the "no chart library" rule.
// Axes, gridlines, threshold lines, hover tooltip. Points: [{x, y|null}].
import { useState } from 'react'

const PAD = { l: 46, r: 14, t: 12, b: 24 }

export default function LineChart({
  series,               // [{name, color, points: [{x, y|null}]}]
  width = 720,
  height = 260,
  threshold = null,     // {value, label, color}
  yLabel = '',
  xFormat = (v) => String(v),
  yFormat = (v) => v.toFixed(1),
}) {
  const [hover, setHover] = useState(null)
  const all = series.flatMap((s) => s.points.filter((p) => p.y != null).map((p) => p.y))
  if (all.length === 0) {
    return <div className="chart-empty">—</div>
  }
  let min = Math.min(...all)
  let max = Math.max(...all)
  if (threshold != null && threshold.value != null) {
    min = Math.min(min, threshold.value)
    max = Math.max(max, threshold.value)
  }
  const span = max - min || 1
  min -= span * 0.12
  max += span * 0.12
  const xs = series.flatMap((s) => s.points.map((p) => p.x))
  const xMin = Math.min(...xs)
  const xMax = Math.max(...xs)
  const xSpan = xMax - xMin || 1
  const W = width - PAD.l - PAD.r
  const H = height - PAD.t - PAD.b
  const x = (v) => PAD.l + ((v - xMin) / xSpan) * W
  const y = (v) => PAD.t + H - ((v - min) / (max - min)) * H

  const ticks = 4
  const yTicks = Array.from({ length: ticks + 1 }, (_, i) => min + ((max - min) / ticks) * i)
  const xTickCount = 6
  const xTicks = Array.from({ length: xTickCount + 1 }, (_, i) => xMin + (xSpan / xTickCount) * i)

  const gridLines = yTicks.map((v, i) => (
    <g key={`y${i}`}>
      <line x1={PAD.l} x2={PAD.l + W} y1={y(v)} y2={y(v)} className="chart-grid" />
      <text x={PAD.l - 6} y={y(v) + 3} textAnchor="end" className="chart-tick">{yFormat(v)}</text>
    </g>
  ))
  const xTickEls = xTicks.map((v, i) => (
    <text key={`x${i}`} x={x(v)} y={height - 6} textAnchor="middle" className="chart-tick">{xFormat(v)}</text>
  ))

  const hoverIdx = hover != null ? hover.i : null

  return (
    <div className="chart-wrap" style={{ position: 'relative' }}>
      <svg
        width={width} height={height} viewBox={`0 0 ${width} ${height}`}
        className="chart" role="img" aria-label={yLabel}
        onMouseLeave={() => setHover(null)}
      >
        {gridLines}
        {xTickEls}
        {threshold != null && threshold.value != null && (
          <g>
            <line x1={PAD.l} x2={PAD.l + W} y1={y(threshold.value)} y2={y(threshold.value)}
              stroke={threshold.color || 'var(--danger)'} strokeWidth="1.5" strokeDasharray="6 4" />
            {threshold.label && (
              <text x={PAD.l + W - 4} y={y(threshold.value) - 5} textAnchor="end"
                fill={threshold.color || 'var(--danger)'} className="chart-threshold-label">
                {threshold.label}
              </text>
            )}
          </g>
        )}
        {series.map((s, si) => {
          const d = s.points
            .map((p, i) => (p.y == null ? null : `${i === 0 || s.points[i - 1]?.y == null ? 'M' : 'L'}${x(p.x).toFixed(1)},${y(p.y).toFixed(1)}`))
            .filter(Boolean).join(' ')
          return (
            <g key={si}>
              <path d={d} fill="none" stroke={s.color} strokeWidth="2" strokeLinejoin="round" strokeLinecap="round" />
              {hoverIdx != null && s.points[hoverIdx]?.y != null && (
                <circle cx={x(s.points[hoverIdx].x)} cy={y(s.points[hoverIdx].y)} r="4" fill={s.color} />
              )}
            </g>
          )
        })}
        {hoverIdx != null && (
          <line x1={x(series[0].points[hoverIdx]?.x)} x2={x(series[0].points[hoverIdx]?.x)}
            y1={PAD.t} y2={PAD.t + H} className="chart-cursor" />
        )}
        <rect x={PAD.l} y={PAD.t} width={W} height={H} fill="transparent"
          onMouseMove={(e) => {
            const rect = e.currentTarget.getBoundingClientRect()
            const px = e.clientX - rect.left
            const frac = px / W
            const i = Math.round(frac * (series[0]?.points.length - 1 || 0))
            if (i >= 0 && i < (series[0]?.points.length || 0)) setHover({ i })
          }}
        />
      </svg>
      {hoverIdx != null && (
        <div className="chart-tooltip" style={{
          left: Math.min(x(series[0].points[hoverIdx].x) + 12, width - 150),
          top: PAD.t + 8,
        }}>
          <div className="chart-tooltip-x">{xFormat(series[0].points[hoverIdx].x)}</div>
          {series.map((s) => (
            <div key={s.name} className="chart-tooltip-row">
              <span className="chart-tooltip-dot" style={{ background: s.color }} />
              <span>{s.name}</span>
              <span className="chart-tooltip-val">
                {s.points[hoverIdx]?.y != null ? yFormat(s.points[hoverIdx].y) : '—'}
              </span>
            </div>
          ))}
        </div>
      )}
      {yLabel && <div className="chart-ylabel">{yLabel}</div>}
    </div>
  )
}
