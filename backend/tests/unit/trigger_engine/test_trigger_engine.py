"""Unit tests for TriggerEngine and helpers using mocked AsyncSession.

QA lens pass (correctness, bugs, edge cases, error paths) on the
trigger_engine test package:

* ``_apply_payload_mapping`` rejects mapping targets that write to reserved
  input-payload keys (``_work_item_id`` / ``_modulo.work_item`` /
  ``_feedback_correction``) — a trigger can never forge system-injected data;
* ``_extract_work_item_refs`` contract: ``None`` when ref_paths is not a list,
  skips non-dict/missing-kind/missing-path/empty-value entries, returns
  ``None`` when nothing survives, and stamps ``{kind, ref, source: "derived"}``
  otherwise;
* ``_is_unique_violation`` fails closed for a non-Exception ``orig``;
* the webhook/replay rate-limit resolution chain: limit on the trigger's own
  ``config_json`` (no pipeline lookup), missing pipeline row skips the check,
  and an un-exceeded limit passes the run through with the computed key;
* replay accepted-event gate passes when the payload carries an accepted
  event dict.
"""

import asyncio
import datetime
import hashlib
import hmac
import time
import uuid
from collections.abc import AsyncGenerator, Generator
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError

from modulo.core.trigger_engine import (
    ConcurrentRunLimitError,
    DuplicateWebhookError,
    HmacValidationError,
    PipelineRateLimitError,
    ReplayNotFoundError,
    TimestampExpiredError,
    TriggerBusyError,
    TriggerEngine,
    TriggerInactiveError,
    TriggerNotFoundError,
    _apply_payload_mapping,
    _extract_field,
    _extract_work_item_refs,
    _is_unique_violation,
    _matches_event_filters,
    sha256_hex,
    verify_hmac,
    verify_timestamp,
)
from modulo.db.models.trigger import Trigger

# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------


def _replay_auth_headers() -> dict[str, str]:
    """Bearer JWT for the replay route (ADR 017: runner-or-HMAC).

    The route uses ``get_current_tenant_user_optional``, which decodes the
    Bearer directly — a token signed with the test secret_key is enough.
    """
    from modulo.auth.jwt import create_access_token

    token = create_access_token(
        "ci@test.local",
        _VALID_32,
        organisation_id=str(uuid.uuid4()),
        account_id=str(uuid.uuid4()),
        org_role="admin",
    )
    return {"Authorization": f"Bearer {token}"}


_VALID_TS: int


@pytest.fixture(autouse=True)
def refresh_valid_timestamp() -> None:
    global _VALID_TS
    _VALID_TS = int(time.time())


def _sha256_sig(body: bytes, secret: str, timestamp: int | None = None) -> str:
    payload = f"{timestamp}.".encode() + body if timestamp is not None else body
    return "sha256=" + hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()


def _make_trigger(
    *,
    active: bool = True,
    hmac_secret: str | None = None,
    payload_mapping: dict[str, str] | None = None,
    max_concurrent_runs: int = 5,
    accepted_events: list[str] | None = None,
    extra_config: dict[str, Any] | None = None,
) -> MagicMock:
    t = MagicMock(spec=Trigger)
    t.id = uuid.uuid4()
    t.pipeline_id = uuid.uuid4()
    t.organisation_id = uuid.uuid4()
    t.active = active
    config: dict[str, Any] = {}
    if hmac_secret is not None:
        config["hmac_secret"] = hmac_secret
    if payload_mapping is not None:
        config["payload_mapping"] = payload_mapping
    if accepted_events is not None:
        config["accepted_events"] = accepted_events
    if extra_config is not None:
        config.update(extra_config)
    t.config_json = config
    t.max_concurrent_runs = max_concurrent_runs
    return t


def _make_session(
    *,
    trigger: MagicMock | None = None,
    active_run_count: int = 0,
    dedup_exists: bool = False,
    pipeline_rate_limit: dict[str, Any] | None = None,
    recent_run_count: int = 0,
    pipeline_found: bool = True,
) -> AsyncMock:
    """Build a mocked session that returns the given trigger and run count."""
    session = AsyncMock()

    lock_result = MagicMock()
    lock_result.scalar_one.return_value = True

    trigger_result = MagicMock()
    trigger_result.scalar_one_or_none.return_value = trigger
    trigger_result.scalar_one.return_value = trigger

    dedup_result = MagicMock()
    dedup_result.scalar_one_or_none.return_value = MagicMock() if dedup_exists else None

    generic_result = MagicMock()

    count_result = MagicMock()
    count_result.scalar_one.return_value = active_run_count

    recent_count_result = MagicMock()
    recent_count_result.scalar_one.return_value = recent_run_count

    # Pre-trigger guardrail pass (FAR-214) queries guardrail rows BEFORE the
    # dedup insert — return none by default so the pass is a no-op.
    guardrail_result = MagicMock()
    guardrail_result.scalars.return_value.all.return_value = []

    call_count = 0

    # Pipeline lookup for rate-limit config (call 7+). No rate limit by default.
    pipeline_result = MagicMock()
    pipeline_result.scalar_one_or_none.return_value = MagicMock() if pipeline_found else None
    if pipeline_found:
        pipeline_result.scalar_one_or_none.return_value.rate_limit_config = pipeline_rate_limit

    async def _execute(stmt: Any, *args: Any, **kwargs: Any) -> Any:
        nonlocal call_count
        # RLS set_config (org / execution context) is plumbing — do not consume
        # a result slot or shift the call-count routing below.
        if "set_config" in str(stmt):
            return MagicMock()
        call_count += 1
        # Order: 1=advisory lock, 2=trigger lookup, 3=guardrail rows (FAR-214),
        #        4=dedup SELECT, 5=dedup DELETE, 6=count active runs,
        #        7=pipeline lookup (rate limit), 8=recent rate-limited count, 9+=other
        if call_count == 1:
            return lock_result
        if call_count == 2:
            return trigger_result
        if call_count == 3:
            return guardrail_result
        if call_count == 4:
            return dedup_result
        if call_count == 5:
            return generic_result
        if call_count == 6:
            return count_result
        if call_count == 7:
            return pipeline_result
        if call_count == 8:
            return recent_count_result
        return pipeline_result

    session.execute = _execute
    session.add = MagicMock()
    session.flush = AsyncMock()

    # Replace AsyncMock get_bind with sync MagicMock (Python 3.13+ AsyncMock returns coroutines)
    bind_mock = MagicMock()
    bind_mock.dialect.name = "postgresql"
    session.in_transaction = MagicMock(return_value=True)
    session.get_bind = MagicMock(return_value=bind_mock)

    nested_cm = AsyncMock()
    nested_cm.__aenter__ = AsyncMock(return_value=None)
    nested_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin_nested = MagicMock(return_value=nested_cm)

    return session


_ORG = uuid.UUID("00000000-0000-0000-0000-000000000001")
_SNAP = uuid.UUID("00000000-0000-0000-0000-000000000003")
_RAW_BODY = b'{"action": "opened", "number": 42}'
_RAW_PAYLOAD: dict[str, Any] = {"action": "opened", "number": 42}

_VALID_32 = "a" * 32


@pytest.fixture(autouse=True)
def _org_not_paused() -> Generator[None, None, None]:
    """Default engine-level org-pause state to not-paused.

    The engine now gates via ``settings_resolver.ensure_triggers_resumable``
    (which reads ``settings_resolver.org_is_paused``) and ``create_run`` reads
    the same helper — both must read False or the mocked sessions fail-closed
    as paused. Paused-specific tests override this with a nested
    ``return_value=True``.
    """
    with patch("modulo.db.settings_resolver.org_is_paused", new_callable=AsyncMock, return_value=False):
        yield


# ---------------------------------------------------------------------------
# TestClient fixture for webhook route tests
# ---------------------------------------------------------------------------


@pytest.fixture
def webhook_client() -> Generator[TestClient, None, None]:
    import sys
    import types

    # Mock langchain_google_vertexai to prevent the import chain from
    # hanging on google.cloud.aiplatform file I/O during app import.
    _mock_lgv = types.ModuleType("langchain_google_vertexai")
    _mock_lgv.ChatVertexAI = type("ChatVertexAI", (), {})
    _was_in_sys = "langchain_google_vertexai" in sys.modules
    if not _was_in_sys:
        sys.modules["langchain_google_vertexai"] = _mock_lgv

    try:
        from modulo.api.dependencies import _get_engine, get_db_session
        from modulo.api.main import app
        from modulo.auth.dependencies import get_current_user
        from modulo.auth.jwt import AuthenticatedPrincipal
        from modulo.settings import Settings, get_settings
    finally:
        if not _was_in_sys:
            sys.modules.pop("langchain_google_vertexai", None)

    _fake_org_id = uuid.uuid4()
    _fake_user_id = uuid.uuid4()

    def _settings() -> Settings:
        return Settings(
            database_url="postgresql+asyncpg://localhost/test",
            secret_key=_VALID_32,
            fernet_key=_VALID_32,
            modulo_admin_password="pw",
            redis_url="",
        )

    def _principal() -> AuthenticatedPrincipal:
        return AuthenticatedPrincipal(
            username="ci@test.local",
            organisation_id=_fake_org_id,
            account_id=_fake_user_id,
            org_role="admin",
        )

    trigger_mock = MagicMock()
    trigger_mock.id = uuid.uuid4()
    trigger_mock.pipeline_id = uuid.uuid4()
    trigger_mock.active = True
    trigger_mock.config_json = {}
    trigger_mock.max_concurrent_runs = 5

    execute_result = MagicMock()
    execute_result.scalar_one_or_none.return_value = trigger_mock
    execute_result.scalar_one.return_value = trigger_mock

    snapshot_mock = MagicMock()
    snapshot_mock.id = uuid.uuid4()

    session = AsyncMock()
    bind = MagicMock()
    bind.dialect.name = "sqlite"
    session.in_transaction = MagicMock(return_value=True)
    session.get_bind = MagicMock(return_value=bind)
    session.info = {}
    session.execute = AsyncMock(return_value=execute_result)
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)

    async def _session() -> AsyncGenerator[AsyncMock, None]:
        yield session

    app.dependency_overrides[get_settings] = _settings
    app.dependency_overrides[get_db_session] = _session
    app.dependency_overrides[_get_engine] = lambda: MagicMock()
    app.dependency_overrides[get_current_user] = _principal

    with (
        patch("modulo.db.crud.pipeline_snapshot.create_snapshot_from_live_graph", return_value=snapshot_mock),
        patch("modulo.core.rate_limiter.RateLimiterRegistry.check", return_value=True),
        patch("modulo.db.settings_resolver.org_is_paused", new_callable=AsyncMock, return_value=False),
    ):
        yield TestClient(app, raise_server_exceptions=False)

    app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# Pure helper function tests
