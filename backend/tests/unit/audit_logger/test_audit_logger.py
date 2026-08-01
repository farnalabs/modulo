"""Unit tests for cryptographic audit chaining."""

import logging
import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.exc import IntegrityError, ProgrammingError, SQLAlchemyError

from modulo.core.audit_logger import (
    BATCH_MAX_SIZE,
    LIST_MAX_LIMIT,
    LIST_MIN_LIMIT,
    _compute_event_hash,
    _get_chain_head_locked,
    append_audit_event,
    export_chain,
    get_audit_events_batch,
    get_chain_head,
    list_audit_events,
    verify_chain,
)
from modulo.db.models.audit_event import AuditChainHead

ORG_ID = "00000000-0000-0000-0000-000000000001"
EVENT_ID = "00000000-0000-0000-0000-000000000002"

_EVENT_SPEC = [
    "id",
    "organisation_id",
    "event_type",
    "account_id",
    "resource_type",
    "resource_id",
    "payload_json",
    "request_id",
    "previous_hash",
    "created_at",
]


def _make_event(
    *,
    event_id=None,
    org_id=None,
    event_type="e",
    account_id=None,
    resource_type=None,
    resource_id=None,
    payload_json=None,
    request_id=None,
    previous_hash=None,
    created_at_val="t",
):
    """Build a minimal mock AuditEvent with the attributes the hash/verify code touches."""
    created_at = MagicMock()
    created_at.isoformat = MagicMock(return_value=created_at_val)
    event = MagicMock(spec=_EVENT_SPEC)
    event.id = event_id or uuid.uuid4()
    event.organisation_id = org_id
    event.event_type = event_type
    event.account_id = account_id
    event.resource_type = resource_type
    event.resource_id = resource_id
    event.payload_json = payload_json if payload_json is not None else {}
    event.request_id = request_id
    event.previous_hash = previous_hash
    event.created_at = created_at
    return event


def _scalar_result(value):
    r = MagicMock()
    r.scalar = MagicMock(return_value=value)
    return r


def _scalars_result(values):
    r = MagicMock()
    r.scalars = MagicMock(return_value=values)
    return r


def _head_result(head):
    r = MagicMock()
    r.scalar_one_or_none = MagicMock(return_value=head)
    return r


class TestEventHash:
    @pytest.mark.parametrize(
        "event_type,previous_hash,org_id_str,event_id_str,expected_different",
        [
            ("a", None, ORG_ID, EVENT_ID, False),
            ("b", None, ORG_ID, EVENT_ID, True),
            ("e", "prev", ORG_ID, EVENT_ID, True),
            ("e", None, "00000000-0000-0000-0000-000000000003", EVENT_ID, True),
            ("e", None, ORG_ID, "00000000-0000-0000-0000-000000000099", True),
        ],
    )
    def test_compute_event_hash(self, event_type, previous_hash, org_id_str, event_id_str, expected_different):
        h1 = _compute_event_hash("a", None, None, None, {}, None, None, EVENT_ID, ORG_ID, "t")
        h2 = _compute_event_hash(event_type, None, None, None, {}, None, previous_hash, event_id_str, org_id_str, "t")
        if expected_different:
            assert h1 != h2
        else:
            assert h1 == h2
            assert len(h1) == 64


class TestAppendAuditEvent:
    @pytest.fixture
    def session(self):
        s = MagicMock()
        s.flush = AsyncMock()
        s.add = MagicMock()
        s.begin = MagicMock()
        return s

    async def test_first_event_creates_chain_head(self, session):
        async def _execute(*a, **kw):
            r = MagicMock()
            r.scalar_one_or_none = MagicMock(return_value=None)
            return r

        session.execute = _execute

        event = await append_audit_event(
            session,
            org_id=uuid.uuid4(),
            event_type="test.event",
        )
        assert event.previous_hash is None
        assert session.add.call_count >= 2

    async def test_second_event_links_to_first(self, session):
        org_id = uuid.uuid4()
        event_id = uuid.uuid4()
        h = _compute_event_hash("first", None, None, None, {}, None, None, str(event_id), str(org_id), "t")
        head = AuditChainHead(organisation_id=org_id, last_event_hash=h, last_event_id=event_id, event_count=1)

        call_count = 0

        async def _execute(*a, **kw):
            nonlocal call_count
            call_count += 1
            r = MagicMock()
            r.scalar_one_or_none = MagicMock(return_value=head if call_count == 1 else None)
            return r

        session.execute = _execute

        event = await append_audit_event(
            session,
            org_id=org_id,
            event_type="second.event",
        )
        assert event.previous_hash == h


