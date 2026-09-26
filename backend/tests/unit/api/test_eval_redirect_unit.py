"""FAR-1101 chunk-3b acceptance coverage   unit-level redirect semantics.

Covers criteria 7, 8, 9, 16 (the shared write helper
``create_or_update_eval`` branch dispatch + validation matrix), 19, 20, 26
(MCP delete/update error envelopes) and 21 (guardrail config-as-code soft
deletes)   all without a database.

Criteria text lives in the internal chunk-3b spec (not in this public repo).
"""

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from sqlalchemy.exc import IntegrityError

from modulo.api.mcp_server import (
    delete_eval_definition,
    update_eval_definition,
)
from modulo.core.eval_engine.eval_definition_write import (
    create_or_update_eval,
    validate_guardrail_request,
)
from modulo.core.eval_engine.policy_gate import PolicyGateBindingViolationError

_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")
_API_KEY = "mk_testprefix_testsecretkey1234567890abc"


def _make_eval(**kwargs: object) -> SimpleNamespace:
    defaults = {
        "id": uuid.uuid4(),
        "pipeline_id": uuid.uuid4(),
        "node_id": None,
        "name": "eval",
        "eval_type": "regex",
        "config_json": {},
        "failure_behaviour": "warn",
        "pass_threshold": None,
        "suite_id": None,
        "eval_suite_id": None,
        "account_id": _USER_ID,
        "version": 1,
        "pre_version_raw": None,
        "deleted_at": None,
        "deleted_by": None,
        "organisation_id": _ORG_ID,
    }
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


def _make_session(return_obj: object) -> AsyncMock:
    session = AsyncMock()
    execute_result = MagicMock()
    execute_result.scalar_one_or_none = MagicMock(return_value=return_obj)
    session.execute = AsyncMock(return_value=execute_result)
    session.add = MagicMock()
    session.delete = AsyncMock()
    session.flush = AsyncMock()
    return session


def _set_context(role: str) -> None:
    from modulo.api.mcp_server import (
        _ctx_auth_token,
        _ctx_auth_type,
        _ctx_org_id,
        _ctx_role,
        _ctx_user_id,
    )

    _ctx_org_id.set(_ORG_ID)
    _ctx_user_id.set(_USER_ID)
    _ctx_role.set(role)
    _ctx_auth_token.set(_API_KEY)
    _ctx_auth_type.set("api_key")


def _clear_context() -> None:
    from modulo.api.mcp_server import (
        _ctx_auth_token,
        _ctx_auth_type,
        _ctx_org_id,
        _ctx_role,
        _ctx_user_id,
    )

    _ctx_org_id.set(None)
    _ctx_user_id.set(None)
    _ctx_role.set(None)
    _ctx_auth_token.set(None)
    _ctx_auth_type.set(None)


_VALID_GUARDRAIL_CONFIG = {"action": "block", "type": "regex"}


# ---------------------------------------------------------------------------
# Criteria 7, 8, 9, 16   create_or_update_eval branch dispatch
# ---------------------------------------------------------------------------


class TestGuardrailBranchDispatch:
    """Criterion 7   guardrail-typed writes run the config validator only."""

    @patch("modulo.db.models.policy_gate.PolicyGate", new_callable=MagicMock)
    @patch("modulo.core.eval_engine.eval_definition_write.validate_binding", new_callable=MagicMock)
    @patch(
        "modulo.core.eval_engine.eval_definition_write.validate_guardrail_request",
        new_callable=MagicMock,
    )
    async def test_guardrail_routes_to_validator_not_binding(
        self,
        mock_validator: MagicMock,
        mock_binding: MagicMock,
        mock_gate_cls: MagicMock,
    ) -> None:
        session = _make_session(None)
        node_id = uuid.uuid4()

        row = await create_or_update_eval(
            session,
            org_id=_ORG_ID,
            account_id=_USER_ID,
            pipeline_id=uuid.uuid4(),
            node_id=node_id,
            name="guardrail",
            eval_type="guardrail",
            config_json=dict(_VALID_GUARDRAIL_CONFIG),
            failure_behaviour="block",
            pass_threshold=None,
            suite_id=None,
        )

        assert row.eval_type == "guardrail"
        assert row.node_id == node_id
        mock_validator.assert_called_once_with(eval_type="guardrail", config_json=_VALID_GUARDRAIL_CONFIG)
        mock_gate_cls.assert_not_called()
        # Persisted Eval row only   no gate add, no binding validation.
        assert mock_binding.call_count == 0
        assert session.add.call_count == 1


