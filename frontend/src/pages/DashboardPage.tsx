import { ArrowRight, Building2, FileStack, ShieldAlert, Sparkles } from 'lucide-react'
import { useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { Button, Card, CardHeader, CornerArrow, CountUp, Empty, ErrorBox, RiskBadge, ScoreBar, Spinner, STRENGTH_COLOR, cn } from '../components/ui'
import { api } from '../lib/api'
import { DETECTOR_LABEL, DOCUMENT_TYPE_LABEL, formatDate, humanize } from '../lib/format'
import { useApi, useIdentity } from '../lib/identity'
import type { ApplicationSummary, Stats } from '../lib/types'

const DETECTORS = ['application_validation', 'certificate_authenticity', 'duplicate_detection', 'faculty_consistency', 'regulatory_compliance', 'photo_authenticity']
const SHORT: Record<string, string> = {
  application_validation: 'App',
  certificate_authenticity: 'Cert',
  duplicate_detection: 'Dupes',
  faculty_consistency: 'Faculty',
  regulatory_compliance: 'Norms',
  photo_authenticity: 'Photos',
}

export default function DashboardPage() {
  const apps = useApi(() => api.applications())
  const stats = useApi(() => api.stats())
  const { identity } = useIdentity()
  const navigate = useNavigate()

  if (apps.error) return <ErrorBox error={apps.error} />
  if (apps.loading && !apps.data) return <Spinner label="Loading review queue…" />

  const rows = apps.data ?? []
  const s = stats.data
  const name = identity.actor.split(/[._]/).map((p) => p.charAt(0).toUpperCase() + p.slice(1)).join(' ')
  const firstHigh = rows.find((r) => r.risk?.level === 'high')

  return (
    <div className="space-y-6 animate-rise">
      <section className="flex flex-wrap items-end justify-between gap-8">
        <div className="min-w-0 flex-1">
          <h1 className="font-display text-4xl font-light tracking-tight text-slate-900 sm:text-5xl">Welcome in, {name}</h1>
          {s && <PrioritySplit stats={s} />}
        </div>
        {s && (
          <div className="grid w-full grid-cols-3 gap-4 sm:flex sm:w-auto sm:gap-12">
            <BigNumber icon={<Building2 className="h-3.5 w-3.5" />} value={s.applications} label="Applications" />
            <BigNumber icon={<ShieldAlert className="h-3.5 w-3.5" />} value={Object.values(s.open_findings_by_detector).reduce((a, d) => a + (d.strong ?? 0), 0)} label="Open strong" />
            <BigNumber icon={<FileStack className="h-3.5 w-3.5" />} value={s.documents} label="Documents" />
          </div>
        )}
      </section>

      {s && (
        <section className="grid gap-4 md:grid-cols-2 xl:grid-cols-4">
          <InsightCard stats={s} onOpen={() => firstHigh && navigate(`/applications/${firstHigh.id}`)} />
          <DetectorBars stats={s} onOpen={() => navigate('/findings')} />
          <ReviewRing stats={s} onOpen={() => navigate('/audit')} />
          <TopIndicators stats={s} />
        </section>
      )}

      <Card className="overflow-hidden">
        <CardHeader
          title="Review queue"
          subtitle="Ordered by review priority. Priority decides what to open first — never the outcome."
          action={
            <Button size="sm" onClick={() => navigate('/submit')}>
              <Sparkles className="h-3.5 w-3.5" /> Scrutinise an application
            </Button>
          }
        />
        {rows.length === 0 ? (
          <Empty title="No applications yet">
            Submit one from{' '}
            <Link className="text-iris-600 underline" to="/submit">
              Scrutinise
            </Link>
            , or load the demo set: <code className="font-mono">python -m demo.load --fresh</code>
          </Empty>
        ) : (
          <div className="overflow-x-auto px-3 pb-3">
            <table className="w-full border-separate border-spacing-y-1 text-sm">
              <thead>
                <tr className="text-left">
                  <th className="label-xs px-4 py-2">Institution</th>
                  <th className="label-xs px-3 py-2">Priority</th>
                  <th className="label-xs w-48 px-3 py-2">Score</th>
                  <th className="label-xs px-3 py-2">Open indicators</th>
                  <th className="label-xs px-3 py-2">Reviewed</th>
                  <th className="px-3 py-2" />
                </tr>
              </thead>
              <tbody>
                {rows.map((row) => (
                  <QueueRow key={row.id} row={row} onOpen={() => navigate(`/applications/${row.id}`)} />
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>
    </div>
  )
}

// ── Header pieces ─────────────────────────────────────────────────────────

function PrioritySplit({ stats }: { stats: Stats }) {
  const { high = 0, medium = 0, low = 0 } = stats.risk_levels
  const total = Math.max(1, high + medium + low)
  const findings = Object.values(stats.findings_by_review_status).reduce((a, b) => a + b, 0)
  const reviewed = findings - (stats.findings_by_review_status.open ?? 0)
  const reviewedPct = findings ? Math.round((reviewed / findings) * 100) : 0
  const segments = [
    { key: 'high', label: 'High priority', value: high, cls: 'bg-ink-900 text-white' },
    { key: 'medium', label: 'Medium', value: medium, cls: 'bg-butter-500 text-ink-900' },
    { key: 'low', label: 'Low', value: low, cls: 'hatch bg-white/70 text-slate-700 ring-1 ring-black/5' },
  ].filter((s) => s.value > 0)
  return (
    <div className="mt-6 flex max-w-4xl items-end gap-2">
      {segments.map((seg) => (
        <div key={seg.key} style={{ flexGrow: seg.value / total, flexBasis: 0 }} className="min-w-[96px]">
          <div className="mb-2 pl-1 text-sm text-slate-600">{seg.label}</div>
          <div className={cn('flex h-12 items-center rounded-full px-4 text-sm font-medium', seg.cls)}>
            {seg.value} <span className="ml-1 opacity-70">({Math.round((seg.value / total) * 100)}%)</span>
          </div>
        </div>
      ))}
      <div className="w-28 shrink-0">
        <div className="mb-2 pl-1 text-sm text-slate-600">Reviewed</div>
        <div className="flex h-12 items-center rounded-full px-4 text-sm ring-1 ring-black/20">{reviewedPct}%</div>
      </div>
    </div>
  )
}

function BigNumber({ icon, value, label }: { icon: React.ReactNode; value: number; label: string }) {
  return (
    <div>
      <div className="flex items-end gap-2">
        <span className="mb-3 hidden h-7 w-7 items-center justify-center rounded-full bg-white/80 text-slate-600 ring-1 ring-black/5 sm:flex">{icon}</span>
        <span className="num text-5xl font-light leading-none text-slate-900 sm:text-6xl">
          <CountUp value={value} />
        </span>
      </div>
      <div className="mt-1 text-sm text-slate-600 sm:pl-9">{label}</div>
    </div>
  )
}

// ── Row of four cards ─────────────────────────────────────────────────────

function InsightCard({ stats, onOpen }: { stats: Stats; onOpen: () => void }) {
  const high = stats.risk_levels.high ?? 0
  return (
    <div className="relative overflow-hidden rounded-[1.75rem] bg-iris-600 p-6 text-white shadow-[0_20px_40px_-20px_rgba(62,59,240,0.6)]">
      <svg className="pointer-events-none absolute inset-0 h-full w-full" viewBox="0 0 300 260" preserveAspectRatio="xMidYMid slice" aria-hidden>
        <circle cx="250" cy="250" r="190" fill="none" stroke="white" strokeOpacity="0.28" strokeWidth="1" />
        <circle cx="120" cy="-10" r="95" fill="none" stroke="white" strokeOpacity="0.22" strokeWidth="1" />
        <circle cx="40" cy="70" r="2.5" fill="white" />
        <circle cx="230" cy="40" r="2" fill="white" />
      </svg>
      <div className="relative flex items-start justify-between">
        <span className="rounded-full px-3 py-1 text-xs ring-1 ring-white/50">Insight</span>
        <button onClick={onOpen} className="flex h-9 w-9 items-center justify-center rounded-full ring-1 ring-white/50 transition hover:bg-white hover:text-iris-600" title="Open the most urgent application">
          <ArrowRight className="h-4 w-4 -rotate-45" />
        </button>
      </div>
      <p className="font-display relative mt-16 text-2xl font-light leading-snug">
        {high} of {stats.applications} applications need a closer look first.
      </p>
      <p className="relative mt-2 text-xs text-white/70">Strong indicators from {Object.entries(stats.open_findings_by_detector).filter(([, d]) => d.strong).length} detectors · every one links to its evidence</p>
    </div>
  )
}

function DetectorBars({ stats, onOpen }: { stats: Stats; onOpen: () => void }) {
  const [hover, setHover] = useState<string | null>(null)
  const totals = DETECTORS.map((d) => {
    const c = stats.open_findings_by_detector[d] ?? { strong: 0, medium: 0, weak: 0 }
    return { detector: d, ...c, total: (c.strong ?? 0) + (c.medium ?? 0) + (c.weak ?? 0) }
  })
  const max = Math.max(1, ...totals.map((t) => t.total))
  const top = totals.reduce((a, b) => (b.strong > a.strong ? b : a), totals[0])
  const openTotal = totals.reduce((a, t) => a + t.total, 0)
  return (
    <Card className="p-6">
      <div className="flex items-start justify-between">
        <h3 className="font-display text-xl font-light text-slate-900">Open indicators</h3>
        <CornerArrow title="All findings" onClick={onOpen} />
      </div>
      <div className="mt-2 flex items-baseline gap-3">
        <span className="num text-4xl font-light">
          <CountUp value={openTotal} />
        </span>
        <span className="text-xs leading-tight text-slate-500">
          awaiting a reviewer
          <br />
          across six checks
        </span>
      </div>
      <div className="relative mt-5 flex h-32 items-end justify-between gap-2 border-b border-dashed border-slate-200 px-1">
        {totals.map((t) => {
          const highlight = t.detector === top.detector && top.strong > 0
          return (
            <div key={t.detector} className="relative flex flex-1 flex-col items-center" onMouseEnter={() => setHover(t.detector)} onMouseLeave={() => setHover(null)}>
              {(hover === t.detector || (highlight && !hover)) && (
                <span className={cn('absolute -top-8 whitespace-nowrap rounded-full px-2 py-0.5 text-[10px] font-medium', highlight ? 'bg-butter-500 text-ink-900' : 'bg-ink-900 text-white')}>
                  {t.total} · {t.strong} strong
                </span>
              )}
              <div
                className={cn('w-2 rounded-full transition-all duration-700', highlight ? 'bg-butter-500' : t.total ? 'bg-ink-900' : 'hatch bg-slate-100')}
                style={{ height: `${Math.max(10, (t.total / max) * 104)}px` }}
              />
              <span className={cn('mt-1.5 h-1.5 w-1.5 rounded-full', highlight ? 'bg-butter-500' : 'bg-ink-900')} />
            </div>
          )
        })}
      </div>
      <div className="mt-2 flex justify-between gap-1 px-1 text-[10px] text-slate-500">
        {totals.map((t) => (
          <span key={t.detector} className="flex-1 truncate text-center" title={DETECTOR_LABEL[t.detector]}>
            {SHORT[t.detector]}
          </span>
        ))}
      </div>
    </Card>
  )
}

function ReviewRing({ stats, onOpen }: { stats: Stats; onOpen: () => void }) {
  const total = Object.values(stats.findings_by_review_status).reduce((a, b) => a + b, 0)
  const open = stats.findings_by_review_status.open ?? 0
  const reviewed = total - open
  const fraction = total ? reviewed / total : 0
  const r = 62
  const circumference = 2 * Math.PI * r
  return (
    <Card className="p-6">
      <div className="flex items-start justify-between">
        <h3 className="font-display text-xl font-light text-slate-900">Review progress</h3>
        <CornerArrow title="Audit trail" onClick={onOpen} />
      </div>
      <div className="mt-2 flex items-center justify-center">
        <svg viewBox="0 0 160 160" className="h-40 w-40" role="img" aria-label={`${reviewed} of ${total} indicators reviewed`}>
          {Array.from({ length: 48 }).map((_, i) => {
            const angle = (i / 48) * Math.PI * 2 - Math.PI / 2
            return <line key={i} x1={80 + Math.cos(angle) * 72} y1={80 + Math.sin(angle) * 72} x2={80 + Math.cos(angle) * 76} y2={80 + Math.sin(angle) * 76} stroke="#cfcfca" strokeWidth="1.2" />
          })}
          <circle cx="80" cy="80" r={r} fill="none" stroke="#efefec" strokeWidth="10" />
          <circle
            cx="80"
            cy="80"
            r={r}
            fill="none"
            stroke="var(--color-butter-500)"
            strokeWidth="10"
            strokeLinecap="round"
            strokeDasharray={`${Math.max(fraction * circumference, 0.001)} ${circumference}`}
            transform="rotate(-90 80 80)"
            style={{ transition: 'stroke-dasharray 0.9s ease' }}
          />
          <text x="80" y="80" textAnchor="middle" className="num fill-slate-900" style={{ fontSize: 30, fontWeight: 300 }}>
            {reviewed}/{total}
          </text>
          <text x="80" y="100" textAnchor="middle" className="fill-slate-500" style={{ fontSize: 10 }}>
            indicators reviewed
          </text>
        </svg>
      </div>
      <div className="mt-1 flex justify-between text-xs text-slate-500">
        <span>{stats.review_actions} review actions</span>
        <span className="flex items-center gap-1">
          <span className={cn('h-1.5 w-1.5 rounded-full', stats.audit_chain_ok ? 'bg-emerald-500' : 'bg-red-500')} /> {stats.audit_entries} audit entries
        </span>
      </div>
    </Card>
  )
}

function TopIndicators({ stats }: { stats: Stats }) {
  const items = stats.top_open_types.slice(0, 5)
  return (
    <div className="rounded-[1.75rem] bg-ink-900 p-6 text-white shadow-[0_20px_40px_-24px_rgba(20,20,22,0.8)]">
      <div className="flex items-baseline justify-between">
        <h3 className="font-display text-xl font-light">Most common</h3>
        <span className="num text-3xl font-light">{stats.top_open_types.reduce((a, t) => a + t.count, 0)}</span>
      </div>
      <p className="mt-1 text-[11px] text-white/50">Open medium & strong indicators, by type</p>
      <ul className="mt-4 space-y-3">
        {items.map((t) => (
          <li key={t.type + t.strength} className="flex items-center gap-3">
            <span className="flex h-9 w-9 shrink-0 items-center justify-center rounded-full bg-white/10">
              <span className="h-2 w-2 rounded-full" style={{ background: STRENGTH_COLOR[t.strength] }} />
            </span>
            <span className="min-w-0 flex-1">
              <span className="block truncate text-sm">{humanize(t.type)}</span>
              <span className="text-[11px] text-white/50">{t.strength}</span>
            </span>
            <span className="num text-lg font-light text-butter-500">{t.count}</span>
          </li>
        ))}
        {items.length === 0 && <li className="text-sm text-white/60">Nothing open at medium or strong.</li>}
      </ul>
    </div>
  )
}

// ── Queue row ─────────────────────────────────────────────────────────────

function QueueRow({ row, onOpen }: { row: ApplicationSummary; onOpen: () => void }) {
  const initials = row.institution
    .split(' ')
    .filter((w) => w.length > 2 && w[0] === w[0].toUpperCase())
    .slice(0, 2)
    .map((w) => w[0])
    .join('')
  return (
    <tr onClick={onOpen} className="group cursor-pointer">
      <td className="rounded-l-2xl px-4 py-3 transition group-hover:bg-slate-50">
        <div className="flex items-center gap-3">
          <span className={cn('flex h-10 w-10 shrink-0 items-center justify-center rounded-full text-xs font-semibold', row.risk?.level === 'high' ? 'bg-ink-900 text-white' : row.risk?.level === 'medium' ? 'bg-butter-500 text-ink-900' : 'bg-slate-100 text-slate-700')}>
            {initials}
          </span>
          <div className="min-w-0">
            <div className="truncate font-medium text-slate-900">{row.institution}</div>
            <div className="text-xs text-slate-500" title={row.document_types.map((t) => DOCUMENT_TYPE_LABEL[t] ?? t).join(', ')}>
              APP-{String(row.id).padStart(4, '0')} · {row.document_count} documents · {formatDate(row.created_at)}
            </div>
          </div>
        </div>
      </td>
      <td className="px-3 py-3 transition group-hover:bg-slate-50">
        <RiskBadge level={row.risk?.level} size="sm" />
      </td>
      <td className="px-3 py-3 transition group-hover:bg-slate-50">
        <div className="flex items-center gap-3">
          <ScoreBar score={row.risk?.score ?? 0} level={row.risk?.level} />
          <span className="num w-8 text-right text-base font-light text-slate-800">{Math.round(row.risk?.score ?? 0)}</span>
        </div>
      </td>
      <td className="px-3 py-3 transition group-hover:bg-slate-50">
        <div className="flex gap-1.5 text-[11px]">
          {(['strong', 'medium', 'weak'] as const).map((k) => (
            <span key={k} className={cn('inline-flex items-center gap-1.5 rounded-full px-2.5 py-1 ring-1', row.open_by_strength[k] ? 'bg-white text-slate-800 ring-black/10' : 'text-slate-300 ring-transparent')}>
              <span className="h-1.5 w-1.5 rounded-full" style={{ background: row.open_by_strength[k] ? STRENGTH_COLOR[k] : '#e3e3df' }} />
              {row.open_by_strength[k]} {k}
            </span>
          ))}
        </div>
      </td>
      <td className="px-3 py-3 text-xs tabular-nums text-slate-600 transition group-hover:bg-slate-50">
        {row.reviewed_findings}/{row.finding_count}
      </td>
      <td className="rounded-r-2xl px-3 py-3 text-right transition group-hover:bg-slate-50">
        <span className="ml-auto flex h-8 w-8 items-center justify-center rounded-full bg-white text-slate-500 ring-1 ring-black/5 transition group-hover:bg-ink-900 group-hover:text-white">
          <ArrowRight className="h-4 w-4 -rotate-45" />
        </span>
      </td>
    </tr>
  )
}
