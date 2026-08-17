#!/usr/bin/env python3
"""Cross-platform pre-commit wrapper for product map graph integrity.

Replaces `tools/graph-validate.ps1` + `tools/product-map-metadata.ps1`. Every
ref resolves, every BDD exists, every PRD ref matches a section, every node has
required fields, plus coverage-orphan / route-orphan / naughty-section-anchor
checks.

Exit code: 0 = clean, 1 = issues found.

Flags (argparse):
  --fix  Regenerate docs/product-map/_index.md from the current entries.
  --ci   Print raw issue lines with no colors (CI-friendly).
"""

from __future__ import annotations

import argparse
import os
import re
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PRODUCT_MAP = os.path.join(REPO_ROOT, "docs", "product-map")
PRD_FILE = os.path.join(REPO_ROOT, "docs", "prd.md")
BDD_ROOT = os.path.join(REPO_ROOT, "backend", "tests", "bdd", "features")

# Group label map used by --fix when regenerating _index.md.
_GROUP_LABELS = {
    "core": "Core Platform",
    "auth": "Auth and Security",
    "teams": "Teams",
    "evals": "Evals and Feedback",
    "connectors": "Connectors",
    "pipelines": "Pipelines",
    "frontend": "Frontend",
    "observability": "Observability",
    "infra": "Infrastructure",
    "model-backends": "Model Backends",
    "variants": "Run Variants",
}

_RE_FRONTMATTER = re.compile(r"(?s)^---[\r\n]+(.+?)[\r\n]+---")
_RE_CONFLICT = re.compile(r"<<<<<<<|=======|>>>>>>>")
_RE_ID = re.compile(r"(?m)^id:\s*(\S+)")
_RE_BDD_INLINE = re.compile(r"(?m)^bdd:\s*(.+?)[\r\n]")
_RE_BDD_BLOCK = re.compile(r"(?m)^bdd:\s*\n((?:\s+- .+\n?)+)")
_RE_DEPENDS_INLINE = re.compile(r"(?m)^depends-on:\s*\[(.*?)\]")
_RE_DEPENDS_BLOCK = re.compile(r"(?m)^depends-on:\s*\n((?:\s+- .+\n?)+)")
_RE_CODE_INLINE = re.compile(r"(?m)^code:\s*\[(.*?)\]")
_RE_CODE_BLOCK = re.compile(r"(?m)^code:\s*\n((?:\s+- .+\n?)+)")
_RE_UNIT_INLINE = re.compile(r"(?m)^unit-tests:\s*\[(.*?)\]")
_RE_UNIT_BLOCK = re.compile(r"(?m)^unit-tests:\s*\n((?:\s+- .+\n?)+)")
_RE_STATUS = re.compile(r"(?m)^status:\s*(covered|partial|gap)")
_RE_PRD_LINE = re.compile(r"^prd:\s*(.*)$")
_RE_PRD_LIST_ITEM = re.compile(r"^\s+-\s+(.+?)\s*$")
_RE_NEW_KEY = re.compile(r"^[A-Za-z][A-Za-z0-9-]*:")
_RE_SECTION_HEADING = re.compile(r"^(#{2,6})\s+(\d+(?:\.\d+)*[a-z]?)(?:\.)?(?:\s+(.+))?$")


def get_prd_references(frontmatter: str) -> list[str]:
    """Extract the `prd:` references from frontmatter (inline and block list)."""
    references: list[str] = []
    in_prd_list = False
    for line in frontmatter.splitlines():
        m = _RE_PRD_LINE.match(line)
        if m:
            in_prd_list = True
            value = m.group(1).strip()
            if value:
                references.extend(_split_quoted(value))
                in_prd_list = False
            continue
        if in_prd_list:
            m = _RE_PRD_LIST_ITEM.match(line)
            if m:
                references.append(m.group(1).strip().strip('"').strip("'"))
                continue
            if _RE_NEW_KEY.match(line):
                break
    return [r for r in references if r]


def _split_quoted(value: str) -> list[str]:
    return [part.strip().strip('"').strip("'") for part in value.split(",")]


def _block_lines(block: str, *, strip_hash: bool = True) -> list[str]:
    result = []
    for raw in block.split("\n"):
        item = raw
        item = re.sub(r"^\s*-\s*", "", item)
        item = item.replace('"', "").replace("'", "")
        if strip_hash:
            item = re.sub(r"#.*", "", item)
        item = item.strip()
        if item:
            result.append(item)
    return result


