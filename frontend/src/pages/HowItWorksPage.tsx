import { Ban, Bot, ClipboardCheck, Camera, Code2, FileCheck2, Gauge, Link2, Lock, ScrollText, Settings2, ShieldCheck, Users } from 'lucide-react'
import type { ReactNode } from 'react'
import { Link } from 'react-router-dom'
import { Card } from '../components/ui'

const DETECTORS = [
  {
    icon: <FileCheck2 className="h-5 w-5" />,
    title: 'Certificate authenticity',
    steps: ['Text layer or OCR', 'Fields traced back to the page', 'QR decoded — never opened', 'Mock issuer lookup', 'Signature integrity', 'Duplicates across the corpus'],
  },
  {
    icon: <Users className="h-5 w-5" />,
    title: 'Faculty consistency',
    steps: ['Declared list (CSV)', 'College website snapshot', 'AICTE records (mock)', 'Per department, with tolerance', 'Capture date of each source', 'Same faculty ID elsewhere'],
  },
  {
    icon: <Camera className="h-5 w-5" />,
    title: 'Photo authenticity',
    steps: ['Reuse across submissions', 'EXIF: date, GPS, editor', 'ELA heatmap', 'Copy-move regions', 'Synthetic-content hint (low confidence)', 'Claim vs image (advisory)'],
  },
  {
    icon: <ClipboardCheck className="h-5 w-5" />,
    title: 'Application & compliance',
    steps: ['Required documents present', 'Certificate names the applicant', 'Accreditation covers the session', 'Student:faculty ratio', 'Cadre ratio', 'Doctorate for senior faculty'],
  },
  {
    icon: <Gauge className="h-5 w-5" />,
    title: 'Review priority',
    steps: ['Points per indicator', 'Confidence-weighted', 'Repeats decay', 'Sub-score per detector', 'Floors for strong signals', 'Orders the queue only'],
  },
]

const PRINCIPLES: { icon: ReactNode; title: string; body: string }[] = [
  { icon: <Ban className="h-4 w-4" />, title: 'No automated decision', body: 'Nothing approves, rejects or concludes. There is no endpoint that could. An official decides.' },
  { icon: <Bot className="h-4 w-4" />, title: 'The model reads; code decides', body: 'An LLM may extract fields or rephrase an explanation. Every value it returns must be found in the source text, and no status, strength or score ever comes from it.' },
  { icon: <Link2 className="h-4 w-4" />, title: 'Evidence on every flag', body: 'Each indicator carries its reason, the values compared, the source consulted and a confidence.' },
  { icon: <ScrollText className="h-4 w-4" />, title: 'Hash-chained audit trail', body: 'Checks, records consulted, findings, reviews and every read of a report are appended to a tamper-evident log.' },
  { icon: <Settings2 className="h-4 w-4" />, title: 'Rules are versioned config', body: 'Thresholds, weights and allowlists live in YAML. Each run stores the versions and hashes it was judged under; re-runs never overwrite.' },
  { icon: <Lock className="h-4 w-4" />, title: 'Privacy by default', body: 'No URL from a document is ever contacted, reverse image search is internal, and the audit trail holds ids — never document text.' },
]

