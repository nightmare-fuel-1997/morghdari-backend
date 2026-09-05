// FlockDial — the farm's instrument face. Poultry people count in flock days:
// the day number sets the temperature target, the feed curve, everything. So
// each farm wears a small dial ringed in five-day ticks, filled to today.
// The arc sweeps in once as the card lands — one quiet moment, then it's a
// static instrument (no looping motion), and reduced-motion users get the
// fully-drawn dial instantly via the global reduced-motion rule.
import { useId } from 'react'

export default function FlockDial({ day, cycleDays, tr, index = 0 }) {
  // Final day (or overrun): the ring finishes full, the lamp dims, and the
  // legend becomes the harvest note — a completed cycle, not a stopped one.
  const complete = day >= cycleDays
  const dayA11y = complete ? 'flockHarvestA11y' : 'flockDayA11y'
  const title = tr(dayA11y).replace('{d}', day).replace('{c}', cycleDays)
  const frac = Math.min(1, Math.max(0, day / cycleDays))
  const ticks = []
  for (let i = 0; i < 9; i++) {
    const a = (i / 9) * Math.PI * 2 - Math.PI / 2 // five-day marks, 12 o'clock start
    ticks.push(
      <line
        key={i}
        x1={32 + Math.cos(a) * 28.5}
        y1={32 + Math.sin(a) * 28.5}
        x2={32 + Math.cos(a) * 25.5}
        y2={32 + Math.sin(a) * 25.5}
        className="flock-dial-tick"
      />,
    )
  }
  return (
    <svg
      viewBox="0 0 64 64"
      width={64}
      height={64}
      className={`flock-dial${complete ? ' complete' : ''}`}
      role="img"
      aria-label={title}
      title={title}
      style={{ '--dial-delay': `${Math.min(index * 80, 320) + 320}ms` }}
    >
      <circle cx="32" cy="32" r="24" className="flock-dial-track" />
      <circle
        cx="32"
        cy="32"
        r="24"
        pathLength="1"
        transform="rotate(-90 32 32)"
        className="flock-dial-arc"
        style={{ strokeDasharray: `${frac} 1` }}
      />
      {ticks}
      <text x="32" y="28" textAnchor="middle" className="flock-dial-day">{day}</text>
      <text x="32" y="41" textAnchor="middle" className="flock-dial-cycle">
        {complete ? tr('flockHarvest') : tr('flockOfCycle').replace('{c}', cycleDays)}
      </text>
    </svg>
  )
}