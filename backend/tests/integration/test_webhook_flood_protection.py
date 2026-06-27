"""Integration tests for webhook flood protection and deduplication.

Tests the full webhook pipeline in TriggerEngine:
  1. Deduplication — identical payload bodies produce only one accepted run
  2. Flood protection — max_concurrent_runs enforcement via TriggerEngine
  3. TriggerEvent audit records correctly capture dedup and flood events
"""

import hashlib
import hmac
import time
import uuid

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from modulo.core.trigger_engine import (
    ConcurrentRunLimitError,
    DuplicateWebhookError,
    HmacValidationError,
    TimestampExpiredError,
    TriggerEngine,
)
from modulo.db.rls import set_rls_org

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _hmac_sign(body: bytes, secret: str, ts: int) -> str:
    payload = f"{ts}.".encode() + body
    return "sha256=" + hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()


def _valid_timestamp() -> str:
    return str(int(time.time()))


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture(scope="module")
async def test_org(db_engine: AsyncEngine) -> uuid.UUID:
    org_id = uuid.uuid4()
    async with db_engine.connect() as conn:
        async with conn.begin():
            await conn.execute(
                text("INSERT INTO organisations (id, name, slug, settings_json) "
                     "VALUES (:id, :name, :slug, '{}'::json)"),
                {"id": str(org_id), "name": "Webhook Flood Org",
                 "slug": f"flood-{org_id.hex[:8]}"},
            )
    return org_id


@pytest_asyncio.fixture(scope="module")
async def test_user(db_engine: AsyncEngine, test_org: uuid.UUID) -> uuid.UUID:
    user_id = uuid.uuid4()
    async with db_engine.connect() as conn:
        async with conn.begin():
            await conn.execute(
                text("INSERT INTO users (id, organisation_id, email, display_name, "
                     "org_role, auth_provider, active, password_hash) "
                     "VALUES (:id, :oid, :email, :name, 'admin', 'local', true, 'hash')"),
                {"id": str(user_id), "oid": str(test_org),
                 "email": "flood-test@example.com", "name": "Flood Test User"},
            )
    return user_id


@pytest_asyncio.fixture(scope="module")
async def test_pipeline(db_engine: AsyncEngine, test_org: uuid.UUID, test_user: uuid.UUID) -> uuid.UUID:
    pipeline_id = uuid.uuid4()
    async with db_engine.connect() as conn:
        async with conn.begin():
            await conn.execute(
                text("INSERT INTO pipelines (id, organisation_id, name, created_by, "
                     "max_concurrent_runs, lock_wait_timeout_seconds, node_timeout_seconds, "
                     "run_context_defaults, graph_nodes_json) "
                     "VALUES (:id, :oid, :name, :uid, 10, 30, 300, '{}'::json, '[]'::json)"),
                {"id": str(pipeline_id), "oid": str(test_org),
                 "name": "Webhook Pipeline", "uid": str(test_user)},
            )
    return pipeline_id


@pytest_asyncio.fixture(scope="module")
async def test_snapshot(
    db_engine: AsyncEngine, test_org: uuid.UUID, test_pipeline: uuid.UUID
) -> uuid.UUID:
    snapshot_id = uuid.uuid4()
    async with db_engine.connect() as conn:
        async with conn.begin():
            await conn.execute(
                text("INSERT INTO pipeline_snapshots (id, pipeline_id, organisation_id, "
                     "snapshot_version, graph_json, connector_bindings_json, "
                     "schema_pins_json, prompt_pins_json, model_backend_pins_json, "
                     "run_context_defaults) "
                     "VALUES (:id, :pid, :oid, 1, '{}'::json, '[]'::json, "
                     "'[]'::json, '[]'::json, '[]'::json, '{}'::json)"),
                {"id": str(snapshot_id), "pid": str(test_pipeline), "oid": str(test_org)},
            )
    return snapshot_id


