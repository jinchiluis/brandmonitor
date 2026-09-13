"""Backup snapshot, tiered rotation, and off-box mirroring."""

import gzip
import sqlite3
import tarfile
from datetime import date, timedelta
from pathlib import Path

import pytest

from src.backup import (
    keep_set,
    parse_day,
    prune,
    run_backup,
    snapshot_name,
    stem_for,
    write_snapshot,
)


def make_db(path: Path, rows: int = 50) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE item (id INTEGER PRIMARY KEY, body TEXT)")
    conn.executemany("INSERT INTO item (body) VALUES (?)",
                     [(f"row {i} " + "x" * 200,) for i in range(rows)])
    conn.commit()
    conn.close()
    return path


class TestNaming:
    def test_stem_drops_every_suffix(self):
        assert stem_for(Path("data/brandmonitor.sqlite3")) == "brandmonitor"
        assert stem_for(Path("/var/db/app.pre-purge.sqlite3")) == "app"

    def test_parse_day_roundtrips(self):
        name = snapshot_name("brandmonitor", date(2026, 9, 10))
        assert name == "brandmonitor-20260910.db.gz"
        assert parse_day(Path(name), "brandmonitor") == date(2026, 9, 10)

    @pytest.mark.parametrize("name", [
        "other-20260910.db.gz",           # another app's snapshot
        "brandmonitor-notadate.db.gz",
        "brandmonitor-20260910.db",       # uncompressed leftover
        "brandmonitor.pre-purge.sqlite3",  # ad-hoc snapshot, not ours to rotate
    ])
    def test_parse_day_rejects_foreign_names(self, name):
        assert parse_day(Path(name), "brandmonitor") is None


class TestKeepSet:
    def test_daily_tier_keeps_the_most_recent(self):
        days = [date(2026, 9, 10) - timedelta(days=i) for i in range(30)]
        keep = keep_set(days, daily=14, weekly=0, monthly=0)
        assert keep == set(days[:14])

    def test_weekly_tier_keeps_one_per_iso_week(self):
        days = [date(2026, 9, 10) - timedelta(days=i) for i in range(60)]
        keep = keep_set(days, daily=0, weekly=4, monthly=0)
        assert len(keep) == 4
        assert len({d.isocalendar()[:2] for d in keep}) == 4

    def test_monthly_tier_keeps_the_newest_of_each_month(self):
        days = [date(2026, m, d) for m in (5, 6, 7) for d in (1, 15, 28)]
        keep = keep_set(days, daily=0, weekly=0, monthly=3)
        assert keep == {date(2026, 5, 28), date(2026, 6, 28), date(2026, 7, 28)}

    def test_tiers_overlap_so_early_counts_stay_small(self):
        """One date can fill the daily, weekly and monthly slot at once."""
        keep = keep_set([date(2026, 9, 10)], daily=14, weekly=8, monthly=12)
        assert keep == {date(2026, 9, 10)}

    def test_a_year_of_dailies_collapses_to_the_tier_budget(self):
        days = [date(2026, 9, 10) - timedelta(days=i) for i in range(400)]
        keep = keep_set(days, daily=14, weekly=8, monthly=12)
        assert len(keep) <= 14 + 8 + 12
        # Reach matters more than count: the oldest survivor is most of a year
        # back, which a flat 30-day cutoff could never provide at any size.
        assert min(keep) < date(2026, 9, 10) - timedelta(days=300)

    def test_zero_tiers_keep_nothing(self):
        days = [date(2026, 9, 10) - timedelta(days=i) for i in range(5)]
        assert keep_set(days, daily=0, weekly=0, monthly=0) == set()


