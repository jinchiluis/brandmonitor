"""Tests for body-fetch pacing and per-source fairness.

The first body run sent all 100 of its requests to one domain because tasks sorted
alphabetically by source. These guard both halves of the fix: the batch is shared
between sources, and one host is never hit faster than the configured interval.
"""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.bodies import interleave_by_source, pace_host  # noqa: E402


def task(slug, url):
    return {"source_slug": slug, "external_id": url}


class TestInterleaveBySource:
    def test_alternates_between_sources(self):
        tasks = [task("a.de", f"https://a.de/{i}") for i in range(3)]
        tasks += [task("b.de", f"https://b.de/{i}") for i in range(3)]
        order = [t["source_slug"] for t in interleave_by_source(tasks)]
        assert order == ["a.de", "b.de"] * 3

    def test_preserves_order_within_a_source(self):
        tasks = [task("a.de", f"https://a.de/{i}") for i in range(3)]
        tasks += [task("b.de", "https://b.de/0")]
        out = [t["external_id"] for t in interleave_by_source(tasks)
               if t["source_slug"] == "a.de"]
        assert out == ["https://a.de/0", "https://a.de/1", "https://a.de/2"]

    def test_keeps_every_task(self):
        tasks = ([task("a.de", f"https://a.de/{i}") for i in range(5)] +
                 [task("b.de", f"https://b.de/{i}") for i in range(2)] +
                 [task("c.de", "https://c.de/0")])
        assert len(interleave_by_source(tasks)) == 8

    def test_a_large_source_no_longer_monopolises_the_limit(self):
        """The real failure: one source took the entire first batch."""
        tasks = [task("big.de", f"https://big.de/{i}") for i in range(500)]
        tasks += [task("small.de", f"https://small.de/{i}") for i in range(5)]
        first_ten = [t["source_slug"] for t in interleave_by_source(tasks)[:10]]
        assert first_ten.count("small.de") == 5

    def test_exhausted_source_drops_out(self):
        tasks = [task("a.de", "https://a.de/0")]
        tasks += [task("b.de", f"https://b.de/{i}") for i in range(3)]
        order = [t["source_slug"] for t in interleave_by_source(tasks)]
        assert order == ["a.de", "b.de", "b.de", "b.de"]

    def test_empty_input(self):
        assert interleave_by_source([]) == []


class TestPaceHost:
    def test_first_hit_on_a_host_does_not_wait(self):
        assert pace_host("https://a.de/1", {}, delay=5) == 0.0

    def test_second_hit_on_the_same_host_waits(self):
        seen = {}
        pace_host("https://a.de/1", seen, delay=0.05)
        start = time.monotonic()
        pace_host("https://a.de/2", seen, delay=0.05)
        assert time.monotonic() - start >= 0.04

    def test_a_different_host_is_not_delayed(self):
        seen = {}
        pace_host("https://a.de/1", seen, delay=5)
        start = time.monotonic()
        pace_host("https://b.de/1", seen, delay=5)
        assert time.monotonic() - start < 0.1

    def test_hosts_are_tracked_case_insensitively(self):
        seen = {}
        pace_host("https://A.de/1", seen, delay=5)
        assert "a.de" in seen and len(seen) == 1

    def test_zero_delay_disables_pacing(self):
        seen = {}
        pace_host("https://a.de/1", seen, delay=0)
        assert pace_host("https://a.de/2", seen, delay=0) == 0.0

    def test_elapsed_time_counts_against_the_delay(self):
        """A slow fetch already provided the gap; do not sleep the full interval."""
        seen = {"a.de": time.monotonic() - 10}
        assert pace_host("https://a.de/1", seen, delay=0.05) == 0.0
