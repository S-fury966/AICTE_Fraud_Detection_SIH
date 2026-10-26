/** Shapes returned by the backend API (backend/app/api/main.py). */

export type Strength = 'strong' | 'medium' | 'weak'
export type RiskLevel = 'high' | 'medium' | 'low'
export type ReviewStatus = 'open' | 'confirmed' | 'dismissed' | 'needs_more_info'
export type VerificationStatus =
  | 'verified'
  | 'mismatch_detected'
  | 'expired'
  | 'verification_pending'
  | 'source_unavailable'
  | 'manual_review_required'

export interface ReviewActionEntry {
  actor: string
  role: string
  action: string
  note: string | null
  created_at: string
}

export interface Finding {
  id: number
  detector: string
  type: string
  strength: Strength
  reason: string
  evidence: Record<string, unknown> | null
  confidence: string | null
  review_status: ReviewStatus
  created_at: string
  review_actions: ReviewActionEntry[]
  // Present in the cross-application queue
  document_id?: number
  document_type?: string
  original_filename?: string
  application_id?: number
  institution?: string
}

export interface Subscore {
  label: string
  weight: number
  raw_points: number
  score: number
  finding_count: number
  strong: number
  medium: number
  weak: number
}

export interface Contribution {
  finding_id: number
  detector: string
  type: string
  strength: Strength
  confidence: string | null
  review_status: string
  base_points: number
  multiplier: number
  points: number
  repeat_index: number
}

export interface Risk {
  score: number
  level: RiskLevel
  subscores: Record<string, Subscore>
  contributions: Contribution[]
  excluded_findings: { id: number; type: string; review_status: string }[]
  floors_applied: { rule: string; from: string; to: string }[]
  config_version: string
  config_hash: string
  assessed_at?: string
  trigger?: string
}

export interface ApplicationSummary {
  id: number
  institution: string
  category: string
  created_at: string
  document_count: number
  document_types: string[]
  finding_count: number
  open_findings: number
  reviewed_findings: number
  open_by_strength: Record<Strength, number>
  risk: { level: RiskLevel; score: number; subscores: Record<string, number>; assessed_at: string } | null
}

export interface Stage {
  stage: string
  status: 'ok' | 'degraded' | 'skipped' | 'failed'
  detail_status: string | null
  reason: string | null
  duration_ms: number | null
}

export interface ExtractedField {
  name: string
  value: unknown
  source_snippet: string | null
  page: number | null
  confidence: string | null
  validated: boolean
  value_iso: string | null
  invalidation_reason: string | null
  model_value: unknown
}

export interface Comparison {
  name: string
  outcome: string
  submitted_value: unknown
  issuer_value: unknown
  reason: string | null
  similarity: number | null
  method: string | null
}

export interface Lookup {
  certificate_id: string
  id_origin: string
  trust: string | null
  outcome: string | null
  status: VerificationStatus | null
  status_reasons: string[] | null
  comparisons: Comparison[] | null
  issuer_record: Record<string, unknown> | null
  http_status: number | null
  latency_ms: number | null
  attempt_count: number | null
  error: string | null
}

export interface SignatureRow {
  signature_index: number
  field_name: string | null
  present: boolean
  parsed: boolean
  coverage: string | null
  covers_whole_file: boolean
  digest_matches: boolean | null
  digest_algorithm: string | null
  signer_name_unverified: string | null
  claimed_signing_time_unverified: string | null
  signer_identity_verified: false
  appended_byte_length: number | null
  parse_error: string | null
}

export interface Run {
  id: number
  status: string
  actor: string
  role: string
  started_at: string
  finished_at: string | null
  config_versions: Record<string, string | null>
  provider: string | null
  model_name: string | null
  prompt_version: string | null
  registry_hash: string | null
  stages: Stage[]
  pages: { page_number: number; extraction_method: string | null; avg_confidence: number | null; decision_reason: string | null; text: string | null }[]
  extracted_fields: ExtractedField[]
  qr_detections: { raw_text: string; page: number | null; parsed_host: string | null; certificate_id: string | null; decoder: string | null; source: string | null; payload_kind: string | null }[]
  verification_lookups: Lookup[]
  signatures: SignatureRow[]
  perceptual_hashes: { page: number; hash_kind: string; hash_value: string; bit_count: number }[]
  details: Record<string, any>
  findings: Finding[]
}