class TestSnapshot:
    def test_snapshot_is_gzipped_and_restores(self, tmp_path):
        db = make_db(tmp_path / "data" / "brandmonitor.sqlite3")
        archive = write_snapshot(db, tmp_path / "backups", date(2026, 9, 10))

        assert archive.name == "brandmonitor-20260910.db.gz"
        restored = tmp_path / "restored.db"
        with gzip.open(archive, "rb") as fin:
            restored.write_bytes(fin.read())
        conn = sqlite3.connect(str(restored))
        assert conn.execute("SELECT COUNT(*) FROM item").fetchone()[0] == 50
        conn.close()

    def test_no_uncompressed_leftover(self, tmp_path):
        db = make_db(tmp_path / "brandmonitor.sqlite3")
        write_snapshot(db, tmp_path / "backups", date(2026, 9, 10))
        assert list((tmp_path / "backups").glob("*.db")) == []

    def test_snapshot_works_while_a_writer_holds_the_db(self, tmp_path):
        """The whole point of the online backup API - see the module docstring."""
        db = make_db(tmp_path / "brandmonitor.sqlite3")
        holder = sqlite3.connect(str(db))
        holder.execute("INSERT INTO item (body) VALUES ('uncommitted')")
        try:
            archive = write_snapshot(db, tmp_path / "backups", date(2026, 9, 10))
        finally:
            holder.close()
        assert archive.stat().st_size > 0

    def test_rerunning_the_same_day_overwrites(self, tmp_path):
        db = make_db(tmp_path / "brandmonitor.sqlite3")
        write_snapshot(db, tmp_path / "backups", date(2026, 9, 10))
        write_snapshot(db, tmp_path / "backups", date(2026, 9, 10))
        assert len(list((tmp_path / "backups").glob("*.db.gz"))) == 1


class TestPrune:
    def test_prune_removes_only_unkept_own_snapshots(self, tmp_path):
        for name in ("brandmonitor-20260901.db.gz", "brandmonitor-20260910.db.gz",
                     "other-20260901.db.gz", "notes.txt"):
            (tmp_path / name).write_bytes(b"x")

        removed = prune(tmp_path, "brandmonitor", {date(2026, 9, 10)})

        assert removed == ["brandmonitor-20260901.db.gz"]
        assert (tmp_path / "other-20260901.db.gz").exists()
        assert (tmp_path / "notes.txt").exists()

    def test_prune_tolerates_a_missing_directory(self, tmp_path):
        assert prune(tmp_path / "nope", "brandmonitor", set()) == []


