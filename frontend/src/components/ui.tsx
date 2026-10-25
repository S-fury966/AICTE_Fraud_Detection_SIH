import { clsx } from 'clsx'
import { ArrowUpRight, CircleDashed, Loader2, XCircle } from 'lucide-react'
import { useEffect, useRef, useState, type ReactNode } from 'react'
import { objectUrlWithIdentity } from '../lib/api'
import { display, humanize } from '../lib/format'
import type { RiskLevel, Strength } from '../lib/types'

export function cn(...parts: Parameters<typeof clsx>) {
  return clsx(...parts)
}

export function Card({ className, children }: { className?: string; children: ReactNode }) {
  return <div className={cn('card', className)}>{children}</div>
}

export function CardHeader({ title, subtitle, action, icon }: { title: ReactNode; subtitle?: ReactNode; action?: ReactNode; icon?: ReactNode }) {
  return (
    <div className="flex items-start justify-between gap-4 px-6 pt-5 pb-3">
      <div className="flex items-start gap-3">
        {icon && <div className="mt-0.5 flex h-8 w-8 shrink-0 items-center justify-center rounded-full bg-slate-100 text-slate-700">{icon}</div>}
        <div>
          <h3 className="font-display text-[17px] font-medium tracking-tight text-slate-900">{title}</h3>
          {subtitle && <p className="mt-0.5 text-xs leading-relaxed text-slate-500">{subtitle}</p>}
        </div>
      </div>
      {action}
    </div>
  )
}

/** The round "open" affordance from the references. */
export function CornerArrow({ onClick, title }: { onClick?: () => void; title?: string }) {
  return (
    <button onClick={onClick} title={title} className="focus-ring flex h-9 w-9 shrink-0 items-center justify-center rounded-full bg-white text-slate-700 shadow-sm ring-1 ring-black/5 transition hover:bg-ink-900 hover:text-white">
      <ArrowUpRight className="h-4 w-4" />
    </button>
  )
}

export function Button({
  children,
  onClick,
  variant = 'primary',
  size = 'md',
  disabled,
  type = 'button',
  className,
  title,
}: {
  children: ReactNode
  onClick?: () => void
  variant?: 'primary' | 'secondary' | 'ghost' | 'danger' | 'success' | 'butter' | 'iris'
  size?: 'sm' | 'md'
  disabled?: boolean
  type?: 'button' | 'submit'
  className?: string
  title?: string
}) {
  return (
    <button
      type={type}
      onClick={onClick}
      disabled={disabled}
      title={title}
      className={cn(
        'focus-ring inline-flex items-center justify-center gap-1.5 rounded-full font-medium transition active:scale-[0.98] disabled:cursor-not-allowed disabled:opacity-50',
        size === 'sm' ? 'px-3 py-1.5 text-xs' : 'px-5 py-2.5 text-sm',
        variant === 'primary' && 'bg-ink-900 text-white hover:bg-ink-700',
        variant === 'secondary' && 'bg-white text-slate-800 shadow-sm ring-1 ring-black/10 hover:bg-slate-50',
        variant === 'ghost' && 'text-slate-600 hover:bg-black/5',
        variant === 'danger' && 'bg-white text-red-700 ring-1 ring-red-200 hover:bg-red-50',
        variant === 'success' && 'bg-white text-emerald-800 ring-1 ring-emerald-200 hover:bg-emerald-50',
        variant === 'butter' && 'bg-butter-500 text-ink-900 hover:bg-butter-400',
        variant === 'iris' && 'bg-iris-600 text-white hover:bg-iris-500',
        className,
      )}
    >
      {children}
    </button>
  )
}

export function Spinner({ label }: { label?: string }) {
  return (
    <div className="flex items-center justify-center gap-2 py-16 text-sm text-slate-500">
      <Loader2 className="h-4 w-4 animate-spin" /> {label ?? 'Loading…'}
    </div>
  )
}

export function ErrorBox({ error }: { error: unknown }) {
  const message = error instanceof Error ? error.message : String(error)
  return (
    <div className="flex items-start gap-3 rounded-3xl bg-white/85 px-5 py-4 text-sm text-red-800 ring-1 ring-red-200">
      <XCircle className="mt-0.5 h-4 w-4 shrink-0" />
      <div>
        <div className="font-medium">Could not load from the API</div>
        <div className="mt-0.5 text-red-700">{message}</div>
        <div className="mt-1 text-xs text-slate-500">
          Is the backend running? <code className="font-mono">cd backend && python -m app.api</code>
        </div>
      </div>
    </div>
  )
}

export function Empty({ title, children, icon }: { title: string; children?: ReactNode; icon?: ReactNode }) {
  return (
    <div className="flex flex-col items-center justify-center px-6 py-12 text-center">
      <div className="mb-3 text-slate-300">{icon ?? <CircleDashed className="h-8 w-8" />}</div>
      <div className="font-display text-base font-medium text-slate-800">{title}</div>
      {children && <div className="mt-1 max-w-md text-xs text-slate-500">{children}</div>}
    </div>
  )
}

// ── Status pills. A coloured dot plus a label: colour never carries meaning alone.