@pytest_asyncio.fixture(scope="module")
async def test_trigger(
    db_engine: AsyncEngine, test_org: uuid.UUID, test_pipeline: uuid.UUID, test_user: uuid.UUID,
) -> tuple[uuid.UUID, str]:
    trigger_id = uuid.uuid4()
    hmac_secret = "whsec_test_secret_key_1234567890"
    async with db_engine.connect() as conn:
        async with conn.begin():
            await conn.execute(
                text("INSERT INTO triggers (id, organisation_id, pipeline_id, "
                     "trigger_type, active, max_concurrent_runs, config_json, created_by) "
                     "VALUES (:id, :oid, :pid, 'webhook', true, 20, (:config)::json, :uid)"),
                {"id": str(trigger_id), "oid": str(test_org), "pid": str(test_pipeline),
                 "config": f'{{"hmac_secret": "{hmac_secret}"}}',
                 "uid": str(test_user)},
            )
    return trigger_id, hmac_secret


# ---------------------------------------------------------------------------
# Deduplication tests
# ---------------------------------------------------------------------------


class TestWebhookDeduplication:

    async def test_identical_payloads_dedup(
        self, db_engine: AsyncEngine, test_org: uuid.UUID,
        test_snapshot: uuid.UUID, test_trigger: tuple[uuid.UUID, str],
    ) -> None:
        trigger_id, hmac_secret = test_trigger
        body = b'{"event": "push", "ref": "refs/heads/main"}'
        raw_payload = {"event": "push", "ref": "refs/heads/main"}
        ts = _valid_timestamp()
        sig = _hmac_sign(body, hmac_secret, int(ts))
        payload_hash = hashlib.sha256(body).hexdigest()

        engine = TriggerEngine()
        factory = async_sessionmaker(db_engine, expire_on_commit=False)

        # First request should succeed
        async with factory() as session:
            async with session.begin():
                await set_rls_org(session, test_org)
                _run, event, _ = await engine.handle_webhook(
                    session, trigger_id=trigger_id, org_id=test_org,
                    raw_body=body, raw_payload=raw_payload,
                    hmac_signature=sig, modulo_timestamp=ts,
                    snapshot_id=test_snapshot,
                )
                assert event.validation_result == "accepted"

        # Remaining 19 are deduplicated
        dedup_count = 0
        for _ in range(19):
            async with factory() as session:
                async with session.begin():
                    await set_rls_org(session, test_org)
                    with pytest.raises(DuplicateWebhookError):
                        await engine.handle_webhook(
                            session, trigger_id=trigger_id, org_id=test_org,
                            raw_body=body, raw_payload=raw_payload,
                            hmac_signature=sig, modulo_timestamp=ts,
                            snapshot_id=test_snapshot,
                        )
                    result = await session.execute(
                        text("SELECT validation_result FROM trigger_events "
                             "WHERE trigger_id = :tid AND validation_result = 'deduplicated' "
                             "AND raw_payload_hash = :hash LIMIT 1"),
                        {"tid": str(trigger_id), "hash": payload_hash},
                    )
                    if result.scalar_one_or_none() is not None:
                        dedup_count += 1

        assert dedup_count >= 1, "No deduplicated TriggerEvent found"

        # Only one accepted event
        async with factory() as session:
            async with session.begin():
                await set_rls_org(session, test_org)
                result = await session.execute(
                    text("SELECT count(*) FROM trigger_events "
                         "WHERE trigger_id = :tid AND raw_payload_hash = :hash "
                         "AND validation_result = 'accepted'"),
                    {"tid": str(trigger_id), "hash": payload_hash},
                )
                assert result.scalar_one() == 1

    async def test_different_payloads_not_deduped(
        self, db_engine: AsyncEngine, test_org: uuid.UUID,
        test_snapshot: uuid.UUID, test_trigger: tuple[uuid.UUID, str],
    ) -> None:
        trigger_id, hmac_secret = test_trigger
        engine = TriggerEngine()
        factory = async_sessionmaker(db_engine, expire_on_commit=False)

        run_ids = []
        for i in range(5):
            body = f'{{"event": "push", "seq": {i}}}'.encode()
            ts = _valid_timestamp()
            sig = _hmac_sign(body, hmac_secret, int(ts))
            async with factory() as session:
                async with session.begin():
                    await set_rls_org(session, test_org)
                    run, event, _ = await engine.handle_webhook(
                        session, trigger_id=trigger_id, org_id=test_org,
                        raw_body=body, raw_payload={"event": "push", "seq": i},
                        hmac_signature=sig, modulo_timestamp=ts,
                        snapshot_id=test_snapshot,
                    )
                    run_ids.append(run.id)
                    assert event.validation_result == "accepted"

        assert len(set(run_ids)) == 5


