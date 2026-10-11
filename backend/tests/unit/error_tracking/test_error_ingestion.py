"""Tests for ErrorIngestionService, schemas, session key store, and rate limiting."""

from __future__ import annotations

import hashlib
import hmac
import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.api.middleware.rate_limiter import RateLimitMiddleware
from modulo.api.models.error import ErrorEventInput, ErrorGroupResult, ErrorIngestRequest, ErrorIngestResponse
from modulo.core.error_tracking import ErrorIngestionService, SessionKeyStore

_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")


def _make_session() -> AsyncMock:
    session = AsyncMock(spec=AsyncSession)
    result = MagicMock()
    result.scalars.return_value.all.return_value = []
    session.execute.return_value = result
    return session


# =========================================================================
# Fingerprint — parametrized
# =========================================================================


class TestFingerprint:
    """Parametrized: fingerprint determinism, uniqueness, normalization."""

    svc = ErrorIngestionService()

    @pytest.mark.parametrize(
        ("msg1", "stack1", "src1", "msg2", "stack2", "src2", "should_match"),
        [
            pytest.param("test error", None, "backend", "test error", None, "backend", True, id="same_input_same_hash"),
            pytest.param(
                "error one", None, "backend", "error two", None, "backend", False, id="different_message_different_hash"
            ),
            pytest.param(
                "test", None, "backend", "test", None, "frontend", False, id="different_source_different_hash"
            ),
            pytest.param(
                "test", "line 1\nline 2", "backend", "test", None, "backend", False, id="stacktrace_affects_hash"
            ),
            pytest.param(
                "error",
                "traceback line 1\ntraceback line 2",
                "backend",
                "error",
                "traceback line 1\ntraceback line 2",
                "backend",
                True,
                id="deterministic_with_stacktrace",
            ),
        ],
    )
    def test_fingerprint(
        self,
        msg1: str,
        stack1: str | None,
        src1: str,
        msg2: str,
        stack2: str | None,
        src2: str,
        should_match: bool,
    ) -> None:
        fp1 = self.svc.fingerprint(message=msg1, stacktrace=stack1, source=src1)
        fp2 = self.svc.fingerprint(message=msg2, stacktrace=stack2, source=src2)
        if should_match:
            assert fp1 == fp2
        else:
            assert fp1 != fp2

    def test_fingerprint_hex_length(self) -> None:
        fp = self.svc.fingerprint(message="x", source="celery")
        assert len(fp) == 64


class TestFingerprintNormalization:
    svc = ErrorIngestionService()

    def test_top_five_frames_only(self) -> None:
        # Two stacks identical in their first 5 lines but differing after line 5
        # must yield the SAME fingerprint (normalisation keeps only the top 5).
        head = "\n".join(f"frame {i}" for i in range(5))
        stack_a = head + "\n" + "tail A"
        stack_b = head + "\n" + "totally different tail B"
        fa = self.svc.fingerprint(message="x", stacktrace=stack_a, source="x")
        fb = self.svc.fingerprint(message="x", stacktrace=stack_b, source="x")
        assert fa == fb
        # Changing a line WITHIN the top 5 changes the fingerprint.
        stack_c = "\n".join(["frame 0", "frame X", "frame 2", "frame 3", "frame 4", "tail A"])
        fc = self.svc.fingerprint(message="x", stacktrace=stack_c, source="x")
        assert fc != fa

    def test_file_paths_are_stripped(self) -> None:
        # The File "…", line N, prefix is stripped, so identical code with a
        # different absolute path yields the same fingerprint.
        a = '  File "/app/deploy/main.py", line 42, in handler\n    result = do_work()'
        b = '  File "/home/dev/other/main.py", line 99, in handler\n    result = do_work()'
        fa = self.svc.fingerprint(message="x", stacktrace=a, source="x")
        fb = self.svc.fingerprint(message="x", stacktrace=b, source="x")
        assert fa == fb

    def test_none_and_empty_stacktrace_are_equivalent(self) -> None:
        none_fp = self.svc.fingerprint(message="x", stacktrace=None, source="x")
        empty_fp = self.svc.fingerprint(message="x", stacktrace="", source="x")
        assert none_fp == empty_fp
        assert len(none_fp) == 64


