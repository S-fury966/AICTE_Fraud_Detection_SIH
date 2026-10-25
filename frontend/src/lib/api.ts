/**
 * The API client. An officer's requests carry their name in X-Actor / X-Role.
 * A senior reviewer or administrator signs in with the role's passcode and
 * their requests carry the signed token instead: the backend refuses those
 * roles in a header. Either way the identity is validated, enforced and
 * written into the audit log.
 */
import type {
  ApplicationReport,
  ApplicationSummary,
  AuditEntry,
  DemoSample,
  Finding,
  Meta,
  NetworkData,
  SearchResults,
  Stats,
  SubmitResult,
} from './types'

export class ApiError extends Error {
  status: number
  constructor(status: number, message: string) {
    super(message)
    this.status = status
  }
}

const IDENTITY_KEY = 'authentiq.identity'

export interface Identity {
  actor: string
  role: string
  /** Present only for a signed-in role. */
  token?: string
  expiresAt?: number
}

export const DEFAULT_IDENTITY: Identity = { actor: 'officer.sharma', role: 'officer' }

export function loadIdentity(): Identity {
  try {
    const raw = localStorage.getItem(IDENTITY_KEY)
    if (raw) {
      const stored = JSON.parse(raw) as Identity
      // An expired sign-in falls back to an officer, never to the old role.
      if (stored.token && (stored.expiresAt ?? 0) * 1000 <= Date.now()) return DEFAULT_IDENTITY
      if (!stored.token && stored.role !== 'officer') return DEFAULT_IDENTITY
      return stored
    }
  } catch {
    /* storage unavailable: fall through to the default */
  }
  return DEFAULT_IDENTITY
}

export function saveIdentity(identity: Identity) {
  try {
    localStorage.setItem(IDENTITY_KEY, JSON.stringify(identity))
  } catch {
    /* non-fatal */
  }
}

let current: Identity = loadIdentity()
export function setRequestIdentity(identity: Identity) {
  current = identity
}

let onSessionRejected: (() => void) | null = null
/** Called when the backend refuses the current token (expired, or the API restarted). */
export function setSessionRejectedHandler(handler: (() => void) | null) {
  onSessionRejected = handler
}

function headers(extra?: HeadersInit): HeadersInit {
  const identity: Record<string, string> = current.token
    ? { Authorization: `Bearer ${current.token}` }
    : { 'X-Actor': current.actor, 'X-Role': current.role }
  return { ...identity, ...(extra ?? {}) }
}

function rejected(response: Response) {
  if (response.status === 401 && current.token) onSessionRejected?.()
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, { ...init, headers: headers(init?.headers) })
  if (!response.ok) {
    // A refused passcode is not a rejected session.
    if (path !== '/api/session') rejected(response)
    let message = response.statusText
    try {
      const body = await response.json()
      message = typeof body.detail === 'string' ? body.detail : JSON.stringify(body.detail ?? body)
    } catch {
      /* keep statusText */
    }
    throw new ApiError(response.status, message)
  }
  return response.json() as Promise<T>
}

export const api = {
  health: () => request<{ status: string; audit_chain_ok: boolean; schema_version: string }>('/api/health'),
  meta: () => request<Meta>('/api/meta'),
  signIn: (actor: string, role: string, passcode: string) =>
    request<{ token: string; actor: string; role: string; expires_at: number }>('/api/session', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ actor, role, passcode }),
    }),
  signOut: () => request<{ ok: true }>('/api/session/end', { method: 'POST' }),
  stats: () => request<Stats>('/api/stats'),
  applications: () => request<ApplicationSummary[]>('/api/applications'),
  report: (id: number, allRuns = false) =>
    request<ApplicationReport>(`/api/applications/${id}/report${allRuns ? '?all_runs=true' : ''}`),
  applicationAudit: (id: number) => request<AuditEntry[]>(`/api/applications/${id}/audit`),
  rerun: (id: number) => request<SubmitResult>(`/api/applications/${id}/rerun`, { method: 'POST' }),
  findings: (params: Record<string, string | undefined>) => {
    const query = new URLSearchParams(Object.entries(params).filter(([, v]) => v) as [string, string][])
    return request<Finding[]>(`/api/findings?${query}`)
  },
  review: (findingId: number, action: string, note?: string) =>
    request<{ ok: true; finding: Finding; risk: unknown }>(`/api/findings/${findingId}/review`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ action, note: note || null }),
    }),
  audit: (limit = 300, offset = 0) => request<{ total: number; entries: AuditEntry[] }>(`/api/audit?limit=${limit}&offset=${offset}`),
  verifyChain: () => request<{ ok: boolean; entries_checked: number; first_broken_id: number | null; problem: string | null }>('/api/audit/verify'),
  config: () => request<{ name: string; config_version: string; hash: string; raw: string }[]>('/api/config'),
  evaluation: () => request<any>('/api/evaluation'),
  samples: () => request<DemoSample[]>('/api/demo/samples'),
  submitSample: (key: string) => request<SubmitResult>(`/api/demo/samples/${key}/submit`, { method: 'POST' }),
  submit: (form: FormData) => request<SubmitResult>('/api/applications', { method: 'POST', body: form }),
  network: () => request<NetworkData>('/api/network'),
  search: (q: string) => request<{ query: string; results: SearchResults }>(`/api/search?q=${encodeURIComponent(q)}`),
}