export default function HowItWorksPage() {
  return (
    <div className="space-y-8 animate-rise">
      <div className="grid items-stretch gap-5 lg:grid-cols-[1.4fr_1fr]">
        <div className="flex flex-col justify-center py-4">
          <div className="flex items-center gap-2 text-xs font-medium uppercase tracking-[0.14em] text-slate-500">
            <ShieldCheck className="h-4 w-4 text-iris-600" /> Smart India Hackathon 2026 · SIH26261
          </div>
          <h1 className="mt-4 font-display text-4xl font-light leading-[1.1] tracking-tight text-slate-900 sm:text-6xl">
            Evidence first. <span className="text-slate-400">Decisions left to people.</span>
          </h1>
          <p className="mt-5 max-w-xl text-sm leading-relaxed text-slate-600">
            A first-pass scrutiny of AICTE Extension of Approval applications: it reads the certificate, the faculty list and the infrastructure photos, validates the application against the configured norms, and raises preliminary indicators with their evidence — ordering the queue so officials see the riskiest files first.
          </p>
          <div className="mt-7 flex flex-wrap gap-3">
            <Link to="/submit" className="rounded-full bg-ink-900 px-5 py-2.5 text-sm font-medium text-white hover:bg-ink-700">
              Run a sample application
            </Link>
            <Link to="/" className="rounded-full bg-butter-500 px-5 py-2.5 text-sm font-medium text-ink-900 hover:bg-butter-400">
              Open the review queue
            </Link>
          </div>
        </div>
        <div className="relative overflow-hidden rounded-[1.75rem] bg-iris-600 p-7 text-white shadow-[0_24px_48px_-24px_rgba(62,59,240,0.65)]">
          <svg className="pointer-events-none absolute inset-0 h-full w-full" viewBox="0 0 400 360" preserveAspectRatio="xMidYMid slice" aria-hidden>
            <circle cx="360" cy="380" r="250" fill="none" stroke="white" strokeOpacity="0.25" />
            <circle cx="150" cy="-20" r="120" fill="none" stroke="white" strokeOpacity="0.2" />
            <circle cx="60" cy="90" r="3" fill="white" />
            <circle cx="320" cy="60" r="2" fill="white" />
          </svg>
          <span className="relative rounded-full px-3 py-1 text-xs ring-1 ring-white/50">What it never does</span>
          <ul className="relative mt-10 space-y-4 font-display text-xl font-light leading-snug">
            <li>Approve or reject an application.</li>
            <li>Call a document fake or forged.</li>
            <li>Open a URL found in a submission.</li>
            <li>Let a language model decide anything.</li>
          </ul>
        </div>
      </div>

      <section>
        <h2 className="mb-3 text-sm font-semibold uppercase tracking-wider text-slate-500">Five checks, one queue</h2>
        <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-5">
          {DETECTORS.map((d, i) => (
            <Card key={d.title} className="p-5">
              <div className="flex items-center gap-3">
                <div className="flex h-11 w-11 items-center justify-center rounded-full bg-ink-900 text-butter-500">{d.icon}</div>
                <div>
                  <div className="label-xs">Check {i + 1}</div>
                  <div className="text-sm font-semibold text-slate-900">{d.title}</div>
                </div>
              </div>
              <ol className="mt-4 space-y-2">
                {d.steps.map((s, j) => (
                  <li key={s} className="flex items-start gap-2 text-xs text-slate-600">
                    <span className="mt-0.5 flex h-4 w-4 shrink-0 items-center justify-center rounded-full bg-slate-100 text-[9px] font-semibold text-slate-500">{j + 1}</span>
                    {s}
                  </li>
                ))}
              </ol>
            </Card>
          ))}
        </div>
      </section>

      <section>
        <h2 className="mb-3 text-sm font-semibold uppercase tracking-wider text-slate-500">Why a reviewer can trust the output</h2>
        <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
          {PRINCIPLES.map((p) => (
            <Card key={p.title} className="p-5">
              <div className="flex items-center gap-2 text-sm font-semibold text-slate-900">
                <span className="text-iris-600">{p.icon}</span>
                {p.title}
              </div>
              <p className="mt-2 text-xs leading-relaxed text-slate-600">{p.body}</p>
            </Card>
          ))}
        </div>
      </section>

      <section>
        <h2 className="mb-3 text-sm font-semibold uppercase tracking-wider text-slate-500">Fixed verification status taxonomy</h2>
        <Card className="flex flex-wrap gap-2 p-5 text-xs">
          {['verified', 'mismatch detected', 'expired', 'verification pending', 'source unavailable', 'manual review required'].map((s) => (
            <span key={s} className="rounded-full border border-slate-200 px-3 py-1 font-medium text-slate-700">
              {s}
            </span>
          ))}
          <span className="ml-auto flex items-center gap-1.5 text-slate-500">
            <Code2 className="h-3.5 w-3.5" /> set by code logic only — never by a model
          </span>
        </Card>
      </section>
    </div>
  )
}