# ---------------------------------------------------------------------------
# HMAC validation tests
# ---------------------------------------------------------------------------


class TestWebhookHmac:

    async def test_missing_hmac_rejected(
        self, db_engine: AsyncEngine, test_org: uuid.UUID,
        test_snapshot: uuid.UUID, test_trigger: tuple[uuid.UUID, str],
    ) -> None:
        trigger_id, _ = test_trigger
        engine = TriggerEngine()
        factory = async_sessionmaker(db_engine, expire_on_commit=False)
        async with factory() as session:
            async with session.begin():
                await set_rls_org(session, test_org)
                with pytest.raises(HmacValidationError):
                    await engine.handle_webhook(
                        session, trigger_id=trigger_id, org_id=test_org,
                        raw_body=b'{"event": "push"}', raw_payload={"event": "push"},
                        hmac_signature=None, modulo_timestamp=_valid_timestamp(),
                        snapshot_id=test_snapshot,
                    )

    async def test_invalid_hmac_rejected(
        self, db_engine: AsyncEngine, test_org: uuid.UUID,
        test_snapshot: uuid.UUID, test_trigger: tuple[uuid.UUID, str],
    ) -> None:
        trigger_id, _ = test_trigger
        engine = TriggerEngine()
        factory = async_sessionmaker(db_engine, expire_on_commit=False)
        async with factory() as session:
            async with session.begin():
                await set_rls_org(session, test_org)
                with pytest.raises(HmacValidationError):
                    await engine.handle_webhook(
                        session, trigger_id=trigger_id, org_id=test_org,
                        raw_body=b'{"event": "push"}', raw_payload={"event": "push"},
                        hmac_signature="sha256=invalidhash",
                        modulo_timestamp=_valid_timestamp(),
                        snapshot_id=test_snapshot,
                    )


# ---------------------------------------------------------------------------
# Timestamp validation tests
# ---------------------------------------------------------------------------


class TestWebhookTimestamp:

    async def test_expired_timestamp_rejected(
        self, db_engine: AsyncEngine, test_org: uuid.UUID,
        test_snapshot: uuid.UUID, test_trigger: tuple[uuid.UUID, str],
    ) -> None:
        trigger_id, hmac_secret = test_trigger
        engine = TriggerEngine()
        factory = async_sessionmaker(db_engine, expire_on_commit=False)
        body = b'{"event": "old"}'
        old_ts = str(int(time.time()) - 600)
        sig = _hmac_sign(body, hmac_secret, int(old_ts))

        async with factory() as session:
            async with session.begin():
                await set_rls_org(session, test_org)
                with pytest.raises(TimestampExpiredError):
                    await engine.handle_webhook(
                        session, trigger_id=trigger_id, org_id=test_org,
                        raw_body=body, raw_payload={"event": "old"},
                        hmac_signature=sig, modulo_timestamp=old_ts,
                        snapshot_id=test_snapshot,
                    )


# ---------------------------------------------------------------------------
# Flood protection tests
# ---------------------------------------------------------------------------


