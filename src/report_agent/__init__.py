"""The weekly report stack: freeze a window, assess it, render it, verify it.

Four stages, and only the middle one calls a model:

    run.py export-window --client --since --until   frozen bundle (read-only)
    run.py assess        --bundle                   decisions + issue register
    run.py report        --bundle                   zh report, ledger, coverage
    run.py verify-report --bundle                   verification.json

The split is the point. Stage 1 and stages 3-4 are deterministic and can be
re-run on the same bundle forever; stage 2 is the only place judgment lives, and
it writes keyed rows rather than prose so stage 4 can check them. See
``report_plan.md`` for the worked example this shape came out of.
"""
