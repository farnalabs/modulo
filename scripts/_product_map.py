"""Shared product map parsing helpers.

Used by `run_graph_validate.py` and `run_graph_query.py`. These functions
replace the PowerShell parsing in the retired `tools/product-map-metadata.ps1`.
"""

from __future__ import annotations

import os
import re

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


def iter_product_map_entries(product_map_dir: str):
    """Yield (path, name) for every non-index .md entry under product_map_dir."""
    for root, _dirs, files in os.walk(product_map_dir):
        for name in sorted(files):
            if name == "_index.md" or not name.endswith(".md"):
                continue
            yield os.path.join(root, name), name
