"""Router-level unit tests for the evidence surface (FAR-944, §2.5).

The evidence routes are exercised in isolation with a mocked session and the
module-level DB helpers patched, the same way the rest of this package stubs
them. The redaction *logic* is covered in
``tests/unit/core/evidence/test_evidence_redaction.py``; these tests pin the
ROUTE-level contract: redaction is applied before serialisation per the
caller's role, permitted fields survive, the admin rules surface is
admin-gated, and the error arms map to the documented status codes.
"""

from __future__ import annotations

import types
import uuid
from collections.abc import AsyncGenerator, Generator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError, ProgrammingError, SQLAlchemyError

from modulo.api.dependencies import _get_engine, get_db_session, get_plan_context
from modulo.api.main import app
from modulo.api.routes import evidence as evidence_routes
from modulo.auth.dependencies import get_current_tenant_user, get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal, TenantPrincipal
from modulo.core.evidence_redaction import REDACTED_SENTINEL
from modulo.settings import Settings, get_settings
from tests.unit.api.mock_session import configure_mock_session

_VALID_32 = "a" * 32
_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")
_RUN_ID = uuid.UUID("00000000-0000-0000-0000-0000000000aa")
_EVIDENCE_ID = uuid.UUID("00000000-0000-0000-0000-0000000000bb")
_NOW = datetime(2026, 1, 1, tzinfo=UTC)

_ROUTES = "modulo.api.routes.evidence"


def _make_settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key=_VALID_32,
        fernet_key=_VALID_32,
        modulo_admin_password="testpass",
    )


def _make_mock_session() -> AsyncMock:
    session = configure_mock_session(AsyncMock())
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    return session


def _fake_evidence(**overrides: Any) -> MagicMock:
    row = MagicMock()
    row.id = overrides.get("id", _EVIDENCE_ID)
    row.organisation_id = overrides.get("organisation_id", _ORG_ID)
    row.key = overrides.get("key", "eval_guardrail_abc")
    row.subject_type = overrides.get("subject_type", "run")
    row.subject_id = overrides.get("subject_id", str(_RUN_ID))
    row.value = overrides.get("value", {"raw_output": "secret", "status": "complete"})
    row.observed_at = overrides.get("observed_at", _NOW)
    row.producer_type = overrides.get("producer_type", "eval")
    row.producer_id = overrides.get("producer_id")
    row.created_at = overrides.get("created_at", _NOW)
    row.updated_at = overrides.get("updated_at", _NOW)
    return row


def _fake_rule(**overrides: Any) -> MagicMock:
    rule = MagicMock()
    rule.id = overrides.get("id", _EVIDENCE_ID)
    rule.key_pattern = overrides.get("key_pattern", "llm_judge.*")
    rule.role = overrides.get("role", "viewer")
    rule.redacted_fields = overrides.get("redacted_fields", ["sensitive_output"])
    rule.created_at = overrides.get("created_at", _NOW)
    rule.updated_at = overrides.get("updated_at", _NOW)
    return rule


@contextmanager
def _client(org_role: str) -> Generator[TestClient, None, None]:
    mock_session = _make_mock_session()

    async def override_session() -> AsyncGenerator[AsyncMock, None]:
        yield mock_session

    app.dependency_overrides[get_settings] = _make_settings
    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[_get_engine] = lambda: MagicMock()
    app.dependency_overrides[get_current_user] = lambda: AuthenticatedPrincipal(
        username="tester",
        organisation_id=_ORG_ID,
        account_id=_USER_ID,
        org_role=org_role,
    )
    app.dependency_overrides[get_current_tenant_user] = lambda: TenantPrincipal(
        username="tester",
        organisation_id=_ORG_ID,
        account_id=_USER_ID,
        org_role=org_role,
    )
    mock_plan = MagicMock()
    mock_plan.feature_enabled.return_value = True
    app.dependency_overrides[get_plan_context] = lambda: mock_plan
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


def _viewer_catch_all_rule() -> MagicMock:
    """A stored viewer catch-all rule redacting ``sensitive_output``."""
    return _fake_rule(key_pattern="*", role="viewer", redacted_fields=["sensitive_output"])


_LIST_URL = f"/api/v1/evidence?run_id={_RUN_ID}"