class TestWebhookFloodProtection:

    async def _fill_active_runs(
        self, db_engine: AsyncEngine, test_org: uuid.UUID,
        test_pipeline: uuid.UUID, test_snapshot: uuid.UUID,
        test_trigger: uuid.UUID, count: int, tag: str = "flood",
    ) -> None:
        factory = async_sessionmaker(db_engine, expire_on_commit=False)
        async with factory() as session:
            async with session.begin():
                await set_rls_org(session, test_org)
                for i in range(count):
                    run_id = uuid.uuid4()
                    tid_str = f"{test_org}:{tag}-{i}"
                    await session.execute(
                        text("INSERT INTO runs (id, organisation_id, pipeline_id, "
                             "snapshot_id, trigger_type, status, input_hash, "
                             "langgraph_thread_id, trigger_id) "
                             "VALUES (:rid, :oid, :pid, :sid, 'webhook', 'running', "
                             ":hash, :tid, :tid2)"),
                        {"rid": str(run_id), "oid": str(test_org), "pid": str(test_pipeline),
                         "sid": str(test_snapshot),
                         "hash": hashlib.sha256(f"{tag}-{i}".encode()).hexdigest(),
                         "tid": tid_str, "tid2": str(test_trigger)},
                    )

    async def test_flood_protection_rejects_when_at_limit(
        self, db_engine: AsyncEngine, test_org: uuid.UUID,
        test_pipeline: uuid.UUID, test_snapshot: uuid.UUID,
        test_trigger: tuple[uuid.UUID, str],
    ) -> None:
        trigger_id, hmac_secret = test_trigger
        await self._fill_active_runs(db_engine, test_org, test_pipeline,
                                      test_snapshot, trigger_id, 20, tag="reject")

        body = b'{"event": "flood-test"}'
        ts = _valid_timestamp()
        sig = _hmac_sign(body, hmac_secret, int(ts))

        engine = TriggerEngine()
        factory = async_sessionmaker(db_engine, expire_on_commit=False)
        async with factory() as session:
            async with session.begin():
                await set_rls_org(session, test_org)
                with pytest.raises(ConcurrentRunLimitError) as exc_info:
                    await engine.handle_webhook(
                        session, trigger_id=trigger_id, org_id=test_org,
                        raw_body=body, raw_payload={"event": "flood-test"},
                        hmac_signature=sig, modulo_timestamp=ts,
                        snapshot_id=test_snapshot,
                    )
                assert exc_info.value.limit == 20

    async def test_flood_protection_logs_concurrency_limit_event(
        self, db_engine: AsyncEngine, test_org: uuid.UUID,
        test_pipeline: uuid.UUID, test_snapshot: uuid.UUID,
        test_trigger: tuple[uuid.UUID, str],
    ) -> None:
        trigger_id, hmac_secret = test_trigger
        await self._fill_active_runs(db_engine, test_org, test_pipeline,
                                      test_snapshot, trigger_id, 20, tag="logtest")

        body = b'{"event": "log-test"}'
        ts = _valid_timestamp()
        sig = _hmac_sign(body, hmac_secret, int(ts))

        engine = TriggerEngine()
        factory = async_sessionmaker(db_engine, expire_on_commit=False)
        async with factory() as session:
            async with session.begin():
                await set_rls_org(session, test_org)
                with pytest.raises(ConcurrentRunLimitError):
                    await engine.handle_webhook(
                        session, trigger_id=trigger_id, org_id=test_org,
                        raw_body=body, raw_payload={"event": "log-test"},
                        hmac_signature=sig, modulo_timestamp=ts,
                        snapshot_id=test_snapshot,
                    )
                # Event was flushed and committed (exception caught by pytest.raises)
                result = await session.execute(
                    text("SELECT validation_result, trigger_type, run_id "
                         "FROM trigger_events "
                         "WHERE trigger_id = :tid "
                         "AND validation_result = 'concurrency_limit_reached'"),
                    {"tid": str(trigger_id)},
                )
                row = result.fetchone()
                assert row is not None, "Expected concurrency_limit_reached TriggerEvent"
                assert row.trigger_type == "webhook"
                assert row.run_id is None


