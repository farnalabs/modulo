#!/usr/bin/env python3
"""Prune old Modulo backups based on retention policy.

Retention:
  - Keep 7 most recent daily backups
  - Keep 4 most recent weekly backups (Sundays)
  - Keep 12 most recent monthly backups (1st of month)
  - Everything else is deleted
"""

from __future__ import annotations

import argparse
import glob
import os
import re
import sys
from datetime import date, timedelta
from typing import NamedTuple


BACKUP_RE = re.compile(
    r"modulo-backup-(?P<org>[a-f0-9]+)-(?P<ts>\d{8})T.*\.tar\.gz\.enc$"
)


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
    pattern = os.path.join(backup_dir, "modulo-backup-*.tar.gz.enc")
    for path in glob.glob(pattern):
        basename = os.path.basename(path)
        m = BACKUP_RE.match(basename)
        if m:
            ts = m.group("ts")
            try:
                d = date(int(ts[:4]), int(ts[4:6]), int(ts[6:8]))
            except ValueError:
                continue
            backups.append(BackupFile(path=path, date=d, org=m.group("org")))
    return sorted(backups, key=lambda b: b.date, reverse=True)


def classify_backups(backups: list[BackupFile]) -> set[str]:
    keep: set[str] = set()

    by_org: dict[str, list[BackupFile]] = {}
    for b in backups:
        by_org.setdefault(b.org, []).append(b)

    for org, org_backups in by_org.items():
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
            if monthly_count < 12 and is_first:
                if (year, month) not in seen_months:
                    seen_months.add((year, month))
                    reason = "monthly"
                    monthly_count += 1
            if reason is None and weekly_count < 4 and is_sunday:
                if (iso_year, iso_week) not in seen_weeks:
                    seen_weeks.add((iso_year, iso_week))
                    reason = "weekly"
                    weekly_count += 1
            if reason is None and daily_count < 7:
                reason = "daily"
                daily_count += 1

            if reason:
                keep.add(b.path)
            else:
                pass

    return keep


def prune_backups(backup_dir: str, keep: set[str], dry_run: bool) -> None:
    for entry in os.listdir(backup_dir):
        path = os.path.join(backup_dir, entry)
        if os.path.isfile(path) and BACKUP_RE.match(entry):
            if path not in keep:
                if dry_run:
                    print(f"Would delete: {entry}")
                else:
                    os.unlink(path)
                    print(f"Deleted: {entry}")


def main() -> None:
    args = parse_args()
    if not os.path.isdir(args.backup_dir):
        print(f"ERROR: backup directory not found: {args.backup_dir}")
        sys.exit(1)

    backups = collect_backups(args.backup_dir)
    print(f"Found {len(backups)} backup(s) in {args.backup_dir}")

    if not backups:
        return

    keep = classify_backups(backups)

    kept = sum(1 for b in backups if b.path in keep)
    to_delete = len(backups) - kept
    print(f"Keeping {kept}, pruning {to_delete}")

    if args.dry_run:
        print(f"Dry-run: would prune {to_delete} backup(s)")
    prune_backups(args.backup_dir, keep, args.dry_run)
    print("Done.")


if __name__ == "__main__":
    main()