class TestVerifyChain:
    @pytest.fixture
    def session(self):
        s = MagicMock()
        s.flush = AsyncMock()
        s.add = MagicMock()
        s.begin_nested = MagicMock()
        return s

    async def test_verify_empty_chain(self, session):
        call_count = 0

        async def _execute(*a, **kw):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                # First call: count query
                return _scalar_result(0)
            # Second call: fetch events
            return _scalars_result([])

        session.execute = _execute

        result = await verify_chain(session, uuid.uuid4())
        assert result["valid"] is True
        assert result["total_events"] == 0
        assert result["checked_events"] == 0
        assert result["truncated"] is False
        assert result["chain_head_match"] is None
        assert result["chain_count_mismatch"] is None

    async def test_verify_valid_chain(self, session):
        org_id = uuid.uuid4()
        event_id1 = uuid.uuid4()
        event_id2 = uuid.uuid4()

        h1 = _compute_event_hash("e1", None, None, None, {}, None, None, str(event_id1), str(org_id), "t1")
        h2 = _compute_event_hash("e2", None, None, None, {}, None, h1, str(event_id2), str(org_id), "t2")

        e1 = _make_event(event_id=event_id1, org_id=org_id, event_type="e1", previous_hash=None, created_at_val="t1")
        e2 = _make_event(event_id=event_id2, org_id=org_id, event_type="e2", previous_hash=h1, created_at_val="t2")

        call_count = 0

        async def _execute(*a, **kw):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                # First call: count query
                return _scalar_result(2)
            if call_count == 2:
                # Second call: fetch events
                return _scalars_result([e1, e2])
            # Third call: get_chain_head
            head = MagicMock()
            head.last_event_hash = h2
            head.event_count = 2
            return _head_result(head)

        session.execute = _execute

        result = await verify_chain(session, org_id)
        assert result["valid"] is True
        assert result["checked_events"] == 2
        assert result["truncated"] is False
        assert result["chain_head_match"] is True
        assert result["chain_count_mismatch"] is False

    async def test_verify_detects_tampered_chain(self, session):
        org_id = uuid.uuid4()
        event_id1 = uuid.uuid4()
        event_id2 = uuid.uuid4()

        e1 = _make_event(event_id=event_id1, org_id=org_id, event_type="e1", previous_hash=None, created_at_val="t1")
        e2 = _make_event(
            event_id=event_id2,
            org_id=org_id,
            event_type="e2",
            previous_hash="bad-hash",
            created_at_val="t2",
        )

        call_count = 0

        async def _execute(*a, **kw):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                # First call: count query
                return _scalar_result(2)
            # Second call: fetch events
            return _scalars_result([e1, e2])

        session.execute = _execute

        result = await verify_chain(session, org_id)
        assert result["valid"] is False
        assert result["first_gap_index"] == 1
        assert result["first_tampered_id"] == str(event_id2)

    async def test_verify_missing_head_is_invalid(self, session):
        """Events exist but no chain head -> chain is treated as corrupted."""
        org_id = uuid.uuid4()
        e1 = _make_event(event_id=uuid.uuid4(), org_id=org_id, event_type="e1", created_at_val="t1")

        call_count = 0

        async def _execute(*a, **kw):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return _scalar_result(1)
            if call_count == 2:
                return _scalars_result([e1])
            return _head_result(None)

        session.execute = _execute

        result = await verify_chain(session, org_id)
        assert result["valid"] is False
        assert result["chain_head_match"] is None
        assert result["chain_count_mismatch"] is None
        assert result["checked_events"] == 1

    async def test_verify_truncated_chain(self, session):
        """total_events > max_events -> partial check flagged as truncated."""
        org_id = uuid.uuid4()
        h1 = _compute_event_hash("e1", None, None, None, {}, None, None, str(uuid.uuid4()), str(org_id), "t1")
        e1 = _make_event(org_id=org_id, event_type="e1", previous_hash=None, created_at_val="t1")
        e2 = _make_event(org_id=org_id, event_type="e2", previous_hash=h1, created_at_val="t2")
        # Simulate the full chain head pointing at an event that was NOT fetched.
        head = MagicMock()
        head.last_event_hash = "hash-not-in-window"
        head.event_count = 5

        call_count = 0

        async def _execute(*a, **kw):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                # count reports more events than max_events
                return _scalar_result(5)
            if call_count == 2:
                return _scalars_result([e1, e2])
            return _head_result(head)

        session.execute = _execute

        result = await verify_chain(session, org_id, max_events=2)
        assert result["truncated"] is True
        assert result["valid"] is False
        assert result["checked_events"] == 2