# ---------------------------------------------------------------------------


def test_sha256_hex_is_hex_string() -> None:
    result = sha256_hex(b"hello")
    assert len(result) == 64
    assert all(c in "0123456789abcdef" for c in result)


def test_sha256_hex_is_deterministic() -> None:
    # Golden value pins the exact digest so a change in the hash algorithm
    # fails loudly instead of silently altering trigger identifiers.
    assert sha256_hex(b"x") == "2d711642b726b04401627ca9fbac32f5c8530fb1903cc4db02258717921a4881"


@pytest.mark.parametrize("raw", [None, "not-bytes", 42, ["bytes"]])
def test_sha256_hex_returns_empty_for_non_bytes(raw: object) -> None:
    assert not sha256_hex(raw)


class TestVerifyTimestamp:
    @pytest.mark.parametrize(
        ("timestamp_input", "expect_raises"),
        [
            (lambda: str(int(time.time())), False),
            (lambda: str(int(time.time()) - 600), True),
            (lambda: str(int(time.time()) + 600), True),
            (lambda: None, True),
            (lambda: "not-a-number", True),
        ],
    )
    def test_verify_timestamp(self, timestamp_input, expect_raises) -> None:
        ts = timestamp_input() if callable(timestamp_input) else timestamp_input
        if expect_raises:
            with pytest.raises(TimestampExpiredError):
                verify_timestamp(ts)
        else:
            result = verify_timestamp(ts)
            assert isinstance(result, int)


@pytest.mark.parametrize(
    ("secret", "body", "sig_maker", "expected"),
    [
        ("my-secret", b"payload", lambda b, s: (_sha256_sig(b, s, timestamp := int(time.time())), timestamp), True),
        ("my-secret", b"payload", lambda b, s: (_sha256_sig(b, s), None), True),
        ("secret", b"payload", lambda b, s: ("sha256=wrong", int(time.time())), False),
        ("secret", b"payload", lambda b, s: (None, int(time.time())), False),
        # Signature is bound to the timestamp: signing a different timestamp than the
        # one presented to verify_hmac must fail (verifies the timestamp is part of the HMAC).
        ("s", b"x", lambda b, s: (_sha256_sig(b, s, timestamp=int(time.time()) - 600), int(time.time())), False),
        # Body mutation must invalidate the signature.
        (
            "s",
            b"original-body",
            lambda b, s: (_sha256_sig(b"tampered", s, timestamp=int(time.time())), int(time.time())),
            False,
        ),
    ],
)
def test_verify_hmac(secret, body, sig_maker, expected) -> None:
    sig, ts = sig_maker(body, secret)
    assert verify_hmac(body, secret, sig, timestamp=ts) is expected


@pytest.mark.parametrize(
    ("data", "field_path", "expected"),
    [
        ({"a": 1}, "a", 1),
        ({"a": {"b": {"c": "deep"}}}, "a.b.c", "deep"),
        ({"a": 1}, "b", None),
        ({"a": "not-a-dict"}, "a.b", None),
    ],
)
def test_extract_field(data, field_path, expected) -> None:
    assert _extract_field(data, field_path) == expected


def test_apply_payload_mapping_empty_returns_raw() -> None:
    raw: dict[str, Any] = {"x": 1, "y": 2}
    assert _apply_payload_mapping(raw, {}) == raw


def test_apply_payload_mapping_extracts_fields() -> None:
    raw: dict[str, Any] = {"pr": {"number": 7, "title": "Fix bug"}}
    mapping = {"pr_number": "pr.number", "pr_title": "pr.title"}
    result = _apply_payload_mapping(raw, mapping)
    assert result == {"pr_number": 7, "pr_title": "Fix bug"}


def test_apply_payload_mapping_missing_path_gives_none() -> None:
    result = _apply_payload_mapping({"a": 1}, {"x": "missing.path"})
    assert result == {"x": None}


def test_apply_payload_mapping_returns_new_dict() -> None:
    raw: dict[str, Any] = {"x": 1}
    result = _apply_payload_mapping(raw, {})
    assert result == raw
    assert result is not raw


@pytest.mark.parametrize("reserved_key", ["_work_item_id", "_modulo.work_item", "_feedback_correction"])
def test_apply_payload_mapping_rejects_reserved_keys(reserved_key: str) -> None:
    """A mapping target that writes to a reserved key must be rejected.

    Otherwise a webhook payload (or manual POST body) could forge a
    work-item id / feedback-correction context that is meant to be set only
    through explicit ``create_run`` kwargs.
    """
    raw: dict[str, Any] = {"number": 7}
    with pytest.raises(ValueError, match=reserved_key):
        _apply_payload_mapping(raw, {reserved_key: "number"})


def test_extract_work_item_refs_none_when_not_a_list() -> None:
    assert _extract_work_item_refs({"a": 1}, "not-a-list") is None
    assert _extract_work_item_refs({"a": 1}, None) is None


def test_extract_work_item_refs_none_when_no_matching_paths() -> None:
    assert _extract_work_item_refs({"a": 1}, []) is None


def test_extract_work_item_refs_extracts_derived_refs() -> None:
    payload = {"pull_request": {"number": 7, "title": "Fix bug"}}
    ref_paths = [
        {"kind": "github_pr", "path": "pull_request.number"},
        {"kind": "github_pr_title", "path": "pull_request.title"},
    ]
    result = _extract_work_item_refs(payload, ref_paths)
    assert result == [
        {"kind": "github_pr", "ref": "7", "source": "derived"},
        {"kind": "github_pr_title", "ref": "Fix bug", "source": "derived"},
    ]


def test_extract_work_item_refs_skips_invalid_and_empty_entries() -> None:
    payload = {"pr": {"number": 7}, "empty": "   ", "present": "value"}
    ref_paths: list[Any] = [
        "not-a-dict",
        None,
        {"kind": "no_path"},
        {"path": "pr.number"},
        {"kind": "", "path": "present"},
        {"kind": "missing_field", "path": "does.not.exist"},
        {"kind": "empty_value", "path": "empty"},
        {"kind": "github_pr", "path": "pr.number"},
    ]
    result = _extract_work_item_refs(payload, ref_paths)
    assert result == [{"kind": "github_pr", "ref": "7", "source": "derived"}]


def test_extract_work_item_refs_all_skipped_returns_none() -> None:
    assert _extract_work_item_refs({"a": 1}, [{"kind": "k", "path": "missing"}]) is None


class TestIsUniqueViolation:
    @staticmethod
    def _integrity(orig: Exception) -> IntegrityError:
        return IntegrityError("stmt", {}, orig)

    def test_postgres_pgcode_23505(self) -> None:
        class _PgError(Exception):
            pgcode = "23505"

        assert _is_unique_violation(self._integrity(_PgError())) is True

    def test_postgres_non_unique_pgcode(self) -> None:
        class _PgError(Exception):
            pgcode = "23503"  # foreign key violation

        assert _is_unique_violation(self._integrity(_PgError())) is False

    def test_sqlite_unique_constraint_message(self) -> None:
        err = Exception("UNIQUE constraint failed: webhook_dedup_hash.key")
        assert _is_unique_violation(self._integrity(err)) is True

    def test_mariadb_duplicate_entry_1062(self) -> None:
        class _MySQLError(Exception):
            def __init__(self) -> None:
                super().__init__(1062, "Duplicate entry 'abc' for key 'PRIMARY'")

        assert _is_unique_violation(self._integrity(_MySQLError())) is True

    def test_other_integrity_error(self) -> None:
        assert _is_unique_violation(self._integrity(Exception("some other error"))) is False

    def test_orig_none(self) -> None:
        assert _is_unique_violation(IntegrityError("stmt", {}, None)) is False

    def test_orig_not_an_exception(self) -> None:
        # A DBAPI orig that is not an Exception (e.g. a bare driver object)
        # must fail closed as "not unique" without raising.
        assert _is_unique_violation(self._integrity(cast(Any, object()))) is False


