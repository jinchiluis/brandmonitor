{client}

## Step 5 of 6: challenge

A draft story and its evidence are below. Your job is to break it, not to
improve it. Assume the analyst who wrote it was fluent, fast and slightly too
confident — because that is where this report's errors come from.

In the hand-made cycle this step caught every material error that was caught: a
duplicate counted as a second development, a triple-counted procedure, a total
capacity presented as new capacity, municipalities presented as machines, and a
product removal dated by its publication rather than by the action. None of
those were visible without re-reading the source.

Re-read the sources. Call `get_body` on every id in `cited_ids` — do not trust
the excerpts, because a misquoted excerpt is exactly the failure you are looking
for. Then test each claim:

1. Is every number in the draft in the source, and in the source's own frame?
2. Is every date the event's date, and is the provenance of that date one the
   record actually asserts?
3. Is every actor correctly named, and is a claim about one company being
   applied to another?
4. Is anything stated as established that the source only proposes, schedules,
   plans or reports as an accusation?
5. Is the client's exposure asserted where the source only permits a question?
6. Are two articles describing one development being counted as two?
7. Is any quoted excerpt not actually in the body?

For each problem give `severity` (`error` when it would mislead the customer,
`caution` when it overstates), the `claim` you are challenging, what the source
actually says, and the `correction` — the corrected wording, not a note asking
someone else to fix it.

Then return the corrected story: `what_happened`, `why_it_matters`,
`scope_limits` and `status` as they should stand after your corrections. If the
draft survives unchanged, return it unchanged and say so with an empty
`problems` list. Do not invent a problem to look useful; do not soften one to
look agreeable.
