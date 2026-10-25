import { Network, Share2 } from 'lucide-react'
import { useMemo, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { Legend } from '../components/charts'
import { Card, CardHeader, cn, Empty, ErrorBox, RiskBadge, Spinner, StrengthBadge } from '../components/ui'
import { api } from '../lib/api'
import { useApi } from '../lib/identity'
import type { NetworkData, NetworkEdge, NetworkNode } from '../lib/types'

const W = 900
const H = 560
const RISK_FILL: Record<string, string> = { high: '#e5484d', medium: '#f5b83d', low: '#2f9e6e' }
const EDGE_STROKE: Record<string, string> = { strong: '#e5484d', medium: '#f28c38', weak: '#a3a39e' }

interface Placed extends NetworkNode {
  x: number
  y: number
}

/** A small deterministic force layout: same data, same picture. */
function layout(data: NetworkData): Placed[] {
  const nodes = data.nodes.filter((n) => n.connected)
  const placed: Placed[] = nodes.map((n, i) => {
    const angle = (i / Math.max(nodes.length, 1)) * Math.PI * 2
    return { ...n, x: W / 2 + Math.cos(angle) * 300, y: H / 2 + Math.sin(angle) * 200 }
  })
  const index = new Map(placed.map((n, i) => [n.id, i]))
  const links = data.edges.map((e) => [index.get(e.source)!, index.get(e.target)!] as const).filter(([a, b]) => a !== undefined && b !== undefined)
  for (let step = 0; step < 400; step++) {
    const cooling = 1 - step / 400
    const fx = new Array(placed.length).fill(0)
    const fy = new Array(placed.length).fill(0)
    for (let i = 0; i < placed.length; i++) {
      for (let j = i + 1; j < placed.length; j++) {
        const dx = placed[i].x - placed[j].x
        const dy = placed[i].y - placed[j].y
        const d2 = Math.max(dx * dx + dy * dy, 100)
        const f = 90000 / d2
        const d = Math.sqrt(d2)
        fx[i] += (dx / d) * f
        fy[i] += (dy / d) * f
        fx[j] -= (dx / d) * f
        fy[j] -= (dy / d) * f
      }
    }
    for (const [a, b] of links) {
      const dx = placed[b].x - placed[a].x
      const dy = placed[b].y - placed[a].y
      const d = Math.max(Math.sqrt(dx * dx + dy * dy), 1)
      const f = (d - 340) * 0.04
      fx[a] += (dx / d) * f
      fy[a] += (dy / d) * f
      fx[b] -= (dx / d) * f
      fy[b] -= (dy / d) * f
    }
    placed.forEach((n, i) => {
      fx[i] += (W / 2 - n.x) * 0.004
      fy[i] += (H / 2 - n.y) * 0.006
      n.x = Math.min(W - 110, Math.max(110, n.x + Math.max(-20, Math.min(20, fx[i])) * cooling))
      n.y = Math.min(H - 66, Math.max(50, n.y + Math.max(-20, Math.min(20, fy[i])) * cooling))
    })
  }
  return placed
}

export default function NetworkPage() {
  const { data, error, loading } = useApi(() => api.network())
  const navigate = useNavigate()
  const [selected, setSelected] = useState<NetworkEdge | null>(null)
  const [hover, setHover] = useState<string | null>(null)
  const placed = useMemo(() => (data ? layout(data) : []), [data])
  const byId = new Map(placed.map((n) => [n.id, n]))

  if (error) return <ErrorBox error={error} />
  if (loading && !data) return <Spinner />
  if (!data) return null
  const isolated = data.nodes.filter((n) => !n.connected)

  return (
    <div className="space-y-5 animate-rise">
      <div>
        <h1 className="font-display text-4xl font-light tracking-tight text-slate-900">Connections</h1>
        <p className="mt-1 text-sm text-slate-500">
          Institutions linked by the same certificate, QR payload, file, photo or faculty member. One finding sits in one report; the pattern only shows here.
        </p>
      </div>

      <div className="grid grid-cols-1 gap-5 xl:grid-cols-[1fr_340px]">
        <Card className="overflow-hidden">
          <CardHeader
            icon={<Network className="h-4 w-4" />}
            title={`${data.edges.length} link(s) between ${placed.length} institutions`}
            subtitle={data.note}
            action={<Legend items={[{ label: 'strong link', color: EDGE_STROKE.strong }, { label: 'medium', color: EDGE_STROKE.medium }]} />}
          />
          {placed.length === 0 ? (
            <Empty title="No shared evidence between institutions" icon={<Share2 className="h-8 w-8" />} />
          ) : (
            <div className="overflow-x-auto bg-[radial-gradient(circle_at_center,#ffffff,#f3f2ee)]">
              <svg viewBox={`0 0 ${W} ${H}`} className="h-auto w-full min-w-[640px]" role="img" aria-label="Network of institutions sharing evidence">
                {data.edges.map((edge) => {
                  const a = byId.get(edge.source)
                  const b = byId.get(edge.target)
                  if (!a || !b) return null
                  const total = edge.kinds.reduce((s, k) => s + k.count, 0)
                  const active = selected === edge || hover === edge.source || hover === edge.target
                  return (
                    <g key={`${edge.source}-${edge.target}`} className="cursor-pointer" onClick={() => setSelected(edge)}>
                      <line x1={a.x} y1={a.y} x2={b.x} y2={b.y} stroke="transparent" strokeWidth={18} />
                      <line x1={a.x} y1={a.y} x2={b.x} y2={b.y} stroke={EDGE_STROKE[edge.strength]} strokeWidth={2 + Math.min(total, 4)} strokeOpacity={active ? 0.95 : 0.55} strokeDasharray={edge.strength === 'strong' ? undefined : '8 5'} />
                      <EdgeLabel x={(a.x + b.x) / 2} y={(a.y + b.y) / 2} text={edge.kinds.length > 1 ? `${edge.kinds[0].label} +${edge.kinds.length - 1}` : edge.kinds[0]?.label ?? ''} strength={edge.strength} />
                    </g>
                  )
                })}
                {placed.map((node) => {
                  const r = node.kind === 'records_only' ? 14 : 18 + Math.min(node.open_strong, 5) * 2
                  return (
                    <g
                      key={node.id}
                      transform={`translate(${node.x},${node.y})`}
                      className={cn(node.application_id && 'cursor-pointer')}
                      onMouseEnter={() => setHover(node.id)}
                      onMouseLeave={() => setHover(null)}
                      onClick={() => node.application_id && navigate(`/applications/${node.application_id}`)}
                    >
                      <circle r={r + 5} fill="white" opacity={hover === node.id ? 1 : 0.7} />
                      <circle r={r} fill={node.kind === 'records_only' ? '#e2e8f0' : RISK_FILL[node.risk_level ?? ''] ?? '#94a3b8'} stroke="white" strokeWidth={2} />
                      {node.open_strong > 0 && (
                        <text textAnchor="middle" dy={4} className="fill-white" style={{ fontSize: 11, fontWeight: 700 }}>
                          {node.open_strong}
                        </text>
                      )}
                      <text textAnchor="middle" y={r + 18} className="fill-slate-800" style={{ fontSize: 12, fontWeight: 600, paintOrder: 'stroke', stroke: '#f8fafc', strokeWidth: 4 }}>
                        {node.label.length > 34 ? `${node.label.slice(0, 32)}…` : node.label}
                      </text>
                      <text textAnchor="middle" y={r + 32} className="fill-slate-500" style={{ fontSize: 10, paintOrder: 'stroke', stroke: '#f8fafc', strokeWidth: 4 }}>
                        {node.kind === 'records_only' ? 'AICTE records only' : `${node.risk_level ?? 'unscored'} priority`}
                      </text>
                    </g>
                  )
                })}
              </svg>
            </div>
          )}
          <div className="border-t border-slate-200/70 px-5 py-2.5 text-[11px] text-slate-500">
            Node colour = review priority (with label) · number = open strong indicators · click a node to open its application, a link for its evidence
          </div>
        </Card>

        <div className="space-y-4">
          <Card>
            <CardHeader title={selected ? 'Shared evidence' : 'Select a link'} subtitle={selected ? `${byId.get(selected.source)?.label} ↔ ${byId.get(selected.target)?.label}` : 'Click any line in the graph'} />
            <div className="space-y-3 p-4">
              {!selected && <p className="text-xs text-slate-500">A link means the two institutions submitted something in common. It is a reason to look at both, not a conclusion about either.</p>}
              {selected && (
                <>
                  <StrengthBadge strength={selected.strength} />
                  {selected.kinds.map((k) => (
                    <div key={k.kind} className="flex justify-between text-sm">
                      <span className="text-slate-700">{k.label}</span>
                      <span className="tabular-nums text-slate-500">×{k.count}</span>
                    </div>
                  ))}
                  <div className="space-y-1.5 border-t border-slate-200/70 pt-3">
                    {selected.details.map((d, i) => (
                      <div key={i} className="font-mono text-[11px] text-slate-500">
                        {String(d.label)}
                        {d.certificate_number ? ` · ${String(d.certificate_number)}` : ''}
                        {d.count ? ` · ${String(d.count)} people` : ''}
                        {Array.isArray(d.documents) ? ` · documents #${(d.documents as number[]).join(', #')}` : ''}
                      </div>
                    ))}
                  </div>
                </>
              )}
            </div>
          </Card>
          <Card>
            <CardHeader title="No shared evidence" subtitle={`${isolated.length} institution(s)`} />
            <ul className="divide-y divide-slate-50">
              {isolated.map((node) => (
                <li key={node.id} className="flex cursor-pointer items-center justify-between gap-2 px-4 py-2 hover:bg-slate-50" onClick={() => node.application_id && navigate(`/applications/${node.application_id}`)}>
                  <span className="truncate text-xs text-slate-700">{node.label}</span>
                  <RiskBadge level={node.risk_level} size="sm" />
                </li>
              ))}
            </ul>
          </Card>
        </div>
      </div>
    </div>
  )
}

function EdgeLabel({ x, y, text, strength }: { x: number; y: number; text: string; strength: string }) {
  const width = text.length * 6.2 + 16
  return (
    <g transform={`translate(${x},${y})`}>
      <rect x={-width / 2} y={-10} width={width} height={20} rx={10} fill="white" stroke={EDGE_STROKE[strength]} strokeWidth={1} />
      <text textAnchor="middle" dy={4} className="fill-slate-700" style={{ fontSize: 10.5, fontWeight: 500 }}>
        {text}
      </text>
    </g>
  )
}