class TestComputeRateLimitKey:
    def test_exact_mode_extracts_fields_sorted(self) -> None:
        config = {"key_fields": ["repo", "org"], "match_mode": "exact"}
        payload = {"org": "acme", "repo": "app", "other": 1}
        assert TriggerEngine._compute_rate_limit_key(payload, config) == '{"org": "acme", "repo": "app"}'

    def test_exact_missing_field_is_null(self) -> None:
        config = {"key_fields": ["repo"], "match_mode": "exact"}
        assert TriggerEngine._compute_rate_limit_key({}, config) == '{"repo": null}'

    def test_presence_mode_present(self) -> None:
        config = {"key_fields": ["repo"], "match_mode": "presence"}
        assert TriggerEngine._compute_rate_limit_key({"repo": "anything"}, config) == '{"repo": "__present__"}'

    def test_presence_mode_absent(self) -> None:
        config = {"key_fields": ["repo"], "match_mode": "presence"}
        assert TriggerEngine._compute_rate_limit_key({}, config) == '{"repo": null}'

    def test_no_key_fields_is_empty_object(self) -> None:
        assert TriggerEngine._compute_rate_limit_key({"a": 1}, {}) == "{}"


# ---------------------------------------------------------------------------
# TriggerEngine._try_insert_dedup
# ---------------------------------------------------------------------------


class TestTryInsertDedup:
    def _session(self, flush_side_effect: Any = None) -> AsyncMock:
        """Session where the dedup SELECT finds nothing and the flush raises as configured."""
        session = AsyncMock()
        existing = MagicMock()
        existing.scalar_one_or_none.return_value = None
        delete_result = MagicMock()
        nested = AsyncMock()
        nested.__aenter__ = AsyncMock(return_value=None)
        nested.__aexit__ = AsyncMock(return_value=False)
        session.begin_nested = MagicMock(return_value=nested)
        session.flush = AsyncMock(side_effect=flush_side_effect)

        call_count = 0

        async def _execute(stmt: Any, *args: Any, **kwargs: Any) -> Any:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return existing
            return delete_result

        session.execute = _execute
        session.add = MagicMock()
        return session

    async def test_insert_succeeds_returns_true(self) -> None:
        engine = TriggerEngine()
        session = self._session()
        result = await engine._try_insert_dedup(session, uuid.uuid4(), uuid.uuid4(), "hash-1")
        assert result is True
        session.add.assert_called_once()

    async def test_duplicate_hash_returns_false(self) -> None:
        engine = TriggerEngine()
        session = AsyncMock()
        existing = MagicMock()
        existing.scalar_one_or_none.return_value = MagicMock()
        session.execute = AsyncMock(return_value=existing)
        session.add = MagicMock()
        session.flush = AsyncMock()

        result = await engine._try_insert_dedup(session, uuid.uuid4(), uuid.uuid4(), "hash-1")

        assert result is False
        session.add.assert_not_called()

    async def test_unique_violation_returns_false(self) -> None:
        class _PgError(Exception):
            pgcode = "23505"

        engine = TriggerEngine()
        session = self._session(IntegrityError("INSERT", {}, _PgError()))
        result = await engine._try_insert_dedup(session, uuid.uuid4(), uuid.uuid4(), "hash-1")
        assert result is False

    async def test_non_unique_integrity_error_propagates(self) -> None:
        class _PgError(Exception):
            pgcode = "23503"  # FK violation — not a duplicate

        engine = TriggerEngine()
        session = self._session(IntegrityError("INSERT", {}, _PgError()))
        with pytest.raises(IntegrityError):
            await engine._try_insert_dedup(session, uuid.uuid4(), uuid.uuid4(), "hash-1")


# ---------------------------------------------------------------------------
# TriggerEngine.evaluate_condition — sync-friendly one-off evaluation
# ---------------------------------------------------------------------------


class TestTriggerEngineEvaluateCondition:
    """Tests for the static ``TriggerEngine.evaluate_condition`` helper."""

    @pytest.fixture(autouse=True)
    def polling_env(self):
        settings = MagicMock()
        settings.fernet_key = _VALID_32
        with (
            patch("modulo.settings.get_settings", return_value=settings),
            patch("modulo.core.trigger_engine.create_secrets_backend") as mock_sb,
            patch("modulo.core.trigger_engine.polling._build_polling_connector") as mock_build,
            patch("modulo.core.trigger_engine.polling.evaluate_condition") as mock_eval,
        ):
            backend = AsyncMock()
            backend.get_secret.return_value = '{"token": "test-token"}'
            mock_sb.return_value = backend

            connector = AsyncMock()
            query_result = MagicMock()
            query_result.records = [{"number": 1}]
            query_result.total = 1
            connector.query.return_value = query_result
            mock_build.return_value = connector

            mock_eval.return_value = True
            yield mock_sb, mock_build, mock_eval, connector

    @staticmethod
    def _session(instance: Any) -> AsyncMock:
        session = AsyncMock()
        conn_result = MagicMock()
        conn_result.scalar_one_or_none.return_value = instance
        session.execute = AsyncMock(return_value=conn_result)
        return session

    @staticmethod
    async def _run(session: AsyncMock, instance_id: uuid.UUID) -> dict[str, Any]:
        return await TriggerEngine.evaluate_condition(
            session,
            trigger=MagicMock(),
            org_id=uuid.uuid4(),
            connector_instance_id=instance_id,
            poll_query="issues",
            condition_expression=None,
        )

    async def test_connector_instance_not_found_returns_error(self) -> None:
        instance_id = uuid.uuid4()
        result = await self._run(self._session(None), instance_id)

        assert result == {"status": "error", "error": f"Connector instance {instance_id} not found"}

    async def test_condition_met_returns_records(self) -> None:
        result = await self._run(self._session(MagicMock()), uuid.uuid4())

        assert result["status"] == "condition_met"
        assert result["records"] == [{"number": 1}]
        assert result["total"] == 1

    async def test_no_match_returns_records(self, polling_env) -> None:
        _sb, _build, mock_eval, _connector = polling_env
        mock_eval.return_value = False
        result = await self._run(self._session(MagicMock()), uuid.uuid4())

        assert result["status"] == "no_match"
        assert result["records"] == [{"number": 1}]

    async def test_connector_init_failed_returns_error(self, polling_env) -> None:
        mock_sb, _build, _eval, _connector = polling_env
        mock_sb.side_effect = RuntimeError("fernet key invalid")
        result = await self._run(self._session(MagicMock()), uuid.uuid4())

        assert result["status"] == "error"
        assert "Connector init failed" in result["error"]

    async def test_query_failed_returns_error(self, polling_env) -> None:
        _sb, _build, _eval, connector = polling_env
        connector.query.side_effect = RuntimeError("upstream 500")
        result = await self._run(self._session(MagicMock()), uuid.uuid4())

        assert result["status"] == "error"
        assert "Query failed" in result["error"]

    async def test_condition_evaluation_failed_returns_error(self, polling_env) -> None:
        _sb, _build, mock_eval, _connector = polling_env
        mock_eval.side_effect = ValueError("Invalid JMESPath expression")
        result = await self._run(self._session(MagicMock()), uuid.uuid4())

        assert result["status"] == "error"
        assert "Condition evaluation failed" in result["error"]

    @pytest.mark.parametrize(
        "stage",
        ["connector_init", "query", "condition_eval"],
    )
    async def test_cancelled_error_propagates(self, polling_env, stage: str) -> None:
        mock_sb, _build, mock_eval, connector = polling_env
        if stage == "connector_init":
            mock_sb.side_effect = asyncio.CancelledError()
        elif stage == "query":
            connector.query.side_effect = asyncio.CancelledError()
        else:
            mock_eval.side_effect = asyncio.CancelledError()

        with pytest.raises(asyncio.CancelledError):
            await self._run(self._session(MagicMock()), uuid.uuid4())


# ---------------------------------------------------------------------------
# TriggerEngine.handle_webhook — happy path
# ---------------------------------------------------------------------------


async def test_handle_webhook_success_no_hmac() -> None:
    trigger = _make_trigger()
    session = _make_session(trigger=trigger, active_run_count=0)

    run_mock = MagicMock()
    run_mock.id = uuid.uuid4()

    with (
        patch("modulo.core.trigger_engine.create_run", return_value=run_mock),
        patch("modulo.core.trigger_engine.time.time", return_value=_VALID_TS),
    ):
        result = await TriggerEngine().handle_webhook(
            session,
            trigger_id=trigger.id,
            org_id=_ORG,
            raw_body=_RAW_BODY,
            raw_payload=_RAW_PAYLOAD,
            hmac_signature=None,
            modulo_timestamp=str(_VALID_TS),
            snapshot_id=_SNAP,
        )

    run, te, _payload = result
    assert run is run_mock
    assert te.validation_result == "accepted"


async def test_handle_webhook_success_with_hmac() -> None:
    secret = "test-secret"
    body = _RAW_BODY
    ts = _VALID_TS
    sig = _sha256_sig(body, secret, timestamp=ts)
    trigger = _make_trigger(hmac_secret=secret)
    session = _make_session(trigger=trigger, active_run_count=0)

    run_mock = MagicMock()
    run_mock.id = uuid.uuid4()

    with (
        patch("modulo.core.trigger_engine.create_run", return_value=run_mock),
        patch("modulo.core.trigger_engine.time.time", return_value=ts),
    ):
        run, _, _ = await TriggerEngine().handle_webhook(
            session,
            trigger_id=trigger.id,
            org_id=_ORG,
            raw_body=body,
            raw_payload=_RAW_PAYLOAD,
            hmac_signature=sig,
            modulo_timestamp=str(ts),
            snapshot_id=_SNAP,
        )

    assert run is run_mock


