#!/usr/bin/env python3
"""Prune old Modulo backups based on retention policy.  # noqa: N999

Retention:
  - Keep 7 most recent daily backups
  - Keep 4 most recent weekly backups (Sundays)
  - Keep 12 most recent monthly backups (1st of month)
  - Everything else is deleted
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from datetime import date
from pathlib import Path
from typing import NamedTuple

# ruff: noqa: N999

BACKUP_RE = re.compile(r"modulo-backup-(?P<org>[a-f0-9]+)-(?P<ts>\d{8})T.*\.tar\.gz\.enc$")


def _safe_dir(path: str) -> str:
    """Resolve *path* and require it to be an existing directory."""
    resolved = os.path.realpath(path)
    if not Path(resolved).is_dir():
        raise ValueError(f"backup directory is not a real directory: {path!r}")
    return resolved


def _path_within(base: str, path: str) -> str:
    """Resolve *path* and require it to stay within *base*."""
    base_resolved = os.path.realpath(base)
    resolved = os.path.realpath(path)
    if resolved != base_resolved and not resolved.startswith(base_resolved + os.sep):
        raise ValueError(f"path {resolved!r} is outside the allowed directory {base!r}")
    return resolved


class BackupFile(NamedTuple):
    path: str
    date: date
    org: str


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Prune old Modulo backups")
    parser.add_argument("--backup-dir", "-d", default=".", help="Backup directory")
    parser.add_argument("--dry-run", "-n", action="store_true", help="Show what would be deleted")
    parser.add_argument("--keep-daily", type=int, default=7, help="Daily backups to keep")
    parser.add_argument("--keep-weekly", type=int, default=4, help="Weekly backups to keep")
    parser.add_argument("--keep-monthly", type=int, default=12, help="Monthly backups to keep")
    return parser.parse_args()


def collect_backups(backup_dir: str) -> list[BackupFile]:
    backups: list[BackupFile] = []
    safe_dir = _safe_dir(backup_dir)
    for path in Path(safe_dir).glob("modulo-backup-*.tar.gz.enc"):
        basename = Path(path).name
        m = BACKUP_RE.match(basename)
        if m:
            ts = m.group("ts")
            try:
                d = date(int(ts[:4]), int(ts[4:6]), int(ts[6:8]))
            except ValueError:
                continue
            backups.append(BackupFile(path=str(path), date=d, org=m.group("org")))
    return sorted(backups, key=lambda b: b.date, reverse=True)


def classify_backups(
    backups: list[BackupFile],
    *,
    keep_daily: int = 7,
    keep_weekly: int = 4,
    keep_monthly: int = 12,
) -> set[str]:
    keep: set[str] = set()

    by_org: dict[str, list[BackupFile]] = {}
    for b in backups:
        by_org.setdefault(b.org, []).append(b)

    for org_backups in by_org.values():
        sorted_backups = sorted(org_backups, key=lambda x: x.date, reverse=True)

        daily_count = 0
        weekly_count = 0
        monthly_count = 0
        seen_weeks: set[tuple[int, int]] = set()
        seen_months: set[tuple[int, int]] = set()

        for b in sorted_backups:
            year, month, day = b.date.year, b.date.month, b.date.day
            iso_year, iso_week, iso_weekday = b.date.isocalendar()
            is_sunday = iso_weekday == 7
            is_first = day == 1

            reason = None
            if monthly_count < keep_monthly and is_first and (year, month) not in seen_months:
                seen_months.add((year, month))
                reason = "monthly"
                monthly_count += 1
            if reason is None and weekly_count < keep_weekly and is_sunday and (iso_year, iso_week) not in seen_weeks:
                seen_weeks.add((iso_year, iso_week))
                reason = "weekly"
                weekly_count += 1
            if reason is None and daily_count < keep_daily:
                reason = "daily"
                daily_count += 1

            if reason:
                keep.add(b.path)

    return keep


def prune_backups(backup_dir: str, keep: set[str], dry_run: bool) -> None:
    safe_dir = _safe_dir(backup_dir)
    for entry in Path(safe_dir).iterdir():
        path = _path_within(safe_dir, os.fspath(entry))
        if entry.is_file() and BACKUP_RE.match(entry.name) and path not in keep:
            if dry_run:
                print(f"Would delete: {entry.name}")
            else:
                entry.unlink()
                print(f"Deleted: {entry.name}")


def main() -> None:
    args = parse_args()
    if not Path(args.backup_dir).is_dir():
        print(f"ERROR: backup directory not found: {args.backup_dir}")
        sys.exit(1)

    backups = collect_backups(args.backup_dir)
    print(f"Found {len(backups)} backup(s) in {args.backup_dir}")

    if not backups:
        return

    keep = classify_backups(
        backups,
        keep_daily=args.keep_daily,
        keep_weekly=args.keep_weekly,
        keep_monthly=args.keep_monthly,
    )

    kept = sum(1 for b in backups if b.path in keep)
    to_delete = len(backups) - kept
    print(f"Keeping {kept}, pruning {to_delete}")

    if args.dry_run:
        print(f"Dry-run: would prune {to_delete} backup(s)")
    prune_backups(args.backup_dir, keep, args.dry_run)
    print("Done.")


if __name__ == "__main__":
    main()
