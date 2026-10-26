import { ArrowLeft, Camera, ClipboardCheck, Download, FileCheck2, History, ListChecks, RefreshCw, ScrollText, Users } from 'lucide-react'
import { useState } from 'react'
import { Link, useParams, useSearchParams } from 'react-router-dom'
import { MeterRow, ScoreGauge } from '../../components/charts'
import { Button, Card, CardHeader, ErrorBox, OfflineReaderBadge, RiskBadge, Spinner, StrengthBadge, Tabs, VerificationBadge } from '../../components/ui'
import { api, downloadWithIdentity } from '../../lib/api'
import { DOCUMENT_TYPE_LABEL, formatDate, humanize } from '../../lib/format'
import { useApi, useIdentity } from '../../lib/identity'
import { allFindings } from '../../lib/report'
import AuditTab from './AuditTab'
import ComplianceTab from './ComplianceTab'
import CertificateTab from './CertificateTab'
import FacultyTab from './FacultyTab'
import FindingsTab from './FindingsTab'
import PhotosTab from './PhotosTab'

type TabId = 'findings' | 'compliance' | 'certificate' | 'faculty' | 'photos' | 'audit'
const RISK_COLOR = { high: 'var(--color-critical)', medium: 'var(--color-warning)', low: 'var(--color-good)' }

