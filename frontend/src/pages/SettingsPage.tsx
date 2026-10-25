import { FileCode2 } from 'lucide-react'
import { useState } from 'react'
import { Card, CardHeader, cn, ErrorBox, Spinner } from '../components/ui'
import { api } from '../lib/api'
import { short } from '../lib/format'
import { useApi, useIdentity } from '../lib/identity'

export default function SettingsPage() {
  const { meta } = useIdentity()
  const { data, error, loading } = useApi(() => api.config())
  const [selected, setSelected] = useState('risk_scoring')

  if (error) return <ErrorBox error={error} />
  if (loading && !data) return <Spinner />
  const current = data?.find((c) => c.name === selected) ?? data?.[0]

  return (
    <div className="space-y-5 animate-rise">
      <div>
        <h1 className="font-display text-4xl font-light tracking-tight text-slate-900">Rules &amp; configuration</h1>
        <p className="mt-1 text-sm text-slate-500">
          Every threshold, weight, tolerance and allowlist lives in versioned YAML — none is hard-coded. Each check run stores the versions and hashes it was judged under. Read-only here; changes are made in <code className="font-mono">config/</code> and take effect on the next run.
        </p>
      </div>

      {meta && (
        <Card>
          <CardHeader title="Who may do what" subtitle="Role is asserted in the prototype (no login); every action is recorded with it" />
          <div className="overflow-x-auto p-4">
            <table className="text-xs">
              <thead>
                <tr>
                  <th className="label-xs py-1 pr-6 text-left">Permission</th>
                  {meta.roles.map((r) => (
                    <th key={r} className="label-xs px-4 py-1">
                      {r.replace('_', ' ')}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {Object.entries(meta.permissions).map(([perm, roles]) => (
                  <tr key={perm} className="border-t border-slate-50">
                    <td className="py-1.5 pr-6 text-slate-700">{perm.replaceAll('_', ' ')}</td>
                    {meta.roles.map((r) => (
                      <td key={r} className="px-4 py-1.5 text-center">
                        {roles.includes(r) ? <span className="text-emerald-600">✓ allowed</span> : <span className="text-slate-300">— no</span>}
                      </td>
                    ))}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Card>
      )}

      <div className="grid grid-cols-1 gap-4 lg:grid-cols-[260px_minmax(0,1fr)]">
        <Card className="p-2">
          {data?.map((c) => (
            <button key={c.name} onClick={() => setSelected(c.name)} className={cn('flex w-full items-start gap-2 rounded-md px-3 py-2 text-left', current?.name === c.name ? 'bg-slate-100' : 'hover:bg-slate-50')}>
              <FileCode2 className="mt-0.5 h-4 w-4 text-slate-400" />
              <div>
                <div className="text-sm font-medium text-slate-800">{c.name}.yaml</div>
                <div className="font-mono text-[10px] text-slate-400">{c.config_version}</div>
              </div>
            </button>
          ))}
        </Card>
        {current && (
          <Card>
            <CardHeader title={`config/${current.name}.yaml`} subtitle={<span className="font-mono">{current.config_version} · sha256 {short(current.hash, 16)}</span>} />
            <pre className="max-h-[70vh] overflow-auto p-5 font-mono text-[12px] leading-relaxed text-slate-700">
              {current.raw.split('\n').map((line, i) => (
                <div key={i} className={line.trim().startsWith('#') ? 'text-slate-400' : ''}>
                  {line || ' '}
                </div>
              ))}
            </pre>
          </Card>
        )}
      </div>
    </div>
  )
}