class TestValidatorMatrix:
    """Criterion 8   the shared guardrail config-vocabulary validator (3 checks)."""

    def test_retry_failure_behaviour_rejected(self) -> None:
        # FAR-1103 chunk 5a retired ``failure_behaviour`` from the public write
        # boundary: the shared validator no longer declares the parameter, so a
        # caller cannot smuggle a guardrail-terminal "retry" through it at all.
        with pytest.raises(TypeError):
            validate_guardrail_request(
                eval_type="guardrail",
                failure_behaviour="retry",  # type: ignore[call-arg]
                config_json=None,
            )

    def test_unknown_failure_behaviour_rejected(self) -> None:
        with pytest.raises(TypeError):
            validate_guardrail_request(
                eval_type="guardrail",
                failure_behaviour="purge",  # type: ignore[call-arg]
                config_json=None,
            )

    def test_invalid_action_rejected(self) -> None:
        with pytest.raises(HTTPException) as excinfo:
            validate_guardrail_request(
                eval_type="guardrail",
                config_json={"action": "explode", "type": "regex"},
            )
        assert excinfo.value.status_code == 422

    def test_invalid_top_level_type_rejected(self) -> None:
        with pytest.raises(HTTPException) as excinfo:
            validate_guardrail_request(
                eval_type="guardrail",
                config_json={"action": "block", "type": "py_eval"},
            )
        assert excinfo.value.status_code == 422

    def test_invalid_nested_detection_type_rejected(self) -> None:
        with pytest.raises(HTTPException) as excinfo:
            validate_guardrail_request(
                eval_type="guardrail",
                config_json={"action": "block", "detection": {"type": "sql"}},
            )
        assert excinfo.value.status_code == 422

    def test_valid_config_passes(self) -> None:
        result = validate_guardrail_request(eval_type="guardrail", config_json=dict(_VALID_GUARDRAIL_CONFIG))
        assert result is None

    def test_none_config_returns_early(self) -> None:
        # A guardrail with no config_json returns immediately.
        result = validate_guardrail_request(eval_type="guardrail", config_json=None)
        assert result is None

    def test_non_guardrail_eval_type_bypasses(self) -> None:
        # Non-guardrail eval types short-circuit regardless of payload shape.
        result = validate_guardrail_request(eval_type="regex", config_json={"action": "explode"})
        assert result is None


class TestSuiteScopedCreate:
    """Criterion 9   suite-scoped non-guardrail writes persist Eval WITHOUT a gate."""

    @patch("modulo.db.models.policy_gate.PolicyGate", new_callable=MagicMock)
    @patch("modulo.core.eval_engine.eval_definition_write.validate_binding", new_callable=MagicMock)
    async def test_suite_scoped_no_gate_no_binding(self, mock_binding: MagicMock, mock_gate_cls: MagicMock) -> None:
        session = _make_session(None)

        row = await create_or_update_eval(
            session,
            org_id=_ORG_ID,
            account_id=_USER_ID,
            pipeline_id=uuid.uuid4(),
            node_id=None,
            name="suite-eval",
            eval_type="regex",
            config_json={"field": "output", "pattern": "ok"},
            failure_behaviour="warn",
            pass_threshold=0.5,
            suite_id="alpha-suite",
        )

        assert row.node_id is None
        assert row.suite_id == "alpha-suite"
        assert row.pass_threshold is not None
        mock_binding.assert_not_called()
        mock_gate_cls.assert_not_called()
        assert session.add.call_count == 1