async def test_handle_webhook_verifies_hmac_after_secret_resync() -> None:
    """After a webhook secret is re-synced, HMAC verification must use the
    CURRENT secret: a signature bound to the previous secret is rejected while
    one bound to the re-synced secret is accepted (same body + timestamp)."""
    old_secret = "old-secret-A"
    new_secret = "new-secret-B"
    body = _RAW_BODY
    ts = _VALID_TS
    trigger = _make_trigger(hmac_secret=new_secret)

    # A signature computed with the pre-resync secret must be rejected.
    stale_session = _make_session(trigger=trigger, active_run_count=0)
    stale_sig = _sha256_sig(body, old_secret, timestamp=ts)
    with pytest.raises(HmacValidationError):
        await TriggerEngine().handle_webhook(
            stale_session,
            trigger_id=trigger.id,
            org_id=_ORG,
            raw_body=body,
            raw_payload=_RAW_PAYLOAD,
            hmac_signature=stale_sig,
            modulo_timestamp=str(ts),
            snapshot_id=_SNAP,
        )

    # A signature computed with the post-resync secret must be accepted.
    current_session = _make_session(trigger=trigger, active_run_count=0)
    current_sig = _sha256_sig(body, new_secret, timestamp=ts)
    run_mock = MagicMock()
    run_mock.id = uuid.uuid4()
    with (
        patch("modulo.core.trigger_engine.create_run", return_value=run_mock),
        patch("modulo.core.trigger_engine.time.time", return_value=ts),
    ):
        run, _, _ = await TriggerEngine().handle_webhook(
            current_session,
            trigger_id=trigger.id,
            org_id=_ORG,
            raw_body=body,
            raw_payload=_RAW_PAYLOAD,
            hmac_signature=current_sig,
            modulo_timestamp=str(ts),
            snapshot_id=_SNAP,
        )

    assert run is run_mock


async def test_handle_webhook_applies_payload_mapping() -> None:
    mapping = {"action": "action", "pr_num": "number"}
    trigger = _make_trigger(payload_mapping=mapping)
    session = _make_session(trigger=trigger, active_run_count=0)

    run_mock = MagicMock()
    run_mock.id = uuid.uuid4()

    with (
        patch("modulo.core.trigger_engine.create_run", return_value=run_mock) as mock_create,
        patch("modulo.core.trigger_engine.time.time", return_value=_VALID_TS),
    ):
        _, _, input_payload = await TriggerEngine().handle_webhook(
            session,
            trigger_id=trigger.id,
            org_id=_ORG,
            raw_body=_RAW_BODY,
            raw_payload=_RAW_PAYLOAD,
            hmac_signature=None,
            modulo_timestamp=str(_VALID_TS),
            snapshot_id=_SNAP,
        )

    called_payload = mock_create.call_args.kwargs["input_payload"]
    assert called_payload == {"action": "opened", "pr_num": 42}
    assert input_payload == called_payload


# ---------------------------------------------------------------------------
# TriggerEngine.handle_webhook — validation failures
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("trigger_overrides", "session_overrides", "hmac_sig", "mod_ts_factory", "expected_exc", "extra_assert"),
    [
        ({}, {"trigger": None}, None, lambda: str(int(time.time())), TriggerNotFoundError, None),
        ({"active": False}, {}, None, lambda: str(int(time.time())), TriggerInactiveError, None),
        ({}, {}, None, lambda: None, TimestampExpiredError, None),
        ({"hmac_secret": "secret"}, {}, "sha256=wrong", lambda: str(int(time.time())), HmacValidationError, None),
        ({"hmac_secret": "secret"}, {}, None, lambda: str(int(time.time())), HmacValidationError, None),
        ({}, {"dedup_exists": True}, None, lambda: str(int(time.time())), DuplicateWebhookError, None),
    ],
)
async def test_handle_webhook_validation_raises(
    trigger_overrides, session_overrides, hmac_sig, mod_ts_factory, expected_exc, extra_assert
) -> None:
    session_kwargs = dict(session_overrides)
    session_trigger = session_kwargs.pop("trigger", None)
    trigger = _make_trigger(**trigger_overrides)
    if session_trigger is None and "trigger" in session_overrides:
        session = _make_session(trigger=None, **session_kwargs)
        trigger_id = uuid.uuid4()
    else:
        session = _make_session(trigger=trigger, **session_kwargs)
        trigger_id = trigger.id
    mod_ts = mod_ts_factory() if callable(mod_ts_factory) else mod_ts_factory
    with pytest.raises(expected_exc) as exc_info:
        await TriggerEngine().handle_webhook(
            session,
            trigger_id=trigger_id,
            org_id=_ORG,
            raw_body=_RAW_BODY,
            raw_payload=_RAW_PAYLOAD,
            hmac_signature=hmac_sig,
            modulo_timestamp=mod_ts,
            snapshot_id=_SNAP,
        )
    if extra_assert is not None:
        assert extra_assert(exc_info)


@pytest.mark.parametrize(
    ("trigger_overrides", "session_overrides", "hmac_sig", "mod_ts_factory", "expected_exc", "expected_vr"),
    [
        ({}, {}, None, lambda: str(int(time.time()) - 600), TimestampExpiredError, "timestamp_expired"),
        (
            {"hmac_secret": "secret"},
            {},
            "sha256=bad",
            lambda: str(int(time.time())),
            HmacValidationError,
            "hmac_failed",
        ),
        ({}, {"dedup_exists": True}, None, lambda: str(int(time.time())), DuplicateWebhookError, "deduplicated"),
        (
            {"max_concurrent_runs": 1},
            {"active_run_count": 1},
            None,
            lambda: str(int(time.time())),
            None,  # no longer raises - run is queued
            "concurrency_limit_reached",
        ),
    ],
)
async def test_handle_webhook_logs_trigger_event(
    trigger_overrides, session_overrides, hmac_sig, mod_ts_factory, expected_exc, expected_vr
) -> None:
    trigger = _make_trigger(**trigger_overrides)
    session = _make_session(trigger=trigger, **session_overrides)
    mod_ts = mod_ts_factory() if callable(mod_ts_factory) else mod_ts_factory
    if expected_exc is not None:
        with pytest.raises(expected_exc):
            await TriggerEngine().handle_webhook(
                session,
                trigger_id=trigger.id,
                org_id=_ORG,
                raw_body=_RAW_BODY,
                raw_payload=_RAW_PAYLOAD,
                hmac_signature=hmac_sig,
                modulo_timestamp=mod_ts,
                snapshot_id=_SNAP,
            )
    else:
        await TriggerEngine().handle_webhook(
            session,
            trigger_id=trigger.id,
            org_id=_ORG,
            raw_body=_RAW_BODY,
            raw_payload=_RAW_PAYLOAD,
            hmac_signature=hmac_sig,
            modulo_timestamp=mod_ts,
            snapshot_id=_SNAP,
        )
    found = any(getattr(c[0][0], "validation_result", None) == expected_vr for c in session.add.call_args_list)
    assert found


async def test_handle_webhook_busy_lock_not_acquired() -> None:
    """When the advisory lock is already held, handle_webhook must raise TriggerBusyError."""
    session = _make_session(trigger=_make_trigger())
    lock_result = MagicMock()
    lock_result.scalar_one.return_value = False
    session.execute = AsyncMock(return_value=lock_result)

    with pytest.raises(TriggerBusyError):
        await TriggerEngine().handle_webhook(
            session,
            trigger_id=uuid.uuid4(),
            org_id=_ORG,
            raw_body=_RAW_BODY,
            raw_payload=_RAW_PAYLOAD,
            hmac_signature=None,
            modulo_timestamp=str(_VALID_TS),
            snapshot_id=_SNAP,
        )
    session.add.assert_not_called()


async def test_handle_webhook_event_type_not_accepted() -> None:
    """accepted_events configured but payload has no matching event -> RuntimeError + event logged."""
    trigger = _make_trigger(accepted_events=["pull_request"])
    session = _make_session(trigger=trigger, active_run_count=0)

    with (
        patch("modulo.core.trigger_engine.create_run", return_value=MagicMock(id=uuid.uuid4())),
        pytest.raises(RuntimeError, match="none of the accepted event types"),
    ):
        await TriggerEngine().handle_webhook(
            session,
            trigger_id=trigger.id,
            org_id=_ORG,
            raw_body=_RAW_BODY,
            raw_payload={"action": "opened"},
            hmac_signature=None,
            modulo_timestamp=str(_VALID_TS),
            snapshot_id=_SNAP,
        )

    assert any(
        getattr(c[0][0], "validation_result", None) == "event_type_not_accepted" for c in session.add.call_args_list
    )


async def test_handle_webhook_event_type_accepted_passes() -> None:
    """A matching accepted_events key passes validation and creates a run."""
    trigger = _make_trigger(accepted_events=["pull_request"])
    session = _make_session(trigger=trigger, active_run_count=0)
    raw_payload = {"action": "opened", "pull_request": {"number": 7}}

    run_mock = MagicMock(id=uuid.uuid4())
    with (
        patch("modulo.core.trigger_engine.create_run", return_value=run_mock),
        patch("modulo.core.trigger_engine.time.time", return_value=_VALID_TS),
    ):
        run, te, _ = await TriggerEngine().handle_webhook(
            session,
            trigger_id=trigger.id,
            org_id=_ORG,
            raw_body=b'{"action": "opened", "pull_request": {"number": 7}}',
            raw_payload=raw_payload,
            hmac_signature=None,
            modulo_timestamp=str(_VALID_TS),
            snapshot_id=_SNAP,
        )

    assert run is run_mock
    assert te.validation_result == "accepted"


# ---------------------------------------------------------------------------
# TriggerEngine._matches_event_filters — dotted-path value filtering
# ---------------------------------------------------------------------------


