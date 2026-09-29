"""Evidence append-only carve-out gate (FAR-961 chunk 9a, spec §2.4).

``modulo.core.evidence_retention`` is the SINGLE sanctioned deletion path
for the ``evidence`` table.  This scanner fails the suite when any other
module under ``src/modulo`` deletes evidence rows — via the ORM
(``delete(Evidence)``, ``session.delete(Evidence...)``), via the Table API
(``Evidence.__table__.delete``), or via raw SQL (``DELETE FROM evidence``).

Migrations are deliberately NOT exempt: a future migration that needs to
delete evidence data must update this gate in the same change (a conscious,
reviewed carve-out decision).

The scanner is proven against planted violations in a synthetic tree so a
vacuous pass (a pattern that silently never matches) cannot go unnoticed.
"""

from __future__ import annotations

import re
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = BACKEND_ROOT / "src" / "modulo"

_SANCTIONED_RELPATH = "core/evidence_retention.py"
_ORM_PATTERNS = ("delete(Evidence", "session.delete(Evidence", "Evidence.__table__.delete")
_RAW_SQL_PATTERN = re.compile(r"DELETE\s+FROM\s+evidence\b", re.IGNORECASE)


def _scan_tree(root: Path) -> list[str]:
    """Return one violation per offending file (matched patterns listed)."""
    violations: list[str] = []
    for path in sorted(root.rglob("*.py")):
        rel = path.relative_to(root).as_posix()
        if rel == _SANCTIONED_RELPATH:
            continue
        source = path.read_text(encoding="utf-8")
        matched = [pattern for pattern in _ORM_PATTERNS if pattern in source]
        if _RAW_SQL_PATTERN.search(source):
            matched.append("DELETE FROM evidence (raw SQL)")
        if matched:
            violations.append(f"{rel}: forbidden evidence deletion via {matched}")
    return violations


def _plant_tree(tmp_path: Path, files: dict[str, str]) -> Path:
    for rel, content in files.items():
        target = tmp_path / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    return tmp_path


def test_scanner_flags_orm_deletion_outside_sanctioned_module(tmp_path: Path) -> None:
    root = _plant_tree(
        tmp_path,
        {
            "core/evidence_retention.py": "",
            "api/routes/evidence.py": (
                "from sqlalchemy import delete\nfrom modulo.db.models.evidence import Evidence\n\n\n"
                "def wipe(session):\n    session.execute(delete(Evidence))\n"
            ),
        },
    )

    violations = _scan_tree(root)

    assert len(violations) == 1
    assert "api/routes/evidence.py" in violations[0]
    assert "delete(Evidence" in violations[0]


def test_scanner_flags_raw_sql_deletion_case_insensitively(tmp_path: Path) -> None:
    root = _plant_tree(
        tmp_path,
        {
            "core/evidence_retention.py": "",
            "db/repositories/evidence_repo.py": (
                "def purge(conn):\n    conn.execute('Delete From Evidence WHERE key = :k')\n"
            ),
        },
    )

    violations = _scan_tree(root)

    assert len(violations) == 1
    assert "DELETE FROM evidence (raw SQL)" in violations[0]


def test_scanner_flags_session_delete_and_table_api_patterns(tmp_path: Path) -> None:
    root = _plant_tree(
        tmp_path,
        {
            "core/evidence_retention.py": "",
            "api/a.py": "def wipe(session):\n    session.delete(EvidenceRow)\n",
            "api/b.py": "def truncate():\n    return Evidence.__table__.delete()\n",
        },
    )

    violations = _scan_tree(root)

    assert len(violations) == 2


def test_scanner_clears_the_sanctioned_module(tmp_path: Path) -> None:
    root = _plant_tree(
        tmp_path,
        {
            "core/evidence_retention.py": (
                "async def batch(session, ids):\n"
                "    await session.execute(delete(Evidence).where(Evidence.id.in_(ids)))\n"
            ),
            "api/other.py": "from sqlalchemy import delete\nfrom modulo.db.models.pipeline import Pipeline\n\n\n"
            "def wipe(session):\n    session.execute(delete(Pipeline))\n",
        },
    )

    assert not _scan_tree(root)


def test_real_source_tree_has_no_evidence_deletions_outside_retention() -> None:
    sanctioned_file = SRC_ROOT / "core" / "evidence_retention.py"
    assert sanctioned_file.is_file()

    # The gate is only meaningful while the sanctioned module still owns the
    # deletion path — a silent relocation must fail loudly here.
    assert "delete(Evidence)" in sanctioned_file.read_text(encoding="utf-8")

    violations = _scan_tree(SRC_ROOT)

    assert not violations