export default function ApplicationPage() {
  const id = Number(useParams().id)
  const { can } = useIdentity()
  const { data: report, error, loading, reload } = useApi(() => api.report(id), [id])
  const [params, setParams] = useSearchParams()
  const tab = (params.get('tab') as TabId) || 'findings'
  const setTab = (next: TabId) => setParams(next === 'findings' ? {} : { tab: next }, { replace: true })
  const [rerunning, setRerunning] = useState(false)
  const [exporting, setExporting] = useState(false)
  const [actionError, setActionError] = useState<unknown>(null)

  if (error) return <ErrorBox error={error} />
  if (loading && !report) return <Spinner label="Loading application…" />
  if (!report) return null

  const findings = allFindings(report)
  const risk = report.risk
  const kinds = new Set(report.document_summaries.map((d) => d.kind))

  async function rerun() {
    setRerunning(true)
    setActionError(null)
    try {
      await api.rerun(id)
      reload()
    } catch (err) {
      setActionError(err)
    } finally {
      setRerunning(false)
    }
  }

  const tabs = [
    { id: 'findings' as const, label: 'Indicators', count: findings.filter((f) => f.review_status === 'open').length, icon: <ListChecks className="h-4 w-4" /> },
    ...(report.application_check ? [{ id: 'compliance' as const, label: 'Compliance', count: report.application_check.checks.filter((c) => c.outcome === 'not_met').length, icon: <ClipboardCheck className="h-4 w-4" /> }] : []),
    ...(kinds.has('certificate') ? [{ id: 'certificate' as const, label: 'Certificate', icon: <FileCheck2 className="h-4 w-4" /> }] : []),
    ...(kinds.has('faculty') ? [{ id: 'faculty' as const, label: 'Faculty', icon: <Users className="h-4 w-4" /> }] : []),
    ...(kinds.has('photo') ? [{ id: 'photos' as const, label: 'Photos', count: report.document_summaries.filter((d) => d.kind === 'photo').length, icon: <Camera className="h-4 w-4" /> }] : []),
    { id: 'audit' as const, label: 'History', icon: <ScrollText className="h-4 w-4" /> },
  ]

  return (
    <div className="space-y-5 animate-rise">
      <Link to="/" className="inline-flex items-center gap-1 text-xs text-slate-500 hover:text-slate-800">
        <ArrowLeft className="h-3.5 w-3.5" /> Review queue
      </Link>

      <div className="flex flex-wrap items-start justify-between gap-4">
        <div>
          <div className="label-xs">APP-{String(report.application.id).padStart(4, '0')} · {humanize(report.application.category)}</div>
          <h1 className="mt-1 font-display text-4xl font-light tracking-tight text-slate-900">{report.institution.name}</h1>
          <div className="mt-1 flex flex-wrap items-center gap-2 text-xs text-slate-500">
            <span>Submitted {formatDate(report.application.created_at, true)} · {report.documents.length} documents · scored under <span className="font-mono">{risk.config_version}</span></span>
            {report.field_extraction_fallback?.fallback_used && (
              <OfflineReaderBadge
                reason={report.field_extraction_fallback.fallback_reason}
                breakerOpen={report.field_extraction_fallback.breaker_open}
              />
            )}
          </div>
        </div>
        <div className="flex items-center gap-2">
          <Button
            variant="secondary"
            size="sm"
            disabled={exporting}
            title="A PDF of the indicators, evidence and reviewer actions for the file. The export is recorded in the audit trail."
            onClick={async () => {
              setExporting(true)
              setActionError(null)
              try {
                await downloadWithIdentity(`/api/applications/${id}/export.pdf`, `scrutiny-note-APP-${String(id).padStart(4, '0')}.pdf`)
              } catch (err) {
                setActionError(err)
              } finally {
                setExporting(false)
              }
            }}
          >
            <Download className="h-3.5 w-3.5" /> {exporting ? 'Preparing…' : 'Scrutiny note (PDF)'}
          </Button>
          {can('rerun') && (
            <Button variant="secondary" size="sm" onClick={rerun} disabled={rerunning} title="New check runs under the current rules. Earlier runs are kept.">
              <RefreshCw className={rerunning ? 'h-3.5 w-3.5 animate-spin' : 'h-3.5 w-3.5'} /> {rerunning ? 'Re-running…' : 'Re-run under current rules'}
            </Button>
          )}
        </div>
      </div>

      {actionError ? <ErrorBox error={actionError} /> : null}

      <div className="grid gap-4 lg:grid-cols-[300px_1fr]">
        <Card className="flex flex-col items-center p-5">
          <div className="label-xs self-start">Review priority</div>
          <ScoreGauge score={risk.score} color={RISK_COLOR[risk.level]} />
          <RiskBadge level={risk.level} size="lg" />
          <div className="mt-4 w-full space-y-3">
            {Object.entries(risk.subscores).map(([key, sub]) => (
              <MeterRow key={key} label={sub.label} value={sub.score} max={100} display={sub.score.toFixed(0)} sub={sub.finding_count ? `· ${sub.finding_count}` : ''} color={sub.strong ? 'var(--color-critical)' : sub.medium ? 'var(--color-serious)' : '#94a3b8'} />
            ))}
          </div>
          {risk.floors_applied.length > 0 && (
            <div className="mt-3 w-full rounded-xl bg-slate-50 p-2 text-[11px] text-slate-600">
              {risk.floors_applied.map((f) => (
                <div key={f.rule}>
                  Raised {f.from} → {f.to} by rule <span className="font-mono">{f.rule}</span>
                </div>
              ))}
            </div>
          )}
        </Card>

        <Card className="overflow-hidden">
          <div className="relative overflow-hidden bg-iris-600 px-6 py-6 text-white">
            <svg className="pointer-events-none absolute inset-0 h-full w-full" viewBox="0 0 600 160" preserveAspectRatio="xMidYMid slice" aria-hidden>
              <circle cx="560" cy="170" r="140" fill="none" stroke="white" strokeOpacity="0.25" />
              <circle cx="470" cy="-30" r="80" fill="none" stroke="white" strokeOpacity="0.2" />
              <circle cx="40" cy="30" r="2.5" fill="white" />
            </svg>
            <div className="relative flex items-center justify-between gap-3">
              <span className="rounded-full px-3 py-1 text-xs ring-1 ring-white/50">Why this priority</span>
              <span className="text-[11px] text-white/70">explanation · {report.explanation.generated_by}</span>
            </div>
            <p className="font-display relative mt-4 max-w-3xl text-xl font-light leading-snug sm:text-2xl">{report.explanation.rewrite ?? report.explanation.headline}</p>
          </div>
          <div className="space-y-4 p-6">
            {report.explanation.points.length > 0 && (
              <ul className="space-y-2.5">
                {report.explanation.points.map((point) => (
                  <li key={point.finding_id} className="flex items-start gap-3 text-[13px] leading-relaxed text-slate-600">
                    <span className="mt-0.5">
                      <StrengthBadge strength={point.strength} />
                    </span>
                    <span>{point.text.replace(/^\[[^\]]+\]\s*/, '')}</span>
                  </li>
                ))}
              </ul>
            )}
            {report.explanation.notes.map((n) => (
              <p key={n} className="text-xs text-slate-500">
                {n}
              </p>
            ))}
            <p className="border-t border-dashed border-slate-200 pt-3 text-[11px] italic text-slate-400">{report.explanation.disclaimer}</p>
          </div>
        </Card>
      </div>

      <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
        {report.document_summaries.map((doc) => (
          <Card key={doc.document_id} className="p-4">
            <div className="flex items-start justify-between gap-2">
              <div className="min-w-0">
                <div className="label-xs">{DOCUMENT_TYPE_LABEL[doc.document_type] ?? humanize(doc.document_type)}</div>
                <div className="mt-0.5 truncate text-sm font-medium text-slate-800" title={doc.original_filename}>
                  {doc.original_filename}
                </div>
              </div>
              <span className="text-[11px] tabular-nums text-slate-400">#{doc.document_id}</span>
            </div>
            <div className="mt-3 flex flex-wrap items-center gap-2">
              <VerificationBadge status={doc.verification_status} />
              {doc.strong > 0 && <span className="text-[11px] font-semibold text-red-700">{doc.strong} strong</span>}
              {doc.medium > 0 && <span className="text-[11px] font-semibold text-orange-700">{doc.medium} medium</span>}
              {doc.weak > 0 && <span className="text-[11px] text-slate-500">{doc.weak} weak</span>}
              {doc.finding_count === 0 && <span className="text-[11px] text-slate-400">no indicators</span>}
            </div>
          </Card>
        ))}
      </div>

      <Card>
        <div className="px-3 pt-2">
          <Tabs tabs={tabs} active={tab} onChange={setTab} />
        </div>
        <div className="p-5">
          {tab === 'findings' && <FindingsTab report={report} findings={findings} onReviewed={reload} />}
          {tab === 'compliance' && <ComplianceTab report={report} />}
          {tab === 'certificate' && <CertificateTab report={report} />}
          {tab === 'faculty' && <FacultyTab report={report} />}
          {tab === 'photos' && <PhotosTab report={report} />}
          {tab === 'audit' && <AuditTab applicationId={id} />}
        </div>
      </Card>

      {report.risk_history.length > 1 && (
        <Card>
          <CardHeader title="Priority history" subtitle="Every assessment is kept; nothing is overwritten" icon={<History className="h-4 w-4" />} />
          <div className="flex flex-wrap gap-2 p-5">
            {report.risk_history.map((h) => (
              <div key={h.id} className="rounded-xl ring-1 ring-black/[0.06] px-3 py-2 text-xs">
                <div className="flex items-center gap-2">
                  <RiskBadge level={h.level} size="sm" /> <span className="font-semibold tabular-nums">{h.score.toFixed(1)}</span>
                </div>
                <div className="mt-1 text-slate-500">
                  {humanize(h.trigger)} · {h.actor}
                </div>
                <div className="text-slate-400">{formatDate(h.created_at, true)}</div>
              </div>
            ))}
          </div>
        </Card>
      )}
    </div>
  )
}
