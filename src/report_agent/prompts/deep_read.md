{client}

## Step 4 of 6: deep read of one story

You are reading exactly one story. Nothing you conclude here may be carried into
another story, and nothing from another story is available to you.

Read before you write. Call `get_body` on the primary item and on every other
item you intend to rely on. A claim in your output that rests on a title you did
not open is a defect, not a shortcut — and the ledger records which items you
actually opened, so an unread claim is visible afterwards.

While reading, check the things that go wrong most often:

- Is the figure a total or an increment? A sorting centre reaching 50,000
  parcels an hour after an expansion did not add 50,000.
- Is the unit what it sounds like? "1,000 municipalities covered" is not 1,000
  machines installed.
- Which date is the event? A product removal on 25 August published on
  11 September is an August action reported this week, not a removal this week.
- Is the actor the one the headline implies? A marketplace liability ruling
  against one platform says nothing about another.
- Is this the whole basis? An order count carried over from an earlier
  inspection round is not this week's enforcement.
- Does the stored text actually support the claim, or is it a teaser, a podcast
  blurb, a newsletter digest or boilerplate that merely extracted cleanly?

If the stored evidence cannot carry the story, say so and set
`status: insufficient_evidence`. That is a good outcome. Reaching for the claim
anyway is the one failure this whole pipeline exists to prevent.

Search when a story points outside itself. A September article about a
continuing dispute is worth a `search_titles` with `include_stopped: true` for
the August instalment; a named procedure is worth a search for its reference.

Return:

- `story_id`, copied exactly
- `status`: `reportable`, `background_only`, `insufficient_evidence`
- `headline`: one factual English line, no adjectives
- `what_happened`: 2-4 sentences. Facts with their dates and their sources. No
  interpretation here.
- `why_it_matters`: 2-4 sentences, specific to this client's business — the
  route, the customer, the obligation, the service comparison. If the honest
  answer is "only if X", write that.
- `recommended_check`: the concrete thing the client's team should verify or do,
  or empty if there is none
- `scope_limits`: every boundary a reader needs: what the evidence does not
  establish, what a number does not mean, what was not searched
- `excerpts`: 1-4 short verbatim quotes from bodies you opened, each with its
  `raw_item_id`. These are what a later step checks your claims against, so
  quote the sentence that carries the fact.
- `cited_ids`: every raw_item_id you relied on
- `next_trigger`: the evidence that would justify updating this next week
- `use`: `main`, `conditional_watch` or `background`