class TestListEvidenceRedaction:
    def test_list_redacts_sensitive_fields_for_viewer(self) -> None:
        row = _fake_evidence()
        with (
            _client("viewer") as client,
            patch(f"{_ROUTES}._load_evidence_rows", new=AsyncMock(return_value=[row])),
            patch(f"{_ROUTES}.set_rls_org", new=AsyncMock()),
            patch(f"{_ROUTES}.set_rls_user_context", new=AsyncMock()),
            patch(f"{_ROUTES}.load_redaction_rules", new=AsyncMock(return_value=[_viewer_catch_all_rule()])),
        ):
            resp = client.get(_LIST_URL)

        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        assert data[0]["value"]["raw_output"] == REDACTED_SENTINEL
        # Permitted fields survive alongside the sentinel (no over-redaction).
        assert data[0]["value"]["status"] == "complete"
        assert data[0]["key"] == "eval_guardrail_abc"
        assert data[0]["subject_id"] == str(_RUN_ID)

    def test_list_does_not_redact_for_admin(self) -> None:
        row = _fake_evidence()
        with (
            _client("admin") as client,
            patch(f"{_ROUTES}._load_evidence_rows", new=AsyncMock(return_value=[row])),
            patch(f"{_ROUTES}.set_rls_org", new=AsyncMock()),
            patch(f"{_ROUTES}.set_rls_user_context", new=AsyncMock()),
            patch(f"{_ROUTES}.load_redaction_rules", new=AsyncMock(return_value=[])),
        ):
            resp = client.get(_LIST_URL)

        assert resp.status_code == 200
        data = resp.json()
        assert data[0]["value"]["raw_output"] == "secret"

    def test_list_redacts_for_runner_when_no_rule_targets_it(self) -> None:
        # Regression (FAR-944 review): runner is the default org role and a
        # valid evidence.read principal. With no rule for runner it must still
        # fail CLOSED and redact both classes.
        row = _fake_evidence()
        with (
            _client("runner") as client,
            patch(f"{_ROUTES}._load_evidence_rows", new=AsyncMock(return_value=[row])),
            patch(f"{_ROUTES}.set_rls_org", new=AsyncMock()),
            patch(f"{_ROUTES}.set_rls_user_context", new=AsyncMock()),
            patch(f"{_ROUTES}.load_redaction_rules", new=AsyncMock(return_value=[])),
        ):
            resp = client.get(_LIST_URL)

        assert resp.status_code == 200
        data = resp.json()
        assert data[0]["value"]["raw_output"] == REDACTED_SENTINEL
        assert data[0]["value"]["status"] == "complete"

    def test_list_empty_returns_no_rows(self) -> None:
        with (
            _client("viewer") as client,
            patch(f"{_ROUTES}._load_evidence_rows", new=AsyncMock(return_value=[])),
            patch(f"{_ROUTES}.set_rls_org", new=AsyncMock()),
            patch(f"{_ROUTES}.set_rls_user_context", new=AsyncMock()),
            patch(f"{_ROUTES}.load_redaction_rules", new=AsyncMock(return_value=[])),
        ):
            resp = client.get(_LIST_URL)

        assert resp.status_code == 200
        assert not resp.json()


class TestListEvidenceErrorArms:
    def _get(self, side_effect: Exception) -> Any:
        with (
            _client("viewer") as client,
            patch(f"{_ROUTES}._load_evidence_rows", new=AsyncMock(side_effect=side_effect)),
            patch(f"{_ROUTES}.set_rls_org", new=AsyncMock()),
            patch(f"{_ROUTES}.set_rls_user_context", new=AsyncMock()),
            patch(f"{_ROUTES}.load_redaction_rules", new=AsyncMock(return_value=[])),
        ):
            return client.get(_LIST_URL)

    def test_integrity_error_returns_409(self) -> None:
        resp = self._get(IntegrityError("stmt", "params", "orig"))
        assert resp.status_code == 409

    def test_programming_error_returns_501(self) -> None:
        resp = self._get(ProgrammingError("stmt", "params", "orig"))
        assert resp.status_code == 501

    def test_sqlalchemy_error_returns_503(self) -> None:
        resp = self._get(SQLAlchemyError("db down"))
        assert resp.status_code == 503

    def test_unexpected_error_returns_500(self) -> None:
        resp = self._get(RuntimeError("boom"))
        assert resp.status_code == 500


class TestGetEvidence:
    URL = f"/api/v1/evidence/{_EVIDENCE_ID}"

    def test_get_redacts_for_viewer(self) -> None:
        row = _fake_evidence()
        with (
            _client("viewer") as client,
            patch(f"{_ROUTES}._load_evidence_row", new=AsyncMock(return_value=row)),
            patch(f"{_ROUTES}.set_rls_org", new=AsyncMock()),
            patch(f"{_ROUTES}.set_rls_user_context", new=AsyncMock()),
            patch(f"{_ROUTES}.load_redaction_rules", new=AsyncMock(return_value=[_viewer_catch_all_rule()])),
        ):
            resp = client.get(self.URL)

        assert resp.status_code == 200
        assert resp.json()["value"]["raw_output"] == REDACTED_SENTINEL

    def test_get_missing_row_returns_404(self) -> None:
        with (
            _client("viewer") as client,
            patch(f"{_ROUTES}._load_evidence_row", new=AsyncMock(return_value=None)),
            patch(f"{_ROUTES}.set_rls_org", new=AsyncMock()),
            patch(f"{_ROUTES}.set_rls_user_context", new=AsyncMock()),
        ):
            resp = client.get(self.URL)

        assert resp.status_code == 404


