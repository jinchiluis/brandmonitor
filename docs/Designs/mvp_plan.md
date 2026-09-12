# Brandmonitor MVP Plan

Status: **in progress**. Collection, storage, body enrichment, client profiles, the
deterministic selector, Safety Gate alerts, and DSA aggregates are built. Assessment
and report generation remain. This is the implementation plan for the first customer pilot.
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

- Keep the eight configured German/EU regulatory article sources independent from
  news collection and assess them by topic rather than brand.
- Collect Bundestag and Bundesrat procedures (DIP, every procedure type, not only
  bills) and a hand-picked list of European Parliament procedures as regulatory
  items, assessed the same way. Ministry drafts before cabinet are not covered.
- The EU Safety Gate collector stores the strongest measured item-level source,
  including product, brand, risk, measure, origin, notifying country, recall URL,
  and online trader.
- Keep DSA Transparency Database data as a small aggregate background series. It
  contains no useful product-level detail and says nothing directly about J&T.
- Store each source's native payload inside the shared raw-item envelope; do not
  force structured alerts or aggregates into the article schema.
- Deep-assess only relevant or uncertain records.

### Reputation

- Continue daily collection from the configured news, trade, and association sites.
- Use the versioned client profile for brands, competitors, customers, topics, and
  sector vocabulary.
- Use deterministic selection first, a cheap title relevance check for title-only
  publishers, and full assessment only after body evidence exists.
- Fetch title-only bodies only after a match; do not bulk-fetch paid publications.
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

Still required before the pilot report:

- confirmation whether the September 10 keyword list extends or replaces the earlier
  monitoring terms;
- explanation of the customer's identifier `01519` and expected alert cadence;
- product and material categories;
- sourcing countries and EU legal role;
- approximate turnover/headcount bands where relevant;
- confirmation that the deliverable is monitoring, not compliance advice;
- report window and delivery expectations.

These inputs can start as small JSON files under `clients/<slug>/`. No admin UI or
generic configuration framework is needed.

## 6. First vertical slice

1. ~~Run collectors through brandmonitor with visible source errors.~~
2. ~~Store versioned raw output and enriched bodies in SQLite.~~
3. ~~Apply a versioned client profile and deterministic candidate selector.~~
4. ~~Build Safety Gate and store its native structured payload.~~
5. Sketch the report and define the structured assessment output it requires.
6. Implement title gating, fetch-on-match, and full assessment.
7. Store assessments with prompt/profile/source versions.
8. Render a minimal Chinese report and run both tracks over one fixed pilot window.

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