# =========================================================================
# Ingest
# =========================================================================


class TestIngest:
    async def test_creates_event_and_group(self) -> None:
        svc = ErrorIngestionService()
        session = _make_session()
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

            create_kwargs = create_mock.await_args.kwargs
            assert create_kwargs["session"] is session
            assert create_kwargs["org_id"] == _ORG_ID
            assert create_kwargs["fingerprint"] == svc.fingerprint(message="test", stacktrace=None, source="backend")
            assert create_kwargs["level"] == "error"
            assert create_kwargs["message"] == "test"
            assert create_kwargs["source"] == "backend"
            assert create_kwargs["environment"] is None
            assert create_kwargs["version"] is None

            upsert_kwargs = upsert_mock.await_args.kwargs
            assert upsert_kwargs["org_id"] == _ORG_ID
            assert upsert_kwargs["fingerprint"] == create_kwargs["fingerprint"]
            assert upsert_kwargs["sample_event_id"] == event_mock.id

    async def test_ingest_existing_group_returns_is_new_false(self) -> None:
        svc = ErrorIngestionService()
        session = _make_session()
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

    async def test_ingest_sanitizes_secrets_before_fingerprint(self) -> None:
        """P8: two events differing only by an embedded secret must hash to the
        SAME fingerprint/group, and the stored message is redacted at the
        ingest choke point."""
        svc = ErrorIngestionService()
        session = _make_session()
        created: list[dict] = []

        async def fake_create_event(session: Any, org_id: Any, **kwargs: Any) -> MagicMock:
            created.append(kwargs)
            return MagicMock(id=uuid.uuid4())

        with (
            patch("modulo.core.error_tracking.create_error_event", side_effect=fake_create_event),
            patch(
                "modulo.core.error_tracking.get_error_group_by_fingerprint",
                AsyncMock(return_value=None),
            ),
            patch("modulo.core.error_tracking.upsert_error_group") as upsert_mock,
        ):
            upsert_mock.return_value = MagicMock(id=uuid.uuid4())
            await svc.ingest(
                session,
                _ORG_ID,
                {"level": "error", "message": "call failed with sk-abcdefghijkl1234", "source": "backend"},
            )
            await svc.ingest(
                session,
                _ORG_ID,
                {"level": "error", "message": "call failed with sk-zzzzzzzzzzzzzzz9", "source": "backend"},
            )

        assert created[0]["fingerprint"] == created[1]["fingerprint"]
        assert created[0]["message"] == "call failed with <redacted>"
        assert created[1]["message"] == "call failed with <redacted>"

    @pytest.mark.parametrize(
        ("event_data", "missing"),
        [
            ({"level": "error", "source": "backend"}, "message"),
            ({"message": "test", "source": "backend"}, "level"),
            ({"level": "error", "message": "test"}, "source"),
        ],
        ids=["missing_message", "missing_level", "missing_source"],
    )
    async def test_ingest_rejects_incomplete_event_data(self, event_data: dict, missing: str) -> None:
        svc = ErrorIngestionService()
        session = _make_session()

        with (
            patch("modulo.core.error_tracking.create_error_event", AsyncMock()) as create_mock,
            patch("modulo.core.error_tracking.get_error_group_by_fingerprint", AsyncMock()),
            patch("modulo.core.error_tracking.upsert_error_group", AsyncMock()),
        ):
            with pytest.raises(ValueError, match=rf"'{missing}'"):
                await svc.ingest(session, _ORG_ID, event_data)
            create_mock.assert_not_awaited()