def parse_entry(path: str, name: str) -> dict:
    """Parse a product map entry's frontmatter into a structured dict."""
    with open(path, encoding="utf-8") as fh:
        content = fh.read()

    issues: list[str] = []
    if not _RE_FRONTMATTER.search(content):
        issues.append(f"FILE|{name}|missing frontmatter")
        return {"issues": issues}
    if _RE_CONFLICT.search(content):
        issues.append(f"CONFLICT|{name}|file contains unresolved merge conflict markers")

    fm = _RE_FRONTMATTER.search(content).group(1)

    id_match = _RE_ID.search(fm)
    entry_id = id_match.group(1) if id_match else None

    # bdd
    bdd: list[str] = []
    m = _RE_BDD_INLINE.search(fm)
    if m:
        b_list = m.group(1).strip()
        if b_list.startswith("["):
            bdd = [
                x for x in b_list.replace("[", "").replace("]", "").replace('"', "").replace(" ", "").split(",") if x
            ]
    m = _RE_BDD_BLOCK.search(fm)
    if m:
        bdd.extend(x for x in _block_lines(m.group(1)) if x)
    bdd = list(dict.fromkeys(x for x in bdd if x))

    # depends-on
    dep: list[str] = []
    m = _RE_DEPENDS_INLINE.search(fm)
    if m:
        dep = [x for x in m.group(1).replace(" ", "").split(",") if x]
    m = _RE_DEPENDS_BLOCK.search(fm)
    if m:
        dep.extend(x for x in _block_lines(m.group(1)) if x)
    dep = list(dict.fromkeys(x for x in dep if x))

    prd_refs = get_prd_references(fm)
    prd = ", ".join(prd_refs) if prd_refs else None

    # code paths
    code_paths: list[str] = []
    m = _RE_CODE_INLINE.search(fm)
    if m:
        code_paths = [x for x in m.group(1).replace(" ", "").split(",") if x]
    m = _RE_CODE_BLOCK.search(fm)
    if m:
        lines = [re.sub(r"^\s*-\s*", "", x).replace('"', "").strip() for x in m.group(1).split("\n")]
        code_paths = list(dict.fromkeys([x for x in code_paths + lines if x]))

    # unit-tests
    unit_tests: list[str] = []
    m = _RE_UNIT_INLINE.search(fm)
    if m:
        unit_tests = [re.sub(r"\s+\(.*\)\s*$", "", x).replace(" ", "").replace('"', "") for x in m.group(1).split(",")]
        unit_tests = [x for x in unit_tests if x]
    m = _RE_UNIT_BLOCK.search(fm)
    if m:
        ut_lines = _block_lines(m.group(1), strip_hash=False)
        ut_lines = [re.sub(r"\s+\(.*\)\s*$", "", x) for x in ut_lines]
        unit_tests = list(dict.fromkeys([x for x in unit_tests + ut_lines if x]))

    if not entry_id:
        issues.append(f"NODE|{name}|missing id field")
    if not prd:
        issues.append(f"NODE|{name}|missing prd field")
    if not _RE_STATUS.search(fm):
        issues.append(f"NODE|{name}|missing or invalid status")

    return {
        "issues": issues,
        "id": entry_id,
        "prd": prd,
        "bdd": bdd,
        "depends": dep,
        "code_paths": code_paths,
        "unit_tests": unit_tests,
        "path": path,
        "name": name,
    }


def get_prd_sections(lines: list[str]) -> dict:
    """Return {sections: set, coverage_sections: set, names: dict}."""
    sections: set[str] = set()
    coverage_sections: set[str] = set()
    names: dict[str, str] = {}
    for line in lines:
        m = _RE_SECTION_HEADING.match(line)
        if m:
            level = len(m.group(1))
            section = m.group(2)
            sections.add(section)
            if m.group(3):
                names[section] = m.group(3).strip()
            if level <= 3:
                coverage_sections.add(section)
    return {"sections": sections, "coverage_sections": coverage_sections, "names": names}


def _split_prd_refs(prd: str | None) -> list[str]:
    if not prd or re.match(r"(?i)^N/A$", prd):
        return []
    return [r.strip().lstrip("§") for r in prd.split(",")]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check product map graph integrity.")
    parser.add_argument("--fix", action="store_true", help="Regenerate _index.md")
    parser.add_argument("--ci", action="store_true", help="CI-friendly raw output (no colors)")
    args = parser.parse_args(argv)

    issues: list[str] = []
    entries: list[dict] = []

    # 1. Validate frontmatter (recursive, matching Get-ChildItem -Recurse)
    for root, _dirs, files in os.walk(PRODUCT_MAP):
        for name in sorted(files):
            if name == "_index.md" or not name.endswith(".md"):
                continue
            path = os.path.join(root, name)
            entry = parse_entry(path, name)
            issues.extend(entry["issues"])
            entries.append(entry)

    # 2. Validate BDD refs
    for e in entries:
        for b in e["bdd"]:
            if not b:
                continue
            if not os.path.exists(os.path.join(REPO_ROOT, b)):
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
    with open(PRD_FILE, encoding="utf-8") as fh:
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
            r = os.path.join(REPO_ROOT, line.strip())
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
            r = os.path.join(REPO_ROOT, line.strip())
            if not os.path.exists(r):
                issues.append(f"UTEST|{e['id']}|{line} not found")

    # 6. Fix _index.md
    if args.fix:
        idx = os.path.join(PRODUCT_MAP, "_index.md")
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
                rel_path = os.path.relpath(e["path"], PRODUCT_MAP).replace(os.sep, "/")
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
    route_dir = os.path.join(REPO_ROOT, "backend", "src", "modulo", "api", "routes")
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
