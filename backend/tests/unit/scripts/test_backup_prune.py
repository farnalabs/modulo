"""Unit tests for backup-prune.py — retention-based backup pruning."""

from __future__ import annotations

import sys
from datetime import date
from importlib.machinery import SourceFileLoader
from importlib.util import module_from_spec, spec_from_loader
from pathlib import Path
from unittest.mock import patch

import pytest

for parent in Path(__file__).resolve().parents:
    script_path = parent / "scripts" / "backup-prune.py"
    if script_path.exists():
        break
else:
    raise RuntimeError("Could not find repo root (scripts/backup-prune.py)")

_prune_loader = SourceFileLoader("backup_prune", str(script_path))
prune = module_from_spec(spec_from_loader("backup_prune", _prune_loader))
_prune_loader.exec_module(prune)

BackupFile = prune.BackupFile
BackupFiles = list[BackupFile]


def make_backup(datestr: str, org: str = "abc123") -> BackupFile:
    """Build a BackupFile with a path matching the real naming convention."""
    return BackupFile(
        path=f"/tmp/modulo-backup-{org}-{datestr}T010101.tar.gz.enc",
        date=date(int(datestr[:4]), int(datestr[4:6]), int(datestr[6:8])),
        org=org,
    )


def kept_dates(backups: BackupFiles) -> list[date]:
    keep = prune.classify_backups(backups)
    return sorted(b.date for b in backups if b.path in keep)


DAILY_STREAM = [
    "20260702",
    "20260703",
    "20260704",
    "20260706",
    "20260707",
    "20260708",
    "20260709",
    "20260710",
    "20260711",
    "20260713",
]

MONTHLY_STREAM = [
    make_backup(date(y, m, 1).strftime("%Y%m%d"))
    for y in (2025, 2026)
    for m in range(1, 13)
    if date(2025, 1, 1) <= date(y, m, 1) <= date(2026, 6, 1)
]


# ---------------------------------------------------------------------------
# collect_backups
# ---------------------------------------------------------------------------


def test_collect_backups_parses_matching_files(tmp_path):
    names = [
        "modulo-backup-abc123-20260701T010101.tar.gz.enc",
        "modulo-backup-def456-20260702T010101.tar.gz.enc",
    ]
    for n in names:
        (tmp_path / n).write_text("x")

    backups = prune.collect_backups(str(tmp_path))

    assert len(backups) == 2
    by_org = {b.org: b for b in backups}
    assert by_org["abc123"].date == date(2026, 7, 1)
    assert by_org["def456"].date == date(2026, 7, 2)


def test_collect_backups_skips_non_matching_files(tmp_path):
    (tmp_path / "modulo-backup.tar.gz.enc").write_text("x")
    (tmp_path / "other.txt").write_text("x")
    (tmp_path / "modulo-backup-abc123-20260701T010101").write_text("x")

    assert prune.collect_backups(str(tmp_path)) == []


def test_collect_backups_skips_invalid_dates(tmp_path):
    (tmp_path / "modulo-backup-abc123-20261340T010101.tar.gz.enc").write_text("x")
    (tmp_path / "modulo-backup-abc123-20260701T010101.tar.gz.enc").write_text("x")

    backups = prune.collect_backups(str(tmp_path))

    assert [b.date for b in backups] == [date(2026, 7, 1)]


def test_collect_backups_returns_empty_for_empty_dir(tmp_path):
    assert prune.collect_backups(str(tmp_path)) == []


def test_collect_backups_sorts_by_date_descending(tmp_path):
    for ds in ("20260701", "20260615", "20260620", "20260601"):
        (tmp_path / f"modulo-backup-abc123-{ds}T010101.tar.gz.enc").write_text("x")

    backups = prune.collect_backups(str(tmp_path))

    dates = [b.date for b in backups]
    assert dates == sorted(dates, reverse=True)


# ---------------------------------------------------------------------------
# classify_backups
# ---------------------------------------------------------------------------


def test_classify_empty_returns_empty_set():
    assert prune.classify_backups([]) == set()


def test_classify_keeps_all_when_under_limits():
    backups = [make_backup(f"2026070{i}") for i in range(2, 6)]
    assert len(kept_dates(backups)) == 4


def test_classify_keeps_seven_most_recent_daily():
    backups = [make_backup(d) for d in DAILY_STREAM]

    kept = kept_dates(backups)

    assert kept == [date(2026, 7, d) for d in (6, 7, 8, 9, 10, 11, 13)]


def test_classify_keeps_first_of_month_backups():
    backups = [make_backup(d) for d in ("20260301", "20260302", "20260303")]
    kept = kept_dates(backups)
    assert date(2026, 3, 1) in kept


def test_classify_keeps_only_four_newest_weekly():
    backups = []
    d = date(2026, 6, 2)
    while d <= date(2026, 6, 30):
        backups.append(make_backup(d.strftime("%Y%m%d")))
        d = date.fromordinal(d.toordinal() + 1)
    for ds in ("20260503", "20260510", "20260517"):
        backups.append(make_backup(ds))

    kept = kept_dates(backups)
    kept_sundays = [b.date for b in backups if b.date in kept and b.date.isocalendar().weekday == 7]

    assert kept_sundays == [date(2026, 6, d) for d in (7, 14, 21, 28)]
    assert date(2026, 5, 17) not in kept


def test_classify_keeps_twelve_most_recent_monthly():
    backups = list(MONTHLY_STREAM)
    d = date(2026, 6, 2)
    while d <= date(2026, 6, 30):
        if d.isocalendar().weekday != 7:
            backups.append(make_backup(d.strftime("%Y%m%d")))
        d = date.fromordinal(d.toordinal() + 1)

    kept = kept_dates(backups)
    kept_monthlies = [b.date for b in backups if b.date.day == 1 and b.date in kept]

    assert len(kept_monthlies) >= 12
    assert date(2026, 6, 1) in kept
    assert date(2025, 5, 1) not in kept
    assert date(2025, 1, 1) not in kept


