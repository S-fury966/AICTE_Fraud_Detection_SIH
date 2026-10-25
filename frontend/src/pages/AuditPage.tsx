import { Link2, Lock, ShieldCheck, ShieldX } from 'lucide-react'
import { useState } from 'react'
import { Button, Card, CardHeader, Empty, ErrorBox, Spinner } from '../components/ui'
import { api } from '../lib/api'
import { formatDate, humanize, short } from '../lib/format'
import { useApi, useIdentity } from '../lib/identity'

export default function AuditPage() {
  const { can, meta } = useIdentity()
  const allowed = can('view_audit_log')
  const { data, error, loading } = useApi(() => (allowed ? api.audit(400) : Promise.resolve(null)), [allowed])
  const [verify, setVerify] = useState<{ ok: boolean; entries_checked: number; problem: string | null } | null>(null)
  const [verifying, setVerifying] = useState(false)
  const [verifyError, setVerifyError] = useState<unknown>(null)

  if (!meta) return <Spinner />
  if (!allowed) {
    return (
      <Empty title="Audit trail is restricted" icon={<Lock className="h-8 w-8" />}>
        Only senior reviewers and administrators can read the audit trail. Choose one of those roles in the header and sign in with its passcode — the sign-in, and any refused attempt, is itself recorded.
      </Empty>
    )
  }

  async function runVerify() {
    setVerifying(true)
    setVerifyError(null)
    try {
      setVerify(await api.verifyChain())
    } catch (err) {
      setVerifyError(err)
    } finally {
      setVerifying(false)
    }
  }

  return (
    <div className="space-y-5 animate-rise">
      <div className="flex flex-wrap items-end justify-between gap-4">
        <div>
          <h1 className="font-display text-4xl font-light tracking-tight text-slate-900">Audit trail</h1>
          <p className="mt-1 text-sm text-slate-500">Append-only, hash-chained log of checks run, records consulted, findings, human actions and every read of a report or file.</p>
        </div>
        {can('verify_audit_chain') && (
          <Button onClick={runVerify} disabled={verifying}>
            <Link2 className="h-4 w-4" /> {verifying ? 'Recomputing…' : 'Verify hash chain'}
          </Button>
        )}
      </div>
      {verify && (
        <div className={`flex items-center gap-3 rounded-lg border p-4 text-sm ${verify.ok ? 'border-emerald-200 bg-emerald-50 text-emerald-900' : 'border-red-200 bg-red-50 text-red-900'}`}>
          {verify.ok ? <ShieldCheck className="h-5 w-5" /> : <ShieldX className="h-5 w-5" />}
          {verify.ok ? `Chain intact — ${verify.entries_checked} entries recomputed, every hash matches.` : verify.problem}
        </div>
      )}
      {error || verifyError ? <ErrorBox error={error || verifyError} /> : null}
      {loading && !data && <Spinner />}
      {data && (
        <Card>
          <CardHeader title={`${data.total} entries`} subtitle="Newest first. Details hold ids, counts and statuses — never document text." />
          <div className="overflow-x-auto">
            <table className="w-full text-xs">
              <thead>
                <tr className="border-b border-slate-200/70 text-left">
                  <th className="label-xs px-4 py-2">#</th>
                  <th className="label-xs px-3 py-2">When</th>
                  <th className="label-xs px-3 py-2">Who</th>
                  <th className="label-xs px-3 py-2">Action</th>
                  <th className="label-xs px-3 py-2">Entity</th>
                  <th className="label-xs px-3 py-2">Details</th>
                  <th className="label-xs px-3 py-2">Hash</th>
                </tr>
              </thead>
              <tbody>
                {data.entries.map((e) => (
                  <tr key={e.id} className="border-b border-slate-50 align-top">
                    <td className="px-4 py-2 tabular-nums text-slate-400">{e.id}</td>
                    <td className="whitespace-nowrap px-3 py-2 text-slate-500">{formatDate(e.timestamp, true)}</td>
                    <td className="px-3 py-2">
                      <div className="font-medium text-slate-800">{e.actor}</div>
                      <div className="text-[10px] text-slate-400">{humanize(e.role)}</div>
                    </td>
                    <td className="px-3 py-2 font-medium text-slate-700">{humanize(e.action)}</td>
                    <td className="px-3 py-2 text-slate-500">
                      {e.entity_type} {e.entity_id}
                    </td>
                    <td className="max-w-sm truncate px-3 py-2 font-mono text-[10px] text-slate-400" title={JSON.stringify(e.details)}>
                      {e.details ? JSON.stringify(e.details) : ''}
                    </td>
                    <td className="px-3 py-2 font-mono text-[10px] text-slate-400" title={`entry ${e.entry_hash}\nprevious ${e.previous_hash}`}>
                      {short(e.entry_hash, 12)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Card>
      )}
    </div>
  )
}
