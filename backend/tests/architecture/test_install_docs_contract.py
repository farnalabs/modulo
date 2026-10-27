"""Install-path docs contracts (FAR-2183 F4 + F6).

Two install-path defects were "documentation-only" in the sense that the code
already supported the fix, but the documented path never told the operator:

* **F4** - the production compose path created no user, so a healthy stack
  answered ``401`` on login. The docs now name a first-run account command and
  a login verification step; the command is asserted to exist.
* **F6** - the quickstart omitted ``MODULO_SYSTEM_DATABASE_URL``, so
  ``dispatcher_reconcile`` never ran and ``/healthz/ready`` returned ``503``
  forever. The quickstart now sets it and documents the ``"status": "ok"``
  readiness check.

These assertions keep the documented remediation from silently disappearing.
"""

from __future__ import annotations

from pathlib import Path

# tests/architecture/ -> tests/ -> backend/ -> repo root.
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
_DEPLOYMENT_DOC = _REPO_ROOT / "docs" / "deployment.md"
_QUICKSTART_DOC = _REPO_ROOT / "docs" / "quickstart.md"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# F6 - the quickstart must configure the system database URL
# ---------------------------------------------------------------------------


def test_quickstart_sets_the_system_database_url() -> None:
    text = _read(_QUICKSTART_DOC)
    assert "MODULO_SYSTEM_DATABASE_URL=postgresql+asyncpg://modulo_system:" in text


def test_quickstart_documents_the_readiness_ok_check() -> None:
    text = _read(_QUICKSTART_DOC)
    assert "/healthz/ready" in text
    assert '"status": "ok"' in text
    assert "dispatcher_reconcile" in text


# ---------------------------------------------------------------------------
# F4 - the production path must create a login and verify it
# ---------------------------------------------------------------------------


def test_production_docs_document_a_first_run_user() -> None:
    text = _read(_DEPLOYMENT_DOC)
    assert "modulo users add" in text
    assert "MODULO_USERS=admin@example.com" in text


def test_production_docs_document_a_login_verification() -> None:
    text = _read(_DEPLOYMENT_DOC)
    assert "/api/v1/auth/login" in text
    assert "401" in text


def test_production_docs_document_the_readiness_requirement() -> None:
    text = _read(_DEPLOYMENT_DOC)
    assert "MODULO_SYSTEM_DATABASE_URL" in text
    assert "dispatcher_reconcile" in text


def test_documented_first_run_command_exists() -> None:
    """The docs name a command that must actually exist on the ``modulo`` CLI."""
    from click.testing import CliRunner

    from modulo.cli.users import users

    assert "add" in users.commands
    result = CliRunner().invoke(users, ["add", "--help"])
    assert result.exit_code == 0
    assert "--admin" in result.output