class TestIngestBatch:
    async def test_batch_creates_multiple_events(self) -> None:
        svc = ErrorIngestionService()
        session = _make_session()
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

    def test_batch_max_20(self) -> None:
        with pytest.raises(ValidationError):
            ErrorIngestRequest(events=[{"level": "error", "message": "x", "source": "backend"}] * 21)

    async def test_batch_item_failure_does_not_abort_batch(self) -> None:
        """A single malformed event must not prevent valid events from being ingested."""
        svc = ErrorIngestionService()
        session = _make_session()
        event_mock = MagicMock()
        event_mock.id = uuid.uuid4()
        grp = MagicMock()
        grp.id = uuid.uuid4()

        with (
            patch(
                "modulo.core.error_tracking.create_error_event",
                AsyncMock(side_effect=[event_mock, ValueError("boom"), event_mock]),
            ),
            patch("modulo.core.error_tracking.get_error_group_by_fingerprint", AsyncMock(return_value=None)),
            patch("modulo.core.error_tracking.upsert_error_group", AsyncMock(return_value=grp)),
        ):
            events = [
                {"level": "error", "message": "ok1", "source": "backend"},
                {"level": "error", "message": "ok2", "source": "backend"},
                {"level": "error", "message": "ok3", "source": "backend"},
            ]
            results = await svc.ingest_batch(session, _ORG_ID, events)

        assert len(results) == 2
        assert all("group_id" in r for r in results)


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
# Schema validation — parametrized
# =========================================================================


class TestErrorEventInputValidation:
    @pytest.mark.parametrize(
        ("kwargs", "should_pass", "match"),
        [
            pytest.param(
                {"level": "error", "message": "Something broke", "source": "backend"}, True, None, id="valid_input"
            ),
            pytest.param(
                {"level": "error", "message": "", "source": "backend"},
                False,
                "message must not be empty",
                id="empty_message",
            ),
            pytest.param(
                {"level": "debug", "message": "test", "source": "backend"}, False, "Invalid level", id="invalid_level"
            ),
            pytest.param(
                {"level": "error", "message": "test", "source": "mobile"}, False, "Invalid source", id="invalid_source"
            ),
            pytest.param({"level": "error", "message": "test", "source": "saq"}, True, None, id="saq_source_accepted"),
            pytest.param(
                {
                    "level": "error",
                    "message": "test",
                    "source": "backend",
                    "breadcrumbs": [{"msg": str(i)} for i in range(51)],
                },
                False,
                "breadcrumbs must not exceed",
                id="too_many_breadcrumbs",
            ),
            pytest.param(
                {
                    "level": "error",
                    "message": "test",
                    "source": "backend",
                    "breadcrumbs": [{"msg": str(i)} for i in range(50)],
                },
                True,
                None,
                id="breadcrumbs_at_limit",
            ),
            pytest.param(
                {"level": "error", "message": "  hello  ", "source": "backend"}, True, None, id="message_stripped"
            ),
            pytest.param(
                {"level": "critical", "message": "test", "source": "celery"}, True, None, id="optional_fields_none"
            ),
        ],
    )
    def test_validation(self, kwargs: dict, should_pass: bool, match: str | None) -> None:
        if should_pass:
            e = ErrorEventInput(**kwargs)
            assert e.level == kwargs["level"]
            if kwargs.get("message") == "  hello  ":
                assert e.message == "hello"
        else:
            with pytest.raises(ValidationError, match=match):
                ErrorEventInput(**kwargs)


class TestErrorIngestRequestValidation:
    @pytest.mark.parametrize(
        ("events", "should_pass"),
        [
            pytest.param([], False, id="empty_rejected"),
            pytest.param([{"level": "error", "message": "test", "source": "backend"}], True, id="single_valid"),
            pytest.param(
                [{"level": "error", "message": f"err{i}", "source": "backend"} for i in range(20)],
                True,
                id="max_20_valid",
            ),
            pytest.param(
                [{"level": "error", "message": f"err{i}", "source": "backend"} for i in range(21)],
                False,
                id="too_many_rejected",
            ),
        ],
    )
    def test_validation(self, events: list[dict], should_pass: bool) -> None:
        if should_pass:
            r = ErrorIngestRequest(events=events)
            assert len(r.events) == len(events)
        else:
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
        rule = next((r for r in rules if r.path_prefix == "/api/v1/errors/ingest"), None)
        assert rule is not None, "Rate limit rule for /api/v1/errors/ingest not found"
        assert rule.max_requests == 10, "Expected 10 requests per window"
        assert rule.window_s == 60, "Expected 60-second window"
