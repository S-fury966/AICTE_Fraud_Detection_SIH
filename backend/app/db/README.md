# Persistence, audit trail and the pipeline (step 5)

Stores what steps 1–4 produce, so records survive, later steps can query
history, and every finding traces back to its evidence.

```
Pipeline(...).process_document(application_id, file_path, document_type, actor, role)
  -> RunSummary(run_id, status, stages, finding_count, verification_status, ...)
```

## Runs are never overwritten

A re-run creates a **new** `check_runs` row with its own children. Nothing
mutates an existing run, and the repository exposes no update or delete for
one.

History is the point. A reviewer must be able to see that a document was clean
under last month's rules and is flagged under this month's — impossible if a
run is edited in place. That is also why each run stores the config versions
*and* hashes it was judged under, alongside the prompt version, provider, model
and registry hash: the rules on disk will have moved on, and "under which rules
was this judged?" has to stay answerable.

## The audit chain

Every `audit_log` entry hashes its own content plus the previous entry's hash.
Altering or removing one entry invalidates every entry after it.

`verify_chain()` recomputes the chain and reports the first break,
distinguishing two cases because they mean different things:

- a wrong `entry_hash` — the row's content was altered after it was written;
- a wrong `previous_hash` — an entry was removed, reordered or inserted.

Append-only is enforced **twice**: SQLite triggers reject `UPDATE` and `DELETE`
on `audit_log`, and this module offers no function to do either. A test asserts
both locks, and another drops the triggers — as anyone with file access
could — edits a row directly, and confirms the chain still catches it. Triggers
stop the accident; the chain catches the deliberate act.

Details carry **ids, counts and statuses only** — never page text, source
snippets or raw model output. An audit trail that quoted the documents would
become a second, unguarded copy of exactly the content it exists to protect,
and it is the one table most likely to be exported and kept.

## Degradation is a result, not a crash

An expected failure is recorded as a stage result and the run continues into
whatever stage can still do something useful:

| What fails | What still happens |
|---|---|
| Text extraction | QR still decodes from the file; the issuer lookup uses the QR id at `lower` trust |
| The LLM provider | Fields are degraded; QR and issuer stages run on the QR id |
| The issuer | `source_unavailable` is stored; stages 1–3 are untouched |
| The whole file is corrupt | Stage 1 `failed`, later stages `skipped` **with their reasons** |

Crashing would throw away the stages that did work — which is precisely the
evidence a reviewer needs in order to understand why the rest is missing.

**Each stage commits in its own transaction**, so a failure in stage 3 cannot
roll back stages 1 and 2. A test asserts that directly.

A skipped stage still gets a `stage_results` row. A missing row would be
indistinguishable from a stage that ran and found nothing, and those are
opposite facts.

## One findings table for every detector

`findings` carries a `detector` column rather than there being a table per
detector. A reviewer works a single queue, so a per-detector schema would make
the system's most common query a UNION — and the faculty and photo detectors
are still to come.

`review_status` lives on the finding because every queue query filters on it;
`review_actions` is the history of how it got there. Unlike `audit_log`, that
table is not hash-chained: it is working state a reviewer legitimately adds to,
and every action is mirrored into `audit_log`, which is the tamper-evident
record.

The lifecycle and the action-to-status map are config, so what "dismiss" means
for a given organisation is its own decision.

## Files are content-addressed

Uploads are copied to `data/store/<ab>/<sha256><ext>`; the database stores the
hash and the path, never the bytes. Two consequences, both wanted:

- Identical files share one stored copy, so a resubmission shows up as a shared
  hash instead of something to discover later — groundwork for duplicate
  detection.
- The path is derived from the content, so a stored file cannot be quietly
  swapped for different bytes without its name ceasing to match.
  `verify_stored_file` checks that.

## Portability

Every column is `Integer`, `String`, `Text`, `Float`, `Boolean`, `DateTime` or
generic `JSON` — all rendered natively by SQLAlchemy on both SQLite and
Postgres, so Postgres stays a drop-in rather than a migration project. A test
asserts no other type creeps in.

Foreign keys are enforced: SQLite ignores them unless each connection says
otherwise, so `PRAGMA foreign_keys=ON` is issued on connect and read back by
`foreign_keys_enforced()`. Without it, deleting an application would silently
leave orphaned runs and findings — and the orphans would still be served to a
reviewer.

Schema creation is `create_all` plus a `schema_version` row. **No Alembic yet**:
the prototype's schema still moves in whole steps, and a migration history
nobody trusts is worse than none.

## Roles

There is no login (a confirmed decision), so a role is asserted by the caller
rather than proven. Validation against the config list cannot stop a caller
claiming `admin`, but it does stop a typo inventing a role that no policy
covers and no later authorisation check would recognise. Every write takes an
actor and a role and records both.

## Running it

```bash
# Process one document: store it, run steps 1-4, persist everything
python -m app.pipeline <file> --institution "<name>" --actor <name> --role officer \
    [--in-process-issuer] [--as-of 2026-01-15] [--application <id>]

# Read it all back
python -m app.reports <application_id> [--all-runs] [--actor <name>]

# The audit trail
python -m app.audit verify          # exit 1 if the chain is broken
python -m app.audit list --limit 20

# Step 6, standalone against an already-stored document
python -m app.duplicates <document_id>
python -m app.duplicates --relationships <document_id>
```

`--in-process-issuer` runs the mock issuer through FastAPI's `TestClient`, so
no server and no open port are needed.

## Added in step 6

Three tables, and the reason each one is a table rather than a column:

| Table | Holds | Why separate |
|---|---|---|
| `signature_details` | one row per PDF signature: byte range, coverage, both digests, the appended region's size, the unverified signer claims | written even for a clean file - a findings table alone cannot distinguish "one intact signature" from "no signature", which are different facts |
| `perceptual_hashes` | document, run, page, hash kind, value, hash size | a document has several hashes (one per kind, later one per page), and the photo detector will store image hashes here too |
| `cross_references` | a relationship between two documents: normalised id pair, kind, `same_institution`, evidence, the run that discovered it | see below |

`cross_references` is what reconciles two rules that otherwise conflict.
Duplicate detection has to be **order independent** - the set of relationships
must not depend on which application arrived first - but **runs are never
overwritten**, and when B arrives and matches A, the fact belongs to both
documents while A's run finished before B existed.

So the relationship lives on neither run. The pair is normalised (lower id
first) with a unique constraint, and `cross_references_for_document()` matches
on **either** column, so a relationship discovered by B's run appears on A's
side of `get_application_report` without a byte of A's run being touched.

`check_runs` also gained `signature_config_version` and
`duplicate_config_version`, both nullable: runs made before step 6 existed are
not retrospectively annotated with rules that were never applied to them.

## What this step does not do

No risk score and no overall verification status beyond the per-document status
step 4 already returns. Findings, relationships and statuses only; combining
them is step 7. No HTTP API, no UI.
