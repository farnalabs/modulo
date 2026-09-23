"""Unit tests for the eval-definition management MCP tools.

Mirrors backend/tests/unit/mcp/test_get_run_output.py: we patch
``validate_current_auth`` and ``_session`` and use an ``AsyncMock`` session that
returns scripted scalars so the tools can run without a real database.
"""

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from modulo.api.mcp_server import (
    create_eval_definition,
    delete_eval_definition,
    update_eval_definition,
)

_PLACEHOLDER_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_PLACEHOLDER_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")
_API_KEY = "mk_testprefix_testsecretkey1234567890abc"


def _make_eval_def(**kwargs: object) -> SimpleNamespace:
    """Build a mutable fake EvalDefinition whose attributes are readable/writable."""
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
        "account_id": _PLACEHOLDER_USER_ID,
        "version": 1,
        "pre_version_raw": None,
        "deleted_at": None,
        "deleted_by": None,
    }
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


def _make_session_cm(return_obj: object) -> AsyncMock:
    """Return an ``_session``-compatible async context manager yielding a mock session."""
    sess = AsyncMock()
    execute_result = MagicMock()
    execute_result.scalar_one_or_none = MagicMock(return_value=return_obj)
    sess.execute = AsyncMock(return_value=execute_result)
    sess.add = MagicMock()
    sess.delete = AsyncMock()
    sess.flush = AsyncMock()
    cm = AsyncMock()
    cm.__aenter__ = AsyncMock(return_value=sess)
    cm.__aexit__ = AsyncMock(return_value=False)
    return cm


def _set_context(role: str) -> None:
    from modulo.api.mcp_server import (
        _ctx_auth_token,
        _ctx_auth_type,
        _ctx_org_id,
        _ctx_role,
        _ctx_user_id,
    )

    _ctx_org_id.set(_PLACEHOLDER_ORG_ID)
    _ctx_user_id.set(_PLACEHOLDER_USER_ID)
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


# ---------------------------------------------------------------------------
# create_eval_definition
# ---------------------------------------------------------------------------


class TestCreateEvalDefinition:
    def setup_method(self) -> None:
        _set_context("admin")

    def teardown_method(self) -> None:
        _clear_context()

    async def test_create_persists_and_returns_dict(self) -> None:
        # FAR-1100 chunk 3 → 3b freeze: creation is frozen — the guard fires
        # before auth/validation/DB work, so the tool returns the typed freeze
        # error instead of persisting.  Revert to the original success
        # assertion when chunk 3b lands (CO-8).
        result = await create_eval_definition(
            pipeline_id=str(uuid.uuid4()),
            name="my-eval",
            eval_type="regex",
            config_json={"k": "v"},
        )

        assert result["error"] == "definition_frozen"
        assert "chunk 3b" in result["detail"]

    async def test_operator_gets_insufficient_scope(self) -> None:
        # FAR-1100 chunk 3 → 3b freeze: the guard fires before the scope check,
        # so an operator now receives definition_frozen rather than
        # insufficient_scope.  Revert when chunk 3b lands (CO-8).
        _set_context("operator")

        result = await create_eval_definition(
            pipeline_id=str(uuid.uuid4()),
            name="my-eval",
            eval_type="regex",
        )

        assert result["error"] == "definition_frozen"
        assert "chunk 3b" in result["detail"]

    async def test_invalid_eval_type_rejected(self) -> None:
        # FAR-1100 chunk 3 → 3b freeze: the guard fires before validation, so
        # this returns definition_frozen instead of invalid_eval_type.  Revert
        # when chunk 3b lands (CO-8).
        result = await create_eval_definition(
            pipeline_id=str(uuid.uuid4()),
            name="my-eval",
            eval_type="not_a_type",
        )

        assert result["error"] == "definition_frozen"
        assert "chunk 3b" in result["detail"]

    async def test_unknown_pipeline_returns_not_found(self) -> None:
        # FAR-1100 chunk 3 → 3b freeze: the guard fires before the pipeline
        # lookup, so this returns definition_frozen instead of
        # pipeline_not_found.  Revert when chunk 3b lands (CO-8).
        result = await create_eval_definition(
            pipeline_id=str(uuid.uuid4()),
            name="my-eval",
            eval_type="regex",
        )

        assert result["error"] == "definition_frozen"
        assert "chunk 3b" in result["detail"]

    async def test_eval_type_trailing_newline_rejected(self) -> None:
        # Pins re.fullmatch semantics: a trailing newline would be accepted by
        # re.match/$ anchors but must be rejected here.  FAR-1100 chunk 3 → 3b
        # freeze: the guard fires before validation, so this now returns
        # definition_frozen.  Revert when chunk 3b lands (CO-8).
        result = await create_eval_definition(
            pipeline_id=str(uuid.uuid4()),
            name="my-eval",
            eval_type="regex\n",
        )

        assert result["error"] == "definition_frozen"
        assert "chunk 3b" in result["detail"]

    async def test_oversized_name_rejected(self) -> None:
        # FAR-1100 chunk 3 → 3b freeze: the guard fires before validation, so
        # this returns definition_frozen instead of invalid_name.  Revert when
        # chunk 3b lands (CO-8).
        result = await create_eval_definition(
            pipeline_id=str(uuid.uuid4()),
            name="x" * 256,
            eval_type="regex",
        )

        assert result["error"] == "definition_frozen"
        assert "chunk 3b" in result["detail"]


