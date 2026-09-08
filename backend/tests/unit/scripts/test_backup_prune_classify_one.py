"""Unit tests for the new ``_classify_one`` helper in scripts/backup-prune.py."""

from __future__ import annotations

from datetime import date
from importlib.machinery import SourceFileLoader
from importlib.util import module_from_spec, spec_from_loader
from pathlib import Path

for parent in Path(__file__).resolve().parents:
    script_path = parent / "scripts" / "backup-prune.py"
    if script_path.exists():
        break
else:
    raise RuntimeError("Could not find repo root (scripts/backup-prune.py)")

_loader = SourceFileLoader("backup_prune_cls", str(script_path))
prune = module_from_spec(spec_from_loader("backup_prune_cls", _loader))
_loader.exec_module(prune)

BackupFile = prune.BackupFile


def _bk(datestr: str, org: str = "abc123") -> BackupFile:
    return BackupFile(
        path=f"/tmp/modulo-backup-{org}-{datestr}T010101.tar.gz.enc",
        date=date(int(datestr[:4]), int(datestr[4:6]), int(datestr[6:8])),
        org=org,
    )


def test_classify_one_monthly_first_of_month():
    b = _bk("20260101")
    counts = [0, 0, 0]
    seen_weeks: set = set()
    seen_months: set = set()
    reason = prune._classify_one(b, counts, seen_weeks, seen_months, keep_daily=7, keep_weekly=4, keep_monthly=6)
    assert reason == "monthly"
    assert counts == [0, 0, 1]
    assert (2026, 1) in seen_months


def test_classify_one_weekly_sunday():
    # 2026-01-04 is a Sunday (iso_weekday == 7).
    b = _bk("20260104")
    counts = [0, 0, 0]
    seen_weeks: set = set()
    seen_months: set = set()
    reason = prune._classify_one(b, counts, seen_weeks, seen_months, keep_daily=7, keep_weekly=4, keep_monthly=6)
    assert reason == "weekly"
    assert counts == [0, 1, 0]


def test_classify_one_daily_ordinary_day():
    b = _bk("20260102")
    counts = [0, 0, 0]
    seen_weeks: set = set()
    seen_months: set = set()
    reason = prune._classify_one(b, counts, seen_weeks, seen_months, keep_daily=7, keep_weekly=4, keep_monthly=6)
    assert reason == "daily"
    assert counts == [1, 0, 0]


def test_classify_one_pruned_when_buckets_full():
    b = _bk("20260102")
    counts = [7, 4, 6]  # all at their keep limits
    seen_weeks: set = set()
    seen_months: set = set()
    reason = prune._classify_one(b, counts, seen_weeks, seen_months, keep_daily=7, keep_weekly=4, keep_monthly=6)
    assert reason is None
    assert counts == [7, 4, 6]


def test_classify_one_monthly_already_seen_falls_through():
    b = _bk("20260101")
    counts = [0, 0, 0]
    seen_weeks: set = set()
    seen_months = {(2026, 1)}
    reason = prune._classify_one(b, counts, seen_weeks, seen_months, keep_daily=7, keep_weekly=4, keep_monthly=6)
    # not monthly (already seen); not Sunday; becomes daily
    assert reason == "daily"
    assert counts == [1, 0, 0]