class TestExportChain:
    @pytest.fixture
    def session(self):
        s = MagicMock()
        s.flush = AsyncMock()
        s.execute = MagicMock()
        s.add = MagicMock()
        s.begin_nested = MagicMock()
        return s

    async def test_export_empty(self, session):
        call_count = 0

        async def _execute(*a, **kw):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return _scalars_result([])
            return _scalar_result(0)

        session.execute = _execute

        result = await export_chain(session, uuid.uuid4())
        assert result["total"] == 0
        assert result["items"] == []

    async def test_export_page_two(self, session):
        org_id = uuid.uuid4()
        total = 50

        call_count = 0

        async def _execute(*a, **kw):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return _scalars_result(
                    [
                        _make_event(
                            org_id=org_id,
                            event_type="test.event",
                            payload_json={"seq": i},
                            created_at_val="2025-06-01T00:00:00+00:00",
                        )
                        for i in range(10)
                    ]
                )
            return _scalar_result(total)

        session.execute = _execute

        result = await export_chain(session, org_id, page=2, page_size=10)
        assert result["page"] == 2
        assert result["page_size"] == 10
        assert result["total"] == 50
        assert len(result["items"]) == 10

    async def test_export_clamps_page_and_size(self, session):
        org_id = uuid.uuid4()

        async def _execute(*a, **kw):
            r = MagicMock()
            r.scalars = MagicMock(return_value=[])
            r.scalar = MagicMock(return_value=0)
            return r

        session.execute = _execute

        result = await export_chain(session, org_id, page=0, page_size=0)
        assert result["page"] == 1
        assert result["page_size"] == 1

        result = await export_chain(session, org_id, page=-3, page_size=5000)
        assert result["page"] == 1
        assert result["page_size"] == 1000

    async def test_export_applies_filters(self, session):
        """All _apply_filters branches (event_type, actor, resource, dates) are wired."""
        org_id = uuid.uuid4()
        actor_id = uuid.uuid4()
        from_date = datetime(2025, 1, 1, tzinfo=UTC)
        to_date = datetime(2025, 12, 31, tzinfo=UTC)

        executed = []

        async def _execute(stmt, *a, **kw):
            executed.append(stmt)
            r = MagicMock()
            r.scalars = MagicMock(return_value=[])
            r.scalar = MagicMock(return_value=0)
            return r

        session.execute = _execute

        result = await export_chain(
            session,
            org_id,
            event_type="user.login",
            actor_user_id=actor_id,
            resource_type="pipeline",
            from_date=from_date,
            to_date=to_date,
        )
        assert result["total"] == 0
        assert result["items"] == []

        # Every filter must reach the SQL sent to the DB, on both the items
        # and count queries, not just return an empty page.
        sql = [str(stmt.compile(compile_kwargs={"literal_binds": True})) for stmt in executed]
        for stmt_sql in sql:
            assert "user.login" in stmt_sql
            assert actor_id.hex in stmt_sql
            assert "pipeline" in stmt_sql
            assert "2025-01-01 00:00:00+00:00" in stmt_sql
            assert "2025-12-31 00:00:00+00:00" in stmt_sql