export const RISK_DOT: Record<RiskLevel, string> = {
  high: 'var(--color-critical)',
  medium: 'var(--color-warning)',
  low: 'var(--color-good)',
}

function Dot({ color, pulse }: { color: string; pulse?: boolean }) {
  return (
    <span className="relative inline-flex h-2 w-2 shrink-0">
      {pulse && <span className="absolute inline-flex h-full w-full animate-ping rounded-full opacity-60" style={{ background: color }} />}
      <span className="relative inline-flex h-2 w-2 rounded-full" style={{ background: color }} />
    </span>
  )
}

export function RiskBadge({ level, size = 'md' }: { level: RiskLevel | null | undefined; size?: 'sm' | 'md' | 'lg' }) {
  if (!level) return <span className="text-xs text-slate-400">Not scored</span>
  return (
    <span
      className={cn(
        'inline-flex shrink-0 items-center gap-2 whitespace-nowrap rounded-full font-medium',
        level === 'high' ? 'bg-ink-900 text-white' : 'bg-white text-slate-800 ring-1 ring-black/10',
        size === 'sm' && 'px-2.5 py-1 text-[11px]',
        size === 'md' && 'px-3 py-1.5 text-xs',
        size === 'lg' && 'px-4 py-2 text-sm',
      )}
    >
      <Dot color={RISK_DOT[level]} pulse={level === 'high'} />
      {level.charAt(0).toUpperCase() + level.slice(1)} priority
    </span>
  )
}

export const STRENGTH_COLOR: Record<Strength, string> = {
  strong: 'var(--color-critical)',
  medium: 'var(--color-serious)',
  weak: '#b4b4ae',
}

export function StrengthBadge({ strength }: { strength: Strength | string }) {
  const color = STRENGTH_COLOR[strength as Strength] ?? STRENGTH_COLOR.weak
  return (
    <span
      className={cn(
        'inline-flex shrink-0 items-center gap-1.5 whitespace-nowrap rounded-full px-2 py-0.5 text-[10px] font-semibold uppercase tracking-wide',
        strength === 'strong' ? 'bg-ink-900 text-white' : strength === 'medium' ? 'bg-orange-50 text-orange-900 ring-1 ring-orange-200' : 'bg-slate-100 text-slate-600',
      )}
    >
      <span className="h-1.5 w-1.5 rounded-full" style={{ background: color }} />
      {strength}
    </span>
  )
}

const VERIFICATION_DOT: Record<string, string> = {
  verified: 'var(--color-good)',
  mismatch_detected: 'var(--color-critical)',
  expired: 'var(--color-serious)',
  verification_pending: 'var(--color-iris-500)',
  source_unavailable: '#a3a39e',
  manual_review_required: 'var(--color-warning)',
}

export function VerificationBadge({ status }: { status: string | null | undefined }) {
  if (!status) {
    return <span className="inline-flex items-center gap-1.5 whitespace-nowrap rounded-full bg-white px-2.5 py-1 text-[11px] text-slate-500 ring-1 ring-black/10">No status claimed</span>
  }
  return (
    <span className="inline-flex items-center gap-1.5 whitespace-nowrap rounded-full bg-white px-2.5 py-1 text-[11px] font-medium text-slate-800 ring-1 ring-black/10">
      <Dot color={VERIFICATION_DOT[status] ?? '#a3a39e'} />
      {humanize(status)}
    </span>
  )
}

const REVIEW_STYLE: Record<string, string> = {
  open: 'bg-slate-100 text-slate-700',
  confirmed: 'bg-red-50 text-red-800',
  dismissed: 'bg-slate-100 text-slate-400 line-through',
  needs_more_info: 'bg-iris-100 text-iris-600',
}

export function ReviewBadge({ status }: { status: string }) {
  return <span className={cn('whitespace-nowrap rounded-full px-2 py-0.5 text-[10px] font-semibold uppercase tracking-wide', REVIEW_STYLE[status] ?? REVIEW_STYLE.open)}>{humanize(status)}</span>
}

export function StageDot({ status }: { status: string }) {
  const color = status === 'ok' ? 'bg-emerald-500' : status === 'degraded' ? 'bg-amber-400' : status === 'skipped' ? 'bg-slate-300' : 'bg-red-500'
  return <span className={cn('inline-block h-2 w-2 rounded-full', color)} />
}

/** Filled to the score, hatched for the remainder (the references' "not yet"). */
export function ScoreBar({ score, level, className }: { score: number; level?: RiskLevel | null; className?: string }) {
  const color = level === 'high' ? 'var(--color-ink-900)' : level === 'medium' ? 'var(--color-butter-500)' : 'var(--color-good)'
  return (
    <div className={cn('hatch h-2 w-full overflow-hidden rounded-full bg-slate-100', className)}>
      <div className="h-full rounded-full transition-all duration-700" style={{ width: `${Math.max(3, Math.min(100, score))}%`, background: color }} />
    </div>
  )
}

