# Brandmonitor MVP Plan

Status: **pre-build**. This is the implementation plan for the first customer pilot.
The longer product plans remain available as research and future ideas.

## 1. Goal

Run one real monitoring cycle for one customer and produce a reviewable
Chinese-language report.

The MVP proves four things:

1. The existing collectors reliably return usable source material.
2. Customer-specific relevance filtering works.
3. The assessment prompt produces useful, defensible output.
4. A complete cycle can be rerun without duplicates or hidden source failures.

## 2. Shape of the system

```text
existing collectors
        ↓
normalized raw source records
        ↓
customer profile + customer prompt
        ↓
customer assessments
        ↓
simple Chinese report
```

Collection and customer analysis are separate boundaries. Raw records are stored
before relevance filtering. Adding another customer should normally mean adding a
profile and prompt, then analyzing the already collected material.

## 3. Pilot scope

### Regulatory

- Copy four files from the reference repository's `src/crawler_gov/`: `bundestag.py`,
  `europarl.py`, `europarl_epdb.py`, `pdf_utils.py`. Leave `track_history.py` and
  everything under `src/agents/`.
- Delete `"f.wahlperiode": 21` from the params in `fetch_vorgaenge`. Hardcoded, it
  returns nothing after the next federal election, with no error. Keep
  `f.vorgangstyp` and `f.aktualisiert.start`.
- Move the DIP API key out of `bundestag.py` into `.env`. It expires in May 2027 and
  is shared with another project.
- Collect procedures, documents, and lifecycle updates for a fixed time window.
  Measured volume: 75 legislative procedures per 14 days without the Wahlperiode
  filter, 58 with it. Filtering is for precision, not volume reduction.
- Filter against the customer's product categories, materials, sourcing countries,
  EU legal role, and company-size bands.
- Deep-assess only relevant or uncertain items.

### Reputation

- Reuse `vendor/newscrawler/` for the customer-confirmed websites.
- Use the customer's brands, aliases, products, sellers, and executives for matching.
- Use deterministic entity matching first and an LLM only for ambiguity and
  assessment.
- Include social or manual collection only where the customer has explicitly
  confirmed the platform and method.

### Report

Produce one straightforward Chinese document:

1. Executive summary
2. Regulatory items and recommended monitoring actions
3. Reputation mentions, severity, and recommended communications actions
4. Source list and collection status

The report describes exposure and developments; it does not provide legal advice.

## 4. Minimal data boundary

The SQLite model only needs to represent:

- pipeline runs and per-source success/failure;
- versioned raw source items with stable source IDs or URLs;
- customer assessments tied to a raw item, customer, and prompt/profile version;
- generated reports and their covered time windows.

Exact regulatory and media payloads may differ, but both use this boundary. Do not
force them into one business schema and do not store customer relevance directly on
the shared raw record.

Use separate collection and analysis watermarks. A source item is only marked
collected after it is stored; a customer item is only marked analyzed after its
assessment is stored.

## 5. Customer inputs required

Before the pilot:

- confirmed website and social-platform list;
- complete brand/entity/alias list;
- product and material categories;
- sourcing countries and EU legal role;
- approximate turnover/headcount bands where relevant;
- confirmation that the deliverable is monitoring, not compliance advice;
- report window and delivery expectations.

These inputs can start as small JSON files under `clients/<slug>/`. No admin UI or
generic configuration framework is needed.

## 6. First vertical slice

1. Make one existing collector run through brandmonitor with visible source errors.
2. Store its raw output in SQLite.
3. Run one customer relevance/assessment prompt over the stored records.
4. Store the structured assessment with its prompt/profile version.
5. Render a minimal report from stored assessments.
6. Repeat for the other track.
7. Run both tracks over one fixed pilot window and review the result manually.

Build sequentially in that order. Do not scaffold every future connector or analysis
stage before the first source completes the full path.

## 7. Validation

The MVP is done when:

- one command can run the agreed pilot window;
- rerunning the same window does not create duplicate raw items or assessments;
- every configured source reports success, failure, or explicit zero yield;
- raw collected items remain available even when a customer prompt rejects them;
- every assessment identifies its customer and prompt/profile version;
- a small hand-labeled sample catches obvious relevant and irrelevant cases;
- the Chinese report is useful after human review;
- runtime and LLM cost for the cycle are recorded.

## 8. Explicitly later

- regulatory-to-reputation cross-linking;
- daily alert products;
- competitor comparison and advanced trend analytics;
- automated task management for manual collectors;
- a generalized plugin/connector framework;
- customer portal, API, or self-service configuration;
- automatic failover, CI/CD, or multi-host scheduling;
- porting the full L1–L7 agent framework before the simple prompts prove
  insufficient.

After the pilot, review false positives, false negatives, source failures, report
usefulness, time, and cost. Only that evidence should decide which later feature is
next.
