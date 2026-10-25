import { CalendarClock, Database, FileSpreadsheet, Globe, UserCheck } from 'lucide-react'
import { Legend } from '../../components/charts'
import { Card, CardHeader, cn, Empty, VerificationBadge } from '../../components/ui'
import { formatDate, humanize } from '../../lib/format'
import type { ApplicationReport } from '../../lib/types'

// One colour per source (iris, tangerine, ink): distinct in hue and lightness,
// and every bar also carries its source in the legend and tooltip.
const SOURCE_COLOR = { declared: '#3e3bf0', website: '#f28c38', aicte: '#18181a' }
const SOURCE_META = {
  declared: { label: 'Declared in application', icon: FileSpreadsheet },
  website: { label: 'College website snapshot', icon: Globe },
  aicte: { label: 'AICTE records (mock)', icon: Database },
} as const

export default function FacultyTab({ report }: { report: ApplicationReport }) {
  const doc = report.documents.find((d) => d.document_type === 'faculty_declaration')
  const detail = doc?.runs.at(-1)?.details?.faculty_comparison
  if (!doc || !detail) return <Empty title="No faculty comparison for this application" />

  const departments: any[] = detail.departments ?? []
  const max = Math.max(1, ...departments.flatMap((d) => [d.declared ?? 0, d.website ?? 0, d.aicte ?? 0]))

  return (
    <div className="space-y-5">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="text-sm text-slate-600">
          Overall: <VerificationBadge status={detail.status} />
          {detail.record_match?.method === 'fuzzy' && <span className="ml-2 text-xs text-slate-500">AICTE record matched by name similarity ({detail.record_match.score})</span>}
        </div>
        <div className="text-xs text-slate-500">
          Tolerance per department: max(1, 10% of the AICTE count) · evaluated as of {detail.audit?.as_of}
        </div>
      </div>

      <div className="grid gap-3 md:grid-cols-3">
        {(['declared', 'website', 'aicte'] as const).map((name) => {
          const source = detail.sources?.[name]
          const { label, icon: Icon } = SOURCE_META[name]
          return (
            <Card key={name} className="p-4">
              <div className="flex items-center gap-2">
                <span className="h-2.5 w-2.5 rounded-sm" style={{ background: SOURCE_COLOR[name] }} />
                <Icon className="h-4 w-4 text-slate-400" />
                <span className="text-sm font-medium text-slate-800">{label}</span>
              </div>
              {source?.available ? (
                <>
                  <div className="mt-3 text-2xl font-semibold tabular-nums text-slate-900">{source.total}</div>
                  <div className="text-xs text-slate-500">faculty across {Object.keys(source.counts ?? {}).length} departments</div>
                  <div className="mt-2 flex items-center gap-1 text-[11px] text-slate-500">
                    <CalendarClock className="h-3 w-3" /> captured {formatDate(source.captured_on)}
                  </div>
                  <div className="truncate font-mono text-[10px] text-slate-400">{source.reference}</div>
                </>
              ) : (
                <div className="mt-3 text-xs text-slate-500">
                  <VerificationBadge status="source_unavailable" />
                  <div className="mt-2">{source?.unavailable_reason}</div>
                </div>
              )}
            </Card>
          )
        })}
      </div>

      <Card>
        <CardHeader title="Per-department comparison" subtitle="Per department, not totals: a totals check cannot see headcount moved between departments on paper" action={<Legend items={(['declared', 'website', 'aicte'] as const).map((k) => ({ label: { declared: 'Declared', website: 'Website', aicte: 'AICTE' }[k], color: SOURCE_COLOR[k] }))} />} />
        <div className="overflow-x-auto">
          <table className="w-full text-xs">
            <thead>
              <tr className="border-b border-slate-200/70 text-left">
                <th className="label-xs px-4 py-2">Department</th>
                <th className="label-xs px-3 py-2 text-right">Declared</th>
                <th className="label-xs px-3 py-2 text-right">Website</th>
                <th className="label-xs px-3 py-2 text-right">AICTE</th>
                <th className="label-xs px-3 py-2 text-right">± allowed</th>
                <th className="label-xs w-[34%] px-3 py-2">Comparison</th>
                <th className="label-xs px-3 py-2">Status</th>
              </tr>
            </thead>
            <tbody>
              {departments.map((d) => {
                const offA = d.declared_minus_aicte !== null && Math.abs(d.declared_minus_aicte) > d.allowed_deviation
                const offW = d.declared_minus_website !== null && Math.abs(d.declared_minus_website) > d.allowed_deviation
                return (
                  <tr key={d.department} className="border-b border-slate-50">
                    <td className="px-4 py-2.5 font-medium text-slate-800">{d.department}</td>
                    <td className={cn('px-3 py-2.5 text-right font-semibold tabular-nums', offA || offW ? 'text-red-700' : 'text-slate-900')}>
                      {d.declared}
                      {d.declared_minus_aicte ? <span className="ml-1 text-[10px] font-normal">({d.declared_minus_aicte > 0 ? '+' : ''}{d.declared_minus_aicte})</span> : null}
                    </td>
                    <td className="px-3 py-2.5 text-right tabular-nums text-slate-700">{d.website ?? '—'}</td>
                    <td className="px-3 py-2.5 text-right tabular-nums text-slate-700">{d.aicte ?? '—'}</td>
                    <td className="px-3 py-2.5 text-right tabular-nums text-slate-500">{d.allowed_deviation}</td>
                    <td className="px-3 py-2.5">
                      <div className="space-y-[2px]">
                        {(['declared', 'website', 'aicte'] as const).map((k) => (
                          <div key={k} className="h-1.5 rounded-r" title={`${SOURCE_META[k].label}: ${d[k] ?? 'n/a'}`} style={{ width: `${((d[k] ?? 0) / max) * 100}%`, background: SOURCE_COLOR[k], minWidth: d[k] ? 3 : 0 }} />
                        ))}
                      </div>
                    </td>
                    <td className="px-3 py-2.5">
                      <VerificationBadge status={d.status} />
                    </td>
                  </tr>
                )
              })}
              <tr className="bg-slate-50 font-semibold">
                <td className="px-4 py-2.5 text-slate-700">Total</td>
                <td className="px-3 py-2.5 text-right tabular-nums">{detail.totals?.declared}</td>
                <td className="px-3 py-2.5 text-right tabular-nums">{detail.totals?.website ?? '—'}</td>
                <td className="px-3 py-2.5 text-right tabular-nums">{detail.totals?.aicte ?? '—'}</td>
                <td className="px-3 py-2.5 text-right tabular-nums text-slate-500">{detail.totals?.allowed_deviation ?? '—'}</td>
                <td className="px-3 py-2.5 text-[11px] font-normal text-slate-500" colSpan={2}>
                  {detail.totals?.within_tolerance ? 'Total within tolerance' : detail.totals?.within_tolerance === false ? 'Total outside tolerance' : ''}
                </td>
              </tr>
            </tbody>
          </table>
        </div>
      </Card>

      <Card>
        <CardHeader icon={<UserCheck className="h-4 w-4" />} title="Faculty identity" subtitle="Synthetic faculty ID first (strong); name + qualification match without an ID match is medium at most" />
        <div className="grid gap-4 p-4 text-xs sm:grid-cols-4">
          <Metric label="People declared" value={detail.identity?.declared_people} />
          <Metric label="Compared against" value={detail.identity?.people_compared_against} hint="other institutions' rosters and declarations" />
          <Metric label="Shared by ID" value={Object.values(detail.identity?.shared_by_id ?? {}).reduce((a: number, b: any) => a + b, 0)} hint={Object.keys(detail.identity?.shared_by_id ?? {}).join(', ') || undefined} />
          <Metric label="Not on own AICTE roster" value={detail.identity?.not_in_roster} />
        </div>
        {(detail.findings ?? [])
          .filter((f: any) => f.type === 'faculty_shared_across_institutions' || f.type === 'faculty_possible_shared_by_name')
          .map((f: any, i: number) => (
            <div key={i} className="border-t border-slate-200/70 p-4">
              <div className="mb-2 text-xs font-medium text-slate-700">
                {humanize(f.type)} — {f.evidence.other_institution}
              </div>
              <table className="w-full text-xs">
                <thead>
                  <tr className="text-left">
                    <th className="label-xs py-1 pr-3">Faculty ID</th>
                    <th className="label-xs py-1 pr-3">Declared here as</th>
                    <th className="label-xs py-1 pr-3">Listed there as</th>
                    <th className="label-xs py-1">Source</th>
                  </tr>
                </thead>
                <tbody>
                  {(f.evidence.examples ?? []).map((e: any, j: number) => (
                    <tr key={j} className="border-t border-slate-50">
                      <td className="py-1 pr-3 font-mono">{e.faculty_id ?? e.declared_faculty_id ?? '—'}</td>
                      <td className="py-1 pr-3">{e.declared_name}</td>
                      <td className="py-1 pr-3">{e.other_name}</td>
                      <td className="py-1 text-slate-500">{(e.other_sources ?? [e.other_source]).map(humanize).join(', ')}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ))}
      </Card>
    </div>
  )
}

function Metric({ label, value, hint }: { label: string; value: any; hint?: string }) {
  return (
    <div>
      <div className="label-xs">{label}</div>
      <div className="mt-1 text-xl font-semibold tabular-nums text-slate-900">{value ?? '—'}</div>
      {hint && <div className="text-[11px] text-slate-500">{hint}</div>}
    </div>
  )
}
