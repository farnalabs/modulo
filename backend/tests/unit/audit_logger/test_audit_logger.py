"""Unit tests for cryptographic audit chaining."""

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from modulo.core.audit_logger import (
    _compute_event_hash,
    append_audit_event,
    export_chain,
    get_audit_events_batch,
    list_audit_events,
    verify_chain,
)
from modulo.db.models.audit_event import AuditChainHead

ORG_ID = "00000000-0000-0000-0000-000000000001"
EVENT_ID = "00000000-0000-0000-0000-000000000002"


class TestEventHash:
    def test_same_inputs_produce_same_hash(self):
        h1 = _compute_event_hash("a", None, None, None, {}, None, None, EVENT_ID, ORG_ID, "t")
        h2 = _compute_event_hash("a", None, None, None, {}, None, None, EVENT_ID, ORG_ID, "t")
        assert h1 == h2
        assert len(h1) == 64

    def test_different_inputs_produce_different_hash(self):
        h1 = _compute_event_hash("a", None, None, None, {}, None, None, EVENT_ID, ORG_ID, "t1")
        h2 = _compute_event_hash("b", None, None, None, {}, None, None, EVENT_ID, ORG_ID, "t1")
        assert h1 != h2

    def test_previous_hash_changes_result(self):
        h1 = _compute_event_hash("e", None, None, None, {}, None, None, EVENT_ID, ORG_ID, "t")
        h2 = _compute_event_hash("e", None, None, None, {}, None, "prev", EVENT_ID, ORG_ID, "t")
        assert h1 != h2

    def test_org_id_changes_result(self):
        h1 = _compute_event_hash("e", None, None, None, {}, None, None, EVENT_ID, ORG_ID, "t")
        h2 = _compute_event_hash(
            "e", None, None, None, {}, None, None, EVENT_ID, "00000000-0000-0000-0000-000000000003", "t"
        )
        assert h1 != h2

    def test_event_id_changes_result(self):
        h1 = _compute_event_hash("e", None, None, None, {}, None, None, EVENT_ID, ORG_ID, "t")
        h2 = _compute_event_hash(
            "e", None, None, None, {}, None, None, "00000000-0000-0000-0000-000000000099", ORG_ID, "t"
        )
        assert h1 != h2


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
        s.begin = MagicMock()
        return s

    async def test_verify_empty_chain(self, session):
        async def _execute(*a, **kw):
            r = MagicMock()
            r.scalars = MagicMock(return_value=[])
            return r

        session.execute = _execute

        result = await verify_chain(session, uuid.uuid4())
        assert result["valid"] is True
        assert result["total_events"] == 0

    async def test_verify_valid_chain(self, session):
        org_id = uuid.uuid4()
        event_id1 = uuid.uuid4()
        event_id2 = uuid.uuid4()

        created_at = MagicMock()
        created_at.isoformat = MagicMock(return_value="t1")

        created_at2 = MagicMock()
        created_at2.isoformat = MagicMock(return_value="t2")

        h1 = _compute_event_hash("e1", None, None, None, {}, None, None, str(event_id1), str(org_id), "t1")
        h2 = _compute_event_hash("e2", None, None, None, {}, None, h1, str(event_id2), str(org_id), "t2")

        e1 = MagicMock(
            spec=[
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
        )
        e1.id = event_id1
        e1.organisation_id = org_id
        e1.event_type = "e1"
        e1.account_id = None
        e1.resource_type = None
        e1.resource_id = None
        e1.payload_json = {}
        e1.request_id = None
        e1.previous_hash = None
        e1.created_at = created_at

        e2 = MagicMock(
            spec=[
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
        )
        e2.id = event_id2
        e2.organisation_id = org_id
        e2.event_type = "e2"
        e2.account_id = None
        e2.resource_type = None
        e2.resource_id = None
        e2.payload_json = {}
        e2.request_id = None
        e2.previous_hash = h1
        e2.created_at = created_at2

        call_count = 0

        async def _execute(*a, **kw):
            nonlocal call_count
            call_count += 1
            r = MagicMock()
            if call_count == 1:
                # First call: count query
                r.scalar = MagicMock(return_value=2)
            elif call_count == 2:
                # Second call: fetch events
                r.scalars = MagicMock(return_value=[e1, e2])
            else:
                # Third call: get_chain_head
                head = MagicMock()
                head.last_event_hash = h2
                head.event_count = 2
                r.scalar_one_or_none = MagicMock(return_value=head)
            return r

        session.execute = _execute

        result = await verify_chain(session, org_id)
        assert result["valid"] is True
        assert result["checked_events"] == 2
        assert result["chain_head_match"] is True

    async def test_verify_detects_tampered_chain(self, session):
        org_id = uuid.uuid4()
        event_id1 = uuid.uuid4()
        event_id2 = uuid.uuid4()

        created_at = MagicMock()
        created_at.isoformat = MagicMock(return_value="t1")
        created_at2 = MagicMock()
        created_at2.isoformat = MagicMock(return_value="t2")

        _compute_event_hash("e1", None, None, None, {}, None, None, str(event_id1), str(org_id), "t1")

        e1 = MagicMock(
            spec=[
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
        )
        e1.id = event_id1
        e1.organisation_id = org_id
        e1.event_type = "e1"
        e1.account_id = None
        e1.resource_type = None
        e1.resource_id = None
        e1.payload_json = {}
        e1.request_id = None
        e1.previous_hash = None
        e1.created_at = created_at

        e2 = MagicMock(
            spec=[
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
        )
        e2.id = event_id2
        e2.organisation_id = org_id
        e2.event_type = "e2"
        e2.account_id = None
        e2.resource_type = None
        e2.resource_id = None
        e2.payload_json = {}
        e2.request_id = None
        e2.previous_hash = "bad-hash"
        e2.created_at = created_at2

        call_count = 0

        async def _execute(*a, **kw):
            nonlocal call_count
            call_count += 1
            r = MagicMock()
            if call_count == 1:
                # First call: count query
                r.scalar = MagicMock(return_value=2)
            else:
                # Second call: fetch events
                r.scalars = MagicMock(return_value=[e1, e2])
            return r

        session.execute = _execute

        result = await verify_chain(session, org_id)
        assert result["valid"] is False
        assert result["first_tampered_id"] is not None


class TestExportChain:
    @pytest.fixture
    def session(self):
        s = MagicMock()
        s.flush = AsyncMock()
        s.execute = MagicMock()
        s.add = MagicMock()
        s.begin = MagicMock()
        return s

    async def test_export_empty(self, session):
        call_count = 0

        async def _execute(*a, **kw):
            nonlocal call_count
            call_count += 1
            r = MagicMock()
            if call_count == 1:
                r.scalars = MagicMock(return_value=[])
            else:
                r.scalar = MagicMock(return_value=0)
            return r

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
            r = MagicMock()
            if call_count == 1:
                r.scalars = MagicMock(return_value=[MagicMock(
                    id=uuid.uuid4(),
                    organisation_id=org_id,
                    event_type="test.event",
                    account_id=None,
                    resource_type=None,
                    resource_id=None,
                    payload_json={"seq": i},
                    request_id=None,
                    previous_hash=None,
                    created_at=MagicMock(isoformat=lambda: "2025-06-01T00:00:00+00:00"),
                ) for i in range(10)])
            else:
                r.scalar = MagicMock(return_value=total)
            return r

        session.execute = _execute

        result = await export_chain(session, org_id, page=2, page_size=10)
        assert result["page"] == 2
        assert result["page_size"] == 10
        assert result["total"] == 50
        assert len(result["items"]) == 10


class TestGetAuditEventsBatch:
    @pytest.fixture
    def session(self):
        s = MagicMock()
        s.flush = AsyncMock()
        s.execute = MagicMock()
        s.add = MagicMock()
        s.begin = MagicMock()
        return s

    async def test_batch_valid_uuids(self, session):
        org_id = uuid.uuid4()
        eid1 = str(uuid.uuid4())
        eid2 = str(uuid.uuid4())

        async def _execute(*a, **kw):
            r = MagicMock()
            r.scalars = MagicMock(return_value=[
                MagicMock(
                    id=uuid.UUID(eid1),
                    organisation_id=org_id,
                    event_type="test.event",
                    account_id=None,
                    resource_type=None,
                    resource_id=None,
                    payload_json={"key": "value"},
                    request_id=None,
                    previous_hash=None,
                    created_at=MagicMock(isoformat=lambda: "t"),
                ),
            ])
            return r

        session.execute = _execute

        result = await get_audit_events_batch(session, org_id, [eid1, eid2])
        assert len(result) == 1

    async def test_batch_skips_invalid_uuids(self, session):
        org_id = uuid.uuid4()

        async def _execute(*a, **kw):
            r = MagicMock()
            r.scalars = MagicMock(return_value=[])
            return r

        session.execute = _execute

        result = await get_audit_events_batch(session, org_id, ["not-a-uuid", "also-bad"])
        assert len(result) == 0

    async def test_batch_empty_list(self, session):
        org_id = uuid.uuid4()
        result = await get_audit_events_batch(session, org_id, [])
        assert result == []


class TestListAuditEvents:
    @pytest.fixture
    def session(self):
        s = MagicMock()
        s.flush = AsyncMock()
        s.execute = MagicMock()
        s.add = MagicMock()
        s.begin = MagicMock()
        return s

    async def test_list_default_limit(self, session):
        org_id = uuid.uuid4()

        call_count = 0

        async def _execute(*a, **kw):
            nonlocal call_count
            call_count += 1
            r = MagicMock()
            if call_count == 1:
                r.scalar = MagicMock(return_value=0)
            else:
                r.scalars = MagicMock(return_value=[])
            return r

        session.execute = _execute

        result = await list_audit_events(session, org_id)
        assert result["total"] == 0
        assert result["items"] == []

    async def test_list_with_invalid_cursor(self, session):
        org_id = uuid.uuid4()

        call_count = 0

        async def _execute(*a, **kw):
            nonlocal call_count
            call_count += 1
            r = MagicMock()
            if call_count == 1:
                r.scalar = MagicMock(return_value=0)
            else:
                r.scalars = MagicMock(return_value=[])
            return r

        session.execute = _execute

        result = await list_audit_events(session, org_id, cursor="not-a-uuid")
        assert result["total"] == 0
        assert result["items"] == []

    async def test_list_filter_by_event_type(self, session):
        org_id = uuid.uuid4()

        call_count = 0

        async def _execute(*a, **kw):
            nonlocal call_count
            call_count += 1
            r = MagicMock()
            if call_count == 1:
                r.scalar = MagicMock(return_value=1)
            else:
                r.scalars = MagicMock(return_value=[
                    MagicMock(
                        id=uuid.uuid4(),
                        organisation_id=org_id,
                        event_type="pipeline.autonomy_level_changed",
                        account_id=None,
                        resource_type="pipeline",
                        resource_id=uuid.uuid4(),
                        payload_json={},
                        request_id=None,
                        previous_hash=None,
                        created_at=MagicMock(isoformat=lambda: "t"),
                    ),
                ])
            return r

        session.execute = _execute

        result = await list_audit_events(session, org_id, event_type="pipeline.autonomy_level_changed")
        assert result["total"] == 1
        assert len(result["items"]) == 1
        assert result["items"][0]["event_type"] == "pipeline.autonomy_level_changed"

    async def test_list_with_has_more(self, session):
        org_id = uuid.uuid4()
        limit = 3

        call_count = 0

        async def _execute(*a, **kw):
            nonlocal call_count
            call_count += 1
            r = MagicMock()
            if call_count == 1:
                r.scalar = MagicMock(return_value=10)
            else:
                # Return limit+1 items to trigger has_more
                r.scalars = MagicMock(return_value=[
                    MagicMock(
                        id=uuid.uuid4(),
                        organisation_id=org_id,
                        event_type="test.event",
                        account_id=None,
                        resource_type=None,
                        resource_id=None,
                        payload_json={},
                        request_id=None,
                        previous_hash=None,
                        created_at=MagicMock(isoformat=lambda: f"t{i}"),
                    )
                    for i in range(limit + 1)
                ])
            return r

        session.execute = _execute

        result = await list_audit_events(session, org_id, limit=limit)
        assert len(result["items"]) == limit
        assert result["next_cursor"] is not None


class TestAppendAuditEventEdgeCases:
    @pytest.fixture
    def session(self):
        s = MagicMock()
        s.flush = AsyncMock()
        s.add = MagicMock()
        s.begin = MagicMock()
        return s

    @pytest.mark.parametrize("payload", [None, {}, {"key": "value"}])
    async def test_payload_none_defaults_to_empty_dict(self, session, payload):
        async def _execute(*a, **kw):
            r = MagicMock()
            r.scalar_one_or_none = MagicMock(return_value=None)
            return r

        session.execute = _execute

        event = await append_audit_event(
            session,
            org_id=uuid.uuid4(),
            event_type="test.event",
            payload_json=payload,
        )
        assert event.payload_json == (payload if payload is not None else {})

    async def test_request_id_passed_through(self, session):
        async def _execute(*a, **kw):
            r = MagicMock()
            r.scalar_one_or_none = MagicMock(return_value=None)
            return r

        session.execute = _execute

        event = await append_audit_event(
            session,
            org_id=uuid.uuid4(),
            event_type="test.event",
            request_id="req-abc-123",
        )
        assert event.request_id == "req-abc-123"

    async def test_previous_hash_none_for_first_event(self, session):
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

    async def test_actor_user_id_optional(self, session):
        async def _execute(*a, **kw):
            r = MagicMock()
            r.scalar_one_or_none = MagicMock(return_value=None)
            return r

        session.execute = _execute

        event = await append_audit_event(
            session,
            org_id=uuid.uuid4(),
            event_type="test.event",
            actor_user_id=None,
        )
        assert event.account_id is None

    async def test_resource_type_and_id_optional(self, session):
        async def _execute(*a, **kw):
            r = MagicMock()
            r.scalar_one_or_none = MagicMock(return_value=None)
            return r

        session.execute = _execute

        event = await append_audit_event(
            session,
            org_id=uuid.uuid4(),
            event_type="test.event",
            resource_type=None,
            resource_id=None,
        )
        assert event.resource_type is None
        assert event.resource_id is None


class TestExportEdgeCases:
    @pytest.fixture
    def session(self):
        s = MagicMock()
        s.flush = AsyncMock()
        s.execute = MagicMock()
        s.add = MagicMock()
        s.begin = MagicMock()
        return s

    async def test_export_with_large_payload(self, session):
        org_id = uuid.uuid4()
        large_payload = {"data": "x" * 10000}

        async def _execute(*a, **kw):
            r = MagicMock()
            r.scalars = MagicMock(return_value=[
                MagicMock(
                    id=uuid.uuid4(),
                    organisation_id=org_id,
                    event_type="test.event",
                    account_id=None,
                    resource_type=None,
                    resource_id=None,
                    payload_json=large_payload,
                    request_id=None,
                    previous_hash=None,
                    created_at=MagicMock(isoformat=lambda: "t"),
                ),
            ])
            r.scalar = MagicMock(return_value=1)
            return r

        session.execute = _execute

        result = await export_chain(session, org_id)
        assert len(result["items"]) == 1
        assert result["items"][0]["payload_json"] == large_payload

    async def test_export_with_none_dates(self, session):
        org_id = uuid.uuid4()

        async def _execute(*a, **kw):
            r = MagicMock()
            r.scalars = MagicMock(return_value=[
                MagicMock(
                    id=uuid.uuid4(),
                    organisation_id=org_id,
                    event_type="test.event",
                    account_id=None,
                    resource_type=None,
                    resource_id=None,
                    payload_json={},
                    request_id=None,
                    previous_hash=None,
                    created_at=None,
                ),
            ])
            r.scalar = MagicMock(return_value=1)
            return r

        session.execute = _execute

        result = await export_chain(session, org_id)
        assert result["items"][0]["created_at"] is None
