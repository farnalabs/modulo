"""Tests for ErrorIngestionService, schemas, session key store, and rate limiting."""

from __future__ import annotations

import hashlib
import hmac
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic import ValidationError

from modulo.api.middleware.rate_limiter import RateLimitMiddleware
from modulo.api.models.error import ErrorEventInput, ErrorGroupResult, ErrorIngestRequest, ErrorIngestResponse
from modulo.core.error_tracking import ErrorIngestionService, SessionKeyStore

_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")


# =========================================================================
# Fingerprint
# =========================================================================


class TestFingerprint:
    def test_same_input_same_hash(self) -> None:
        svc = ErrorIngestionService()
        fp1 = svc.fingerprint(message="test error", stacktrace=None, source="backend")
        fp2 = svc.fingerprint(message="test error", stacktrace=None, source="backend")
        assert fp1 == fp2
        assert len(fp1) == 64

    def test_different_message_different_hash(self) -> None:
        svc = ErrorIngestionService()
        fp1 = svc.fingerprint(message="error one", stacktrace=None, source="backend")
        fp2 = svc.fingerprint(message="error two", stacktrace=None, source="backend")
        assert fp1 != fp2

    def test_different_source_different_hash(self) -> None:
        svc = ErrorIngestionService()
        fp1 = svc.fingerprint(message="test", stacktrace=None, source="backend")
        fp2 = svc.fingerprint(message="test", stacktrace=None, source="frontend")
        assert fp1 != fp2

    def test_stacktrace_affects_hash(self) -> None:
        svc = ErrorIngestionService()
        fp1 = svc.fingerprint(message="test", stacktrace="line 1\nline 2", source="backend")
        fp2 = svc.fingerprint(message="test", stacktrace=None, source="backend")
        assert fp1 != fp2

    def test_fingerprint_hex_length(self) -> None:
        svc = ErrorIngestionService()
        fp = svc.fingerprint(message="x", source="celery")
        assert len(fp) == 64

    def test_fingerprint_deterministic_with_stacktrace(self) -> None:
        svc = ErrorIngestionService()
        st = """Traceback (most recent call last):
  File "/app/main.py", line 42, in handler
    result = process(data)
  File "/app/processor.py", line 15, in process
    raise ValueError("invalid")"""
        fp1 = svc.fingerprint(message="error", stacktrace=st, source="backend")
        fp2 = svc.fingerprint(message="error", stacktrace=st, source="backend")
        assert fp1 == fp2


class TestFingerprintNormalization:
    def test_top_5_frames_only(self) -> None:
        svc = ErrorIngestionService()
        long_st = "\n".join(f"line {i}" for i in range(20))
        fp_long = svc.fingerprint(message="x", stacktrace=long_st, source="x")
        short_st = "\n".join(f"line {i}" for i in range(5))
        fp_short = svc.fingerprint(message="x", stacktrace=short_st, source="x")
        assert fp_long == fp_short

    def test_file_paths_stripped(self) -> None:
        svc = ErrorIngestionService()
        st1 = """  File "/app/deploy/path/main.py", line 42, in handler
    result = do_work()"""
        st2 = """  File "/different/path/main.py", line 99, in handler
    result = do_work()"""
        fp1 = svc.fingerprint(message="x", stacktrace=st1, source="x")
        fp2 = svc.fingerprint(message="x", stacktrace=st2, source="x")
        assert fp1 == fp2

    def test_none_stacktrace(self) -> None:
        svc = ErrorIngestionService()
        fp = svc.fingerprint(message="test", stacktrace=None, source="backend")
        assert isinstance(fp, str)
        assert len(fp) == 64

    def test_empty_stacktrace(self) -> None:
        svc = ErrorIngestionService()
        fp = svc.fingerprint(message="test", stacktrace="", source="backend")
        assert isinstance(fp, str)
        assert len(fp) == 64


# =========================================================================
# Ingest
# =========================================================================


