import { useState } from 'react'
import FindingCard from '../components/FindingCard'
import { Empty, ErrorBox, Spinner } from '../components/ui'
import { api } from '../lib/api'
import { DETECTOR_LABEL } from '../lib/format'
import { useApi } from '../lib/identity'

function Select({ label, value, onChange, options }: { label: string; value: string; onChange: (v: string) => void; options: [string, string][] }) {
  return (
    <label className="flex items-center gap-2 text-xs text-slate-500">
      {label}
      <select value={value} onChange={(e) => onChange(e.target.value)} className="focus-ring rounded-xl ring-1 ring-black/[0.06] bg-white px-2 py-1.5 text-xs text-slate-800">
        {options.map(([v, l]) => (
          <option key={v} value={v}>
            {l}
          </option>
        ))}
      </select>
    </label>
  )
}

export default function FindingsPage() {
  const [status, setStatus] = useState('open')
  const [detector, setDetector] = useState('')
  const [strength, setStrength] = useState('')
  const { data, error, loading, reload } = useApi(() => api.findings({ status, detector, strength }), [status, detector, strength])

  return (
    <div className="space-y-5 animate-rise">
      <div>
        <h1 className="font-display text-4xl font-light tracking-tight text-slate-900">All findings</h1>
        <p className="mt-1 text-sm text-slate-500">Every indicator from each document's latest run, across applications. Strongest first.</p>
      </div>
      <div className="flex flex-wrap gap-4 rounded-2xl bg-white ring-1 ring-black/[0.06] px-4 py-3">
        <Select label="Review status" value={status} onChange={setStatus} options={[['', 'Any'], ['open', 'Open'], ['confirmed', 'Confirmed'], ['dismissed', 'Dismissed'], ['needs_more_info', 'Needs more info']]} />
        <Select label="Detector" value={detector} onChange={setDetector} options={[['', 'All'], ...Object.entries(DETECTOR_LABEL)]} />
        <Select label="Strength" value={strength} onChange={setStrength} options={[['', 'All'], ['strong', 'Strong'], ['medium', 'Medium'], ['weak', 'Weak']]} />
        <span className="ml-auto self-center text-xs text-slate-500">{data?.length ?? 0} shown</span>
      </div>
      {error ? <ErrorBox error={error} /> : null}
      {loading && !data && <Spinner />}
      {data && data.length === 0 && <Empty title="Nothing matches these filters" />}
      <div className="space-y-2">
        {data?.map((finding) => (
          <FindingCard key={finding.id} finding={finding} showContext onReviewed={reload} />
        ))}
      </div>
    </div>
  )
}
