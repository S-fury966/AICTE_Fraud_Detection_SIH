import { Camera, CheckCircle2, FileCheck2, Loader2, Users } from 'lucide-react'
import type { StreamEvent } from '../lib/api'
import { DOCUMENT_TYPE_LABEL, humanize } from '../lib/format'
import { cn, RiskBadge, VerificationBadge } from './ui'

const PLANNED: Record<string, string[]> = {
  certificate: ['extraction', 'certificate_fields', 'qr_checks', 'issuer_verification', 'signature_check', 'duplicate_check'],
  faculty: ['faculty_consistency'],
  photo: ['photo_metadata', 'photo_reuse', 'photo_manipulation', 'photo_synthetic_hint', 'photo_claim_consistency'],
}
const STAGE_LABEL: Record<string, string> = {
  extraction: 'Read text',
  certificate_fields: 'Extract fields',
  qr_checks: 'QR check',
  issuer_verification: 'Issuer lookup',
  signature_check: 'Signature',
  duplicate_check: 'Duplicates',
  faculty_consistency: 'Declared vs website vs AICTE',
  photo_metadata: 'EXIF',
  photo_reuse: 'Reuse search',
  photo_manipulation: 'ELA + copy-move',
  photo_synthetic_hint: 'Synthetic hint',
  photo_claim_consistency: 'Claim check',
}
const KIND_ICON = { certificate: FileCheck2, faculty: Users, photo: Camera } as const

interface Lane {
  filename: string
  documentType: string
  kind: string
  stages: Extract<StreamEvent, { event: 'stage' }>[]
  finished?: Extract<StreamEvent, { event: 'document_finished' }>
}

export function lanesFrom(events: StreamEvent[]): Lane[] {
  const lanes: Lane[] = []
  for (const event of events) {
    if (event.event === 'document_started') lanes.push({ filename: event.filename, documentType: event.document_type, kind: event.kind, stages: [] })
    else if (event.event === 'stage' && lanes.length) lanes[lanes.length - 1].stages.push(event)
    else if (event.event === 'document_finished' && lanes.length) lanes[lanes.length - 1].finished = event
  }
  return lanes
}

export default function LivePipeline({ events, running }: { events: StreamEvent[]; running: boolean }) {
  const lanes = lanesFrom(events)
  const app = events.find((e) => e.event === 'application') as Extract<StreamEvent, { event: 'application' }> | undefined
  const done = events.find((e) => e.event === 'done') as Extract<StreamEvent, { event: 'done' }> | undefined
  const totalDocs = app?.documents ?? lanes.length
  const progress = done ? 100 : totalDocs ? Math.round((lanes.filter((l) => l.finished).length / totalDocs) * 100) : 0

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between gap-4">
        <div className="flex items-center gap-2 text-sm font-medium text-slate-800">
          {running ? <Loader2 className="h-4 w-4 animate-spin text-ink-700" /> : <CheckCircle2 className="h-4 w-4 text-emerald-600" />}
          {app ? `APP-${String(app.application_id).padStart(4, '0')} · ${app.institution}` : 'Starting…'}
        </div>
        <div className="flex items-center gap-3">
          {done && <RiskBadge level={done.risk.level} />}
          <span className="text-xs tabular-nums text-slate-500">{done ? `${done.seconds}s` : `${progress}%`}</span>
        </div>
      </div>
      <div className="h-1 w-full overflow-hidden rounded-full bg-slate-100">
        <div className="h-full rounded-full bg-ink-700 transition-all duration-500" style={{ width: `${Math.max(progress, running ? 6 : 0)}%` }} />
      </div>
      <ol className="space-y-3">
        {lanes.map((lane, index) => {
          const planned = PLANNED[lane.kind] ?? []
          const Icon = KIND_ICON[lane.kind as keyof typeof KIND_ICON] ?? FileCheck2
          const findings = lane.finished?.finding_types.length ?? 0
          return (
            <li key={index} className="animate-rise rounded-2xl bg-white ring-1 ring-black/[0.06] p-3">
              <div className="flex flex-wrap items-center justify-between gap-2">
                <div className="flex items-center gap-2 text-sm">
                  <Icon className="h-4 w-4 text-slate-400" />
                  <span className="font-medium text-slate-800">{lane.filename}</span>
                  <span className="text-xs text-slate-400">{DOCUMENT_TYPE_LABEL[lane.documentType] ?? humanize(lane.documentType)}</span>
                </div>
                {lane.finished ? (
                  <div className="flex items-center gap-2 text-xs">
                    <VerificationBadge status={lane.finished.verification_status} />
                    <span className={findings ? 'font-semibold text-orange-700' : 'text-slate-400'}>{findings} indicator(s)</span>
                  </div>
                ) : (
                  <Loader2 className="h-3.5 w-3.5 animate-spin text-slate-400" />
                )}
              </div>
              <div className="mt-2.5 flex flex-wrap gap-1.5">
                {planned.map((stage) => {
                  const result = lane.stages.find((s) => s.stage === stage)
                  const active = !result && !lane.finished && lane.stages.length === planned.indexOf(stage)
                  return (
                    <span
                      key={stage}
                      title={result ? `${result.status}${result.detail_status ? ` · ${humanize(result.detail_status)}` : ''}${result.reason ? ` — ${result.reason}` : ''}` : 'pending'}
                      className={cn(
                        'inline-flex items-center gap-1 rounded-full border px-2 py-0.5 text-[11px] transition-all duration-300',
                        !result && !active && 'border-slate-200 text-slate-300',
                        active && 'border-ink-600 text-ink-700',
                        result?.status === 'ok' && 'border-emerald-200 bg-emerald-50 text-emerald-800',
                        result?.status === 'degraded' && 'border-amber-200 bg-amber-50 text-amber-800',
                        result?.status === 'skipped' && 'border-slate-200 bg-slate-50 text-slate-500',
                        result?.status === 'failed' && 'border-red-200 bg-red-50 text-red-700',
                      )}
                    >
                      {active && <Loader2 className="h-3 w-3 animate-spin" />}
                      {STAGE_LABEL[stage] ?? humanize(stage)}
                      {result && <span className="tabular-nums opacity-60">{Math.round(result.duration_ms)}ms</span>}
                      {!!result?.finding_count && <span className="rounded-full bg-orange-100 px-1 font-semibold text-orange-800">{result.finding_count}</span>}
                    </span>
                  )
                })}
              </div>
            </li>
          )
        })}
      </ol>
    </div>
  )
}
