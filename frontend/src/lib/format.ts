const ACRONYMS: Record<string, string> = {
  qr: 'QR',
  exif: 'EXIF',
  gps: 'GPS',
  ela: 'ELA',
  aicte: 'AICTE',
  pdf: 'PDF',
  id: 'ID',
  ai: 'AI',
  vlm: 'VLM',
}

/** "qr_domain_not_allowlisted" -> "QR domain not allowlisted" */
export function humanize(value: string | null | undefined): string {
  if (!value) return '—'
  const words = value.split('_').map((w) => ACRONYMS[w] ?? w)
  const first = words[0]
  words[0] = ACRONYMS[first.toLowerCase()] ?? first.charAt(0).toUpperCase() + first.slice(1)
  return words.join(' ')
}

export function formatDate(iso: string | null | undefined, withTime = false): string {
  if (!iso) return '—'
  const date = new Date(iso)
  return date.toLocaleString('en-IN', {
    day: '2-digit',
    month: 'short',
    year: 'numeric',
    ...(withTime ? { hour: '2-digit', minute: '2-digit' } : {}),
  })
}

export function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`
}

export function short(hash: string | null | undefined, n = 10): string {
  return hash ? `${hash.slice(0, n)}…` : '—'
}

export function display(value: unknown): string {
  if (value === null || value === undefined || value === '') return '—'
  if (Array.isArray(value)) return value.map(display).join(', ')
  if (typeof value === 'object') return JSON.stringify(value)
  return String(value)
}

export const DETECTOR_LABEL: Record<string, string> = {
  certificate_authenticity: 'Certificate',
  duplicate_detection: 'Duplicates',
  faculty_consistency: 'Faculty',
  photo_authenticity: 'Photos',
  application_validation: 'Application',
  regulatory_compliance: 'Compliance',
}

export const DOCUMENT_TYPE_LABEL: Record<string, string> = {
  accreditation_certificate: 'Accreditation certificate',
  noc: 'No objection certificate',
  faculty_declaration: 'Faculty list',
  lab_photo: 'Lab photo',
  classroom_photo: 'Classroom photo',
  infrastructure_photo: 'Infrastructure photo',
}

export const ROLE_LABEL: Record<string, string> = {
  officer: 'Scrutiny officer',
  senior_reviewer: 'Senior reviewer',
  admin: 'Administrator',
}