class TestRunBackup:
    def test_writes_locally_and_off_box(self, tmp_path):
        db = make_db(tmp_path / "brandmonitor.sqlite3")
        s = run_backup(db_path=db, backup_dir=tmp_path / "b",
                       offbox_dir=tmp_path / "off", day=date(2026, 9, 10))

        assert s.snapshot.exists() and s.offbox.exists()
        assert s.offbox.read_bytes() == s.snapshot.read_bytes()
        assert s.offbox_error is None

    def test_empty_offbox_disables_the_copy(self, tmp_path):
        db = make_db(tmp_path / "brandmonitor.sqlite3")
        s = run_backup(db_path=db, backup_dir=tmp_path / "b", offbox_dir="",
                       day=date(2026, 9, 10))
        assert s.offbox is None and s.offbox_error is None

    def test_todays_snapshot_survives_even_when_tiers_are_zero(self, tmp_path):
        """Never delete what this run just wrote."""
        db = make_db(tmp_path / "brandmonitor.sqlite3")
        s = run_backup(db_path=db, backup_dir=tmp_path / "b", offbox_dir="",
                       day=date(2026, 9, 10),
                       keep_daily=0, keep_weekly=0, keep_monthly=0)
        assert s.snapshot.exists()

    def test_old_snapshots_are_pruned_on_both_sides(self, tmp_path):
        db = make_db(tmp_path / "brandmonitor.sqlite3")
        local, off = tmp_path / "b", tmp_path / "off"
        for d in (local, off):
            d.mkdir(parents=True)
            (d / "brandmonitor-20250101.db.gz").write_bytes(b"stale")

        s = run_backup(db_path=db, backup_dir=local, offbox_dir=off,
                       day=date(2026, 9, 10), keep_daily=1, keep_weekly=0,
                       keep_monthly=0)

        assert s.pruned_local == ["brandmonitor-20250101.db.gz"]
        assert s.pruned_offbox == ["brandmonitor-20250101.db.gz"]

    def test_a_broken_offbox_target_does_not_fail_the_backup(self, tmp_path):
        """A paused sync client must not cost us today's snapshot."""
        db = make_db(tmp_path / "brandmonitor.sqlite3")
        blocker = tmp_path / "off"
        blocker.write_text("this is a file, not a directory")

        s = run_backup(db_path=db, backup_dir=tmp_path / "b", offbox_dir=blocker,
                       day=date(2026, 9, 10))

        assert s.snapshot.exists()
        assert s.offbox is None
        assert s.offbox_error

    def test_size_guard_warns_without_deleting(self, tmp_path):
        db = make_db(tmp_path / "brandmonitor.sqlite3", rows=500)
        s = run_backup(db_path=db, backup_dir=tmp_path / "b", offbox_dir="",
                       day=date(2026, 9, 10), warn_total_mb=0.000001)
        assert s.over_warn_limit
        assert s.snapshot.exists()

    def test_missing_database_is_an_error(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            run_backup(db_path=tmp_path / "gone.sqlite3",
                       backup_dir=tmp_path / "b", offbox_dir="")


def make_state(data: Path) -> None:
    register = data / "reports" / "jt-express-2026-09-05_2026-09-11" / "issue-register.json"
    register.parent.mkdir(parents=True)
    register.write_text('{"issues": []}', encoding="utf-8")
    gate = data / "title_gate" / "jt-express" / "2026-09-13.jsonl"
    gate.parent.mkdir(parents=True)
    gate.write_text('{"id": 1, "keep": true}\n', encoding="utf-8")
    (data / "log").mkdir()
    (data / "log" / "run.log").write_text("regenerated, not state")


class TestStateArchive:
    def test_state_directories_are_archived_and_mirrored(self, tmp_path):
        data = tmp_path / "data"
        db = make_db(data / "brandmonitor.sqlite3")
        make_state(data)

        s = run_backup(db_path=db, backup_dir=tmp_path / "b",
                       offbox_dir=tmp_path / "off", day=date(2026, 9, 13))

        assert s.state.name == "brandmonitor-state-20260913.tar.gz"
        assert s.state_dirs == ["reports", "title_gate"]
        assert s.state_offbox.read_bytes() == s.state.read_bytes()
        with tarfile.open(s.state) as tar:
            names = tar.getnames()
        assert "reports/jt-express-2026-09-05_2026-09-11/issue-register.json" in names
        assert "title_gate/jt-express/2026-09-13.jsonl" in names
        assert not any(n.startswith("log") for n in names)

    def test_archive_restores_by_extracting_into_data(self, tmp_path):
        data = tmp_path / "data"
        db = make_db(data / "brandmonitor.sqlite3")
        make_state(data)
        s = run_backup(db_path=db, backup_dir=tmp_path / "b", offbox_dir="",
                       day=date(2026, 9, 13))

        restored = tmp_path / "restored"
        with tarfile.open(s.state) as tar:
            tar.extractall(restored, filter="data")
        assert (restored / "title_gate" / "jt-express" / "2026-09-13.jsonl").read_text(
            encoding="utf-8") == '{"id": 1, "keep": true}\n'

    def test_no_state_directories_means_no_archive(self, tmp_path):
        """A fresh checkout has no reports yet; that is not a failure."""
        db = make_db(tmp_path / "brandmonitor.sqlite3")
        s = run_backup(db_path=db, backup_dir=tmp_path / "b", offbox_dir="",
                       day=date(2026, 9, 13))
        assert s.state is None and s.state_error is None
        assert list((tmp_path / "b").glob("*.tar.gz")) == []

    def test_state_rotation_is_separate_from_snapshot_rotation(self, tmp_path):
        data = tmp_path / "data"
        db = make_db(data / "brandmonitor.sqlite3")
        make_state(data)
        local = tmp_path / "b"
        local.mkdir()
        (local / "brandmonitor-state-20250101.tar.gz").write_bytes(b"stale")
        (local / "brandmonitor-20250101.db.gz").write_bytes(b"stale")

        s = run_backup(db_path=db, backup_dir=local, offbox_dir="",
                       day=date(2026, 9, 13), keep_daily=1, keep_weekly=0,
                       keep_monthly=0)

        assert sorted(s.pruned_local) == ["brandmonitor-20250101.db.gz",
                                          "brandmonitor-state-20250101.tar.gz"]
        assert sorted(p.name for p in local.iterdir()) == [
            "brandmonitor-20260913.db.gz", "brandmonitor-state-20260913.tar.gz"]

    def test_a_state_failure_does_not_cost_the_snapshot(self, tmp_path, monkeypatch):
        data = tmp_path / "data"
        db = make_db(data / "brandmonitor.sqlite3")
        make_state(data)

        def locked(*args, **kwargs):
            raise PermissionError("file in use by a report run")
        monkeypatch.setattr(tarfile.TarFile, "add", locked)

        s = run_backup(db_path=db, backup_dir=tmp_path / "b",
                       offbox_dir=tmp_path / "off", day=date(2026, 9, 13))

        assert s.snapshot.exists() and s.offbox.exists()
        assert s.state is None and "PermissionError" in s.state_error
        assert list((tmp_path / "b").glob("*.tar.gz*")) == []