class TestGetAuditEventsBatch:
    @pytest.fixture
    def session(self):
        s = MagicMock()
        s.flush = AsyncMock()
        s.execute = MagicMock()
        s.add = MagicMock()
        s.begin_nested = MagicMock()
        return s

    async def test_batch_valid_uuids(self, session):
        org_id = uuid.uuid4()
        eid1 = str(uuid.uuid4())
        eid2 = str(uuid.uuid4())

        async def _execute(*a, **kw):
            return _scalars_result(
                [
                    _make_event(
                        event_id=uuid.UUID(eid1),
                        org_id=org_id,
                        event_type="test.event",
                        payload_json={"key": "value"},
                    )
                ]
            )

        session.execute = _execute

        result = await get_audit_events_batch(session, org_id, [eid1, eid2])
        assert len(result) == 1
        assert result[0]["id"] == eid1

    async def test_batch_skips_invalid_uuids(self, session):
        org_id = uuid.uuid4()

        async def _execute(*a, **kw):
            return _scalars_result([])

        session.execute = _execute

        result = await get_audit_events_batch(session, org_id, ["not-a-uuid", "also-bad"])
        assert len(result) == 0

    async def test_batch_empty_list(self, session):
        org_id = uuid.uuid4()
        result = await get_audit_events_batch(session, org_id, [])
        assert result == []

    async def test_batch_truncates_over_max(self, session):
        """More than BATCH_MAX_SIZE ids are truncated with a warning."""
        org_id = uuid.uuid4()
        ids = [str(uuid.uuid4()) for _ in range(BATCH_MAX_SIZE + 5)]

        async def _execute(*a, **kw):
            return _scalars_result([])

        session.execute = _execute

        result = await get_audit_events_batch(session, org_id, ids)
        assert result == []


