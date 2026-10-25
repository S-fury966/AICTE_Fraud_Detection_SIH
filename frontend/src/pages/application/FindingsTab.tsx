import { CheckCircle2 } from 'lucide-react'
import { useState } from 'react'
import FindingCard from '../../components/FindingCard'
import { Empty } from '../../components/ui'
import { DETECTOR_LABEL } from '../../lib/format'
import type { ApplicationReport, Finding } from '../../lib/types'

const ORDER = ['application_validation', 'certificate_authenticity', 'duplicate_detection', 'faculty_consistency', 'regulatory_compliance', 'photo_authenticity']
const RANK = { strong: 0, medium: 1, weak: 2 } as const

// Fires against nearly every certificate sharing an issuer's template (step 6
// measured why); collapsed by default so it does not bury the decisive ones.
const COLLAPSIBLE = new Set(['near_duplicate_page_image'])

export default function FindingsTab({ report, findings, onReviewed }: { report: ApplicationReport; findings: Finding[]; onReviewed: () => void }) {
  const [showCollapsed, setShowCollapsed] = useState(false)
  const points = new Map(report.risk.contributions.map((c) => [c.finding_id, c.points]))

  if (findings.length === 0) {
    return (
      <Empty title="No indicators raised" icon={<CheckCircle2 className="h-8 w-8 text-emerald-300" />}>
        The automated checks raised nothing for priority review. That is not a finding that the documents are sound.
      </Empty>
    )
  }

  const groups = ORDER.map((detector) => ({
    detector,
    items: findings.filter((f) => f.detector === detector).sort((a, b) => RANK[a.strength] - RANK[b.strength]),
  })).filter((g) => g.items.length)

  return (
    <div className="space-y-6">
      {groups.map(({ detector, items }) => {
        const main = items.filter((f) => !COLLAPSIBLE.has(f.type) || f.strength !== 'weak')
        const folded = items.filter((f) => COLLAPSIBLE.has(f.type) && f.strength === 'weak')
        return (
          <section key={detector}>
            <div className="mb-2 flex items-baseline justify-between">
              <h3 className="text-sm font-semibold text-slate-800">{DETECTOR_LABEL[detector] ?? detector}</h3>
              <span className="text-xs text-slate-400">{items.length} indicator(s)</span>
            </div>
            <div className="space-y-2">
              {main.map((f) => (
                <div key={f.id} className="relative">
                  <FindingCard finding={f} onReviewed={onReviewed} />
                  {points.has(f.id) && (
                    <span className="absolute right-11 top-3 rounded bg-slate-50 px-1.5 py-0.5 text-[10px] tabular-nums text-slate-500" title="Contribution to the review-priority score">
                      +{points.get(f.id)?.toFixed(1)} pts
                    </span>
                  )}
                </div>
              ))}
              {folded.length > 0 && (
                <div className="rounded-2xl border border-dashed border-slate-300 p-3">
                  <button className="text-xs font-medium text-iris-600 hover:underline" onClick={() => setShowCollapsed(!showCollapsed)}>
                    {showCollapsed ? 'Hide' : 'Show'} {folded.length} weak page-similarity match(es)
                  </button>
                  <p className="mt-1 text-[11px] text-slate-500">
                    Certificates from one issuer share a template, and a template sibling is indistinguishable from a re-saved copy by page image alone. The certificate-number checks are what decide.
                  </p>
                  {showCollapsed && (
                    <div className="mt-3 space-y-2">
                      {folded.map((f) => (
                        <FindingCard key={f.id} finding={f} onReviewed={onReviewed} />
                      ))}
                    </div>
                  )}
                </div>
              )}
            </div>
          </section>
        )
      })}
    </div>
  )
}
