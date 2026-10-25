import { Link2, Lock } from 'lucide-react'
import { Empty, ErrorBox, Spinner } from '../../components/ui'
import { api } from '../../lib/api'
import { formatDate, humanize, short } from '../../lib/format'
import { useApi, useIdentity } from '../../lib/identity'
import RunHistory from './RunHistory'

export default function AuditTab({ applicationId }: { applicationId: number }) {
  const { can } = useIdentity()
  const allowed = can('view_audit_log')
  return (
    <div className="space-y-6">
      <RunHistory applicationId={applicationId} />
      {allowed ? (
        <AuditEntries applicationId={applicationId} />
      ) : (
        <Empty title="Audit entries are restricted" icon={<Lock className="h-7 w-7" />}>
          Senior reviewers and administrators can read who did what to this application. Sign in with that role's passcode from the header.
        </Empty>
      )}
    </div>
  )
}

function AuditEntries({ applicationId }: { applicationId: number }) {
  const { data, error, loading } = useApi(() => api.applicationAudit(applicationId), [applicationId])
  if (error) return <ErrorBox error={error} />
  if (loading && !data) return <Spinner />
  if (!data?.length) return <Empty title="No audit entries" />
  return (
    <div>
      <h3 className="mb-2 text-sm font-semibold text-slate-800">Audit entries for this application</h3>
      <p className="mb-4 flex items-center gap-1.5 text-xs text-slate-500">
        <Link2 className="h-3.5 w-3.5" /> Append-only and hash-chained: each entry hashes its content plus the previous entry's hash. Entries hold ids, counts and statuses — never document text.
      </p>
      <ol className="relative space-y-0 border-l border-slate-200 pl-5">
        {data.map((entry) => (
          <li key={entry.id} className="relative pb-4">
            <span className="absolute -left-[25px] top-1.5 h-2.5 w-2.5 rounded-full border-2 border-white bg-ink-600" />
            <div className="flex flex-wrap items-baseline gap-x-2 text-xs">
              <span className="font-semibold text-slate-800">{humanize(entry.action)}</span>
              <span className="text-slate-500">
                {entry.entity_type} {entry.entity_id ?? ''}
              </span>
              <span className="text-slate-400">· {entry.actor} ({humanize(entry.role)})</span>
              <span className="text-slate-400">· {formatDate(entry.timestamp, true)}</span>
            </div>
            {entry.details && (
              <div className="mt-0.5 truncate font-mono text-[10px] text-slate-400" title={JSON.stringify(entry.details)}>
                {JSON.stringify(entry.details)}
              </div>
            )}
            <div className="font-mono text-[10px] text-slate-300">#{entry.id} · {short(entry.entry_hash, 16)}</div>
          </li>
        ))}
      </ol>
    </div>
  )
}
