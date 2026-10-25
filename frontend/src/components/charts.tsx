import { useState } from 'react'

export interface Segment {
  key: string
  label: string
  value: number
  color: string
}

/** One horizontal stacked bar. 2px surface gap between fills, hover tooltip. */
export function StackedBar({ segments, height = 12, total }: { segments: Segment[]; height?: number; total?: number }) {
  const [hover, setHover] = useState<string | null>(null)
  const sum = total ?? segments.reduce((acc, s) => acc + s.value, 0)
  const visible = segments.filter((s) => s.value > 0)
  return (
    <div className="relative">
      <div className="flex w-full gap-[3px] overflow-hidden rounded-full" style={{ height }}>
        {sum === 0 && <div className="hatch h-full w-full rounded-full bg-slate-100" />}
        {visible.map((s) => (
          <div
            key={s.key}
            onMouseEnter={() => setHover(s.key)}
            onMouseLeave={() => setHover(null)}
            className="h-full rounded-full transition-opacity"
            style={{ width: `${(s.value / Math.max(sum, 1)) * 100}%`, background: s.color, opacity: hover && hover !== s.key ? 0.45 : 1 }}
          />
        ))}
      </div>
      {hover && (
        <div className="pointer-events-none absolute -top-8 left-0 z-10 rounded-md bg-ink-900 px-2 py-1 text-[11px] text-white shadow">
          {segments.find((s) => s.key === hover)?.label}: <span className="font-semibold">{segments.find((s) => s.key === hover)?.value}</span>
        </div>
      )}
    </div>
  )
}

export function Legend({ items }: { items: { label: string; color: string; value?: number | string }[] }) {
  return (
    <div className="flex flex-wrap gap-x-4 gap-y-1 text-xs text-slate-600">
      {items.map((item) => (
        <span key={item.label} className="inline-flex items-center gap-1.5">
          <span className="h-2.5 w-2.5 rounded-full" style={{ background: item.color }} />
          {item.label}
          {item.value !== undefined && <span className="font-semibold tabular-nums text-slate-900">{item.value}</span>}
        </span>
      ))}
    </div>
  )
}

/** A labelled horizontal bar, value on the right. Single series: no legend. */
export function MeterRow({ label, value, max, display, color = 'var(--color-series-1)', sub }: { label: string; value: number; max: number; display?: string; color?: string; sub?: string }) {
  return (
    <div className="group">
      <div className="mb-1 flex items-baseline justify-between gap-3 text-xs">
        <span className="font-medium text-slate-700">{label}</span>
        <span className="tabular-nums text-slate-900">
          {display ?? value}
          {sub && <span className="ml-1 text-slate-400">{sub}</span>}
        </span>
      </div>
      <div className="hatch h-2 w-full rounded-full bg-slate-100" title={`${label}: ${display ?? value}`}>
        <div className="h-full rounded-full transition-all duration-700" style={{ width: `${max ? Math.max(1.5, (value / max) * 100) : 0}%`, background: color }} />
      </div>
    </div>
  )
}

/** Ring gauge for the 0-100 review-priority score, with the references' tick ring. */
export function ScoreGauge({ score, color }: { score: number; color: string }) {
  const r = 58
  const circumference = 2 * Math.PI * r
  const filled = (Math.min(100, Math.max(0, score)) / 100) * circumference
  return (
    <svg viewBox="0 0 160 160" className="h-44 w-44" role="img" aria-label={`Review priority score ${score} of 100`}>
      {Array.from({ length: 60 }).map((_, i) => {
        const angle = (i / 60) * Math.PI * 2 - Math.PI / 2
        const on = i / 60 <= score / 100
        return <line key={i} x1={80 + Math.cos(angle) * 70} y1={80 + Math.sin(angle) * 70} x2={80 + Math.cos(angle) * 75} y2={80 + Math.sin(angle) * 75} stroke={on ? '#18181a' : '#d6d6d1'} strokeWidth="1.3" />
      })}
      <circle cx="80" cy="80" r={r} fill="none" stroke="#efefec" strokeWidth="11" />
      <circle cx="80" cy="80" r={r} fill="none" stroke={color} strokeWidth="11" strokeLinecap="round" strokeDasharray={`${Math.max(filled, 0.001)} ${circumference}`} transform="rotate(-90 80 80)" style={{ transition: 'stroke-dasharray 1s ease' }} />
      <text x="80" y="86" textAnchor="middle" className="num fill-slate-900" style={{ fontSize: 40, fontWeight: 300 }}>
        {Math.round(score)}
      </text>
      <text x="80" y="104" textAnchor="middle" className="fill-slate-400" style={{ fontSize: 9 }}>
        of 100
      </text>
    </svg>
  )
}