class _IngestMocks:
    """Context manager that patches CRUD functions at the service's import site."""

    def __init__(self, event_mock, group_mock=None):
        self.event_mock = event_mock
        self.group_mock = group_mock
        self.patches = []

    async def __aenter__(self):
        grp = MagicMock()
        grp.id = uuid.uuid4()

        self.patches = [
            patch("modulo.core.error_tracking.create_error_event", AsyncMock(return_value=self.event_mock)),
            patch("modulo.core.error_tracking.get_error_group_by_fingerprint", AsyncMock(return_value=self.group_mock)),
            patch("modulo.core.error_tracking.upsert_error_group", AsyncMock(return_value=grp)),
        ]
        for p in self.patches:
            p.start()
        return self

    async def __aexit__(self, *args):
        for p in self.patches:
            p.stop()


class TestIngest:
    async def test_creates_event_and_group(self) -> None:
        svc = ErrorIngestionService()
        session = AsyncMock()
        event_mock = MagicMock()
        event_mock.id = uuid.uuid4()

        with (
            patch("modulo.core.error_tracking.create_error_event", AsyncMock(return_value=event_mock)) as create_mock,
            patch("modulo.core.error_tracking.get_error_group_by_fingerprint", AsyncMock(return_value=None)),
            patch("modulo.core.error_tracking.upsert_error_group") as upsert_mock,
        ):
            upsert_group = MagicMock()
            upsert_group.id = uuid.uuid4()
            upsert_mock.return_value = upsert_group

            result = await svc.ingest(
                session,
                _ORG_ID,
                {"level": "error", "message": "test", "source": "backend"},
            )
            assert "group_id" in result
            assert result["is_new"] is True
            create_mock.assert_awaited_once()
            upsert_mock.assert_awaited_once()

    async def test_ingest_existing_group_returns_is_new_false(self) -> None:
        svc = ErrorIngestionService()
        session = AsyncMock()
        event_mock = MagicMock()
        event_mock.id = uuid.uuid4()
        existing_group = MagicMock()
        existing_group.id = uuid.uuid4()

        with (
            patch("modulo.core.error_tracking.create_error_event", AsyncMock(return_value=event_mock)),
            patch("modulo.core.error_tracking.get_error_group_by_fingerprint", AsyncMock(return_value=existing_group)),
            patch("modulo.core.error_tracking.upsert_error_group") as upsert_mock,
        ):
            upsert_group = MagicMock()
            upsert_group.id = uuid.uuid4()
            upsert_mock.return_value = upsert_group

            result = await svc.ingest(
                session,
                _ORG_ID,
                {"level": "error", "message": "test", "source": "backend"},
            )
            assert result["is_new"] is False


class TestIngestBatch:
    async def test_batch_creates_multiple_events(self) -> None:
        svc = ErrorIngestionService()
        session = AsyncMock()
        event_mock = MagicMock()
        event_mock.id = uuid.uuid4()
        grp = MagicMock()
        grp.id = uuid.uuid4()

        with (
            patch("modulo.core.error_tracking.create_error_event", AsyncMock(return_value=event_mock)),
            patch("modulo.core.error_tracking.get_error_group_by_fingerprint", AsyncMock(return_value=None)),
            patch("modulo.core.error_tracking.upsert_error_group", AsyncMock(return_value=grp)),
        ):
            events = [
                {"level": "error", "message": "err1", "source": "backend"},
                {"level": "warning", "message": "err2", "source": "frontend"},
            ]
            results = await svc.ingest_batch(session, _ORG_ID, events)
            assert len(results) == 2
            assert all("group_id" in r for r in results)

    async def test_batch_max_20(self) -> None:
        with pytest.raises(ValidationError):
            ErrorIngestRequest(events=[{"level": "error", "message": "x", "source": "backend"}] * 21)


# =========================================================================
# HMAC verification
# =========================================================================


class TestSessionKeyStore:
    async def test_generate_and_verify(self) -> None:
        store = SessionKeyStore(redis_client=None)
        account_id = str(uuid.uuid4())
        key = await store.generate_key(account_id)
        assert len(key) == 64

        body = b'{"events":[{"level":"error","message":"test","source":"backend"}]}'
        sig = hmac.new(key.encode(), body, hashlib.sha256).hexdigest()
        assert await store.verify_hmac(account_id, body, sig) is True

    async def test_wrong_signature_fails(self) -> None:
        store = SessionKeyStore(redis_client=None)
        account_id = str(uuid.uuid4())
        await store.generate_key(account_id)

        body = b'{"test":"data"}'
        bad_sig = "x" * 64
        assert await store.verify_hmac(account_id, body, bad_sig) is False

    async def test_missing_key_fails(self) -> None:
        store = SessionKeyStore(redis_client=None)
        body = b'{"test":"data"}'
        sig = hmac.new(b"anykey", body, hashlib.sha256).hexdigest()
        assert await store.verify_hmac(str(uuid.uuid4()), body, sig) is False

    async def test_multiple_accounts_independent(self) -> None:
        store = SessionKeyStore(redis_client=None)
        a1, a2 = str(uuid.uuid4()), str(uuid.uuid4())
        k1 = await store.generate_key(a1)
        await store.generate_key(a2)

        body = b"hello"
        sig1 = hmac.new(k1.encode(), body, hashlib.sha256).hexdigest()
        assert await store.verify_hmac(a1, body, sig1) is True
        assert await store.verify_hmac(a2, body, sig1) is False


