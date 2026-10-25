import { ArrowRight, FileUp, Loader2, PlayCircle, Trash2, UploadCloud } from 'lucide-react'
import { useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import LivePipeline from '../components/LivePipeline'
import { Button, Card, CardHeader, ErrorBox, Spinner } from '../components/ui'
import { api, streamSubmission, type StreamEvent } from '../lib/api'
import { DOCUMENT_TYPE_LABEL, humanize } from '../lib/format'
import { useApi, useIdentity } from '../lib/identity'

interface PendingFile {
  file: File
  documentType: string
  claim: string
}

function guessType(name: string): string {
  const lower = name.toLowerCase()
  if (lower.endsWith('.csv') || lower.endsWith('.json')) return 'faculty_declaration'
  if (lower.endsWith('.pdf')) return 'accreditation_certificate'
  if (lower.includes('class') || lower.includes('hall')) return 'classroom_photo'
  return 'lab_photo'
}

// The checks finish in well under a second; events are revealed at a readable
// pace. The elapsed time shown is the server's real figure.
const REVEAL_MS = 90
const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms))

export default function SubmitPage() {
  const navigate = useNavigate()
  const { can, meta } = useIdentity()
  const samples = useApi(() => api.samples())
  const [institution, setInstitution] = useState('')
  const [files, setFiles] = useState<PendingFile[]>([])
  const [busy, setBusy] = useState<string | null>(null)
  const [error, setError] = useState<unknown>(null)
  const [events, setEvents] = useState<StreamEvent[]>([])
  const inputRef = useRef<HTMLInputElement>(null)

  const allowed = can('submit_application')

  function addFiles(list: FileList | null) {
    if (!list) return
    setFiles((prev) => [...prev, ...Array.from(list).map((file) => ({ file, documentType: guessType(file.name), claim: '' }))])
  }

  async function run(label: string, path: string, body: FormData | null) {
    setBusy(label)
    setError(null)
    setEvents([])
    const pending: StreamEvent[] = []
    let finished = false
    const reveal = (async () => {
      while (!finished || pending.length) {
        const next = pending.shift()
        if (next) {
          setEvents((prev) => [...prev, next])
          if (next.event === 'error') setError(new Error(next.detail))
          await sleep(next.event === 'stage' ? REVEAL_MS : 140)
        } else await sleep(30)
      }
    })()
    try {
      await streamSubmission(path, body, (e) => pending.push(e))
    } catch (err) {
      setError(err)
    } finally {
      finished = true
      await reveal
      setBusy(null)
    }
  }

  function submitUpload() {
    const form = new FormData()
    form.append('institution_name', institution)
    form.append('category', 'extension_of_approval')
    files.forEach((f) => {
      form.append('files', f.file)
      form.append('document_types', f.documentType)
      form.append('claims', f.claim)
    })
    run('upload', '/api/applications/stream', form)
  }

  const done = events.find((e) => e.event === 'done') as Extract<StreamEvent, { event: 'done' }> | undefined

  return (
    <div className="space-y-6 animate-rise">
      <div>
        <h1 className="font-display text-4xl font-light tracking-tight text-slate-900">Scrutinise an application</h1>
        <p className="mt-1 text-sm text-slate-500">Documents are checked one application at a time. Nothing is approved or rejected — the result is a set of indicators and a review priority.</p>
      </div>

      {!allowed && <ErrorBox error={new Error('Your current role may not submit applications. Switch role in the header.')} />}
      {error ? <ErrorBox error={error} /> : null}

      {(busy || events.length > 0) && (
        <Card className="p-5">
          <LivePipeline events={events} running={!!busy} />
          {done && !busy && (
            <div className="mt-4 flex flex-wrap items-center justify-between gap-3 border-t border-slate-200/70 pt-4">
              <p className="text-xs text-slate-500">Every stage, finding and the priority assessment are stored with the rule versions used and recorded in the audit trail.</p>
              <Button onClick={() => navigate(`/applications/${done.application_id}`)}>
                Open the evidence <ArrowRight className="h-4 w-4" />
              </Button>
            </div>
          )}
        </Card>
      )}

      <div className="grid gap-6 xl:grid-cols-[1fr_1.15fr]">
        <Card>
          <CardHeader icon={<UploadCloud className="h-4 w-4" />} title="Upload documents" subtitle="Certificate (PDF/image), faculty list (CSV), lab and classroom photos" />
          <div className="space-y-4 p-5">
            <label className="block">
              <span className="label-xs">Institution name</span>
              <input value={institution} onChange={(e) => setInstitution(e.target.value)} placeholder="e.g. Shantiniketan Institute of Technology" className="focus-ring mt-1 w-full rounded-xl ring-1 ring-black/[0.06] px-3 py-2 text-sm" />
            </label>
            <div
              onClick={() => inputRef.current?.click()}
              onDragOver={(e) => e.preventDefault()}
              onDrop={(e) => {
                e.preventDefault()
                addFiles(e.dataTransfer.files)
              }}
              className="flex cursor-pointer flex-col items-center justify-center rounded-3xl border-2 border-dashed border-slate-300 px-4 py-8 text-center transition hover:border-ink-600 hover:bg-slate-50"
            >
              <FileUp className="h-6 w-6 text-slate-400" />
              <div className="mt-2 text-sm font-medium text-slate-700">Drop files or click to browse</div>
              <div className="text-xs text-slate-400">.pdf .jpg .png .csv · up to 25 MB each</div>
              <input ref={inputRef} type="file" multiple hidden accept=".pdf,.jpg,.jpeg,.png,.csv,.json" onChange={(e) => addFiles(e.target.files)} />
            </div>
            {files.length > 0 && (
              <ul className="space-y-2">
                {files.map((f, i) => (
                  <li key={i} className="grid gap-2 rounded-xl ring-1 ring-black/[0.06] p-2 text-xs sm:grid-cols-[1fr_170px_auto]">
                    <div className="min-w-0">
                      <div className="truncate font-medium text-slate-800">{f.file.name}</div>
                      {f.documentType.endsWith('photo') && (
                        <input
                          value={f.claim}
                          onChange={(e) => setFiles(files.map((x, j) => (j === i ? { ...x, claim: e.target.value } : x)))}
                          placeholder="What this photo shows (e.g. CSE Lab – 30 systems)"
                          className="focus-ring mt-1 w-full rounded border border-slate-200 px-2 py-1"
                        />
                      )}
                    </div>
                    <select value={f.documentType} onChange={(e) => setFiles(files.map((x, j) => (j === i ? { ...x, documentType: e.target.value } : x)))} className="focus-ring rounded border border-slate-200 bg-white px-2 py-1">
                      {(meta?.document_types ?? Object.entries(DOCUMENT_TYPE_LABEL).map(([value, label]) => ({ value, label }))).map((t) => (
                        <option key={t.value} value={t.value}>
                          {t.label}
                        </option>
                      ))}
                    </select>
                    <button onClick={() => setFiles(files.filter((_, j) => j !== i))} className="justify-self-end text-slate-400 hover:text-red-600" aria-label="Remove">
                      <Trash2 className="h-4 w-4" />
                    </button>
                  </li>
                ))}
              </ul>
            )}
            <Button disabled={!allowed || !!busy || !institution.trim() || files.length === 0} onClick={submitUpload} className="w-full">
              Run the checks
            </Button>
          </div>
        </Card>

        <Card>
          <CardHeader icon={<PlayCircle className="h-4 w-4" />} title="Demo samples" subtitle="Synthetic applications with known injected issues — each runs through the real pipeline" />
          <div className="divide-y divide-slate-100">
            {samples.error ? <div className="p-5"><ErrorBox error={samples.error} /></div> : null}
            {samples.loading && !samples.data && <Spinner />}
            {samples.data?.map((sample) => (
              <div key={sample.key} className="flex items-start gap-3 px-5 py-3.5">
                <div className="min-w-0 flex-1">
                  <div className="flex flex-wrap items-center gap-2">
                    <span className="text-sm font-medium text-slate-900">{sample.institution}</span>
                    {sample.is_fraud ? (
                      sample.fraud_types.map((t) => (
                        <span key={t} className="rounded-full bg-red-50 px-1.5 py-0.5 text-[10px] font-medium text-red-700">
                          {humanize(t)}
                        </span>
                      ))
                    ) : (
                      <span className="rounded-full bg-emerald-50 px-1.5 py-0.5 text-[10px] font-medium text-emerald-700">clean control</span>
                    )}
                  </div>
                  <p className="mt-0.5 text-xs text-slate-500">{sample.summary}</p>
                  <p className="mt-0.5 text-[11px] text-slate-400">{sample.documents.map((d) => d.file).join(' · ')}</p>
                </div>
                <Button size="sm" variant="secondary" disabled={!allowed || !!busy} onClick={() => run(sample.key, `/api/demo/samples/${sample.key}/stream`, null)}>
                  {busy === sample.key ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <PlayCircle className="h-3.5 w-3.5" />} Run
                </Button>
              </div>
            ))}
          </div>
        </Card>
      </div>
    </div>
  )
}
