import { AlertCircle, CheckCircle2, Timer, XCircle } from 'lucide-react'
import { MeterRow } from '../components/charts'
import { Card, CardHeader, Empty, RiskBadge, Spinner, Stat } from '../components/ui'
import { api, ApiError } from '../lib/api'
import { humanize } from '../lib/format'
import { useApi } from '../lib/identity'

export default function EvaluationPage() {
  const { data, error, loading } = useApi(() => api.evaluation())
  if (loading && !data) return <Spinner />
  if (error) {
    return (
      <Empty title="No evaluation report yet">
        {error instanceof ApiError && error.status === 404 ? (
          <>
            Generate it with <code className="font-mono">cd backend && python -m demo.evaluate</code>
          </>
        ) : (
          String(error)
        )}
      </Empty>
    )
  }
  const types = Object.entries(data.per_fraud_type) as [string, any][]
  const fp = data.false_positives

  return (
    <div className="space-y-6 animate-rise">
      <div>
        <h1 className="font-display text-4xl font-light tracking-tight text-slate-900">Evaluation</h1>
        <p className="mt-1 text-sm text-slate-500">
          Synthetic test set with injected issues; labels come from the generator's recipes, never from a detector. Run on a fresh database each time.
        </p>
      </div>

      <div className="grid grid-cols-2 gap-4 lg:grid-cols-4">
        <Stat label="Detection rate" value={`${Math.round(data.overall_detection_rate * 100)}%`} hint={`${types.reduce((a, [, v]) => a + v.detected, 0)} of ${types.reduce((a, [, v]) => a + v.cases, 0)} injected issues`} />
        <Stat label="False positives" value={fp.medium_or_strong_findings_on_clean} hint={`medium/strong findings on ${fp.clean_applications} clean controls`} />
        <Stat label="Prioritised high" value={`${data.prioritisation.fraud_applications_at_high}/${data.prioritisation.fraud_applications}`} hint="applications with injected issues" />
        <Stat label="Per application" value={`${data.timing.avg_seconds_per_application}s`} hint={`${data.timing.avg_seconds_per_document}s per document · ${data.documents} documents`} accent={<Timer className="h-4 w-4 text-slate-400" />} />
      </div>

      <div className="grid gap-5 lg:grid-cols-2">
        <Card>
          <CardHeader title="Detection rate by injected issue" subtitle="Detected when any finding in the expected group fires at or above its minimum strength" />
          <div className="space-y-4 p-5">
            {types.map(([name, v]) => (
              <MeterRow key={name} label={humanize(name)} value={v.detection_rate * 100} max={100} display={`${Math.round(v.detection_rate * 100)}%`} sub={`(${v.detected}/${v.cases})`} />
            ))}
          </div>
        </Card>
        <Card>
          <CardHeader title="Honest documents the system should still surface" subtitle="Revoked or expired certificates are genuine — and still need a reviewer. Not counted as false positives." />
          <div className="space-y-2 p-5 text-sm">
            {data.honest_flags_expected.detail.map((h: any) => (
              <div key={h.application + h.flag} className="flex items-center gap-2">
                {h.raised ? <CheckCircle2 className="h-4 w-4 text-emerald-600" /> : <XCircle className="h-4 w-4 text-red-600" />}
                <span className="font-medium text-slate-800">{humanize(h.application)}</span>
                <span className="text-slate-500">{humanize(h.flag)}</span>
              </div>
            ))}
            <div className="mt-4 border-t border-slate-200/70 pt-3 text-xs text-slate-500">
              Clean controls: {fp.clean_applications} · not at low priority: {fp.clean_not_low_priority} · weak findings on them:{' '}
              {fp.detail.reduce((a: number, c: any) => a + c.weak_findings, 0)}
            </div>
          </div>
        </Card>
      </div>

      <Card>
        <CardHeader title="Per application" />
        <div className="overflow-x-auto">
          <table className="w-full text-xs">
            <thead>
              <tr className="border-b border-slate-200/70 text-left">
                <th className="label-xs px-5 py-2">Institution</th>
                <th className="label-xs px-3 py-2">Injected</th>
                <th className="label-xs px-3 py-2">Detected by</th>
                <th className="label-xs px-3 py-2">Priority</th>
                <th className="label-xs px-3 py-2 text-right">Time</th>
              </tr>
            </thead>
            <tbody>
              {data.cases.map((c: any) => (
                <tr key={c.key} className="border-b border-slate-50 align-top">
                  <td className="px-5 py-2.5 font-medium text-slate-800">{c.institution}</td>
                  <td className="px-3 py-2.5 text-slate-600">{c.fraud_types.length ? c.fraud_types.map(humanize).join(', ') : <span className="text-emerald-700">none (clean)</span>}</td>
                  <td className="px-3 py-2.5 font-mono text-[11px] text-slate-500">
                    {Object.entries(c.detected).map(([t, by]: [string, any]) => (
                      <div key={t}>{by.length ? by.join(', ') : <span className="text-red-600">missed</span>}</div>
                    ))}
                  </td>
                  <td className="px-3 py-2.5">
                    <RiskBadge level={c.risk_level} size="sm" /> <span className="ml-1 tabular-nums text-slate-500">{c.risk_score}</span>
                  </td>
                  <td className="px-3 py-2.5 text-right tabular-nums text-slate-500">{c.seconds}s</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Card>

      <div className="flex items-start gap-2 rounded-lg border border-amber-200 bg-amber-50 p-4 text-xs text-amber-900">
        <AlertCircle className="mt-0.5 h-4 w-4 shrink-0" /> {data.caveat}
      </div>
    </div>
  )
}