export interface CrossReference {
  id: number
  kind: string
  other_document_id: number
  same_institution: boolean
  evidence: Record<string, unknown> | null
  discovered_in_run_id: number | null
  created_at: string | null
}

export interface DocumentEntry {
  id: number
  document_type: string
  original_filename: string
  file_sha256: string
  size_bytes: number
  uploaded_by: string
  uploaded_role: string
  claim: string | null
  created_at: string
  run_count: number
  runs: Run[]
  cross_references: CrossReference[]
}

export interface DocumentSummary {
  document_id: number
  kind: 'certificate' | 'faculty' | 'photo'
  document_type: string
  original_filename: string
  verification_status: VerificationStatus | null
  run_status: string | null
  finding_count: number
  strong: number
  medium: number
  weak: number
}

export interface Explanation {
  headline: string
  points: { finding_id: number; detector: string; strength: Strength; text: string }[]
  notes: string[]
  disclaimer: string
  generated_by: string
  rewrite?: string
}

export interface ApplicationCheck {
  id: number
  status: VerificationStatus
  config_version: string
  created_at: string
  checks: { detector: string; check: string; label: string; outcome: 'met' | 'not_met' | 'not_evaluated'; status: VerificationStatus; summary: string }[]
  findings: Finding[]
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  norms?: any
}

export interface FieldExtractionFallback {
  document_id: number | null
  provider_used: string | null
  primary_provider: string | null
  fallback_used: boolean
  fallback_category: string | null
  fallback_reason: string | null
  breaker_open: boolean
}

export interface ApplicationReport {
  application: { id: number; category: string; created_at: string }
  application_check: ApplicationCheck | null
  institution: { id: number; name: string; registry_reference: string | null }
  documents: DocumentEntry[]
  risk: Risk
  risk_history: { id: number; score: number; level: RiskLevel; trigger: string; actor: string; role: string; config_version: string; created_at: string }[]
  explanation: Explanation
  field_extraction_fallback: FieldExtractionFallback | null
  document_summaries: DocumentSummary[]
}

export interface AuditEntry {
  id: number
  timestamp: string
  actor: string
  role: string
  action: string
  entity_type: string
  entity_id: string | null
  details: Record<string, unknown> | null
  previous_hash: string | null
  entry_hash: string
}

export interface Meta {
  identity: { actor: string; role: string }
  roles: string[]
  sign_in_required: string[]
  permissions: Record<string, string[]>
  review_actions: Record<string, string | null>
  review_statuses: string[]
  verification_statuses: string[]
  document_types: { value: string; label: string; detector: string }[]
  detectors: Record<string, string>
  extraction_provider: { configured: string; active: string }
  as_of: string | null
}

export interface Stats {
  top_open_types: { type: string; strength: Strength; count: number }[]
  applications: number
  documents: number
  check_runs: number
  risk_levels: Record<RiskLevel | 'unscored', number>
  open_findings_by_detector: Record<string, Record<Strength, number>>
  findings_by_review_status: Record<string, number>
  review_actions: number
  avg_run_ms: number | null
  audit_entries: number
  audit_chain_ok: boolean
}

export interface DemoSample {
  key: string
  institution: string
  summary: string
  is_fraud: boolean
  fraud_types: string[]
  documents: { file: string; document_type: string; claim: string | null }[]
}

export interface SubmitResult {
  application_id: number
  runs: { document_id: number; status: string; finding_count: number; stages: Stage[] }[]
  risk: Risk
  seconds: number
}

export interface NetworkNode {
  id: string
  label: string
  kind: 'institution' | 'records_only'
  application_id: number | null
  risk_level: RiskLevel | null
  risk_score: number | null
  open_strong: number
  connected: boolean
}

export interface NetworkEdge {
  source: string
  target: string
  strength: Strength
  kinds: { kind: string; label: string; count: number }[]
  details: Record<string, unknown>[]
}

export interface NetworkData {
  nodes: NetworkNode[]
  edges: NetworkEdge[]
  note: string
}

export interface SearchResults {
  institutions: { institution: string; application_id: number | null; applications: number }[]
  certificates: { value: string; source: string; document_id: number; application_id: number; institution: string }[]
  faculty: { faculty_id: string | null; name: string; department: string | null; qualification: string | null; application_id: number | null; institution: string | null }[]
  documents: { document_id: number; filename: string; document_type: string; application_id: number | null; institution: string | null }[]
}