def test_matches_event_filters_helper() -> None:
    """The helper accepts matching values and rejects every non-match shape."""
    payload = {"review": {"state": "changes_requested"}, "action": "opened"}

    # Matching value at a dotted path.
    assert _matches_event_filters(payload, {"review.state": ["changes_requested", "commented"]})
    # Non-matching value rejects.
    assert not _matches_event_filters(payload, {"review.state": ["approved"]})
    # Missing key rejects (no crash).
    assert not _matches_event_filters(payload, {"review.author": ["octocat"]})
    # Missing top-level key rejects (no crash).
    assert not _matches_event_filters(payload, {"missing.key": ["x"]})
    # Non-dict intermediate value rejects gracefully.
    assert not _matches_event_filters(payload, {"review.state.value": ["x"]})
    # Non-list allowlist rejects (fail closed — never substring-matches a string).
    assert not _matches_event_filters(payload, {"review.state": "approved"})
    # Non-dict event_filters rejects (fail closed).
    assert not _matches_event_filters(payload, ["review.state"])
    assert not _matches_event_filters(payload, "review.state")
    # Every configured filter must match.
    assert _matches_event_filters(payload, {"review.state": ["changes_requested"], "action": ["opened"]})
    assert not _matches_event_filters(payload, {"review.state": ["changes_requested"], "action": ["closed"]})


# TriggerEngine.handle_webhook — value-based event filtering


async def test_handle_webhook_event_value_filter_rejects() -> None:
    """event_filters configured and payload value outside allowlist -> no run created."""
    trigger = _make_trigger(extra_config={"event_filters": {"review.state": ["changes_requested", "commented"]}})
    session = _make_session(trigger=trigger, active_run_count=0)

    with (
        patch("modulo.core.trigger_engine.create_run") as mock_create,
        patch("modulo.core.trigger_engine.TriggerEngine._try_insert_dedup") as mock_dedup,
        pytest.raises(RuntimeError, match="event value filters"),
    ):
        await TriggerEngine().handle_webhook(
            session,
            trigger_id=trigger.id,
            org_id=_ORG,
            raw_body=b'{"action": "submitted", "review": {"state": "approved"}}',
            raw_payload={"action": "submitted", "review": {"state": "approved"}},
            hmac_signature=None,
            modulo_timestamp=str(_VALID_TS),
            snapshot_id=_SNAP,
        )

    # Rejected before run creation and before dedup.
    mock_create.assert_not_called()
    mock_dedup.assert_not_called()
    assert any(
        getattr(c[0][0], "validation_result", None) == "event_type_not_accepted" for c in session.add.call_args_list
    )


async def test_handle_webhook_event_value_filter_missing_key_rejects() -> None:
    """event_filters path absent from the payload rejects without crashing, no run created."""
    trigger = _make_trigger(extra_config={"event_filters": {"review.state": ["changes_requested"]}})
    session = _make_session(trigger=trigger, active_run_count=0)

    with (
        patch("modulo.core.trigger_engine.create_run") as mock_create,
        pytest.raises(RuntimeError, match="event value filters"),
    ):
        await TriggerEngine().handle_webhook(
            session,
            trigger_id=trigger.id,
            org_id=_ORG,
            raw_body=b'{"action": "submitted"}',
            raw_payload={"action": "submitted"},
            hmac_signature=None,
            modulo_timestamp=str(_VALID_TS),
            snapshot_id=_SNAP,
        )

    mock_create.assert_not_called()
    assert any(
        getattr(c[0][0], "validation_result", None) == "event_type_not_accepted" for c in session.add.call_args_list
    )


async def test_handle_webhook_event_value_filter_matches_passes() -> None:
    """event_filters with a matching payload value passes and creates a run."""
    trigger = _make_trigger(extra_config={"event_filters": {"review.state": ["changes_requested", "commented"]}})
    session = _make_session(trigger=trigger, active_run_count=0)
    raw_payload = {"action": "submitted", "review": {"state": "changes_requested"}}

    run_mock = MagicMock(id=uuid.uuid4())
    with (
        patch("modulo.core.trigger_engine.create_run", return_value=run_mock),
        patch("modulo.core.trigger_engine.time.time", return_value=_VALID_TS),
    ):
        run, te, _ = await TriggerEngine().handle_webhook(
            session,
            trigger_id=trigger.id,
            org_id=_ORG,
            raw_body=b'{"action": "submitted", "review": {"state": "changes_requested"}}',
            raw_payload=raw_payload,
            hmac_signature=None,
            modulo_timestamp=str(_VALID_TS),
            snapshot_id=_SNAP,
        )

    assert run is run_mock
    assert te.validation_result == "accepted"


async def test_handle_webhook_event_value_filter_absent_unchanged() -> None:
    """No event_filters configured -> presence-only accepted_events behaviour is unchanged."""
    trigger = _make_trigger(accepted_events=["pull_request"])
    session = _make_session(trigger=trigger, active_run_count=0)
    raw_payload = {"action": "opened", "pull_request": {"number": 7}}

    run_mock = MagicMock(id=uuid.uuid4())
    with (
        patch("modulo.core.trigger_engine.create_run", return_value=run_mock),
        patch("modulo.core.trigger_engine.time.time", return_value=_VALID_TS),
    ):
        run, te, _ = await TriggerEngine().handle_webhook(
            session,
            trigger_id=trigger.id,
            org_id=_ORG,
            raw_body=b'{"action": "opened", "pull_request": {"number": 7}}',
            raw_payload=raw_payload,
            hmac_signature=None,
            modulo_timestamp=str(_VALID_TS),
            snapshot_id=_SNAP,
        )

    assert run is run_mock
    assert te.validation_result == "accepted"


async def test_handle_webhook_rate_limit_exceeded() -> None:
    """Pipeline rate limit exceeded -> PipelineRateLimitError + rate_limited event logged.

    The rate_limited event is a POST-guardrail event (the pass ran and the dedup
    slot was consumed before the rate-limit check), so its raw_payload_hash is
    the canonical POST-guardrail payload hash — not the raw-body hash (FAR-214)."""
    trigger = _make_trigger()
    session = _make_session(
        trigger=trigger,
        active_run_count=0,
        pipeline_rate_limit={"max_triggers": 1, "window_seconds": 3600},
        recent_run_count=1,
    )

    with (
        patch("modulo.core.trigger_engine.create_run", return_value=MagicMock(id=uuid.uuid4())),
        pytest.raises(PipelineRateLimitError) as exc_info,
    ):
        await TriggerEngine().handle_webhook(
            session,
            trigger_id=trigger.id,
            org_id=_ORG,
            raw_body=_RAW_BODY,
            raw_payload=_RAW_PAYLOAD,
            hmac_signature=None,
            modulo_timestamp=str(_VALID_TS),
            snapshot_id=_SNAP,
        )

    assert exc_info.value.pipeline_id == trigger.pipeline_id
    assert exc_info.value.max_triggers == 1
    assert exc_info.value.window_seconds == 3600
    rate_limited = [
        c[0][0] for c in session.add.call_args_list if getattr(c[0][0], "validation_result", None) == "rate_limited"
    ]
    assert len(rate_limited) == 1
    from modulo.core.trigger_engine.pre_guardrail import canonical_payload_hash

    assert rate_limited[0].raw_payload_hash == canonical_payload_hash(_RAW_PAYLOAD)
    assert rate_limited[0].raw_payload_hash != sha256_hex(_RAW_BODY)


async def test_handle_webhook_rate_limit_pass_through_sets_key() -> None:
    """Rate limit not exceeded -> run created with rate_limit_key from key_fields."""
    trigger = _make_trigger()
    raw_payload = {"repo": "acme/app", "action": "opened"}
    session = _make_session(
        trigger=trigger,
        active_run_count=0,
        pipeline_rate_limit={
            "max_triggers": 10,
            "window_seconds": 3600,
            "key_fields": ["repo"],
            "match_mode": "exact",
        },
        recent_run_count=1,
    )

    with (
        patch("modulo.core.trigger_engine.create_run", return_value=MagicMock(id=uuid.uuid4())) as mock_create,
        patch("modulo.core.trigger_engine.time.time", return_value=_VALID_TS),
    ):
        await TriggerEngine().handle_webhook(
            session,
            trigger_id=trigger.id,
            org_id=_ORG,
            raw_body=_RAW_BODY,
            raw_payload=raw_payload,
            hmac_signature=None,
            modulo_timestamp=str(_VALID_TS),
            snapshot_id=_SNAP,
        )

    assert mock_create.await_count == 1
    assert mock_create.call_args.kwargs["rate_limit_key"] == '{"repo": "acme/app"}'


async def test_handle_webhook_rate_limit_from_trigger_config() -> None:
    """Rate limit may live on the trigger's ``config_json`` itself (not the
    pipeline) — that path must apply it without a pipeline lookup."""
    trigger = _make_trigger(extra_config={"rate_limit": {"max_triggers": 1, "window_seconds": 300}})
    session = _make_session(
        trigger=trigger,
        active_run_count=0,
        pipeline_rate_limit=None,
        recent_run_count=1,
    )

    with (
        patch("modulo.core.trigger_engine.create_run", return_value=MagicMock(id=uuid.uuid4())),
        pytest.raises(PipelineRateLimitError) as exc_info,
    ):
        await TriggerEngine().handle_webhook(
            session,
            trigger_id=trigger.id,
            org_id=_ORG,
            raw_body=_RAW_BODY,
            raw_payload=_RAW_PAYLOAD,
            hmac_signature=None,
            modulo_timestamp=str(_VALID_TS),
            snapshot_id=_SNAP,
        )

    assert exc_info.value.pipeline_id == trigger.pipeline_id
    assert exc_info.value.max_triggers == 1
    assert exc_info.value.window_seconds == 300


