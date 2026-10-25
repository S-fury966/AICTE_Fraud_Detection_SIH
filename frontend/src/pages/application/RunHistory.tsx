import { GitCompare } from 'lucide-react'
import { Card, CardHeader, cn } from '../../components/ui'
import { api } from '../../lib/api'
import { formatDate, humanize } from '../../lib/format'
import { useApi } from '../../lib/identity'
import type { Run } from '../../lib/types'

function countTypes(run: Run): Map<string, number> {
  const counts = new Map<string, number>()
  run.findings.forEach((f) => counts.set(f.type, (counts.get(f.type) ?? 0) + 1))
  return counts
}

/** What changed between two runs of one document: findings and rule versions. */
function diff(previous: Run, current: Run) {
  const before = countTypes(previous)
  const after = countTypes(current)
  const added: string[] = []
  const removed: string[] = []
  new Set([...before.keys(), ...after.keys()]).forEach((type) => {
    const delta = (after.get(type) ?? 0) - (before.get(type) ?? 0)
    if (delta > 0) added.push(`${humanize(type)}${delta > 1 ? ` ×${delta}` : ''}`)
    if (delta < 0) removed.push(`${humanize(type)}${delta < -1 ? ` ×${-delta}` : ''}`)
  })
  const rules = Object.entries(current.config_versions)
    .filter(([name, version]) => previous.config_versions[name] !== version)
    .map(([name, version]) => `${name}: ${previous.config_versions[name] ?? '—'} → ${version}`)
  return { added, removed, rules }
}

export default function RunHistory({ applicationId }: { applicationId: number }) {
  const { data } = useApi(() => api.report(applicationId, true), [applicationId])
  const documents = (data?.documents ?? []).filter((d) => d.runs.length > 1)
  return (
    <Card>
      <CardHeader icon={<GitCompare className="h-4 w-4" />} title="Run history" subtitle="Re-runs add a run and never edit an earlier one, so a change in rules or results stays visible" />
      <div className="p-4">
        {documents.length === 0 && <p className="text-xs text-slate-500">Each document has been checked once. A senior reviewer can re-run the application under the current rules; the comparison appears here.</p>}
        {documents.map((document) => (
          <div key={document.id} className="mb-4 last:mb-0">
            <div className="mb-2 text-xs font-semibold text-slate-700">
              {document.original_filename} <span className="font-normal text-slate-400">· {document.runs.length} runs</span>
            </div>
            <ol className="space-y-2">
              {document.runs.map((run, index) => {
                const change = index > 0 ? diff(document.runs[index - 1], run) : null
                return (
                  <li key={run.id} className="rounded-xl ring-1 ring-black/[0.05] bg-slate-50/60 px-3 py-2 text-xs">
                    <div className="flex flex-wrap items-center gap-2">
                      <span className="font-mono text-slate-500">run #{run.id}</span>
                      <span className="text-slate-400">{formatDate(run.started_at, true)} · {run.actor}</span>
                      <span className="text-slate-600">{run.findings.length} indicator(s)</span>
                    </div>
                    {change && (
                      <div className="mt-1 space-y-0.5">
                        {change.added.length + change.removed.length === 0 && <div className="text-slate-500">Same indicators as the previous run.</div>}
                        {change.added.map((a) => (
                          <div key={a} className="text-red-700">+ {a}</div>
                        ))}
                        {change.removed.map((r) => (
                          <div key={r} className="text-emerald-700">− {r}</div>
                        ))}
                        {change.rules.length > 0 && <div className={cn('text-slate-500')}>Rules changed: {change.rules.join('; ')}</div>}
                      </div>
                    )}
                  </li>
                )
              })}
            </ol>
          </div>
        ))}
      </div>
    </Card>
  )
}
