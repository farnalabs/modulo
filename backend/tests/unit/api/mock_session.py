"""Helpers for contract-correct SQLAlchemy async session test doubles."""

from typing import Any
from unittest.mock import DEFAULT, AsyncMock, MagicMock

from sqlalchemy.sql import Select

# require_permission's per-request kill-switch read (ADR 017 DECISION 3). The
# strict mock raises on un-stubbed queries, so this SELECT on the organisations
# authz_enforce column is stubbed by default to the enforce=True default.
_AUTHZ_ENFORCE_SNIPPET = "authz_enforce"

# FAR-223 PR A: the graph-save route loads the pipeline's guardrail eval rows
# (select(EvalDefinition).where(pipeline_id=..., organisation_id=...,
# eval_type="guardrail")) to enforce the per-node guardrail cap at authoring
# time. The strict mock raises on un-stubbed queries, so this SELECT on the
# eval_definitions table is stubbed by default to no rows — no guardrail rows
# means no cap violation (no 422).
_GUARDRAIL_ROWS_SNIPPET = "FROM eval_definitions"

# FAR-526 Part A: the context-bound decrypt helper (decode_stored_secret_scoped)
# (re-)applies the RLS org via set_rls_org, which issues a
# ``SELECT set_config('app.organisation_id', ...)`` inside the caller's active
# transaction. Routing secrets through the scoped helper is the new normal, so
# the strict mock treats RLS set_config as a benign no-op (an empty result) —
# the session is already scoped by the test, and the config write is a no-return
# SET-LOCAL equivalent.
_RLS_SET_CONFIG_SNIPPET = "set_config"


def _is_authz_enforce_query(stmt: Any) -> bool:
    if not isinstance(stmt, Select):
        return False
    return _AUTHZ_ENFORCE_SNIPPET in str(stmt)


def _is_guardrail_rows_query(stmt: Any) -> bool:
    if not isinstance(stmt, Select):
        return False
    return _GUARDRAIL_ROWS_SNIPPET in str(stmt)


def _is_rls_set_config_query(stmt: Any) -> bool:
    # set_config is issued via sqlalchemy text(), not select() — match the SQL text.
    return _RLS_SET_CONFIG_SNIPPET in str(stmt)


def configure_mock_session(session: AsyncMock, *, allow_empty_execute: bool = False) -> AsyncMock:
    """Configure AsyncSession contracts, requiring explicit query results by default."""
    bind = MagicMock()
    bind.dialect.name = "sqlite"
    session.in_transaction = MagicMock(return_value=True)
    session.get_bind = MagicMock(return_value=bind)
    session.add = MagicMock()
    session.add_all = MagicMock()
    session.expunge = MagicMock()
    session.info = {}
    nested = MagicMock()
    nested.__aenter__ = AsyncMock(return_value=None)
    nested.__aexit__ = AsyncMock(return_value=False)
    session.begin_nested = MagicMock(return_value=nested)
    if allow_empty_execute:
        result = MagicMock()
        result.scalar.return_value = 0
        result.scalar_one.return_value = 0
        result.scalar_one_or_none.return_value = None
        result.first.return_value = None
        result.all.return_value = []
        result.scalars.return_value.all.return_value = []
        session.execute = AsyncMock(return_value=result)
    else:
        execute = AsyncMock()

        def require_explicit_result(*args: Any, **kwargs: Any) -> Any:
            if execute._mock_return_value is not DEFAULT:
                return execute._mock_return_value
            if _is_authz_enforce_query(args[0] if args else None):
                authz_result = MagicMock()
                authz_result.scalar_one_or_none.return_value = None
                return authz_result
            if _is_guardrail_rows_query(args[0] if args else None):
                guardrail_result = MagicMock()
                guardrail_result.scalars.return_value.all.return_value = []
                return guardrail_result
            if _is_rls_set_config_query(args[0] if args else None):
                rls_result = MagicMock()
                rls_result.scalar.return_value = None
                return rls_result
            raise AssertionError(
                "Unexpected session.execute(); stub the expected result or opt in with allow_empty_execute=True"
            )

        execute.side_effect = require_explicit_result
        session.execute = execute
    return session