async def test_handle_webhook_pipeline_not_found_skips_rate_limit() -> None:
    """A missing pipeline row (deleted while the trigger lived on) must not
    blow up the webhook — the rate-limit check is simply skipped."""
    trigger = _make_trigger()
    session = _make_session(
        trigger=trigger,
        active_run_count=0,
        pipeline_rate_limit=None,
        recent_run_count=1,
        pipeline_found=False,
    )

    with (
        patch("modulo.core.trigger_engine.create_run", return_value=MagicMock(id=uuid.uuid4())) as mock_create,
        patch("modulo.core.trigger_engine.time.time", return_value=_VALID_TS),
    ):
        await TriggerEngine().handle_webhook(
            session,
            trigger_id=trigger.id,
            org_id=_ORG,
            raw_body=_RAW_BODY,
            raw_payload=_RAW_PAYLOAD,
            hmac_signature=None,
            modulo_timestamp=str(_VALID_TS),
            snapshot_id=_SNAP,
        )

    assert mock_create.await_count == 1
    assert mock_create.call_args.kwargs["rate_limit_key"] is None


async def test_handle_webhook_paused_org_raises_and_writes_no_dedup() -> None:
    """Org-wide pause: handle_webhook raises TriggersPausedError and does NOT
    attempt the dedup insert (no add, no run, no accepted event)."""
    from modulo.core.exceptions import TriggersPausedError

    trigger = _make_trigger()
    session = _make_session(trigger=trigger, active_run_count=0)

    with (
        patch("modulo.db.settings_resolver.org_is_paused", new_callable=AsyncMock, return_value=True),
        patch("modulo.core.trigger_engine.time.time", return_value=_VALID_TS),
        pytest.raises(TriggersPausedError) as exc_info,
    ):
        await TriggerEngine().handle_webhook(
            session,
            trigger_id=trigger.id,
            org_id=_ORG,
            raw_body=_RAW_BODY,
            raw_payload=_RAW_PAYLOAD,
            hmac_signature=None,
            modulo_timestamp=str(_VALID_TS),
            snapshot_id=_SNAP,
        )

    assert exc_info.value.org_id == _ORG
    assert exc_info.value.trigger_id == trigger.id
    assert exc_info.value.trigger_type == "webhook"
    session.add.assert_not_called()


# ---------------------------------------------------------------------------
# TriggerEngine.replay_event — unit tests
# ---------------------------------------------------------------------------


def _make_replay_session(
    *,
    event: MagicMock | None = None,
    trigger: MagicMock | None = None,
    stored_payload: MagicMock | None = None,
    active_run_count: int = 0,
    lock_acquired: bool = True,
    pipeline_rate_limit: dict[str, Any] | None = None,
    recent_run_count: int = 0,
    pipeline_found: bool = True,
) -> AsyncMock:
    """Build a mocked session for replay_event's query order.

    Query order: 1=TriggerEvent lookup, 2=advisory lock, 3=Trigger lookup,
    4=WebhookPayload lookup, 5=guardrail rows (FAR-214, detection-only),
    6=active-run count, 7=pipeline lookup, 8=recent rate-limited count, 9+=other.
    """
    session = AsyncMock()

    event_result = MagicMock()
    event_result.scalar_one_or_none.return_value = event

    lock_result = MagicMock()
    lock_result.scalar_one.return_value = lock_acquired

    trigger_result = MagicMock()
    trigger_result.scalar_one_or_none.return_value = trigger

    payload_result = MagicMock()
    payload_result.scalar_one_or_none.return_value = stored_payload

    # Pre-trigger guardrail pass on replay (FAR-214) — none bound by default.
    guardrail_result = MagicMock()
    guardrail_result.scalars.return_value.all.return_value = []

    count_result = MagicMock()
    count_result.scalar_one.return_value = active_run_count

    recent_count_result = MagicMock()
    recent_count_result.scalar_one.return_value = recent_run_count

    pipeline_result = MagicMock()
    pipeline_result.scalar_one_or_none.return_value = MagicMock() if pipeline_found else None
    if pipeline_found:
        pipeline_result.scalar_one_or_none.return_value.rate_limit_config = pipeline_rate_limit

    call_count = 0

    async def _execute(stmt: Any, *args: Any, **kwargs: Any) -> Any:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return event_result
        if call_count == 2:
            return lock_result
        if call_count == 3:
            return trigger_result
        if call_count == 4:
            return payload_result
        if call_count == 5:
            return guardrail_result
        if call_count == 6:
            return count_result
        if call_count == 7:
            return pipeline_result
        if call_count == 8:
            return recent_count_result
        return pipeline_result

    session.execute = _execute
    session.add = MagicMock()
    session.flush = AsyncMock()
    return session


def _make_stored_payload(**overrides: Any) -> MagicMock:
    stored = MagicMock()
    stored.raw_body = overrides.get("raw_body", _RAW_BODY)
    stored.raw_payload = overrides.get("raw_payload", _RAW_PAYLOAD)
    return stored


async def test_replay_event_success() -> None:
    trigger = _make_trigger()
    event = MagicMock()
    event.id = uuid.uuid4()
    event.trigger_id = trigger.id
    session = _make_replay_session(
        event=event,
        trigger=trigger,
        stored_payload=_make_stored_payload(),
        active_run_count=0,
    )

    run_mock = MagicMock(id=uuid.uuid4())
    with patch("modulo.core.trigger_engine.create_run", return_value=run_mock):
        run, te, input_payload = await TriggerEngine().replay_event(
            session,
            event_id=event.id,
            org_id=_ORG,
            snapshot_id=_SNAP,
        )

    assert run is run_mock
    assert te.validation_result == "accepted"
    assert input_payload == _RAW_PAYLOAD
    assert any(getattr(c[0][0], "validation_result", None) == "accepted" for c in session.add.call_args_list)


async def test_replay_event_event_not_found() -> None:
    session = _make_replay_session(event=None, trigger=_make_trigger())
    with pytest.raises(ReplayNotFoundError):
        await TriggerEngine().replay_event(
            session,
            event_id=uuid.uuid4(),
            org_id=_ORG,
            snapshot_id=_SNAP,
        )


async def test_replay_event_trigger_not_found() -> None:
    event = MagicMock()
    event.id = uuid.uuid4()
    event.trigger_id = uuid.uuid4()
    session = _make_replay_session(event=event, trigger=None)
    with pytest.raises(TriggerNotFoundError):
        await TriggerEngine().replay_event(
            session,
            event_id=event.id,
            org_id=_ORG,
            snapshot_id=_SNAP,
        )


async def test_replay_event_trigger_inactive() -> None:
    trigger = _make_trigger(active=False)
    event = MagicMock()
    event.id = uuid.uuid4()
    event.trigger_id = trigger.id
    session = _make_replay_session(event=event, trigger=trigger, stored_payload=_make_stored_payload())
    with pytest.raises(TriggerInactiveError):
        await TriggerEngine().replay_event(
            session,
            event_id=event.id,
            org_id=_ORG,
            snapshot_id=_SNAP,
        )


async def test_replay_event_stored_payload_missing() -> None:
    trigger = _make_trigger()
    event = MagicMock()
    event.id = uuid.uuid4()
    event.trigger_id = trigger.id
    session = _make_replay_session(event=event, trigger=trigger, stored_payload=None)
    with pytest.raises(ReplayNotFoundError):
        await TriggerEngine().replay_event(
            session,
            event_id=event.id,
            org_id=_ORG,
            snapshot_id=_SNAP,
        )


async def test_replay_event_busy_lock_not_acquired() -> None:
    trigger = _make_trigger()
    event = MagicMock()
    event.id = uuid.uuid4()
    event.trigger_id = trigger.id
    session = _make_replay_session(event=event, trigger=trigger, lock_acquired=False)
    with pytest.raises(TriggerBusyError):
        await TriggerEngine().replay_event(
            session,
            event_id=event.id,
            org_id=_ORG,
            snapshot_id=_SNAP,
        )


async def test_replay_event_concurrency_limit() -> None:
    trigger = _make_trigger(max_concurrent_runs=2)
    event = MagicMock()
    event.id = uuid.uuid4()
    event.trigger_id = trigger.id
    session = _make_replay_session(
        event=event,
        trigger=trigger,
        stored_payload=_make_stored_payload(),
        active_run_count=2,
    )
    with pytest.raises(ConcurrentRunLimitError):
        await TriggerEngine().replay_event(
            session,
            event_id=event.id,
            org_id=_ORG,
            snapshot_id=_SNAP,
        )


async def test_replay_event_event_type_not_accepted() -> None:
    trigger = _make_trigger(accepted_events=["pull_request"])
    event = MagicMock()
    event.id = uuid.uuid4()
    event.trigger_id = trigger.id
    session = _make_replay_session(
        event=event,
        trigger=trigger,
        stored_payload=_make_stored_payload(raw_payload={"action": "opened"}),
        active_run_count=0,
    )
    with pytest.raises(RuntimeError, match="none of the accepted event types"):
        await TriggerEngine().replay_event(
            session,
            event_id=event.id,
            org_id=_ORG,
            snapshot_id=_SNAP,
        )


async def test_replay_event_value_filter_not_accepted() -> None:
    """A replayed payload outside the event_filters allowlist is rejected."""
    trigger = _make_trigger(extra_config={"event_filters": {"review.state": ["changes_requested", "commented"]}})
    event = MagicMock()
    event.id = uuid.uuid4()
    event.trigger_id = trigger.id
    session = _make_replay_session(
        event=event,
        trigger=trigger,
        stored_payload=_make_stored_payload(raw_payload={"action": "submitted", "review": {"state": "approved"}}),
        active_run_count=0,
    )
    with (
        patch("modulo.core.trigger_engine.create_run") as mock_create,
        pytest.raises(RuntimeError, match="event value filters"),
    ):
        await TriggerEngine().replay_event(
            session,
            event_id=event.id,
            org_id=_ORG,
            snapshot_id=_SNAP,
        )
    mock_create.assert_not_called()
    assert any(
        getattr(c[0][0], "validation_result", None) == "event_type_not_accepted" for c in session.add.call_args_list
    )