class TestListAuditEvents:
    @pytest.fixture
    def session(self):
        s = MagicMock()
        s.flush = AsyncMock()
        s.execute = MagicMock()
        s.add = MagicMock()
        s.begin_nested = MagicMock()
        return s

    async def test_list_default_limit(self, session):
        org_id = uuid.uuid4()

        call_count = 0

        async def _execute(*a, **kw):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return _scalar_result(0)
            return _scalars_result([])

        session.execute = _execute

        result = await list_audit_events(session, org_id)
        assert result["total"] == 0
        assert result["items"] == []
        assert result["limit"] == 50
        assert result["next_cursor"] is None

    async def test_list_with_invalid_cursor(self, session):
        org_id = uuid.uuid4()

        call_count = 0

        async def _execute(*a, **kw):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return _scalar_result(0)
            return _scalars_result([])

        session.execute = _execute

        result = await list_audit_events(session, org_id, cursor="not-a-uuid")
        assert result["total"] == 0
        assert result["items"] == []
        # Falls back to the first page without raising.
        assert result["next_cursor"] is None

    async def test_list_with_valid_cursor(self, session):
        """A well-formed cursor decodes and positions the next page."""
        import json as _json

        org_id = uuid.uuid4()
        cursor_ts = "2025-06-01T00:00:00+00:00"
        cursor_id = str(uuid.uuid4())

        last = _make_event(org_id=org_id, event_type="e3", created_at_val="2025-06-03T00:00:00+00:00")

        call_count = 0

        async def _execute(*a, **kw):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return _scalar_result(3)
            # limit+1 items -> has_more, next_cursor derived from last item
            return _scalars_result([last, last])

        session.execute = _execute

        result = await list_audit_events(
            session,
            org_id,
            limit=1,
            cursor=_json.dumps({"c": cursor_ts, "i": cursor_id}),
        )
        assert len(result["items"]) == 1
        assert result["next_cursor"] is not None

    async def test_list_with_null_created_at_last(self, caplog, session):
        """A last event with null created_at cannot yield a cursor -> warning."""
        org_id = uuid.uuid4()

        last = _make_event(org_id=org_id, event_type="e1")
        last.created_at = None

        call_count = 0

        async def _execute(*a, **kw):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return _scalar_result(1)
            # limit+1 items -> has_more, next_cursor derived from last item
            return _scalars_result([last, last])

        session.execute = _execute

        with caplog.at_level(logging.WARNING):
            result = await list_audit_events(session, org_id, limit=1)
        assert result["next_cursor"] is None
        assert any("cannot produce next cursor" in rec.message for rec in caplog.records)

    async def test_list_filter_by_event_type(self, session):
        org_id = uuid.uuid4()

        call_count = 0

        async def _execute(*a, **kw):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return _scalar_result(1)
            return _scalars_result(
                [
                    _make_event(
                        org_id=org_id,
                        event_type="pipeline.autonomy_level_changed",
                        resource_type="pipeline",
                        resource_id=uuid.uuid4(),
                    )
                ]
            )

        session.execute = _execute

        result = await list_audit_events(session, org_id, event_type="pipeline.autonomy_level_changed")
        assert result["total"] == 1
        assert len(result["items"]) == 1
        assert result["items"][0]["event_type"] == "pipeline.autonomy_level_changed"

    async def test_list_filter_by_actor_and_resource(self, session):
        """actor_user_id and resource_type filters are forwarded to the query."""
        org_id = uuid.uuid4()
        actor_id = uuid.uuid4()

        executed = []

        async def _execute(stmt, *a, **kw):
            executed.append(stmt)
            return _scalar_result(0)

        session.execute = _execute

        result = await list_audit_events(
            session,
            org_id,
            actor_user_id=actor_id,
            resource_type="pipeline",
        )
        assert result["total"] == 0

        # Verify the filters reach the SQL, not just that an empty page returns.
        sql = [str(stmt.compile(compile_kwargs={"literal_binds": True})) for stmt in executed]
        for stmt_sql in sql:
            assert actor_id.hex in stmt_sql
            assert "pipeline" in stmt_sql

    async def test_list_clamps_limit(self, session):
        org_id = uuid.uuid4()

        async def _execute(*a, **kw):
            return _scalar_result(0)

        session.execute = _execute

        low = await list_audit_events(session, org_id, limit=0)
        assert low["limit"] == LIST_MIN_LIMIT

        high = await list_audit_events(session, org_id, limit=LIST_MAX_LIMIT + 100)
        assert high["limit"] == LIST_MAX_LIMIT

    async def test_list_with_has_more(self, session):
        org_id = uuid.uuid4()
        limit = 3

        call_count = 0

        async def _execute(*a, **kw):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return _scalar_result(10)
            # Return limit+1 items to trigger has_more
            return _scalars_result(
                [
                    _make_event(org_id=org_id, event_type="test.event", created_at_val=f"t{i}")
                    for i in range(limit + 1)
                ]
            )

        session.execute = _execute

        result = await list_audit_events(session, org_id, limit=limit)
        assert len(result["items"]) == limit
        assert result["next_cursor"] is not None


class TestGetChainHead:
    async def test_returns_head_when_present(self):
        session = MagicMock()
        org_id = uuid.uuid4()
        head = MagicMock()
        session.execute = AsyncMock(return_value=_head_result(head))

        result = await get_chain_head(session, org_id)
        assert result is head

    async def test_returns_none_when_absent(self):
        session = MagicMock()
        session.execute = AsyncMock(return_value=_head_result(None))

        result = await get_chain_head(session, uuid.uuid4())
        assert result is None


class TestAppendAuditEventEdgeCases:
    @pytest.fixture
    def session(self):
        s = MagicMock()
        s.flush = AsyncMock()
        s.add = MagicMock()
        s.begin_nested = MagicMock()
        return s

    @pytest.mark.parametrize(
        "kwargs,expected_checks",
        [
            ({"payload_json": None}, {"payload_json": {}}),
            ({"payload_json": {}}, {"payload_json": {}}),
            ({"payload_json": {"key": "value"}}, {"payload_json": {"key": "value"}}),
            ({"request_id": "req-abc-123"}, {"request_id": "req-abc-123"}),
            ({}, {"previous_hash": None}),
            ({"actor_user_id": None}, {"account_id": None}),
            ({"resource_type": None, "resource_id": None}, {"resource_type": None, "resource_id": None}),
        ],
    )
    async def test_edge_cases(self, session, kwargs, expected_checks):
        async def _execute(*a, **kw):
            return _head_result(None)

        session.execute = _execute

        event = await append_audit_event(
            session,
            org_id=uuid.uuid4(),
            event_type="test.event",
            **kwargs,
        )
        for field, expected in expected_checks.items():
            assert getattr(event, field) == expected