# =========================================================================
# Schema validation
# =========================================================================


class TestErrorEventInputValidation:
    def test_valid_input(self) -> None:
        e = ErrorEventInput(level="error", message="Something broke", source="backend")
        assert e.level == "error"
        assert e.message == "Something broke"
        assert e.source == "backend"

    def test_empty_message(self) -> None:
        with pytest.raises(ValidationError, match="message must not be empty"):
            ErrorEventInput(level="error", message="", source="backend")

    def test_invalid_level(self) -> None:
        with pytest.raises(ValidationError, match="Invalid level"):
            ErrorEventInput(level="debug", message="test", source="backend")

    def test_invalid_source(self) -> None:
        with pytest.raises(ValidationError, match="Invalid source"):
            ErrorEventInput(level="error", message="test", source="mobile")

    def test_too_many_breadcrumbs(self) -> None:
        with pytest.raises(ValidationError, match="breadcrumbs must not exceed"):
            ErrorEventInput(
                level="error",
                message="test",
                source="backend",
                breadcrumbs=[{"msg": str(i)} for i in range(51)],
            )

    def test_breadcrumbs_at_limit(self) -> None:
        e = ErrorEventInput(
            level="error",
            message="test",
            source="backend",
            breadcrumbs=[{"msg": str(i)} for i in range(50)],
        )
        assert len(e.breadcrumbs) == 50

    def test_message_stripped(self) -> None:
        e = ErrorEventInput(level="error", message="  hello  ", source="backend")
        assert e.message == "hello"

    def test_optional_fields_none(self) -> None:
        e = ErrorEventInput(level="critical", message="test", source="celery")
        assert e.stacktrace is None
        assert e.context_json is None
        assert e.environment is None
        assert e.version is None
        assert e.breadcrumbs is None


class TestErrorIngestRequestValidation:
    def test_empty_events_rejected(self) -> None:
        with pytest.raises(ValidationError):
            ErrorIngestRequest(events=[])

    def test_single_event_valid(self) -> None:
        r = ErrorIngestRequest(events=[{"level": "error", "message": "test", "source": "backend"}])
        assert len(r.events) == 1

    def test_max_20_events(self) -> None:
        events = [{"level": "error", "message": f"err{i}", "source": "backend"} for i in range(20)]
        r = ErrorIngestRequest(events=events)
        assert len(r.events) == 20

    def test_too_many_events_rejected(self) -> None:
        events = [{"level": "error", "message": f"err{i}", "source": "backend"} for i in range(21)]
        with pytest.raises(ValidationError):
            ErrorIngestRequest(events=events)


class TestErrorGroupResult:
    def test_fields(self) -> None:
        r = ErrorGroupResult(group_id="abc-123", is_new=True)
        assert r.group_id == "abc-123"
        assert r.is_new is True


class TestErrorIngestResponse:
    def test_response_model(self) -> None:
        r = ErrorIngestResponse(results=[ErrorGroupResult(group_id="g1", is_new=True)])
        assert len(r.results) == 1


# =========================================================================
# Rate limiting
# =========================================================================


class TestRateLimitRule:
    def test_error_ingest_rule_exists(self) -> None:
        rules = RateLimitMiddleware.RULES
        rule = next((r for r in rules if r[0] == "/api/v1/errors/ingest"), None)
        assert rule is not None, "Rate limit rule for /api/v1/errors/ingest not found"
        assert rule[1] == 10, "Expected 10 requests per window"
        assert rule[2] == 60, "Expected 60-second window"
