# Faculty count consistency (detector 2)

Compares three sources **per department** and reports where they disagree.

| Source | Where it comes from | Capture date |
|---|---|---|
| declared | the CSV uploaded with the application (`faculty_declaration`) | upload date |
| website | a synthetic HTML snapshot in `data/demo/websites/` — **never fetched** | `<meta name="capture-date">` |
| aicte | mock records, `data/demo/aicte_records.json` | `captured_on` per institution |

No LLM. The snapshot pages are regular enough for a deterministic parser
(headings are departments; list items or table rows are people), and a parser's
misreading is reproducible where a model's is not.

## Decisions

**Per department, not totals.** A totals check passes an institution that moved
four people from Mechanical to CSE on paper. `faculty_counts_redistributed`
fires when the total is within tolerance but departments are outside it in
opposite directions.

**Tolerance** is `max(absolute, ceil(relative × AICTE count))` — default
`max(1, 10%)` — so one joiner between two capture dates is not a discrepancy.

**Direction.** Declared above records is the inflation pattern: medium, strong
when the excess is ≥ 25% of the record. Declared below records is weak.
The website is the institution's own publication and never strong on its own.

**Identity (resolved open question): synthetic faculty ID first.** An ID listed
for another institution — in its AICTE roster or in a declaration already in
our database — is strong; medium when every shared person is on *this*
institution's own roster (it is most likely the employer). Without an ID match,
a normalised name **and** the same qualification at ≥ 0.93 similarity is medium
at most: two people can share a name and a degree.

**The snapshot path is confined** to the configured directory. A record naming
`../../etc/passwd` or a URL is refused, as the issuer URL is: the thing being
checked never chooses the source it is checked against.

**Unavailable sources are visible findings worth zero points** in scoring: a
source we could not reach says nothing about the institution, but a missing
comparison must never look like a passed one.

## Finding types

`faculty_count_exceeds_records`, `faculty_count_below_records`,
`faculty_count_differs_from_website`, `faculty_counts_redistributed`,
`faculty_department_not_in_records`, `faculty_shared_across_institutions`,
`faculty_possible_shared_by_name`, `faculty_duplicate_entry`,
`faculty_not_in_records`, `faculty_source_stale`, `faculty_source_unavailable`,
`faculty_declared_list_unreadable`.