class TestAppendAuditEventErrors:
    """Retry and error handling paths for append_audit_event."""

    @pytest.fixture
    def session(self):
        s = MagicMock()
        s.flush = AsyncMock()
        s.add = MagicMock()
        s.begin_nested = MagicMock()
        return s

    async def test_integrity_error_retries_then_succeeds(self, session):
        """A transient IntegrityError on the first attempt is retried."""
        org_id = uuid.uuid4()

        call_count = 0

        async def _execute(*a, **kw):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise IntegrityError("stmt", {}, Exception("duplicate org head"))
            return _head_result(None)

        session.execute = _execute

        event = await append_audit_event(session, org_id=org_id, event_type="test.event")
        assert event.previous_hash is None
        assert call_count == 2

    async def test_integrity_error_exhausts_retries(self, session):
        """Persistent IntegrityError propagates after exhausting retries."""
        from modulo.core.audit_logger import APPEND_MAX_RETRIES

        call_count = 0

        async def _execute(*a, **kw):
            nonlocal call_count
            call_count += 1
            raise IntegrityError("stmt", {}, Exception("duplicate org head"))

        session.execute = _execute

        with pytest.raises(IntegrityError):
            await append_audit_event(session, org_id=uuid.uuid4(), event_type="test.event")
        assert call_count == APPEND_MAX_RETRIES

    async def test_programming_error_propagates(self, session):
        """ProgrammingError (missing table) is logged and re-raised."""
        async def _execute(*a, **kw):
            raise ProgrammingError("stmt", {}, Exception("no such table: audit_events"))

        session.execute = _execute

        with pytest.raises(ProgrammingError):
            await append_audit_event(session, org_id=uuid.uuid4(), event_type="test.event")

    async def test_sqlalchemy_error_propagates(self, session):
        """Other SQLAlchemyError values are logged and re-raised."""
        async def _execute(*a, **kw):
            raise SQLAlchemyError("boom")

        session.execute = _execute

        with pytest.raises(SQLAlchemyError):
            await append_audit_event(session, org_id=uuid.uuid4(), event_type="test.event")


class TestChainHeadLocking:
    async def test_locked_head_query(self):
        session = MagicMock()
        org_id = uuid.uuid4()
        head = MagicMock()
        session.execute = AsyncMock(return_value=_head_result(head))

        result = await _get_chain_head_locked(session, org_id)
        assert result is head


class TestExportEdgeCases:
    @pytest.fixture
    def session(self):
        s = MagicMock()
        s.flush = AsyncMock()
        s.execute = MagicMock()
        s.add = MagicMock()
        s.begin_nested = MagicMock()
        return s

    async def test_export_with_large_payload(self, session):
        org_id = uuid.uuid4()
        large_payload = {"data": "x" * 10000}

        async def _execute(*a, **kw):
            r = MagicMock()
            r.scalars = MagicMock(
                return_value=[
                    _make_event(org_id=org_id, event_type="test.event", payload_json=large_payload)
                ]
            )
            r.scalar = MagicMock(return_value=1)
            return r

        session.execute = _execute

        result = await export_chain(session, org_id)
        assert len(result["items"]) == 1
        assert result["items"][0]["payload_json"] == large_payload

    async def test_export_with_none_dates(self, session):
        org_id = uuid.uuid4()

        async def _execute(*a, **kw):
            event = _make_event(org_id=org_id, event_type="test.event")
            event.created_at = None
            r = MagicMock()
            r.scalars = MagicMock(return_value=[event])
            r.scalar = MagicMock(return_value=1)
            return r

        session.execute = _execute

        result = await export_chain(session, org_id)
        assert result["items"][0]["created_at"] is None