class TestRedactionRulesAdminSurface:
    RULES_URL = "/api/v1/evidence/redaction-rules"

    def test_list_rules_returns_org_rules_for_admin(self) -> None:
        rule = _fake_rule()
        with (
            _client("admin") as client,
            patch(f"{_ROUTES}._load_redaction_rules", new=AsyncMock(return_value=[rule])),
            patch(f"{_ROUTES}.set_rls_org", new=AsyncMock()),
            patch(f"{_ROUTES}.set_rls_user_context", new=AsyncMock()),
        ):
            resp = client.get(self.RULES_URL)

        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        assert data[0]["key_pattern"] == "llm_judge.*"
        assert data[0]["role"] == "viewer"

    def test_list_rules_forbidden_for_viewer(self) -> None:
        with _client("viewer") as client:
            resp = client.get(self.RULES_URL)

        assert resp.status_code == 403

    def test_upsert_rule_returns_stored_rule_for_admin(self) -> None:
        stored = _fake_rule(key_pattern="risk.*", role="operator", redacted_fields=["internal_metadata"])
        with (
            _client("admin") as client,
            patch(f"{_ROUTES}._upsert_redaction_rule", new=AsyncMock(return_value=stored)),
            patch(f"{_ROUTES}.set_rls_org", new=AsyncMock()),
            patch(f"{_ROUTES}.set_rls_user_context", new=AsyncMock()),
        ):
            resp = client.put(
                self.RULES_URL,
                json={"key_pattern": "risk.*", "role": "operator", "redacted_fields": ["internal_metadata"]},
            )

        assert resp.status_code == 200
        assert resp.json()["key_pattern"] == "risk.*"
        assert resp.json()["role"] == "operator"

    def test_upsert_rule_forbidden_for_viewer(self) -> None:
        with _client("viewer") as client:
            resp = client.put(
                self.RULES_URL,
                json={"key_pattern": "risk.*", "role": "viewer", "redacted_fields": ["sensitive_output"]},
            )

        assert resp.status_code == 403

    def test_upsert_rule_rejects_unknown_redaction_class(self) -> None:
        with _client("admin") as client:
            resp = client.put(
                self.RULES_URL,
                json={"key_pattern": "risk.*", "role": "viewer", "redacted_fields": ["not_a_class"]},
            )

        assert resp.status_code == 422

    def test_upsert_rule_integrity_error_returns_409(self) -> None:
        with (
            _client("admin") as client,
            patch(
                f"{_ROUTES}._upsert_redaction_rule",
                new=AsyncMock(side_effect=IntegrityError("stmt", "params", "orig")),
            ),
            patch(f"{_ROUTES}.set_rls_org", new=AsyncMock()),
            patch(f"{_ROUTES}.set_rls_user_context", new=AsyncMock()),
        ):
            resp = client.put(
                self.RULES_URL,
                json={"key_pattern": "risk.*", "role": "viewer", "redacted_fields": ["sensitive_output"]},
            )

        assert resp.status_code == 409


class TestListEvidenceQueryShape:
    """The list query is bounded and run-scoped (FAR-944 review)."""

    async def _capture_stmt(self, node_id: str | None, key: str | None = None) -> Any:
        captured: dict[str, Any] = {}

        class _ScalarResult:
            def all(self) -> list[Any]:
                return []

        class _Result:
            def scalars(self) -> _ScalarResult:
                return _ScalarResult()

        async def _execute(statement: Any) -> _Result:
            captured["stmt"] = statement
            return _Result()

        session = types.SimpleNamespace(execute=_execute)
        await evidence_routes._load_evidence_rows(session, _ORG_ID, _RUN_ID, node_id, key)
        return captured["stmt"]

    async def test_run_level_query_has_limit_and_subject_type_guard(self) -> None:
        stmt = await self._capture_stmt(None)
        compiled = str(stmt.compile(compile_kwargs={"literal_binds": True}))

        assert "LIMIT" in compiled
        assert str(evidence_routes._EVIDENCE_LIST_LIMIT) in compiled
        # The run branch is guarded on subject_type so a future non-run subject
        # whose subject_id equals the run id is not returned.
        assert "subject_type" in compiled
        # No key filter was requested, so the query must not narrow by key.
        assert "evidence.key" not in str(stmt.whereclause)

    async def test_node_level_query_keeps_node_execution_branch(self) -> None:
        stmt = await self._capture_stmt("node-1")
        compiled = str(stmt.compile(compile_kwargs={"literal_binds": True}))

        assert "node_execution" in compiled
        assert "LIMIT" in compiled

    async def test_key_filter_narrows_the_query_when_provided(self) -> None:
        stmt = await self._capture_stmt(None, key="eval.guardrail")
        compiled = str(stmt.compile(compile_kwargs={"literal_binds": True}))

        assert "evidence.key" in str(stmt.whereclause)
        assert "eval.guardrail" in compiled


