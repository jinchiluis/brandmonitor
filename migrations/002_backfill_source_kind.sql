-- Backfill raw_item.source_kind.
--
-- The first collector hardcoded 'news' for every row it wrote, so the regulatory
-- run landed under the news label and the two tracks could not be selected apart —
-- which the design rule about keeping regulatory and reputation processing
-- independent depends on being able to do.
--
-- run.kind was always recorded correctly, and every raw_item carries the run that
-- first saw it, so the truth can be recovered from the run that wrote each row.

UPDATE raw_item
SET source_kind = (
        SELECT run.kind FROM run WHERE run.id = raw_item.first_run_id
    )
WHERE first_run_id IS NOT NULL
  AND (
        SELECT run.kind FROM run WHERE run.id = raw_item.first_run_id
      ) IS NOT NULL;
