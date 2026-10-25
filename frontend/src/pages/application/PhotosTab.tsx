import { Camera, Copy, Eye, Layers, MapPin } from 'lucide-react'
import { useState } from 'react'
import { Link } from 'react-router-dom'
import { Card, cn, Empty, SecureImage, StrengthBadge, VerificationBadge } from '../../components/ui'
import { elaUrl, previewUrl } from '../../lib/api'
import { formatDate, humanize } from '../../lib/format'
import type { ApplicationReport, DocumentEntry } from '../../lib/types'

const PHOTO_TYPES = new Set(['lab_photo', 'classroom_photo', 'infrastructure_photo'])

export default function PhotosTab({ report }: { report: ApplicationReport }) {
  const photos = report.documents.filter((d) => PHOTO_TYPES.has(d.document_type))
  if (!photos.length) return <Empty title="No photographs in this application" />
  return (
    <div className="space-y-4">
      <p className="text-xs text-slate-500">
        Ranked by reliability: reuse &gt; EXIF &gt; manipulation hints &gt; synthetic-content hint &gt; claim vs image. Reverse image search is internal — only our own corpus of past submissions.
      </p>
      {photos.map((doc) => (
        <PhotoView key={doc.id} doc={doc} />
      ))}
    </div>
  )
}

function PhotoView({ doc }: { doc: DocumentEntry }) {
  const run = doc.runs.at(-1)
  const analysis = run?.details?.photo_analysis ?? {}
  const [view, setView] = useState<'photo' | 'ela'>('photo')
  const meta = analysis.photo_metadata?.metadata ?? {}
  const ela = analysis.photo_manipulation?.ela
  const move = analysis.photo_manipulation?.copy_move
  const reuse = analysis.photo_reuse
  const width = meta.width ?? 1280
  const height = meta.height ?? 853
  const findings = run?.findings ?? []

  return (
    <Card className="overflow-hidden">
      <div className="grid gap-0 lg:grid-cols-[minmax(320px,1.1fr)_1fr]">
        <div className="border-b border-slate-200/70 bg-slate-900 lg:border-b-0 lg:border-r">
          <div className="relative">
            <SecureImage src={view === 'ela' && ela?.heatmap ? elaUrl(doc.id) : previewUrl(doc.id, 1000)} alt={doc.claim ?? doc.original_filename} className="w-full" />
            {view === 'photo' && move?.suspicious && (
              <svg viewBox={`0 0 ${width} ${height}`} className="pointer-events-none absolute inset-0 h-full w-full" preserveAspectRatio="none">
                {[move.source_region_xywh, move.target_region_xywh].map((r: number[] | null, i: number) =>
                  r ? <rect key={i} x={r[0]} y={r[1]} width={Math.max(r[2], 8)} height={Math.max(r[3], 8)} fill="none" stroke={i ? '#f5a524' : '#38bdf8'} strokeWidth={Math.max(3, width / 300)} strokeDasharray="12 6" /> : null,
                )}
              </svg>
            )}
          </div>
          <div className="flex items-center justify-between gap-2 px-3 py-2">
            <div className="flex gap-1">
              <button onClick={() => setView('photo')} className={cn('rounded px-2 py-1 text-[11px]', view === 'photo' ? 'bg-white text-slate-900' : 'text-slate-300 hover:text-white')}>
                <Eye className="mr-1 inline h-3 w-3" /> Photo
              </button>
              <button
                disabled={!ela?.heatmap}
                onClick={() => setView('ela')}
                className={cn('rounded px-2 py-1 text-[11px] disabled:opacity-40', view === 'ela' ? 'bg-white text-slate-900' : 'text-slate-300 hover:text-white')}
                title={ela?.heatmap ? 'Error level analysis heatmap' : ela?.reason}
              >
                <Layers className="mr-1 inline h-3 w-3" /> ELA heatmap
              </button>
            </div>
            {move?.suspicious && view === 'photo' && <span className="text-[10px] text-slate-300">dashed boxes: possibly repeated region (blue → amber)</span>}
          </div>
        </div>

        <div className="space-y-4 p-5">
          <div className="flex items-start justify-between gap-3">
            <div>
              <div className="label-xs">{humanize(doc.document_type)}</div>
              <div className="mt-0.5 text-sm font-semibold text-slate-900">{doc.claim ?? doc.original_filename}</div>
              <div className="text-[11px] text-slate-400">{doc.original_filename} · #{doc.id}</div>
            </div>
            <VerificationBadge status={analysis.verification_status} />
          </div>

          {findings.length > 0 ? (
            <ul className="space-y-2">
              {findings.map((f) => (
                <li key={f.id} className="text-xs leading-relaxed text-slate-600">
                  <StrengthBadge strength={f.strength} /> <span className="font-semibold text-slate-800">{humanize(f.type)}</span> — {f.reason}
                </li>
              ))}
            </ul>
          ) : (
            <div className="text-xs text-slate-500">No indicators raised for this photo.</div>
          )}

          <div className="grid gap-x-4 gap-y-1.5 text-xs sm:grid-cols-[120px_1fr]">
            <span className="text-slate-500">
              <Camera className="mr-1 inline h-3 w-3" />
              Camera
            </span>
            <span className="text-slate-800">{meta.has_exif ? `${meta.make ?? '—'} ${meta.model ?? ''}` : 'No EXIF (weak signal only)'}</span>
            <span className="text-slate-500">Captured</span>
            <span className="text-slate-800">{meta.capture_datetime ? formatDate(meta.capture_datetime, true) : '—'}</span>
            <span className="text-slate-500">Software</span>
            <span className="text-slate-800">{meta.software ?? '—'}</span>
            <span className="text-slate-500">
              <MapPin className="mr-1 inline h-3 w-3" />
              GPS
            </span>
            <span className="text-slate-800">
              {meta.gps ? `${meta.gps.lat}, ${meta.gps.lon}` : '—'}
              {analysis.photo_metadata?.gps_distance_km !== undefined && <span className="ml-1 text-slate-500">({analysis.photo_metadata.gps_distance_km} km from campus record)</span>}
            </span>
            <span className="text-slate-500">ELA</span>
            <span className="text-slate-800">{ela?.computed ? `ratio ${ela.ratio} · ${ela.hot_blocks} hot blocks` : ela?.reason ?? '—'}</span>
            <span className="text-slate-500">Copy-move</span>
            <span className="text-slate-800">{move ? `largest matching cluster ${move.largest_cluster} (${move.keypoints} keypoints)` : '—'}</span>
            <span className="text-slate-500">Claim check</span>
            <span className="text-slate-800">{analysis.photo_claim_consistency?.reason ?? humanize(analysis.photo_claim_consistency?.outcome)}</span>
          </div>

          {reuse && (
            <div className="rounded-2xl ring-1 ring-black/[0.06] p-3">
              <div className="mb-2 flex items-center gap-1.5 text-xs font-medium text-slate-700">
                <Copy className="h-3.5 w-3.5" /> Reuse check — compared with {reuse.corpus_photos_compared} earlier photo(s), {reuse.compare_kind} threshold {reuse.threshold}
              </div>
              {reuse.matches?.length ? (
                <div className="space-y-3">
                  {reuse.matches.map((m: any) => (
                    <div key={m.other_document_id} className="flex gap-3">
                      <SecureImage src={previewUrl(m.other_document_id, 300)} alt="Earlier submission" className="h-20 w-28 rounded object-cover" />
                      <div className="text-xs">
                        <div className="font-medium text-slate-800">
                          <Link to={`/applications/${m.other_application_id}`} className="text-iris-600 hover:underline">
                            {m.other_institution}
                          </Link>
                        </div>
                        <div className="text-slate-500">claimed there as “{m.other_claim ?? m.other_filename}”</div>
                        <div className="mt-1 text-slate-600">
                          distance {m.hamming_distance} bits{m.mirrored ? ' · mirrored copy' : ''}
                          {m.identical_file ? ' · identical file' : ''}
                        </div>
                      </div>
                    </div>
                  ))}
                </div>
              ) : (
                <div className="text-xs text-slate-500">No earlier submission matches this photo.</div>
              )}
            </div>
          )}
        </div>
      </div>
    </Card>
  )
}
