import { CheckCircle2, ExternalLink, FileSignature, Fingerprint, Link2, QrCode, ScanText, ShieldQuestion, XCircle } from 'lucide-react'
import type React from 'react'
import { Link } from 'react-router-dom'
import { Card, CardHeader, cn, Empty, SecureImage, StageDot, VerificationBadge } from '../../components/ui'
import { fileUrl, openWithIdentity, previewUrl } from '../../lib/api'
import { display, humanize, short } from '../../lib/format'
import type { ApplicationReport, DocumentEntry, ExtractedField } from '../../lib/types'

const CERT_TYPES = new Set(['accreditation_certificate', 'noc'])

export default function CertificateTab({ report }: { report: ApplicationReport }) {
  const certificates = report.documents.filter((d) => CERT_TYPES.has(d.document_type) || !['faculty_declaration', 'lab_photo', 'classroom_photo', 'infrastructure_photo'].includes(d.document_type))
  if (!certificates.length) return <Empty title="No certificate in this application" />
  return (
    <div className="space-y-8">
      {certificates.map((doc) => (
        <CertificateView key={doc.id} doc={doc} />
      ))}
    </div>
  )
}

function CertificateView({ doc }: { doc: DocumentEntry }) {
  const run = doc.runs.at(-1)
  if (!run) return <Empty title="Not processed yet" />
  const lookups = run.verification_lookups
  const status = run.stages.find((s) => s.stage === 'issuer_verification')?.detail_status

  return (
    <div className="grid grid-cols-1 gap-5 xl:grid-cols-[minmax(280px,380px)_1fr]">
      <div className="space-y-3">
        <div className="overflow-hidden rounded-2xl ring-1 ring-black/[0.06] bg-slate-50">
          <SecureImage src={previewUrl(doc.id, 760)} alt={`Page 1 of ${doc.original_filename}`} className="w-full" />
        </div>
        <div className="flex items-center justify-between text-xs text-slate-500">
          <span className="truncate">{doc.original_filename}</span>
          <button className="inline-flex items-center gap-1 text-iris-600 hover:underline" onClick={() => openWithIdentity(fileUrl(doc.id)).catch(() => undefined)}>
            Open file <ExternalLink className="h-3 w-3" />
          </button>
        </div>
        <Card className="p-4">
          <div className="label-xs mb-2">Check stages · run #{run.id}</div>
          <ol className="space-y-1.5">
            {run.stages.map((stage) => (
              <li key={stage.stage} className="flex items-start gap-2 text-xs">
                <span className="mt-1">
                  <StageDot status={stage.status} />
                </span>
                <div className="min-w-0 flex-1">
                  <div className="flex justify-between gap-2">
                    <span className="font-medium text-slate-700">{humanize(stage.stage)}</span>
                    <span className="tabular-nums text-slate-400">{stage.duration_ms ? `${stage.duration_ms.toFixed(0)} ms` : ''}</span>
                  </div>
                  <div className="text-slate-500">
                    {stage.status}
                    {stage.detail_status ? ` · ${humanize(stage.detail_status)}` : ''}
                  </div>
                  {stage.reason && <div className="text-[11px] text-slate-400">{stage.reason}</div>}
                </div>
              </li>
            ))}
          </ol>
          <div className="mt-3 border-t border-slate-200/70 pt-2 text-[11px] text-slate-400">
            Field reader: <span className="font-mono">{run.provider ?? '—'}</span> / {run.model_name ?? '—'} · prompt {run.prompt_version ?? '—'}
          </div>
        </Card>
      </div>

      <div className="space-y-5">
        <Card>
          <CardHeader
            icon={<ScanText className="h-4 w-4" />}
            title="Fields read from the certificate"
            subtitle="A value is kept only if it can be found in the page text; identifiers must appear verbatim"
          />
          <div className="overflow-x-auto">
            <table className="w-full text-xs">
              <thead>
                <tr className="border-b border-slate-200/70 text-left">
                  <th className="label-xs px-4 py-2">Field</th>
                  <th className="label-xs px-3 py-2">Value</th>
                  <th className="label-xs px-3 py-2">Traced to source</th>
                  <th className="label-xs px-3 py-2">Snippet</th>
                </tr>
              </thead>
              <tbody>
                {run.extracted_fields.map((field) => (
                  <tr key={field.name} className="border-b border-slate-50 align-top">
                    <td className="px-4 py-2 font-medium text-slate-700">{humanize(field.name)}</td>
                    <td className="px-3 py-2 text-slate-900">
                      {display(field.value)}
                      {field.value_iso && <div className="text-[10px] text-slate-400">ISO {field.value_iso}</div>}
                    </td>
                    <td className="px-3 py-2">
                      {field.validated ? (
                        <span className="inline-flex items-center gap-1 text-emerald-700">
                          <CheckCircle2 className="h-3.5 w-3.5" /> yes
                        </span>
                      ) : field.invalidation_reason ? (
                        <span className="inline-flex items-center gap-1 text-red-700" title={`claimed: ${display(field.model_value)}`}>
                          <XCircle className="h-3.5 w-3.5" /> {humanize(field.invalidation_reason)}
                        </span>
                      ) : (
                        <span className="text-slate-400">not printed</span>
                      )}
                    </td>
                    <td className="max-w-xs px-3 py-2 font-mono text-[11px] text-slate-500">{field.source_snippet ?? '—'}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Card>

        <TracedText pages={run.pages} fields={run.extracted_fields} />

        <Card>
          <CardHeader icon={<QrCode className="h-4 w-4" />} title="QR code" subtitle="Decoded and compared. A QR URL is never opened, resolved or followed." />
          <div className="p-4">
            {run.qr_detections.length === 0 ? (
              <div className="text-xs text-slate-500">No QR code found.</div>
            ) : (
              run.qr_detections.map((qr, i) => (
                <div key={i} className="grid gap-1 text-xs sm:grid-cols-[110px_1fr]">
                  <span className="text-slate-500">Payload</span>
                  <span className="break-all font-mono text-slate-800">{qr.raw_text}</span>
                  <span className="text-slate-500">Host</span>
                  <span className="font-mono text-slate-800">{qr.parsed_host ?? '—'}</span>
                  <span className="text-slate-500">Certificate ID</span>
                  <span className="font-mono text-slate-800">{qr.certificate_id ?? '—'}</span>
                  <span className="text-slate-500">Decoded by</span>
                  <span className="text-slate-600">
                    {qr.decoder} · {qr.source} · page {qr.page}
                  </span>
                </div>
              ))
            )}
          </div>
        </Card>

        <Card>
          <CardHeader
            icon={<Fingerprint className="h-4 w-4" />}
            title="Issuer verification (mock issuer)"
            subtitle="The issuer address comes from configuration only — never from the document"
            action={<VerificationBadge status={status} />}
          />
          <div className="space-y-4 p-4">
            {lookups.length === 0 && <div className="text-xs text-slate-500">No lookup was made.</div>}
            {lookups.map((lookup) => (
              <div key={`${lookup.certificate_id}-${lookup.id_origin}`} className="space-y-2">
                <div className="flex flex-wrap items-center gap-2 text-xs">
                  <span className="font-mono font-semibold text-slate-800">{lookup.certificate_id}</span>
                  <span className="rounded-full bg-slate-100 px-1.5 py-0.5 text-[10px] text-slate-600">from {humanize(lookup.id_origin)}</span>
                  {lookup.trust && <span className="rounded-full bg-slate-100 px-1.5 py-0.5 text-[10px] text-slate-600">{lookup.trust} trust</span>}
                  <VerificationBadge status={lookup.status} />
                  <span className="text-slate-400">
                    HTTP {lookup.http_status ?? '—'} · {lookup.latency_ms?.toFixed(0) ?? '—'} ms · {lookup.attempt_count} attempt(s)
                  </span>
                </div>
                {lookup.comparisons && lookup.comparisons.length > 0 && (
                  <div className="overflow-x-auto">
                    <table className="w-full text-xs">
                      <thead>
                        <tr className="border-b border-slate-200/70 text-left">
                          <th className="label-xs py-1.5 pr-3">Field</th>
                          <th className="label-xs py-1.5 pr-3">On the document</th>
                          <th className="label-xs py-1.5 pr-3">Issuer record</th>
                          <th className="label-xs py-1.5">Outcome</th>
                        </tr>
                      </thead>
                      <tbody>
                        {lookup.comparisons.map((c) => (
                          <tr key={c.name} className="border-b border-slate-50 align-top">
                            <td className="py-1.5 pr-3 font-medium text-slate-700">{humanize(c.name)}</td>
                            <td className="py-1.5 pr-3 text-slate-800">{display(c.submitted_value)}</td>
                            <td className="py-1.5 pr-3 text-slate-800">{display(c.issuer_value)}</td>
                            <td className={cn('py-1.5 font-medium', c.outcome === 'match' ? 'text-emerald-700' : c.outcome === 'mismatch' ? 'text-red-700' : 'text-slate-500')} title={c.reason ?? ''}>
                              {humanize(c.outcome)}
                            </td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                )}
                {lookup.error && <div className="text-xs text-slate-500">{lookup.error}</div>}
              </div>
            ))}
          </div>
        </Card>

        <div className="grid gap-5 lg:grid-cols-2">
          <Card>
            <CardHeader icon={<FileSignature className="h-4 w-4" />} title="Digital signature" subtitle="Integrity only: was the file changed after signing?" />
            <div className="space-y-2 p-4 text-xs">
              {run.signatures.length === 0 ? (
                <div className="text-slate-500">No signature present. Neutral: most attachments are unsigned scans.</div>
              ) : (
                run.signatures.map((sig) => (
                  <div key={sig.signature_index} className="space-y-1">
                    <div className="flex items-center gap-2">
                      {sig.digest_matches === false ? <XCircle className="h-4 w-4 text-red-600" /> : <CheckCircle2 className="h-4 w-4 text-emerald-600" />}
                      <span className="font-medium text-slate-800">{sig.field_name ?? `Signature ${sig.signature_index + 1}`}</span>
                    </div>
                    <div className="text-slate-600">
                      Coverage: <span className="font-mono">{sig.coverage ?? '—'}</span> · digest {sig.digest_matches === null ? 'not compared' : sig.digest_matches ? 'matches' : 'DOES NOT match'}
                    </div>
                    <div className="text-slate-500">Claimed signer (unverified): {sig.signer_name_unverified ?? '—'}</div>
                  </div>
                ))
              )}
              <div className="flex items-start gap-1.5 rounded bg-amber-50 p-2 text-[11px] text-amber-900">
                <ShieldQuestion className="mt-0.5 h-3.5 w-3.5 shrink-0" />
                Signer identity is never verified (no certificate chain). An intact signature is neutral — it never lowers priority.
              </div>
            </div>
          </Card>

          <Card>
            <CardHeader icon={<Link2 className="h-4 w-4" />} title="Seen elsewhere in the corpus" subtitle="Relationships with other submitted documents, read from both sides" />
            <div className="p-4">
              {doc.cross_references.length === 0 ? (
                <div className="text-xs text-slate-500">No relationship with any other stored document.</div>
              ) : (
                <ul className="space-y-1.5 text-xs">
                  {doc.cross_references.map((x) => (
                    <li key={x.id} className="flex items-center justify-between gap-2">
                      <span>
                        <span className="font-medium text-slate-800">{humanize(x.kind)}</span>
                        <span className="text-slate-400"> · document #{x.other_document_id}</span>
                      </span>
                      <span className={cn('rounded px-1.5 py-0.5 text-[10px]', x.same_institution ? 'bg-slate-100 text-slate-500' : 'bg-red-50 text-red-700')}>
                        {x.same_institution ? 'same institution' : 'other institution'}
                      </span>
                    </li>
                  ))}
                </ul>
              )}
              <div className="mt-3 text-[11px] text-slate-400">SHA-256 {short(doc.file_sha256, 16)}</div>
            </div>
          </Card>
        </div>
        <div className="text-[11px] text-slate-400">
          Need the other side? Open the{' '}
          <Link to="/findings" className="underline">
            findings queue
          </Link>{' '}
          filtered by detector.
        </div>
      </div>
    </div>
  )
}

/** The page text the fields were read from, with each traced snippet marked. */
function TracedText({
  pages,
  fields,
}: {
  pages: {
    page_number: number
    extraction_method: string | null
    text: string | null
  }[]
  fields: ExtractedField[]
}) {
  const traced = fields.filter((f) => f.validated && f.source_snippet)
  if (!pages.some((p) => p.text)) return null
  return (
    <Card>
      <CardHeader
        icon={<ScanText className="h-4 w-4" />}
        title="Where each value was found"
        subtitle="The extracted page text, with the snippet behind every kept value highlighted. Nothing unhighlighted was used."
      />
      <div className="space-y-3 p-4">
        {pages.map((page) =>
          page.text ? (
            <div key={page.page_number}>
              <div className="label-xs mb-1">
                Page {page.page_number} · {humanize(page.extraction_method)}
              </div>
              <pre className="max-h-80 overflow-auto whitespace-pre-wrap rounded-xl bg-slate-50 p-3 font-mono text-[11.5px] leading-relaxed text-slate-600">{highlight(page.text, traced)}</pre>
            </div>
          ) : null,
        )}
      </div>
    </Card>
  )
}

function highlight(text: string, fields: ExtractedField[]) {
  const marks: { start: number; end: number; name: string }[] = []
  const lower = text.toLowerCase()
  for (const field of fields) {
    const snippet = (field.source_snippet ?? '').trim()
    const at = lower.indexOf(snippet.toLowerCase())
    if (snippet && at >= 0 && !marks.some((m) => at < m.end && at + snippet.length > m.start)) marks.push({ start: at, end: at + snippet.length, name: field.name })
  }
  marks.sort((a, b) => a.start - b.start)
  const out: React.ReactNode[] = []
  let cursor = 0
  marks.forEach((m, i) => {
    if (m.start > cursor) out.push(text.slice(cursor, m.start))
    out.push(
      <mark key={i} title={humanize(m.name)} className="rounded-full bg-saffron-100 px-0.5 text-slate-900 ring-1 ring-amber-300">
        {text.slice(m.start, m.end)}
        <sup className="ml-0.5 font-sans text-[8px] font-semibold uppercase text-amber-700">{humanize(m.name)}</sup>
      </mark>,
    )
    cursor = m.end
  })
  out.push(text.slice(cursor))
  return out
}