# ---------------------------------------------------------------------------
# Full pipeline test — HMAC + dedup + flood + TriggerEvents
# ---------------------------------------------------------------------------


class TestWebhookFullPipeline:

    async def test_full_pipeline_happy_path(
        self, db_engine: AsyncEngine, test_org: uuid.UUID, test_user: uuid.UUID,
    ) -> None:
        """Uses a private pipeline/trigger to avoid state pollution from other tests."""
        factory = async_sessionmaker(db_engine, expire_on_commit=False)
        hmac_secret = "whsec_full_test_secret"
        trigger_id, pipeline_id, snapshot_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()

        async with factory() as session:
            async with session.begin():
                await set_rls_org(session, test_org)
                await session.execute(
                    text("INSERT INTO pipelines (id, organisation_id, name, created_by, "
                         "max_concurrent_runs, lock_wait_timeout_seconds, "
                         "node_timeout_seconds, run_context_defaults, graph_nodes_json) "
                         "VALUES (:id, :oid, :name, :uid, 10, 30, 300, '{}'::json, '[]'::json)"),
                    {"id": str(pipeline_id), "oid": str(test_org),
                     "name": "Full Pipeline Test", "uid": str(test_user)},
                )
                await session.execute(
                    text("INSERT INTO pipeline_snapshots (id, pipeline_id, organisation_id, "
                         "snapshot_version, graph_json, connector_bindings_json, "
                         "schema_pins_json, prompt_pins_json, model_backend_pins_json, "
                         "run_context_defaults) "
                         "VALUES (:id, :pid, :oid, 1, '{}'::json, '[]'::json, "
                         "'[]'::json, '[]'::json, '[]'::json, '{}'::json)"),
                    {"id": str(snapshot_id), "pid": str(pipeline_id), "oid": str(test_org)},
                )
                await session.execute(
                    text("INSERT INTO triggers (id, organisation_id, pipeline_id, "
                         "trigger_type, active, max_concurrent_runs, config_json, created_by) "
                         "VALUES (:id, :oid, :pid, 'webhook', true, 20, (:config)::json, :uid)"),
                    {"id": str(trigger_id), "oid": str(test_org), "pid": str(pipeline_id),
                     "config": f'{{"hmac_secret": "{hmac_secret}"}}',
                     "uid": str(test_user)},
                )

        engine = TriggerEngine()
        body = b'{"action": "opened", "issue": {"number": 42}}'
        ts = _valid_timestamp()
        sig = _hmac_sign(body, hmac_secret, int(ts))

        async with factory() as session:
            async with session.begin():
                await set_rls_org(session, test_org)
                run, event, _ = await engine.handle_webhook(
                    session, trigger_id=trigger_id, org_id=test_org,
                    raw_body=body, raw_payload={"action": "opened", "issue": {"number": 42}},
                    hmac_signature=sig, modulo_timestamp=ts,
                    snapshot_id=snapshot_id,
                )

                assert run.status == "pending"
                assert run.trigger_type == "webhook"
                assert run.organisation_id == test_org
                assert run.snapshot_id == snapshot_id

                assert event.validation_result == "accepted"
                assert event.trigger_id == trigger_id
                assert event.run_id == run.id

                # Verify dedup hash was stored
                payload_hash = hashlib.sha256(body).hexdigest()
                result = await session.execute(
                    text("SELECT payload_hash, expires_at FROM webhook_dedup_hashes "
                         "WHERE trigger_id = :tid AND payload_hash = :hash"),
                    {"tid": str(trigger_id), "hash": payload_hash},
                )
                row = result.fetchone()
                assert row is not None, "Dedup hash should exist"
                assert row.expires_at is not None
