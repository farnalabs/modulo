"""FAR-1385 — the audit list/export filter by acting principal kind.

The filter reads the ``principal_kind`` key the audit wrapper stamps into the
event payload (ADR 058), through SQLAlchemy's portable JSON index access, so it
works on PostgreSQL (``->>``) and SQLite (``json_extract``) alike.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.dialects import postgresql, sqlite

from modulo.core.audit_logger import _apply_filters
from modulo.db.models.audit_event import AuditEvent

_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")


def _compiled(principal_kind: str | None, dialect: object) -> tuple[str, set[object]]:
    query = _apply_filters(select(AuditEvent), _ORG_ID, principal_kind=principal_kind)
    compiled = query.compile(dialect=dialect)  # type: ignore[arg-type]
    return str(compiled), set(compiled.params.values())


def test_no_filter_omits_the_principal_kind_predicate() -> None:
    sql, params = _compiled(None, postgresql.dialect())
    assert "->>" not in sql
    assert "json_extract" not in sql.lower()
    assert "principal_kind" not in params


def test_postgres_filter_uses_json_arrow_and_binds_the_key() -> None:
    sql, params = _compiled("service", postgresql.dialect())
    assert "->>" in sql
    assert "principal_kind" in params
    assert "service" in params


def test_sqlite_filter_uses_json_extract_and_binds_the_key() -> None:
    sql, params = _compiled("human", sqlite.dialect())
    assert "json_extract" in sql.lower()
    assert "principal_kind" in params
    assert "human" in params
