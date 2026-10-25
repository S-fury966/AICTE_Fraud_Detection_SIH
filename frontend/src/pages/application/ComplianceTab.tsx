import { CheckCircle2, CircleDashed, XCircle } from 'lucide-react'
import { Card, CardHeader, cn, Empty, VerificationBadge } from '../../components/ui'
import { formatDate, humanize } from '../../lib/format'
import type { ApplicationReport } from '../../lib/types'

const OUTCOME = {
  met: { icon: CheckCircle2, cls: 'text-emerald-600', label: 'Met' },
  not_met: { icon: XCircle, cls: 'text-red-600', label: 'Not met' },
  not_evaluated: { icon: CircleDashed, cls: 'text-slate-400', label: 'Not evaluated' },
} as const

export default function ComplianceTab({ report }: { report: ApplicationReport }) {
  const check = report.application_check
  if (!check) return <Empty title="Application-level checks have not run for this application" />
  const norms = check.norms
  return (
    <div className="space-y-5">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="flex items-center gap-2 text-sm text-slate-600">
          Overall <VerificationBadge status={check.status} />
        </div>
        <div className="text-xs text-slate-500">
          {check.config_version} · checked {formatDate(check.created_at, true)} · norms are <b>mock values</b> from config, not an official handbook
        </div>
      </div>

      <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-3">
        {check.checks.map((c) => {
          const o = OUTCOME[c.outcome] ?? OUTCOME.not_evaluated
          return (
            <div key={c.check} className={cn('rounded-2xl p-5 ring-1', c.outcome === 'not_met' ? 'bg-ink-900 text-white ring-ink-900' : 'bg-white ring-black/[0.06]')}>
              <div className="flex items-center justify-between gap-2">
                <span className={cn('text-[11px] font-medium uppercase tracking-[0.08em]', c.outcome === 'not_met' ? 'text-white/60' : 'text-slate-500')}>{humanize(c.detector)}</span>
                <span className={cn('flex items-center gap-1 text-xs font-medium', c.outcome === 'not_met' ? 'text-butter-500' : o.cls)}>
                  <o.icon className="h-3.5 w-3.5" /> {o.label}
                </span>
              </div>
              <div className="font-display mt-3 text-lg font-light leading-snug">{c.label}</div>
              <div className={cn('mt-1 text-xs', c.outcome === 'not_met' ? 'text-white/70' : 'text-slate-500')}>{c.summary}</div>
            </div>
          )
        })}
      </div>

      {norms?.departments?.length > 0 && (
        <Card>
          <CardHeader title="Student : faculty ratio" subtitle={`Per department with an intake on record · maximum ${norms.departments[0].max_ratio}:1 (mock norm)`} />
          <div className="overflow-x-auto px-6 pb-5">
            <table className="w-full text-xs">
              <thead>
                <tr className="text-left">
                  <th className="label-xs py-2 pr-3">Department</th>
                  <th className="label-xs py-2 pr-3 text-right">Intake</th>
                  <th className="label-xs py-2 pr-3 text-right">Declared faculty</th>
                  <th className="label-xs py-2 pr-3 text-right">Ratio as declared</th>
                  <th className="label-xs py-2 pr-3 text-right">Recorded faculty</th>
                  <th className="label-xs py-2 text-right">Ratio on records</th>
                </tr>
              </thead>
              <tbody>
                {norms.departments.map((d: any) => (
                  <tr key={d.department} className="border-t border-slate-200/70">
                    <td className="py-2.5 pr-3 font-medium text-slate-800">{d.department}</td>
                    <td className="py-2.5 pr-3 text-right tabular-nums">{d.intake}</td>
                    <td className="py-2.5 pr-3 text-right tabular-nums">{d.declared_faculty}</td>
                    <td className={cn('py-2.5 pr-3 text-right font-semibold tabular-nums', d.declared_meets ? 'text-emerald-700' : 'text-red-700')}>{d.declared_ratio ?? '∞'}:1</td>
                    <td className="py-2.5 pr-3 text-right tabular-nums">{d.recorded_faculty ?? '—'}</td>
                    <td className={cn('py-2.5 text-right font-semibold tabular-nums', d.recorded_meets === false ? 'text-red-700' : d.recorded_meets ? 'text-emerald-700' : 'text-slate-400')}>
                      {d.recorded_ratio !== null ? `${d.recorded_ratio}:1` : '—'}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Card>
      )}

      {norms?.cadre && (
        <Card>
          <CardHeader title="Cadre" subtitle={`Declared designations against the mock ${Object.values(norms.cadre.norm).join(':')} norm`} />
          <div className="grid gap-4 px-6 pb-6 sm:grid-cols-3">
            {norms.cadre.rows.map((r: any) => (
              <div key={r.cadre}>
                <div className="flex items-baseline justify-between text-xs">
                  <span className="text-slate-600">{humanize(r.cadre)}</span>
                  <span className="num text-2xl font-light text-slate-900">{r.count}</span>
                </div>
                <div className="hatch relative mt-2 h-2.5 rounded-full bg-slate-100">
                  <div className="h-full rounded-full bg-ink-900" style={{ width: `${Math.min(100, r.share * 100)}%` }} />
                  <span className="absolute -top-1 h-4.5 w-0.5 rounded bg-butter-500" style={{ left: `${r.target_share * 100}%`, height: 18 }} title={`target ${Math.round(r.target_share * 100)}%`} />
                </div>
                <div className="mt-1 text-[11px] text-slate-500">
                  {Math.round(r.share * 100)}% · target {Math.round(r.target_share * 100)}% (yellow mark)
                </div>
              </div>
            ))}
          </div>
        </Card>
      )}
    </div>
  )
}
