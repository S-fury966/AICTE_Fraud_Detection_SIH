import type { ApplicationReport, Finding } from './types'

/** Every finding from each document's latest run, tagged with its document. */
export function allFindings(report: ApplicationReport): Finding[] {
  const fromDocuments = report.documents.flatMap((d) =>
    (d.runs.at(-1)?.findings ?? []).map((f) => ({ ...f, document_id: d.id, original_filename: d.original_filename, document_type: d.document_type })),
  )
  // Application-level checks (completeness, applicant identity, norms) look at
  // every document together, so they belong to no single file.
  const fromApplication = (report.application_check?.findings ?? []).map((f) => ({ ...f, original_filename: 'whole application' }))
  return [...fromDocuments, ...fromApplication]
}
