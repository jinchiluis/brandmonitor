{client}

## Step 1 of 6: carry-forward

Last cycle left an issue register. Your job is to find out what happened to each
open issue during this window — including in material a gate rejected.

Call `open_issues` first. For each issue that is not `closed`:

1. Read its `next` field. That names the evidence that would justify an update.
2. Search this window for exactly that. Use `search_titles` with
   `include_stopped: true` and then `search_bodies`. Search in German first:
   this is a German corpus, and the English word usually finds nothing.
3. Open what looks like a match with `get_item`, and `get_body` before asserting
   what it says.

Why stopped items are in scope: the gates answer "is this a signal for this
client on its own", and a continuing story often fails that test. A port strike
article that never mentions parcels is stopped as non-parcel-specific, yet it is
the second instalment of an issue already open. That item is exactly what this
step exists to recover.

Bound your search by the open issues. Do not sweep the rejected corpus for new
subjects; a later step handles what is new. If an issue has no new evidence,
that is a finding — return it with `matched: []` and say what you searched for,
so next week knows this was looked for and not found rather than skipped.

Return, for each open issue:

- `issue_id`, copied exactly from the register
- `matched`: raw_item_ids in this window that genuinely continue it
- `searched`: the queries you actually ran, so the negative result is auditable
- `status_now`: one sentence, stating what the cutoff evidence supports and what
  it does not. "Reported on 7 September; status at the cutoff unverified" is
  right. "Still on strike" is wrong unless a source says so at the cutoff.
- `still_open`: false only when the stored evidence closes it