# ---------------------------------------------------------------------------
# update_eval_definition
# ---------------------------------------------------------------------------


class TestUpdateEvalDefinition:
    def setup_method(self) -> None:
        _set_context("admin")

    def teardown_method(self) -> None:
        _clear_context()

    async def test_update_bumps_version_and_snapshots_pre_version_raw(self) -> None:
        # FAR-1100 chunk 3 → 3b freeze: editing is frozen — the guard fires
        # before the DB load/version bump, so the tool returns the typed freeze
        # error.  Revert to the original success assertion when chunk 3b lands
        # (CO-8).
        result = await update_eval_definition(
            eval_id=str(uuid.uuid4()),
            name="new",
        )

        assert result["error"] == "definition_frozen"
        assert "chunk 3b" in result["detail"]

    async def test_operator_gets_insufficient_scope(self) -> None:
        # FAR-1100 chunk 3 → 3b freeze: the guard fires before the scope check,
        # so an operator now receives definition_frozen rather than
        # insufficient_scope.  Revert when chunk 3b lands (CO-8).
        _set_context("operator")

        result = await update_eval_definition(eval_id=str(uuid.uuid4()), name="new")

        assert result["error"] == "definition_frozen"
        assert "chunk 3b" in result["detail"]


# ---------------------------------------------------------------------------
# delete_eval_definition
# ---------------------------------------------------------------------------


class TestDeleteEvalDefinition:
    def setup_method(self) -> None:
        _set_context("admin")

    def teardown_method(self) -> None:
        _clear_context()

    @patch("modulo.core.audit_logger.append_audit_event", new_callable=AsyncMock)
    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    async def test_guardrail_soft_deletes_by_default(
        self,
        mock_session: AsyncMock,
        mock_validate_auth: AsyncMock,
        mock_audit: AsyncMock,
    ) -> None:
        guardrail = _make_eval_def(eval_type="guardrail")
        mock_session.return_value = _make_session_cm(guardrail)

        result = await delete_eval_definition(eval_id=str(guardrail.id), hard=False)

        assert "error" not in result, result
        assert result["soft_deleted"] is True
        assert result["hard_deleted"] is False
        assert guardrail.deleted_at is not None
        assert guardrail.deleted_by == _PLACEHOLDER_USER_ID
        assert mock_audit.await_count == 1
        _, kwargs = mock_audit.call_args
        assert kwargs["event_type"] == "eval_definition.soft_deleted"
        assert kwargs["payload_json"] == {
            "eval_id": str(guardrail.id),
            "name": "eval",
            "purge": False,
        }

    @patch("modulo.core.audit_logger.append_audit_event", new_callable=AsyncMock)
    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    async def test_non_guardrail_hard_purges_with_audit(
        self,
        mock_session: AsyncMock,
        mock_validate_auth: AsyncMock,
        mock_audit: AsyncMock,
    ) -> None:
        plain = _make_eval_def(eval_type="regex")
        cm = _make_session_cm(plain)
        mock_session.return_value = cm

        result = await delete_eval_definition(eval_id=str(plain.id), hard=True)

        assert "error" not in result, result
        assert result["hard_deleted"] is True
        assert cm.__aenter__.return_value.delete.called
        # Hard purge of a non-guardrail is not audited (only guardrails are).
        assert mock_audit.await_count == 0

    @patch("modulo.core.audit_logger.append_audit_event", new_callable=AsyncMock)
    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    async def test_guardrail_hard_purges_with_audit(
        self,
        mock_session: AsyncMock,
        mock_validate_auth: AsyncMock,
        mock_audit: AsyncMock,
    ) -> None:
        guardrail = _make_eval_def(eval_type="guardrail")
        cm = _make_session_cm(guardrail)
        mock_session.return_value = cm

        result = await delete_eval_definition(eval_id=str(guardrail.id), hard=True)

        assert "error" not in result, result
        assert result["hard_deleted"] is True
        assert cm.__aenter__.return_value.delete.called
        assert mock_audit.await_count == 1
        _, kwargs = mock_audit.call_args
        assert kwargs["event_type"] == "eval_definition.purged"
        assert kwargs["payload_json"] == {
            "eval_id": str(guardrail.id),
            "name": "eval",
            "purge": True,
        }

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    async def test_operator_gets_insufficient_scope(
        self,
        mock_session: AsyncMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        _set_context("operator")
        mock_session.return_value = _make_session_cm(_make_eval_def())

        result = await delete_eval_definition(eval_id=str(uuid.uuid4()))

        assert result["error"] == "insufficient_scope"

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    async def test_missing_eval_definition_returns_not_found(
        self,
        mock_session: AsyncMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        mock_session.return_value = _make_session_cm(None)

        result = await delete_eval_definition(eval_id=str(uuid.uuid4()))

        assert result["error"] == "eval_definition_not_found"
