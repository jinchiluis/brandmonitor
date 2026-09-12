{client}

## Step 3 of 6: cluster

Below is the whole shortlist this window kept, plus the open issues that carried
forward. Group it into the stories the report will actually have.

A story is one development, whatever number of articles describe it. In the
hand-made cycle, four groups of articles covering nine pieces described four
developments; reported unmerged that would have been five spurious entries in a
single week. Trade media restate each other within a day or two, and the
restatement usually adds nothing but a second date.

Merge when the items describe the same underlying event, decision, procedure or
announcement — even across sources, even when the headlines differ. Do not merge
two different events that share a subject: two packaging articles about one
relief proposal are one story; a packaging relief proposal and a packaging
advertising ruling are two.

For each story give:

- `story_id`: short, lowercase, hyphenless, stable enough to reuse next week
  (`ports`, `packaging`, `tiktokuk`). Reuse the id of a carried-forward issue
  when the story continues it.
- `label`: a short English name for internal use
- `item_ids`: every shortlisted id in the story
- `primary_id`: the item that best evidences it — usually the earliest full
  report rather than the restatement
- `carries_issue_id`: the open issue this continues, or empty
- `priority`: `high` when an operations or commercial team would act on it this
  week, `medium` when it is worth knowing, `low` when it is context
- `merge_note`: for a multi-item story, one line on what the others add — a
  restatement, a second source, a later development

Put items that belong to no story into `unclustered` with a one-line reason.
Everything on the shortlist appears exactly once, in a story or unclustered.