export type StreamEvent =
  | { event: 'application'; application_id: number; institution: string; documents: number }
  | { event: 'document_started'; index: number; total: number; filename: string; document_type: string; kind: string }
  | { event: 'stage'; run_id: number; stage: string; status: string; detail_status: string | null; reason: string | null; duration_ms: number; finding_count?: number }
  | { event: 'document_finished'; index: number; document_id: number; run_status: string; verification_status: string | null; finding_types: string[] }
  | { event: 'done'; application_id: number; risk: { level: 'high' | 'medium' | 'low'; score: number; subscores: Record<string, number> }; seconds: number }
  | { event: 'error'; detail: string }

/** POST and read the NDJSON progress stream line by line. */
export async function streamSubmission(path: string, body: FormData | null, onEvent: (e: StreamEvent) => void): Promise<void> {
  const response = await fetch(path, { method: 'POST', headers: headers(), body })
  if (!response.ok || !response.body) {
    rejected(response)
    let message = response.statusText
    try {
      const data = await response.json()
      message = typeof data.detail === 'string' ? data.detail : JSON.stringify(data.detail)
    } catch {
      /* keep statusText */
    }
    throw new ApiError(response.status, message)
  }
  const reader = response.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''
  for (;;) {
    const { value, done } = await reader.read()
    if (done) break
    buffer += decoder.decode(value, { stream: true })
    let newline = buffer.indexOf('\n')
    while (newline >= 0) {
      const line = buffer.slice(0, newline).trim()
      buffer = buffer.slice(newline + 1)
      if (line) onEvent(JSON.parse(line) as StreamEvent)
      newline = buffer.indexOf('\n')
    }
  }
}

/** Download a file through fetch, so the reviewer's identity headers go with it. */
export async function downloadWithIdentity(path: string, filename: string): Promise<void> {
  const response = await fetch(path, { headers: headers() })
  if (!response.ok) {
    rejected(response)
    throw new ApiError(response.status, response.statusText)
  }
  const url = URL.createObjectURL(await response.blob())
  const link = document.createElement('a')
  link.href = url
  link.download = filename
  link.click()
  setTimeout(() => URL.revokeObjectURL(url), 2000)
}

/** Fetch a file with the reviewer's identity and return a local object URL.
 *  An <img src> cannot send headers, so views would be audited as the default
 *  officer instead of the person looking. */
export async function objectUrlWithIdentity(path: string): Promise<string> {
  const response = await fetch(path, { headers: headers() })
  if (!response.ok) {
    rejected(response)
    throw new ApiError(response.status, response.statusText)
  }
  return URL.createObjectURL(await response.blob())
}

/** Open a stored document in a new tab, with the view audited under the reviewer's name. */
export async function openWithIdentity(path: string): Promise<void> {
  const tab = window.open('', '_blank')
  const url = await objectUrlWithIdentity(path)
  if (tab) tab.location.href = url
  else window.location.href = url
  setTimeout(() => URL.revokeObjectURL(url), 60_000)
}

export const previewUrl = (documentId: number, width = 900) => `/api/documents/${documentId}/preview.png?width=${width}`
export const elaUrl = (documentId: number) => `/api/documents/${documentId}/ela.png`
export const fileUrl = (documentId: number) => `/api/documents/${documentId}/file`
