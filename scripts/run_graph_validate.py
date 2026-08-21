#!/usr/bin/env python3
"""Cross-platform pre-commit wrapper for product map graph integrity.

Replaces `tools/graph-validate.ps1` + `tools/product-map-metadata.ps1`. Every
ref resolves, every BDD exists, every PRD ref matches a section, every node has
required fields, plus coverage-orphan / route-orphan / naughty-section-anchor
checks.

Exit code: 0 = clean, 1 = issues found.

Flags (argparse):
  --fix        Regenerate docs/product-map/_index.md from the current entries.
  --ci         Print raw issue lines with no colors (CI-friendly).
  --repo-root  Override the repo root (for fixture-based testing).
"""

from __future__ import annotations

import argparse
import os
import re
import sys

from _product_map import (
    _GROUP_LABELS,
    _split_prd_refs,
    get_prd_sections,
    iter_product_map_entries,
    parse_entry,
)

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PRODUCT_MAP = os.path.join(REPO_ROOT, "docs", "product-map")
PRD_FILE = os.path.join(REPO_ROOT, "docs", "prd.md")
BDD_ROOT = os.path.join(REPO_ROOT, "backend", "tests", "bdd", "features")


def _repo_safe_path(base: str, *parts: str) -> str:
    """Resolve *parts* under *base* and verify the result stays within *base*."""
    resolved = os.path.realpath(os.path.join(base, *parts))
    base_resolved = os.path.realpath(base)
    if resolved != base_resolved and not resolved.startswith(base_resolved + os.sep):
        raise ValueError(f"path {resolved!r} is outside the allowed directory {base!r}")
    return resolved


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check product map graph integrity.")
    parser.add_argument("--fix", action="store_true", help="Regenerate _index.md")
    parser.add_argument("--ci", action="store_true", help="CI-friendly raw output (no colors)")
    parser.add_argument("--repo-root", help="Override the repo root (for fixture-based testing)")
    args = parser.parse_args(argv)

    repo_root = args.repo_root or REPO_ROOT
    product_map = os.path.join(repo_root, "docs", "product-map")

    issues: list[str] = []
    entries: list[dict] = []

    # 1. Validate frontmatter (recursive, matching Get-ChildItem -Recurse)
    for path, name in iter_product_map_entries(product_map):
        entry = parse_entry(path, name)
        issues.extend(entry["issues"])
        entries.append(entry)

    # 2. Validate BDD refs
    for e in entries:
        for b in e["bdd"]:
            if not b:
                continue
            if not os.path.exists(os.path.join(repo_root, b)):
                issues.append(f"BDD|{e['id']}|{b} not found")

    # 3. Validate depends-on
    all_ids = {e["id"]: e["path"] for e in entries if e["id"]}
    for e in entries:
        for d in e["depends"]:
            if not d:
                continue
            if d not in all_ids:
                issues.append(f"REF|{e['id']}|depends-on '{d}' not found in any product map entry")

    # 4. Validate PRD section refs
    with open(_repo_safe_path(repo_root, "docs", "prd.md"), encoding="utf-8") as fh:
        prd_lines = fh.read().splitlines()
    prd_metadata = get_prd_sections(prd_lines)
    prd_sections = prd_metadata["sections"]
    for e in entries:
        refs = _split_prd_refs(e["prd"])
        for r in refs:
            if r not in prd_sections:
                issues.append(f"PRD|{e['id']}|section {r} not found in prd.md")

    # 5. Validate code paths exist
    for e in entries:
        for line in e["code_paths"]:
            if not line.strip():
                continue
            r = os.path.join(repo_root, line.strip())
            if not (
                os.path.exists(r)
                or os.path.exists(r + ".py")
                or os.path.exists(r + ".vue")
                or os.path.exists(r + ".ts")
            ):
                issues.append(f"CODE|{e['id']}|{line} not found")

    # 5b. Validate unit-tests refs exist
    for e in entries:
        for line in e["unit_tests"]:
            if not line.strip():
                continue
            r = os.path.join(repo_root, line.strip())
            if not os.path.exists(r):
                issues.append(f"UTEST|{e['id']}|{line} not found")

    # 6. Fix _index.md
    if args.fix:
        idx = _repo_safe_path(repo_root, "docs", "product-map", "_index.md")
        with open(idx, encoding="utf-8") as fh:
            index_content = fh.read()

        groups: dict[str, list[dict]] = {}
        for e in entries:
            group_name = os.path.basename(os.path.dirname(e["path"]))
            groups.setdefault(group_name, []).append(e)

        new_index = ["## Index", ""]
        for group_name in sorted(groups.keys()):
            label = _GROUP_LABELS.get(group_name, group_name)
            new_index.append(f"### {label}")
            for e in sorted(groups[group_name], key=lambda x: x["id"] or ""):
                rel_path = os.path.relpath(e["path"], product_map).replace(os.sep, "/")
                if e["prd"]:
                    new_index.append(f"- [{e['id']}]({rel_path}) => PRD {e['prd']}")
                else:
                    new_index.append(f"- [{e['id']}]({rel_path})")
            new_index.append("")

        # Replace everything from "## Index" to the next "##" heading.
        head = re.sub(r"(?s)## Index.*", "", index_content)
        tail_match = re.search(r"(?s).*## Index.*?\n##", index_content)
        tail = ""
        if tail_match:
            tail = index_content[tail_match.end() - 2 :]  # keep "##"
        else:
            # No subsequent ## heading; tail after the index is empty.
            m = re.search(r"(?s)## Index.*\Z", index_content)
            if m:
                tail = ""
        new_content = (head.rstrip() + "\n\n" + "\n".join(new_index) + "\n\n" + tail).rstrip()
        with open(idx, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(new_content)
        print("Updated _index.md", file=sys.stderr)

    # 7. Coverage orphans and anchors
    prd_section_names = prd_metadata["names"]

    # A. PRD->Map coverage — spec sections (§6-§12) with zero refs.
    spec_sections: set[str] = set()
    for s in prd_metadata["coverage_sections"]:
        m = re.match(r"^(\d+)", s)
        base = int(m.group(1)) if m else -1
        if 6 <= base <= 12:
            spec_sections.add(s)

    prd_counts: dict[str, int] = dict.fromkeys(spec_sections, 0)
    parent_coverage: set[str] = set()
    for e in entries:
        refs = _split_prd_refs(e["prd"])
        for r in refs:
            if r in prd_counts:
                prd_counts[r] += 1
            m = re.match(r"^(\d+)(\.|$)", r)
            base = m.group(1) if m else r
            if base in spec_sections:
                parent_coverage.add(base)
    for s in sorted(spec_sections):
        if prd_counts[s] == 0:
            m = re.match(r"^(\d+)", s)
            base = m.group(1) if m else s
            if base not in parent_coverage:
                name = f" ({prd_section_names[s]})" if s in prd_section_names else ""
                issues.append(f"COVERAGE|PRD {s}{name} -- 0 product map entries reference this section")

    # B. Route->Map orphan check.
    route_dir = os.path.join(repo_root, "backend", "src", "modulo", "api", "routes")
    if os.path.isdir(route_dir):
        route_files = sorted(name for name in os.listdir(route_dir) if name.endswith(".py") and name != "__init__.py")
        for rf in route_files:
            found = False
            for e in entries:
                for cp in e["code_paths"]:
                    if re.search(re.escape(rf), cp):
                        found = True
                        break
                if found:
                    break
            if not found:
                issues.append(f"ORPHAN|{rf}|route module not referenced by any product map entry")

    # C. Naughty-section check — entries anchored to non-spec sections (§13-§15).
    non_spec_sections: dict[str, str] = {n: prd_section_names.get(n, "") for n in ("13", "14", "15")}
    for e in entries:
        refs = _split_prd_refs(e["prd"])
        for r in refs:
            m = re.match(r"^(\d+)", r)
            base = m.group(1) if m else r
            if base in non_spec_sections:
                base_name = non_spec_sections[base]
                issues.append(
                    f"ANCHOR|{e['id']}|prd {r} is a non-spec section ({base_name}) "
                    "-- use a feature subsection as anchor"
                )

    if not issues:
        if not args.ci:
            print(f"Graph is clean - {len(entries)} entries, all refs resolve.", file=sys.stderr)
        return 0

    if args.ci:
        for issue in issues:
            print(issue)
    else:
        print(f"{len(issues)} issues found:", file=sys.stderr)
        for issue in issues:
            kind, _, rest = issue.partition("|")
            parts = rest.split("|", 1)
            detail = f"{parts[0]} -> {parts[1]}" if len(parts) == 2 else parts[0]
            print(f"  [{kind}] {detail}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