/** Segmented pill control; the active tab is the dark pill. */
export function Tabs<T extends string>({ tabs, active, onChange }: { tabs: { id: T; label: string; count?: number; icon?: ReactNode }[]; active: T; onChange: (id: T) => void }) {
  return (
    <div className="inline-flex max-w-full gap-1 overflow-x-auto rounded-full bg-white/70 p-1 ring-1 ring-black/5">
      {tabs.map((tab) => (
        <button
          key={tab.id}
          onClick={() => onChange(tab.id)}
          className={cn(
            'focus-ring flex items-center gap-2 whitespace-nowrap rounded-full px-4 py-2 text-sm font-medium transition',
            active === tab.id ? 'bg-ink-900 text-white shadow' : 'text-slate-600 hover:bg-black/5 hover:text-slate-900',
          )}
        >
          {tab.icon}
          {tab.label}
          {tab.count !== undefined && (
            <span className={cn('rounded-full px-1.5 text-[10px] font-semibold', active === tab.id ? 'bg-butter-500 text-ink-900' : 'bg-slate-100 text-slate-600')}>{tab.count}</span>
          )}
        </button>
      ))}
    </div>
  )
}

/** Evidence, rendered as key/value rows with nested objects expandable. */
export function EvidenceView({ evidence }: { evidence: Record<string, unknown> | null | undefined }) {
  if (!evidence || Object.keys(evidence).length === 0) return <div className="text-xs text-slate-400">No evidence recorded.</div>
  return (
    <dl className="grid grid-cols-[minmax(120px,max-content)_1fr] gap-x-4 gap-y-1.5 text-xs">
      {Object.entries(evidence).map(([key, value]) => (
        <EvidenceRow key={key} name={key} value={value} />
      ))}
    </dl>
  )
}

function EvidenceRow({ name, value }: { name: string; value: unknown }) {
  const [open, setOpen] = useState(false)
  const complex = value !== null && typeof value === 'object' && !(Array.isArray(value) && value.every((v) => typeof v !== 'object'))
  return (
    <>
      <dt className="font-medium text-slate-500">{humanize(name)}</dt>
      <dd className="min-w-0 break-words font-mono text-[11px] text-slate-800">
        {complex ? (
          <>
            <button className="text-iris-600 hover:underline" onClick={() => setOpen(!open)}>
              {open ? 'hide' : `show ${Array.isArray(value) ? `${value.length} item(s)` : 'details'}`}
            </button>
            {open && <pre className="mt-1 max-h-72 overflow-auto rounded-xl bg-slate-50 p-2 text-[11px] leading-relaxed">{JSON.stringify(value, null, 2)}</pre>}
          </>
        ) : (
          display(value)
        )}
      </dd>
    </>
  )
}

/** Counts up to `value` once, on mount. Non-numbers render as given. */
export function CountUp({ value, decimals = 0, suffix = '' }: { value: number; decimals?: number; suffix?: string }) {
  const [shown, setShown] = useState(0)
  const started = useRef(false)
  useEffect(() => {
    if (started.current) {
      setShown(value)
      return
    }
    started.current = true
    const begin = performance.now()
    let frame = 0
    const tick = (now: number) => {
      const t = Math.min(1, (now - begin) / 900)
      setShown(value * (1 - Math.pow(1 - t, 3)))
      if (t < 1) frame = requestAnimationFrame(tick)
    }
    frame = requestAnimationFrame(tick)
    return () => cancelAnimationFrame(frame)
  }, [value])
  return (
    <>
      {shown.toFixed(decimals)}
      {suffix}
    </>
  )
}

export function Stat({ label, value, hint, accent }: { label: string; value: ReactNode; hint?: ReactNode; accent?: ReactNode }) {
  return (
    <Card className="p-5">
      <div className="flex items-center justify-between">
        <div className="text-sm text-slate-500">{label}</div>
        {accent}
      </div>
      <div className="num mt-3 text-4xl font-light text-slate-900">{value}</div>
      {hint && <div className="mt-1.5 text-xs text-slate-500">{hint}</div>}
    </Card>
  )
}

/** An image fetched with the reviewer's identity, so the view is audited under their name. */
export function SecureImage({ src, alt, className }: { src: string; alt: string; className?: string }) {
  // Keyed by src, so a stale image never shows for a new one.
  const [loaded, setLoaded] = useState<{ src: string; url: string | null } | null>(null)
  useEffect(() => {
    let revoked = false
    let objectUrl: string | null = null
    objectUrlWithIdentity(src)
      .then((u) => {
        objectUrl = u
        if (revoked) URL.revokeObjectURL(u)
        else setLoaded({ src, url: u })
      })
      .catch(() => !revoked && setLoaded({ src, url: null }))
    return () => {
      revoked = true
      if (objectUrl) URL.revokeObjectURL(objectUrl)
    }
  }, [src])
  const current = loaded?.src === src ? loaded : null
  if (current && !current.url) return <div className={cn('flex items-center justify-center bg-slate-100 text-[11px] text-slate-400', className)}>preview unavailable</div>
  if (!current) return <div className={cn('hatch animate-pulse bg-slate-100', className)} style={{ minHeight: 80 }} aria-label={`Loading ${alt}`} />
  return <img src={current.url!} alt={alt} className={className} />
}
