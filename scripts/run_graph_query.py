#!/usr/bin/env python3
"""Cross-platform product map graph queries.

Replaces `tools/graph-query.ps1`. Queries the product map graph:
  --uncovered             list entries that need attention (empty/missing bdd)
  --impact feat-<id>      list entries that depend on <id> (downstream dependents)
  --depends feat-<id>     list entries that <id> depends on (upstream prereqs)

Exit code: 0 = matches found / repo clean, 1 = nothing matched (for
--impact/--depends when the id is unknown or has no matching entries; for
--uncovered when entries need attention).

Flags (argparse):
  --repo-root  Override the repo root (for fixture-based testing).
"""

from __future__ import annotations

import argparse
import os
import sys

from _product_map import iter_product_map_entries, parse_entry

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load_entries(product_map_dir: str) -> list[dict]:
    entries = []
    for path, name in iter_product_map_entries(product_map_dir):
        entry = parse_entry(path, name)
        entries.append(entry)
    return entries


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Query the product map graph.")
    parser.add_argument("--uncovered", action="store_true", help="List entries with empty/missing bdd")
    parser.add_argument("--impact", metavar="ID", help="List downstream dependents of <id>")
    parser.add_argument("--depends", metavar="ID", help="List upstream prerequisites of <id>")
    parser.add_argument("--repo-root", help="Override the repo root (for fixture-based testing)")
    args = parser.parse_args(argv)

    repo_root = args.repo_root or REPO_ROOT
    product_map = os.path.join(repo_root, "docs", "product-map")

    entries = _load_entries(product_map)
    by_id = {e["id"]: e for e in entries if e["id"]}

    if args.uncovered:
        print("Entries needing attention (empty or missing bdd coverage):")
        uncovered = sorted((e for e in entries if not e["id"] or not e["bdd"]), key=lambda e: e["name"] or "")
        if not uncovered:
            print("  None - every entry has bdd coverage.")
            return 0
        for e in uncovered:
            print(f"  {e['name']} ({e['id']})")
        print(f"{len(uncovered)} entry(ies) need attention.")
        return 1

    if args.impact and args.depends:
        print("Usage: pass only one of -Impact or -Depends.")
        return 1

    if args.impact or args.depends:
        if args.impact:
            target = args.impact
            print(f"Downstream dependents of {target}:")
            dependents = sorted((e for e in entries if target in e["depends"]), key=lambda e: e["id"] or "")
            if not dependents:
                if target not in by_id:
                    print(f"  '{target}' is not a known product map id.")
                else:
                    print("  None.")
                return 1
            for e in dependents:
                print(f"  {e['id']}")
            return 0
        if args.depends:
            target = args.depends
            print(f"Upstream dependencies of {target}:")
            entry = by_id.get(target)
            if not entry:
                print(f"  '{target}' is not a known product map id.")
                return 1
            if not entry["depends"]:
                print("  None.")
                return 1
            for d in entry["depends"]:
                print(f"  {d}")
            return 0

    parser.print_usage()
    return 1


if __name__ == "__main__":
    sys.exit(main())