async def test_replay_event_accepted_event_present_passes() -> None:
    """When the replayed payload carries an accepted event dict, the event-type
    gate falls through and the run is created."""
    trigger = _make_trigger(accepted_events=["pull_request"])
    event = MagicMock()
    event.id = uuid.uuid4()
    event.trigger_id = trigger.id
    session = _make_replay_session(
        event=event,
        trigger=trigger,
        stored_payload=_make_stored_payload(raw_payload={"pull_request": {"number": 42}}),
        active_run_count=0,
    )

    run_mock = MagicMock(id=uuid.uuid4())
    with patch("modulo.core.trigger_engine.create_run", return_value=run_mock):
        run, te, input_payload = await TriggerEngine().replay_event(
            session,
            event_id=event.id,
            org_id=_ORG,
            snapshot_id=_SNAP,
        )

    assert run is run_mock
    assert te.validation_result == "accepted"
    assert input_payload == {"pull_request": {"number": 42}}


async def test_replay_event_rate_limit_exceeded() -> None:
    trigger = _make_trigger()
    event = MagicMock()
    event.id = uuid.uuid4()
    event.trigger_id = trigger.id
    session = _make_replay_session(
        event=event,
        trigger=trigger,
        stored_payload=_make_stored_payload(),
        active_run_count=0,
        pipeline_rate_limit={"max_triggers": 1, "window_seconds": 3600},
        recent_run_count=1,
    )
    with pytest.raises(PipelineRateLimitError):
        await TriggerEngine().replay_event(
            session,
            event_id=event.id,
            org_id=_ORG,
            snapshot_id=_SNAP,
        )


async def test_replay_event_rate_limit_from_trigger_config() -> None:
    """A rate limit carried on the trigger's ``config_json`` (rather than the
    pipeline row) applies on replay too."""
    trigger = _make_trigger(extra_config={"rate_limit": {"max_triggers": 1, "window_seconds": 300}})
    event = MagicMock()
    event.id = uuid.uuid4()
    event.trigger_id = trigger.id
    session = _make_replay_session(
        event=event,
        trigger=trigger,
        stored_payload=_make_stored_payload(),
        active_run_count=0,
        pipeline_rate_limit=None,
        recent_run_count=1,
    )
    with pytest.raises(PipelineRateLimitError):
        await TriggerEngine().replay_event(
            session,
            event_id=event.id,
            org_id=_ORG,
            snapshot_id=_SNAP,
        )


async def test_replay_event_pipeline_not_found_skips_rate_limit() -> None:
    """A missing pipeline row must not abort a replay — rate limiting is
    skipped and the run is created with no rate_limit_key."""
    trigger = _make_trigger()
    event = MagicMock()
    event.id = uuid.uuid4()
    event.trigger_id = trigger.id
    session = _make_replay_session(
        event=event,
        trigger=trigger,
        stored_payload=_make_stored_payload(),
        active_run_count=0,
        pipeline_rate_limit=None,
        recent_run_count=1,
        pipeline_found=False,
    )

    run_mock = MagicMock(id=uuid.uuid4())
    with patch("modulo.core.trigger_engine.create_run", return_value=run_mock) as mock_create:
        run, te, _ = await TriggerEngine().replay_event(
            session,
            event_id=event.id,
            org_id=_ORG,
            snapshot_id=_SNAP,
        )

    assert run is run_mock
    assert te.validation_result == "accepted"
    assert mock_create.call_args.kwargs["rate_limit_key"] is None


async def test_replay_event_rate_limit_not_exceeded_passes() -> None:
    """A configured rate limit that is NOT yet hit lets the replay through
    with the computed key on the created run."""
    trigger = _make_trigger()
    event = MagicMock()
    event.id = uuid.uuid4()
    event.trigger_id = trigger.id

    raw_payload = {"repo": "acme/app", "action": "opened"}
    session = _make_replay_session(
        event=event,
        trigger=trigger,
        stored_payload=_make_stored_payload(raw_payload=raw_payload),
        active_run_count=0,
        pipeline_rate_limit={
            "max_triggers": 10,
            "window_seconds": 3600,
            "key_fields": ["repo"],
            "match_mode": "exact",
        },
        recent_run_count=1,
    )

    run_mock = MagicMock(id=uuid.uuid4())
    with patch("modulo.core.trigger_engine.create_run", return_value=run_mock) as mock_create:
        run, te, _ = await TriggerEngine().replay_event(
            session,
            event_id=event.id,
            org_id=_ORG,
            snapshot_id=_SNAP,
        )

    assert run is run_mock
    assert te.validation_result == "accepted"
    assert mock_create.call_args.kwargs["rate_limit_key"] == '{"repo": "acme/app"}'


async def test_replay_event_paused_org_raises() -> None:
    """Org-wide pause: replay_event raises TriggersPausedError and writes nothing."""
    from modulo.core.exceptions import TriggersPausedError

    trigger = _make_trigger()
    event = MagicMock()
    event.id = uuid.uuid4()
    event.trigger_id = trigger.id
    session = _make_replay_session(
        event=event,
        trigger=trigger,
        stored_payload=_make_stored_payload(),
        active_run_count=0,
    )

    with (
        patch("modulo.db.settings_resolver.org_is_paused", new_callable=AsyncMock, return_value=True),
        pytest.raises(TriggersPausedError) as exc_info,
    ):
        await TriggerEngine().replay_event(
            session,
            event_id=event.id,
            org_id=_ORG,
            snapshot_id=_SNAP,
        )

    assert exc_info.value.org_id == _ORG
    assert exc_info.value.trigger_id == event.trigger_id
    assert exc_info.value.trigger_type == "webhook"
    session.add.assert_not_called()


# ---------------------------------------------------------------------------
# TriggerEngine.schedule_polling_trigger
# ---------------------------------------------------------------------------


async def test_schedule_polling_trigger_default_interval() -> None:
    trigger = _make_trigger()
    session = AsyncMock()
    session.flush = AsyncMock()

    await TriggerEngine().schedule_polling_trigger(session, trigger=trigger, org_id=_ORG)

    assert trigger.next_fire_at is not None
    delta = (trigger.next_fire_at - datetime.datetime.now(datetime.UTC)).total_seconds()
    assert 55 <= delta <= 65
    session.flush.assert_awaited_once()


async def test_schedule_polling_trigger_custom_interval() -> None:
    trigger = _make_trigger(extra_config={"poll_interval_seconds": 120})
    session = AsyncMock()
    session.flush = AsyncMock()

    await TriggerEngine().schedule_polling_trigger(session, trigger=trigger, org_id=_ORG)

    delta = (trigger.next_fire_at - datetime.datetime.now(datetime.UTC)).total_seconds()
    assert 115 <= delta <= 125


@pytest.mark.parametrize("bad_interval", [0, -5, "10"])
async def test_schedule_polling_trigger_invalid_interval(bad_interval: Any) -> None:
    trigger = _make_trigger(extra_config={"poll_interval_seconds": bad_interval})
    with pytest.raises(ValueError, match="poll_interval_seconds must be >= 1"):
        await TriggerEngine().schedule_polling_trigger(AsyncMock(), trigger=trigger, org_id=_ORG)


async def test_schedule_polling_trigger_none_interval_defaults() -> None:
    trigger = _make_trigger(extra_config={"poll_interval_seconds": None})
    session = AsyncMock()
    session.flush = AsyncMock()

    await TriggerEngine().schedule_polling_trigger(session, trigger=trigger, org_id=_ORG)

    assert trigger.next_fire_at is not None
    session.flush.assert_awaited_once()


# ---------------------------------------------------------------------------
# TriggerEngine.cleanup_expired_payloads
# ---------------------------------------------------------------------------


async def test_cleanup_expired_payloads_deletes_expired() -> None:
    session = AsyncMock()
    expired_result = MagicMock()
    expired_result.scalars.return_value.all.return_value = [uuid.uuid4(), uuid.uuid4()]
    session.execute = AsyncMock(return_value=expired_result)

    count = await TriggerEngine.cleanup_expired_payloads(session)
    assert count == 2
    assert session.execute.await_count == 2  # select then delete


async def test_cleanup_expired_payloads_none_expired() -> None:
    session = AsyncMock()
    empty_result = MagicMock()
    empty_result.scalars.return_value.all.return_value = []
    session.execute = AsyncMock(return_value=empty_result)

    count = await TriggerEngine.cleanup_expired_payloads(session)
    assert count == 0
    assert session.execute.await_count == 1  # only the select runs