def test_classify_retention_is_per_org():
    backups = [make_backup(d, org=org) for org in ("abc123", "def456") for d in DAILY_STREAM]

    keep = prune.classify_backups(backups)

    kept_per_org = {}
    for org in ("abc123", "def456"):
        kept_per_org[org] = sum(1 for b in backups if b.org == org and b.path in keep)
    assert kept_per_org == {"abc123": 7, "def456": 7}


def test_classify_monthly_falls_back_to_daily_when_over_12():
    keep = prune.classify_backups(MONTHLY_STREAM)

    assert len(keep) == 18


# ---------------------------------------------------------------------------
# prune_backups
# ---------------------------------------------------------------------------


def test_prune_dry_run_does_not_delete(tmp_path, capsys):
    keep_path = tmp_path / "modulo-backup-abc123-20260701T010101.tar.gz.enc"
    delete_path = tmp_path / "modulo-backup-abc123-20260601T010101.tar.gz.enc"
    keep_path.write_text("x")
    delete_path.write_text("x")
    keep = {str(keep_path)}

    prune.prune_backups(str(tmp_path), keep, dry_run=True)

    assert keep_path.exists()
    assert delete_path.exists()
    out = capsys.readouterr().out
    assert "Would delete: modulo-backup-abc123-20260601T010101.tar.gz.enc" in out
    assert "Deleted:" not in out


def test_prune_deletes_non_kept_matching_files(tmp_path, capsys):
    keep_path = tmp_path / "modulo-backup-abc123-20260701T010101.tar.gz.enc"
    delete_path = tmp_path / "modulo-backup-abc123-20260601T010101.tar.gz.enc"
    unrelated = tmp_path / "README.md"
    keep_path.write_text("x")
    delete_path.write_text("x")
    unrelated.write_text("x")
    keep = {str(keep_path)}

    prune.prune_backups(str(tmp_path), keep, dry_run=False)

    assert keep_path.exists()
    assert not delete_path.exists()
    assert unrelated.exists()
    assert "Deleted: modulo-backup-abc123-20260601T010101.tar.gz.enc" in capsys.readouterr().out


def test_prune_empty_dir_is_noop(tmp_path, capsys):
    prune.prune_backups(str(tmp_path), set(), dry_run=False)
    assert capsys.readouterr().out == ""


# ---------------------------------------------------------------------------
# parse_args
# ---------------------------------------------------------------------------


def test_parse_args_defaults(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["backup-prune.py"])
    args = prune.parse_args()
    assert args.backup_dir == "."
    assert args.dry_run is False
    assert args.keep_daily == 7
    assert args.keep_weekly == 4
    assert args.keep_monthly == 12


def test_parse_args_overrides(monkeypatch):
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "backup-prune.py",
            "-d",
            "/data/backups",
            "-n",
            "--keep-daily",
            "14",
            "--keep-weekly",
            "2",
            "--keep-monthly",
            "6",
        ],
    )
    args = prune.parse_args()
    assert args.backup_dir == "/data/backups"
    assert args.dry_run is True
    assert args.keep_daily == 14
    assert args.keep_weekly == 2
    assert args.keep_monthly == 6


def _main_ns(backup_dir: str, dry_run: bool):
    return prune.argparse.Namespace(
        backup_dir=backup_dir, dry_run=dry_run, keep_daily=7, keep_weekly=4, keep_monthly=12
    )


def _write_backup_stream(tmp_path) -> None:
    for ds in DAILY_STREAM:
        (tmp_path / f"modulo-backup-abc123-{ds}T010101.tar.gz.enc").write_text("x")


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


def test_main_missing_dir_exits(monkeypatch, capsys, tmp_path):
    ns = _main_ns(str(tmp_path / "does-not-exist"), dry_run=False)
    with (
        patch.object(prune, "parse_args", return_value=ns),
        pytest.raises(SystemExit) as exc,
    ):
        prune.main()
    assert exc.value.code == 1
    assert "backup directory not found" in capsys.readouterr().out


def test_main_empty_dir_reports_zero(monkeypatch, capsys, tmp_path):
    ns = _main_ns(str(tmp_path), dry_run=False)
    with patch.object(prune, "parse_args", return_value=ns):
        prune.main()
    out = capsys.readouterr().out
    assert "Found 0 backup(s)" in out
    assert "Done." not in out


def test_main_dry_run_flow(monkeypatch, capsys, tmp_path):
    _write_backup_stream(tmp_path)
    ns = _main_ns(str(tmp_path), dry_run=True)

    with (
        patch.object(prune, "parse_args", return_value=ns),
        patch.object(prune, "prune_backups") as mock_prune,
    ):
        prune.main()

    out = capsys.readouterr().out
    assert "Found 10 backup(s)" in out
    assert "Keeping 7, pruning 3" in out
    assert "Dry-run: would prune 3 backup(s)" in out
    assert "Done." in out
    mock_prune.assert_called_once()
    assert mock_prune.call_args.args[0] == str(tmp_path)
    assert mock_prune.call_args.args[2] is True


def test_main_real_delete(monkeypatch, capsys, tmp_path):
    _write_backup_stream(tmp_path)
    ns = _main_ns(str(tmp_path), dry_run=False)

    with patch.object(prune, "parse_args", return_value=ns):
        prune.main()

    remaining = sorted(p.name for p in tmp_path.iterdir())
    assert len(remaining) == 7
    out = capsys.readouterr().out
    assert "Deleted: modulo-backup-abc123-20260702T010101.tar.gz.enc" in out
