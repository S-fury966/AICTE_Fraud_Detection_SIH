import { Check, ChevronDown, HelpCircle, MessageSquare, X } from 'lucide-react'
import { useState } from 'react'
import { Link } from 'react-router-dom'
import { api } from '../lib/api'
import { DETECTOR_LABEL, formatDate, humanize } from '../lib/format'
import { useIdentity } from '../lib/identity'
import type { Finding } from '../lib/types'
import { Button, cn, EvidenceView, ReviewBadge, StrengthBadge } from './ui'

const ACTIONS = [
  { action: 'confirm', label: 'Confirm', icon: Check, variant: 'danger' as const, hint: 'The indicator holds up on inspection' },
  { action: 'dismiss', label: 'Dismiss', icon: X, variant: 'secondary' as const, hint: 'Not a concern; it stops counting toward priority' },
  { action: 'request_more_info', label: 'Need info', icon: HelpCircle, variant: 'secondary' as const, hint: 'Ask the institution for clarification' },
  { action: 'comment', label: 'Comment', icon: MessageSquare, variant: 'ghost' as const, hint: 'Note only; status unchanged' },
]

export default function FindingCard({ finding, onReviewed, showContext = false, defaultOpen = false }: { finding: Finding; onReviewed?: (f: Finding) => void; showContext?: boolean; defaultOpen?: boolean }) {
  const { can } = useIdentity()
  const [open, setOpen] = useState(defaultOpen)
  const [note, setNote] = useState('')
  const [busy, setBusy] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [current, setCurrent] = useState(finding)
  const [saved, setSaved] = useState<string | null>(null)

  async function act(action: string) {
    setBusy(action)
    setError(null)
    try {
      const result = await api.review(current.id, action, note)
      setCurrent(result.finding)
      setNote('')
      const risk = result.risk as { level?: string; score?: number }
      setSaved(`Recorded in the audit trail. Application priority now ${risk.level ?? '—'} (${risk.score ?? '—'}).`)
      onReviewed?.(result.finding)
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setBusy(null)
    }
  }

  const rail = current.strength === 'strong' ? 'var(--color-critical)' : current.strength === 'medium' ? 'var(--color-serious)' : '#d6d6d1'

  return (
    <div className={cn('relative overflow-hidden rounded-2xl bg-white ring-1 ring-black/[0.06] transition hover:shadow-[0_8px_24px_-16px_rgba(20,20,22,0.35)]', current.review_status === 'dismissed' && 'opacity-60')}>
      <span className="absolute inset-y-3 left-0 w-1 rounded-r-full" style={{ background: rail }} />
      <button className="flex w-full items-start gap-3 py-3.5 pr-4 pl-5 text-left" onClick={() => setOpen(!open)}>
        <div className="flex-1 space-y-1.5">
          <div className="flex flex-wrap items-center gap-2">
            <StrengthBadge strength={current.strength} />
            <span className="text-sm font-semibold text-slate-900">{humanize(current.type)}</span>
            <span className="rounded-full bg-slate-100 px-1.5 py-0.5 text-[10px] font-medium text-slate-500">{DETECTOR_LABEL[current.detector] ?? current.detector}</span>
            {current.confidence && <span className="text-[10px] text-slate-400">confidence: {current.confidence}</span>}
            <ReviewBadge status={current.review_status} />
          </div>
          <p className="text-[13px] leading-relaxed text-slate-600">{current.reason}</p>
          {showContext && current.institution && (
            <div className="text-xs text-slate-500">
              <Link to={`/applications/${current.application_id}`} className="font-medium text-iris-600 hover:underline" onClick={(e) => e.stopPropagation()}>
                {current.institution}
              </Link>{' '}
              · {current.original_filename}
            </div>
          )}
        </div>
        <ChevronDown className={cn('mt-1 h-4 w-4 shrink-0 text-slate-400 transition', open && 'rotate-180')} />
      </button>

      {open && (
        <div className="space-y-4 border-t border-dashed border-slate-200 py-4 pr-4 pl-5">
          <div>
            <div className="label-xs mb-2">Evidence</div>
            <EvidenceView evidence={current.evidence} />
          </div>

          {current.review_actions.length > 0 && (
            <div>
              <div className="label-xs mb-2">Review history</div>
              <ol className="space-y-1.5 text-xs">
                {current.review_actions.map((entry, index) => (
                  <li key={index} className="flex gap-2 text-slate-600">
                    <span className="text-slate-400">{formatDate(entry.created_at, true)}</span>
                    <span className="font-medium text-slate-800">{entry.actor}</span>
                    <span className="text-slate-400">({humanize(entry.role)})</span>
                    <span>{humanize(entry.action)}</span>
                    {entry.note && <span className="italic text-slate-500">“{entry.note}”</span>}
                  </li>
                ))}
              </ol>
            </div>
          )}

          {can('review_finding') ? (
            <div className="space-y-2">
              <div className="label-xs">Your review</div>
              <textarea
                value={note}
                onChange={(e) => setNote(e.target.value)}
                rows={2}
                placeholder="Note for the record (optional) — e.g. what you checked and against which source"
                className="focus-ring w-full resize-none rounded-xl ring-1 ring-black/[0.06] px-3 py-2 text-xs"
              />
              <div className="flex flex-wrap gap-2">
                {ACTIONS.map(({ action, label, icon: Icon, variant, hint }) => (
                  <Button key={action} size="sm" variant={variant} disabled={busy !== null} onClick={() => act(action)} title={hint}>
                    <Icon className="h-3.5 w-3.5" /> {busy === action ? 'Saving…' : label}
                  </Button>
                ))}
              </div>
              {error && <div className="text-xs text-red-700">{error}</div>}
              {saved && <div className="rounded-full bg-emerald-50 px-2 py-1 text-xs text-emerald-800">{saved}</div>}
            </div>
          ) : (
            <div className="text-xs text-slate-400">Your role cannot record reviews.</div>
          )}
        </div>
      )}
    </div>
  )
}