class TestLoadEvidenceRowHelper:
    """``_load_evidence_row`` scopes the single-row read to the caller's org."""

    async def _load(self, row: Any) -> tuple[Any, Any]:
        captured: dict[str, Any] = {}

        class _Result:
            def scalar_one_or_none(self) -> Any:
                return row

        async def _execute(statement: Any) -> _Result:
            captured["stmt"] = statement
            return _Result()

        session = types.SimpleNamespace(execute=_execute)
        found = await evidence_routes._load_evidence_row(session, _ORG_ID, _EVIDENCE_ID)
        return found, captured["stmt"]

    async def test_returns_the_scoped_row_and_binds_both_ids(self) -> None:
        row = _fake_evidence()

        found, stmt = await self._load(row)

        assert found is row
        compiled = stmt.compile()
        # Both the id and the organisation are bound, so the read cannot cross
        # an org boundary even if the caller supplies a foreign row id.
        assert set(compiled.params.values()) == {_ORG_ID, _EVIDENCE_ID}

    async def test_returns_none_when_the_row_is_absent(self) -> None:
        found, _ = await self._load(None)

        assert found is None


class TestLoadRedactionRulesHelper:
    """``_load_redaction_rules`` returns the org's rules in a deterministic order."""

    async def test_returns_rules_ordered_by_pattern_then_role(self) -> None:
        rules = [_fake_rule(), _fake_rule(key_pattern="*", role="viewer")]
        captured: dict[str, Any] = {}

        class _ScalarResult:
            def all(self) -> list[Any]:
                return rules

        class _Result:
            def scalars(self) -> _ScalarResult:
                return _ScalarResult()

        async def _execute(statement: Any) -> _Result:
            captured["stmt"] = statement
            return _Result()

        session = types.SimpleNamespace(execute=_execute)
        loaded = await evidence_routes._load_redaction_rules(session, _ORG_ID)

        assert loaded == rules
        compiled = str(captured["stmt"])
        assert "ORDER BY" in compiled
        assert "key_pattern" in compiled
        assert "role" in compiled


class TestUpsertRedactionRuleHelper:
    """``_upsert_redaction_rule`` creates, updates, and restores in place."""

    async def _upsert(self, existing: Any) -> tuple[Any, AsyncMock, Any]:
        captured: dict[str, Any] = {}

        class _Result:
            def scalar_one_or_none(self) -> Any:
                return existing

        async def _execute(statement: Any) -> _Result:
            captured["stmt"] = statement
            return _Result()

        session = AsyncMock()
        session.add = MagicMock()  # sync call — not awaitable
        session.execute = _execute
        rule = await evidence_routes._upsert_redaction_rule(
            session,
            _ORG_ID,
            "llm_judge.*",
            "viewer",
            ["internal_metadata"],
        )
        return rule, session, captured["stmt"]

    async def test_creates_a_new_rule_when_none_exists(self) -> None:
        rule, session, stmt = await self._upsert(None)

        session.add.assert_called_once()
        added = session.add.call_args.args[0]
        assert added is rule
        assert added.organisation_id == _ORG_ID
        assert added.key_pattern == "llm_judge.*"
        assert added.role == "viewer"
        assert added.redacted_fields == ["internal_metadata"]
        session.flush.assert_awaited_once()
        # The lookup must opt IN to seeing soft-deleted rows, otherwise a
        # previously-retired rule would be treated as absent and the insert
        # would violate the org+pattern+role unique constraint.
        assert stmt.get_execution_options().get("include_deleted") is True

    async def test_updates_and_restores_an_existing_rule_in_place(self) -> None:
        existing = MagicMock()
        existing.redacted_fields = ["sensitive_output"]
        existing.deleted_at = _NOW

        rule, session, _ = await self._upsert(existing)

        assert rule is existing
        session.add.assert_not_called()
        assert existing.redacted_fields == ["internal_metadata"]
        assert existing.deleted_at is None
        session.flush.assert_awaited_once()