class TestFirstTimeRedirectCreate:
    """The UPDATE path with an ``existing_eval_id`` that names a row which does
    not exist yet (first-time redirect) CREATES the Eval row at version 1
    rather than failing — chunk 3b ``eval_definition_write`` lines 208-225.
    """

    async def test_create_when_existing_eval_id_row_missing(self) -> None:
        session = _make_session(None)  # scalar_one_or_none -> None (no existing row)
        missing_id = uuid.uuid4()

        row = await create_or_update_eval(
            session,
            org_id=_ORG_ID,
            account_id=_USER_ID,
            pipeline_id=uuid.uuid4(),
            node_id=None,
            name="redirected",
            eval_type="regex",
            config_json={"field": "output", "pattern": "ok"},
            failure_behaviour="warn",
            pass_threshold=None,
            suite_id=None,
            existing_eval_id=missing_id,
        )

        assert row.id == missing_id
        assert row.version == 1
        assert session.add.call_count == 1
        session.flush.assert_awaited()


class TestBindingViolationPrecedesPersistence:
    """Criterion 16   a binding violation raises BEFORE anything is persisted."""

    async def test_violation_raises_and_persists_nothing(self) -> None:
        session = _make_session(None)

        def _violate(_pg_fields: dict, _ev_fields: dict) -> None:
            raise PolicyGateBindingViolationError([{"exclusion": "detection mismatch"}])

        with (
            patch(
                "modulo.core.eval_engine.eval_definition_write.validate_binding",
                side_effect=_violate,
            ),
            pytest.raises(PolicyGateBindingViolationError, match="detection mismatch"),
        ):
            await create_or_update_eval(
                session,
                org_id=_ORG_ID,
                account_id=_USER_ID,
                pipeline_id=uuid.uuid4(),
                node_id=uuid.uuid4(),
                name="node-eval",
                eval_type="regex",
                config_json={"field": "output", "pattern": "ok"},
                failure_behaviour="warn",
                pass_threshold=None,
                suite_id=None,
            )

        assert session.add.call_count == 0, "nothing may be persisted before the binding check"


# ---------------------------------------------------------------------------
# Criteria 19, 20, 26   MCP delete/update envelopes (mocked sessions)
# ---------------------------------------------------------------------------


def _result_mock(return_obj: object) -> MagicMock:
    result = MagicMock()
    result.scalar_one_or_none = MagicMock(return_value=return_obj)
    return result


def _session_cm(session: AsyncMock) -> AsyncMock:
    cm = AsyncMock()
    cm.__aenter__ = AsyncMock(return_value=session)
    cm.__aexit__ = AsyncMock(return_value=False)
    return cm


