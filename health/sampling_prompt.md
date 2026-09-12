# Weekly gate-health sampling prompt

Use this prompt from the repository root with an agent that can read the local
files and database. The task is an adversarial audit, not permission to repair
anything.

---

Audit the last complete Europe/Berlin seven-day window for client `jt-express`.
Work read-only: do not edit code, configuration, prompts, client profiles, labels,
gate logs, or the SQLite database. Write only the final review artifact under
`data/health/sampling/<ISO-year>-W<ISO-week>.md`. If that file already exists,
choose a timestamped sibling rather than overwriting it.

Read `CLAUDE.md`, `clients/jt-express/profile.json`,
`docs/selection_and_assessment.md`, and the relevant gate prompts before judging.
Use the current database path from `src/db.py`. Record the exact date window,
profile version, prompt versions, source files, SQL queries or selection method,
and every sampled raw-item ID or title-gate URL so another reviewer can reproduce
the sample.

Build three samples:

1. **Unbiased drops:** deterministically sample 10 title-gate drops and 10 body-gate
   drops across sources. Derive the deterministic ordering from SHA-256 of
   `<ISO week>|<source>|<external_id>`; do not use an unrecorded random seed. Use
   the latest applicable decision per identity and stratify so one large source
   cannot fill the sample.
2. **High-risk drops:** select up to 10 additional drops that deserve skeptical
   review: strong or multiple selector reasons, own/customer brand terms, customs,
   parcel/postal, product-safety or platform-law vocabulary, a new path/source,
   unusually long stored bodies, and parliamentary `relevant IS NULL` decisions.
   Explain why each was selected. Do not describe this risk-weighted group as a
   random sample.
3. **Kept controls:** sample five kept items across routes. Check whether the stored
   evidence is a real article/record rather than navigation, teaser, login,
   aggregation or unrelated template text, and whether the keep is plausibly useful.

Title-gate drops have no stored body by design. For only the sampled title drops,
you may open the public URL read-only to inspect the current article. Record when
the page is unavailable, paywalled, changed, or blocked, and do not infer agreement
from the title alone when the title is ambiguous. Do not bulk-fetch URLs and do not
store fetched content in the production database. For body-gate samples, judge the
stored body/version that the gate actually saw; do not substitute the current web
page.

For every sampled item assign exactly one audit outcome:

- `agree`
- `suspected_false_negative`
- `suspected_false_positive`
- `insufficient_evidence`
- `extraction_problem`
- `source_or_discovery_problem`

For disagreements, state the concrete evidence and identify the loss boundary:
discovery, source policy, title gate, body extraction, body gate, or later
assessment. Be especially skeptical of an apparently irrelevant body whose title,
sidebar or first paragraph hides relevant detail deeper in the text.

The report must contain:

- a short verdict and counts by audit outcome;
- one table per sample group with IDs/URLs, source, original decision, audit
  outcome, confidence and concise reasoning;
- a section named `Suspected misses requiring human confirmation`, even when empty;
- a section named `Coverage limitations` distinguishing URLs never collected from
  stored items rejected downstream;
- a machine-readable JSON block containing the sampled identities and outcomes.

Do not change the system based on your own judgments. End with at most five
specific items for the human reviewer to confirm. A second model's review is an
audit signal, not ground truth; label it accordingly.

---

This prompt intentionally audits stored gate decisions. A separate outside-in
exercise—starting from articles found by external search, newsletters or client
tips—is required to measure discovery beyond the configured sources.
