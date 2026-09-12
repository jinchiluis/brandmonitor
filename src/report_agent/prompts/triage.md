{client}

## Step 2 of 6: triage

Below is one batch of this window's candidates: everything that reached
assessment, with its date, where that date came from, its source, and the gate's
verdict and reason. You decide which are worth an analyst's reading time.

This is a keep/omit call, not an assessment. Keeping something costs a deep
read; omitting it removes it from the week. When the title and the gate reason
leave you unsure whether it matters, keep it — the next steps can drop it after
reading, and nothing can recover what you drop here.

Keep an item when any of these holds:

- it names an own brand, a customer or a competitor in a service, regulatory,
  enforcement, volume or product-safety context
- it reports a rule, ruling, inspection, decision, procedure step or official
  figure in one of this client's regulatory areas
- it reports a change in how goods move into or across Germany or the EU —
  carriers, ports, customs, returns, packaging, platform logistics
- it is the kind of thing an operations or commercial team would act on

Omit an item when:

- the keyword match is the whole connection (the word occurs; the subject is
  elsewhere)
- it is an evergreen explainer or legal-advice page with no new rule, ruling or
  case — undated explainers are the largest single class of this
- it is a hub, category or listing page rather than an article
- it repeats something already in this batch with nothing added; keep the
  earliest or fullest one and omit the rest as `duplicate`

A `week` item with a real date and a plausible subject is kept by default. A
`history` item is kept only if it is evidently the background of something this
week. An `undated` item is kept only if its substance is clearly new; there is
no way to place it in the week otherwise.

For each numbered item return `n`, `keep`, a `reason` of at most 15 words, and
`bucket`: one of `regulatory`, `customer_platform`, `competitor_market`,
`transport_disruption`, `product_safety`, `own_brand`, `other`.