class TestMcpDeleteCutover:
    def setup_method(self) -> None:
        _set_context("admin")

    def teardown_method(self) -> None:
        _clear_context()

    @patch("modulo.core.audit_logger.append_audit_event", new_callable=AsyncMock)
    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    async def test_guardrail_soft_delete_stamps_gate_too(
        self, mock_session: AsyncMock, mock_auth: AsyncMock, mock_audit: AsyncMock
    ) -> None:
        """Criterion 19   soft-deleting a guardrail eval stamps BOTH the Eval
        row and its live PolicyGate; no hard delete occurs."""
        guardrail = _make_eval(eval_type="guardrail", name="soft-me")
        gate = _make_eval(eval_id=guardrail.id)  # PolicyGate stub shares the field names
        session = AsyncMock()
        # 1st execute: Eval lookup -> guardrail; 2nd: gate lookup -> gate;
        # (no further reads on the soft path)
        session.execute = AsyncMock(side_effect=[_result_mock(guardrail), _result_mock(gate)])
        session.delete = AsyncMock()
        session.flush = AsyncMock()
        mock_session.return_value = _session_cm(session)

        result = await delete_eval_definition(eval_id=str(guardrail.id), hard=False)

        assert "error" not in result, result
        assert result["soft_deleted"] is True
        assert result["hard_deleted"] is False
        assert guardrail.deleted_at is not None
        assert guardrail.deleted_by == _USER_ID
        assert gate.deleted_at is not None, "the live PolicyGate must be soft-deleted too"
        assert gate.deleted_by == _USER_ID
        assert session.delete.call_count == 0, "soft delete must not hard-remove anything"

        _, audit_kwargs = mock_audit.call_args
        assert audit_kwargs["event_type"] == "eval_definition.soft_deleted"
        assert audit_kwargs["payload_json"] == {
            "eval_id": str(guardrail.id),
            "name": "soft-me",
            "purge": False,
        }

    @patch("modulo.core.audit_logger.append_audit_event", new_callable=AsyncMock)
    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    async def test_guardrail_soft_delete_without_live_gate(
        self, mock_session: AsyncMock, mock_auth: AsyncMock, mock_audit: AsyncMock
    ) -> None:
        """Soft-deleting a guardrail with NO live PolicyGate still succeeds and
        never stamps a gate (chunk 3b: ``gate is None`` arm)."""
        guardrail = _make_eval(eval_type="guardrail", name="no-gate")
        session = AsyncMock()
        session.execute = AsyncMock(side_effect=[_result_mock(guardrail), _result_mock(None)])
        session.delete = AsyncMock()
        session.flush = AsyncMock()
        mock_session.return_value = _session_cm(session)

        result = await delete_eval_definition(eval_id=str(guardrail.id), hard=False)

        assert "error" not in result, result
        assert result["soft_deleted"] is True
        assert guardrail.deleted_at is not None
        assert guardrail.deleted_by == _USER_ID
        assert session.delete.call_count == 0

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    async def test_hard_delete_blocked_by_decisions(self, mock_session: AsyncMock, mock_auth: AsyncMock) -> None:
        """Criterion 20   IntegrityError on hard delete maps to the
        ``delete_blocked_by_decisions`` error envelope."""
        plain = _make_eval(eval_type="regex")
        session = AsyncMock()
        session.execute = AsyncMock(side_effect=[_result_mock(plain)])
        session.delete = AsyncMock()
        session.flush = AsyncMock(side_effect=IntegrityError("stmt", None, Exception("restrict violation")))
        mock_session.return_value = _session_cm(session)

        result = await delete_eval_definition(eval_id=str(plain.id), hard=True)

        assert result["error"] == "delete_blocked_by_decisions"
        assert session.delete.call_count == 1

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    async def test_guardrail_config_violation_returns_validation_failed(
        self, mock_session: AsyncMock, mock_auth: AsyncMock
    ) -> None:
        """Criterion 26   config-vocabulary violations surface as the
        ``validation_failed`` error envelope with an actionable detail."""
        bad = _make_eval(eval_type="guardrail", config_json={"action": "explode", "type": "regex"})
        session = AsyncMock()
        # 1st execute: Eval lookup -> bad; 2nd execute: gate lookup -> None
        session.execute = AsyncMock(side_effect=[_result_mock(bad), _result_mock(None)])
        mock_session.return_value = _session_cm(session)

        result = await update_eval_definition(eval_id=str(bad.id), name="rename")

        assert result["error"] == "validation_failed"
        assert "action" in result["detail"], result


# ---------------------------------------------------------------------------
# Criterion 21   guardrail config-as-code reconciliation deletes
# ---------------------------------------------------------------------------


class TestGuardrailConfigSoftDeletes:
    async def test_absent_org_level_guardrail_is_soft_deleted(self) -> None:
        """Criterion 21   rows the config layer owns and no longer proposes
        are soft-deleted (deleted_at/deleted_by stamped), node-bound rows are
        NEVER reconciled away."""
        from modulo.api.routes.guardrail_config import _apply_guardrail_deletes

        org_level = _make_eval(name="no-longer-proposed", node_id=None)
        node_bound = _make_eval(name="also-absent", node_id=uuid.uuid4())
        proposed = _make_eval(name="still-proposed", node_id=None)
        account_id = uuid.uuid4()

        await _apply_guardrail_deletes(
            MagicMock(),
            {
                "no-longer-proposed": org_level,
                "also-absent": node_bound,
                "still-proposed": proposed,
            },
            {"still-proposed": object()},
            account_id,
        )

        assert org_level.deleted_at is not None
        assert org_level.deleted_by == account_id
        assert node_bound.deleted_at is None, "node-bound rows must never be reconciled away"
        assert proposed.deleted_at is None