# ---------------------------------------------------------------------------
# Webhook API route — integration smoke tests using TestClient
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("patch_target", "mock_factory", "expected_status", "expected_body", "extra_headers", "body_kwargs"),
    [
        (
            "handle_webhook",
            lambda tid, eid: AsyncMock(side_effect=TriggerNotFoundError(tid)),
            404,
            {"detail": "Trigger not found"},
            {},
            {"json": {"action": "ping"}},
        ),
        (
            "handle_webhook",
            lambda: AsyncMock(side_effect=TimestampExpiredError()),
            400,
            {"detail": "X-Modulo-Timestamp is outside the ±300s replay window"},
            {},
            {"json": {"action": "push"}},
        ),
        (
            "handle_webhook",
            lambda: AsyncMock(side_effect=HmacValidationError()),
            401,
            {"detail": "HMAC signature verification failed"},
            {"X-Modulo-Webhook-Secret": "sha256=bad"},
            {"json": {"action": "push"}},
        ),
        (
            "handle_webhook",
            lambda: AsyncMock(side_effect=DuplicateWebhookError("abc123")),
            400,
            {"detail": "Duplicate webhook payload"},
            {},
            {"json": {"action": "push"}},
        ),
        (
            "handle_webhook",
            lambda tid, eid: AsyncMock(side_effect=ConcurrentRunLimitError(tid, 3)),
            429,
            {"detail": "Concurrent run limit of 3 reached"},
            {},
            {"json": {"action": "push"}},
        ),
        (
            None,
            None,
            400,
            {"detail": "Request body must be a JSON object"},
            {"Content-Type": "application/json"},
            {"content": b"not-json"},
        ),
        (
            "handle_webhook",
            lambda: AsyncMock(return_value=(MagicMock(id=uuid.uuid4()), MagicMock(), {"key": "val"})),
            202,
            {"status": "accepted"},
            {},
            {"json": {"action": "push"}},
        ),
        (
            "replay_event",
            lambda: AsyncMock(return_value=(MagicMock(id=uuid.uuid4()), MagicMock(), {"key": "val"})),
            202,
            {"status": "accepted"},
            _replay_auth_headers,
            {},
        ),
        (
            "replay_event",
            lambda tid, eid: AsyncMock(side_effect=ReplayNotFoundError(eid)),
            404,
            {"detail": "Trigger event not found"},
            _replay_auth_headers,
            {},
        ),
    ],
)
def test_webhook_route(
    webhook_client: TestClient,
    patch_target,
    mock_factory,
    expected_status,
    expected_body,
    extra_headers,
    body_kwargs,
) -> None:
    tid = uuid.uuid4()
    eid = uuid.uuid4() if patch_target == "replay_event" else None

    # ``extra_headers`` may be a factory (callable) so the replay JWT is minted
    # at request time — a token created during collection would expire after the
    # 15-minute access-token TTL during long full-suite runs, making the route
    # treat the caller as unauthenticated and fail the expected status.
    if callable(extra_headers):
        extra_headers = extra_headers()

    if "content" in body_kwargs:
        headers = {"X-Modulo-Timestamp": str(int(time.time()))} | extra_headers
        request_kwargs = {"content": body_kwargs["content"], "headers": headers}
    elif "json" in body_kwargs:
        headers = {"X-Modulo-Timestamp": str(int(time.time()))} | extra_headers
        request_kwargs = {"json": body_kwargs["json"], "headers": headers}
    else:
        request_kwargs = {"headers": dict(extra_headers)}

    url_suffix = f"/replay/{eid}" if eid else ""

    if patch_target is None:
        resp = webhook_client.post(f"/api/v1/triggers/{tid}/webhook{url_suffix}", **request_kwargs)
    else:
        nargs = mock_factory.__code__.co_argcount
        mock_obj = mock_factory(tid, eid) if nargs == 2 else mock_factory()
        with patch(f"modulo.api.routes.webhooks._trigger_engine.{patch_target}", new=mock_obj):
            resp = webhook_client.post(f"/api/v1/triggers/{tid}/webhook{url_suffix}", **request_kwargs)

    assert resp.status_code == expected_status
    body = resp.json()
    assert expected_body.items() <= body.items()
    if expected_status == 202:
        uuid.UUID(body["run_id"])  # success responses must carry a real run id


# ---------------------------------------------------------------------------
# Cleanup
# ---------------------------------------------------------------------------


async def test_cleanup_expired_dedup_hashes() -> None:
    session = _make_session(trigger=_make_trigger())

    lock_result = MagicMock()
    lock_result.scalar_one.return_value = True

    expired_result = MagicMock()
    expired_result.scalars.return_value.all.return_value = [uuid.uuid4(), uuid.uuid4()]

    call_count = 0

    async def _execute(stmt: Any, *args: Any, **kwargs: Any) -> Any:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return lock_result
        return expired_result

    session.execute = _execute

    count = await TriggerEngine.cleanup_expired_dedup_hashes(session)
    assert count == 2


async def test_cleanup_expired_dedup_hashes_lock_contention() -> None:
    session = _make_session(trigger=_make_trigger())

    lock_result = MagicMock()
    lock_result.scalar_one.return_value = False

    async def _execute(stmt: Any, *args: Any, **kwargs: Any) -> Any:
        return lock_result

    session.execute = _execute

    count = await TriggerEngine.cleanup_expired_dedup_hashes(session)
    assert count == 0


async def test_cleanup_expired_dedup_hashes_skips_lock_on_non_postgres() -> None:
    """Non-Postgres backends skip the advisory lock and go straight to the select."""
    session = _make_session(trigger=_make_trigger())
    bind_mock = MagicMock()
    bind_mock.dialect.name = "sqlite"
    session.get_bind = MagicMock(return_value=bind_mock)

    expired_result = MagicMock()
    expired_result.scalars.return_value.all.return_value = [uuid.uuid4()]

    executed: list[str] = []

    async def _capture(stmt: Any, *args: Any, **kwargs: Any) -> Any:
        executed.append(str(stmt).lower())
        return expired_result

    session.execute = _capture

    count = await TriggerEngine.cleanup_expired_dedup_hashes(session)

    assert count == 1
    assert executed
    assert all("pg_try_advisory_xact_lock" not in s for s in executed)


async def test_cleanup_expired_dedup_hashes_none_expired() -> None:
    """No expired hashes means the DELETE is skipped and 0 is returned."""
    session = _make_session(trigger=_make_trigger())

    lock_result = MagicMock()
    lock_result.scalar_one.return_value = True

    empty_result = MagicMock()
    empty_result.scalars.return_value.all.return_value = []

    call_count = 0

    async def _execute(stmt: Any, *args: Any, **kwargs: Any) -> Any:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return lock_result
        return empty_result

    session.execute = _execute

    count = await TriggerEngine.cleanup_expired_dedup_hashes(session)
    assert count == 0
    assert call_count == 2  # lock + select, no delete


# ---------------------------------------------------------------------------
# agent_signal — org-wide pause early check (M10)
# ---------------------------------------------------------------------------


def _make_signal_trigger(*, source_node_id: str = "my-node", snapshot_id: str | None = None) -> MagicMock:
    t = MagicMock(spec=Trigger)
    t.id = uuid.uuid4()
    t.pipeline_id = uuid.uuid4()
    t.organisation_id = _ORG
    t.active = True
    t.max_concurrent_runs = 5
    t.config_json = {
        "source_pipeline_id": str(uuid.uuid4()),
        "source_node_id": source_node_id,
        "snapshot_id": snapshot_id,
    }
    return t


class TestAgentSignalPauseCheck:
    @pytest.mark.asyncio
    async def test_paused_org_skips_before_concurrency_and_snapshot(self) -> None:
        """Paused org: fire_agent_signal records exactly ONE paused event and
        skips the trigger BEFORE the concurrency check / snapshot resolution —
        no wasted snapshot work, no concurrency_limit_reached / invalid_snapshot_id."""
        from modulo.core.trigger_engine.agent_signal import fire_agent_signal

        trigger = _make_signal_trigger()
        session = MagicMock()
        session.add = MagicMock()
        session.flush = AsyncMock()

        trigger_result = MagicMock()
        trigger_result.scalars.return_value.all.return_value = [trigger]

        async def _execute(*args: object, **kwargs: object) -> MagicMock:
            return trigger_result

        session.execute = _execute

        with patch(
            "modulo.core.trigger_engine.agent_signal.org_is_paused",
            new_callable=AsyncMock,
            return_value=True,
        ):
            results = await fire_agent_signal(
                session,
                org_id=_ORG,
                source_run_id=uuid.uuid4(),
                source_pipeline_id=trigger.config_json["source_pipeline_id"],
                completed_node_id="my-node",
                node_output={"result": "ok"},
            )

        assert len(results) == 1
        assert results[0]["status"] == "skipped"
        assert results[0]["reason"] == "triggers_paused"
        # Exactly one paused event written via _log_signal_event (add + flush).
        assert session.add.call_count == 1

    @pytest.mark.asyncio
    async def test_not_paused_org_still_fires(self) -> None:
        """A not-paused org fires normally — the early check is a skip, not a
        block, and the create_run gate stays the TOCTOU backstop."""
        from modulo.core.trigger_engine.agent_signal import fire_agent_signal

        trigger = _make_signal_trigger(snapshot_id=str(uuid.uuid4()))
        session = MagicMock()
        session.add = MagicMock()
        session.flush = AsyncMock()

        trigger_result = MagicMock()
        trigger_result.scalars.return_value.all.return_value = [trigger]
        count_result = MagicMock()
        count_result.scalar_one.return_value = 0

        async def _execute(*args: object, **kwargs: object) -> MagicMock:
            sql = str(args[0]) if args else ""
            if "count(" in sql:
                return count_result
            return trigger_result

        session.execute = _execute

        with (
            patch(
                "modulo.core.trigger_engine.agent_signal.org_is_paused",
                new_callable=AsyncMock,
                return_value=False,
            ),
            patch("modulo.core.trigger_engine.agent_signal.create_run", new_callable=AsyncMock) as create_run,
        ):
            run_mock = MagicMock(id=uuid.uuid4())
            create_run.return_value = run_mock
            results = await fire_agent_signal(
                session,
                org_id=_ORG,
                source_run_id=uuid.uuid4(),
                source_pipeline_id=trigger.config_json["source_pipeline_id"],
                completed_node_id="my-node",
                node_output={"result": "ok"},
            )

        assert len(results) == 1
        assert results[0]["status"] == "fired"
        assert results[0]["run_id"] == str(run_mock.id)
