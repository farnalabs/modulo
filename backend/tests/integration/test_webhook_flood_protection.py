"""Integration tests for webhook flood protection and deduplication.

Tests the full webhook pipeline in TriggerEngine:
  1. Deduplication — identical payload bodies produce only one accepted run
  2. Flood protection — max_concurrent_runs enforcement via TriggerEngine
  3. TriggerEvent audit records correctly capture dedup and flood events
"""

import hashlib
import hmac
import json
import time
import uuid

import pytest
import pytest_asyncio
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from modulo.core.run_admission import has_in_flight_ci_failure_run
from modulo.core.trigger_engine import (
    ConcurrentRunLimitError,
    DuplicateWebhookError,
    HmacValidationError,
    TimestampExpiredError,
    TriggerEngine,
)
from modulo.core.trigger_engine.pre_guardrail import canonical_payload_hash
from modulo.db.crud.run import CI_FAILURE_COALESCE_KEY_FIELD
from modulo.db.models.run import Run
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
# Fixtures — inherited from top-level integration/conftest.py:
#   test_org, test_user, test_pipeline, test_snapshot
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture(scope="module")
async def test_trigger(
    db_engine: AsyncEngine,
    test_org: uuid.UUID,
    test_pipeline: uuid.UUID,
    test_user: uuid.UUID,
) -> tuple[uuid.UUID, str]:
    trigger_id = uuid.uuid4()
    hmac_secret = "whsec_test_secret_key_1234567890"
    async with db_engine.connect() as conn, conn.begin():
        await conn.execute(
            text(
                "INSERT INTO triggers (name, id, organisation_id, pipeline_id, "
                "trigger_type, active, max_concurrent_runs, config_json, account_id) "
                "VALUES (gen_random_uuid()::text, :id, :oid, :pid, 'webhook', true, 20, (:config)::json, :uid)",
            ),
            {
                "id": str(trigger_id),
                "oid": str(test_org),
                "pid": str(test_pipeline),
                "config": json.dumps({"hmac_secret": hmac_secret}),
                "uid": str(test_user),
            },
        )
    return trigger_id, hmac_secret


# ---------------------------------------------------------------------------
# Deduplication tests
# ---------------------------------------------------------------------------


class TestWebhookDeduplication:
    async def test_identical_payloads_dedup(
        self,
        db_engine: AsyncEngine,
        test_org: uuid.UUID,
        test_snapshot: uuid.UUID,
        test_trigger: tuple[uuid.UUID, str],
    ) -> None:
        trigger_id, hmac_secret = test_trigger
        body = b'{"event": "push", "ref": "refs/heads/main"}'
        raw_payload = {"event": "push", "ref": "refs/heads/main"}
        ts = _valid_timestamp()
        sig = _hmac_sign(body, hmac_secret, int(ts))
        # FAR-214: the dedup hash is the canonical POST-guardrail payload hash
        # (not the raw-body hash), so logically identical payloads dedup across
        # encodings. Event lookups key on that canonical hash.
        payload_hash = canonical_payload_hash(raw_payload)

        engine = TriggerEngine()
        factory = async_sessionmaker(db_engine, expire_on_commit=False)

        # First request should succeed
        async with factory() as session, session.begin():
            await set_rls_org(session, test_org)
            _run, event, _ = await engine.handle_webhook(
                session,
                trigger_id=trigger_id,
                org_id=test_org,
                raw_body=body,
                raw_payload=raw_payload,
                hmac_signature=sig,
                modulo_timestamp=ts,
                snapshot_id=test_snapshot,
            )
            assert event.validation_result == "accepted"

        # Remaining 19 are deduplicated
        dedup_count = 0
        for _ in range(19):
            async with factory() as session, session.begin():
                await set_rls_org(session, test_org)
                with pytest.raises(DuplicateWebhookError):
                    await engine.handle_webhook(
                        session,
                        trigger_id=trigger_id,
                        org_id=test_org,
                        raw_body=body,
                        raw_payload=raw_payload,
                        hmac_signature=sig,
                        modulo_timestamp=ts,
                        snapshot_id=test_snapshot,
                    )
            async with factory() as session, session.begin():
                await set_rls_org(session, test_org)
                result = await session.execute(
                    text(
                        "SELECT validation_result FROM trigger_events "
                        "WHERE trigger_id = :tid AND validation_result = 'deduplicated' "
                        "AND raw_payload_hash = :hash LIMIT 1",
                    ),
                    {"tid": str(trigger_id), "hash": payload_hash},
                )
                if result.scalar_one_or_none() is not None:
                    dedup_count += 1

        assert dedup_count >= 1, "No deduplicated TriggerEvent found"

        # Only one accepted event
        async with factory() as session, session.begin():
            await set_rls_org(session, test_org)
            result = await session.execute(
                text(
                    "SELECT count(*) FROM trigger_events "
                    "WHERE trigger_id = :tid AND raw_payload_hash = :hash "
                    "AND validation_result = 'accepted'",
                ),
                {"tid": str(trigger_id), "hash": payload_hash},
            )
            assert result.scalar_one() == 1

    async def test_different_payloads_not_deduped(
        self,
        db_engine: AsyncEngine,
        test_org: uuid.UUID,
        test_snapshot: uuid.UUID,
        test_trigger: tuple[uuid.UUID, str],
    ) -> None:
        trigger_id, hmac_secret = test_trigger
        engine = TriggerEngine()
        factory = async_sessionmaker(db_engine, expire_on_commit=False)

        run_ids = []
        for i in range(5):
            body = json.dumps({"event": "push", "seq": i}).encode()
            ts = _valid_timestamp()
            sig = _hmac_sign(body, hmac_secret, int(ts))
            async with factory() as session, session.begin():
                await set_rls_org(session, test_org)
                run, event, _ = await engine.handle_webhook(
                    session,
                    trigger_id=trigger_id,
                    org_id=test_org,
                    raw_body=body,
                    raw_payload={"event": "push", "seq": i},
                    hmac_signature=sig,
                    modulo_timestamp=ts,
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
        self,
        db_engine: AsyncEngine,
        test_org: uuid.UUID,
        test_snapshot: uuid.UUID,
        test_trigger: tuple[uuid.UUID, str],
    ) -> None:
        trigger_id, _ = test_trigger
        engine = TriggerEngine()
        factory = async_sessionmaker(db_engine, expire_on_commit=False)
        async with factory() as session, session.begin():
            await set_rls_org(session, test_org)
            with pytest.raises(HmacValidationError):
                await engine.handle_webhook(
                    session,
                    trigger_id=trigger_id,
                    org_id=test_org,
                    raw_body=b'{"event": "push"}',
                    raw_payload={"event": "push"},
                    hmac_signature=None,
                    modulo_timestamp=_valid_timestamp(),
                    snapshot_id=test_snapshot,
                )

    async def test_invalid_hmac_rejected(
        self,
        db_engine: AsyncEngine,
        test_org: uuid.UUID,
        test_snapshot: uuid.UUID,
        test_trigger: tuple[uuid.UUID, str],
    ) -> None:
        trigger_id, _ = test_trigger
        engine = TriggerEngine()
        factory = async_sessionmaker(db_engine, expire_on_commit=False)
        async with factory() as session, session.begin():
            await set_rls_org(session, test_org)
            with pytest.raises(HmacValidationError):
                await engine.handle_webhook(
                    session,
                    trigger_id=trigger_id,
                    org_id=test_org,
                    raw_body=b'{"event": "push"}',
                    raw_payload={"event": "push"},
                    hmac_signature="sha256=invalidhash",
                    modulo_timestamp=_valid_timestamp(),
                    snapshot_id=test_snapshot,
                )


# ---------------------------------------------------------------------------
# Timestamp validation tests
# ---------------------------------------------------------------------------


class TestWebhookTimestamp:
    async def test_expired_timestamp_rejected(
        self,
        db_engine: AsyncEngine,
        test_org: uuid.UUID,
        test_snapshot: uuid.UUID,
        test_trigger: tuple[uuid.UUID, str],
    ) -> None:
        trigger_id, hmac_secret = test_trigger
        engine = TriggerEngine()
        factory = async_sessionmaker(db_engine, expire_on_commit=False)
        body = b'{"event": "old"}'
        old_ts = str(int(time.time()) - 600)
        sig = _hmac_sign(body, hmac_secret, int(old_ts))

        async with factory() as session, session.begin():
            await set_rls_org(session, test_org)
            with pytest.raises(TimestampExpiredError):
                await engine.handle_webhook(
                    session,
                    trigger_id=trigger_id,
                    org_id=test_org,
                    raw_body=body,
                    raw_payload={"event": "old"},
                    hmac_signature=sig,
                    modulo_timestamp=old_ts,
                    snapshot_id=test_snapshot,
                )


# ---------------------------------------------------------------------------
# Flood protection tests
# ---------------------------------------------------------------------------


class TestWebhookFloodProtection:
    async def _fill_active_runs(
        self,
        db_engine: AsyncEngine,
        test_org: uuid.UUID,
        test_pipeline: uuid.UUID,
        test_snapshot: uuid.UUID,
        test_trigger: uuid.UUID,
        count: int,
        tag: str = "flood",
    ) -> None:
        factory = async_sessionmaker(db_engine, expire_on_commit=False)
        async with factory() as session, session.begin():
            await set_rls_org(session, test_org)
            for i in range(count):
                run_id = uuid.uuid4()
                tid_str = f"{test_org}:{tag}-{i}-{uuid.uuid4().hex[:6]}"
                await session.execute(
                    text(
                        "INSERT INTO runs (id, organisation_id, pipeline_id, "
                        "snapshot_id, trigger_type, status, input_hash, "
                        "langgraph_thread_id, trigger_id, run_number) "
                        "VALUES (:rid, :oid, :pid, :sid, 'webhook', 'running', "
                        ":hash, :tid, :tid2, :rn)",
                    ),
                    {
                        "rid": str(run_id),
                        "oid": str(test_org),
                        "pid": str(test_pipeline),
                        "sid": str(test_snapshot),
                        "hash": hashlib.sha256(f"{tag}-{i}".encode()).hexdigest(),
                        "tid": tid_str,
                        "tid2": str(test_trigger),
                        "rn": int(run_id.int % 10**9) + 1,
                    },
                )

    async def test_flood_protection_refuses_when_at_limit(
        self,
        db_engine: AsyncEngine,
        test_org: uuid.UUID,
        test_pipeline: uuid.UUID,
        test_snapshot: uuid.UUID,
        test_trigger: tuple[uuid.UUID, str],
    ) -> None:
        """FAR-1863: at the trigger's declared cap the delivery is REFUSED.

        The old accept-and-queue behaviour let the trigger's
        ``max_concurrent_runs`` bound nothing — a webhook burst piled on
        unbounded runs. ``handle_webhook`` now raises ``ConcurrentRunLimitError``
        (the receive route maps it to 429) and creates NO run.
        """
        trigger_id, hmac_secret = test_trigger
        await self._fill_active_runs(db_engine, test_org, test_pipeline, test_snapshot, trigger_id, 20, tag="reject")

        body = b'{"event": "flood-test"}'
        ts = _valid_timestamp()
        sig = _hmac_sign(body, hmac_secret, int(ts))

        engine = TriggerEngine()
        factory = async_sessionmaker(db_engine, expire_on_commit=False)

        # ``test_trigger`` is a module-scoped fixture reused by every class in
        # this file, so absolute counts are unsafe — capture the baseline this
        # delivery must not change and assert the delta.
        async with factory() as session, session.begin():
            await set_rls_org(session, test_org)
            before = await session.execute(
                text("SELECT count(*) FROM runs WHERE trigger_id = :tid AND trigger_type = 'webhook'"),
                {"tid": str(trigger_id)},
            )
            runs_before = before.scalar_one()

        async def _deliver() -> None:
            async with factory() as session, session.begin():
                await set_rls_org(session, test_org)
                await engine.handle_webhook(
                    session,
                    trigger_id=trigger_id,
                    org_id=test_org,
                    raw_body=body,
                    raw_payload={"event": "flood-test"},
                    hmac_signature=sig,
                    modulo_timestamp=ts,
                    snapshot_id=test_snapshot,
                )

        with pytest.raises(ConcurrentRunLimitError):
            await _deliver()

        async with factory() as session, session.begin():
            await set_rls_org(session, test_org)
            counted = await session.execute(
                text(
                    "SELECT count(*) FROM runs WHERE trigger_id = :tid AND trigger_type = 'webhook'",
                ),
                {"tid": str(trigger_id)},
            )
            assert counted.scalar_one() == runs_before, "The refused delivery must not create a run"

    async def test_flood_protection_refusal_creates_no_run_or_accepted_event(
        self,
        db_engine: AsyncEngine,
        test_org: uuid.UUID,
        test_pipeline: uuid.UUID,
        test_snapshot: uuid.UUID,
        test_trigger: tuple[uuid.UUID, str],
    ) -> None:
        """FAR-1863: a refused at-cap delivery creates no run and no accepted event.

        The engine raises BEFORE the run insert, so neither a run nor its
        ``accepted`` TriggerEvent is committed (the transaction unwinds with the
        refusal). This replaces the previous accept-and-queue assertion, where
        the engine logged ``concurrency_limit_reached`` and still created the
        run.
        """
        trigger_id, hmac_secret = test_trigger
        await self._fill_active_runs(db_engine, test_org, test_pipeline, test_snapshot, trigger_id, 20, tag="logtest")

        body = b'{"event": "log-test"}'
        ts = _valid_timestamp()
        sig = _hmac_sign(body, hmac_secret, int(ts))

        engine = TriggerEngine()
        factory = async_sessionmaker(db_engine, expire_on_commit=False)

        # ``test_trigger`` is module-scoped and shared: earlier classes commit
        # accepted events against the same trigger, so the assertion is a delta
        # (this refusal must add no accepted event) rather than an absolute 0.
        async with factory() as session, session.begin():
            await set_rls_org(session, test_org)
            before = await session.execute(
                text(
                    "SELECT count(*) FROM trigger_events WHERE trigger_id = :tid AND validation_result = 'accepted'",
                ),
                {"tid": str(trigger_id)},
            )
            accepted_before = before.scalar_one()

        async def _deliver() -> None:
            async with factory() as session, session.begin():
                await set_rls_org(session, test_org)
                await engine.handle_webhook(
                    session,
                    trigger_id=trigger_id,
                    org_id=test_org,
                    raw_body=body,
                    raw_payload={"event": "log-test"},
                    hmac_signature=sig,
                    modulo_timestamp=ts,
                    snapshot_id=test_snapshot,
                )

        with pytest.raises(ConcurrentRunLimitError):
            await _deliver()

        async with factory() as session, session.begin():
            await set_rls_org(session, test_org)
            accepted = await session.execute(
                text(
                    "SELECT count(*) FROM trigger_events WHERE trigger_id = :tid AND validation_result = 'accepted'",
                ),
                {"tid": str(trigger_id)},
            )
            assert accepted.scalar_one() == accepted_before, "The refused delivery must not record an accepted event"


# ---------------------------------------------------------------------------
# Full pipeline test — HMAC + dedup + flood + TriggerEvents
# ---------------------------------------------------------------------------


class TestWebhookFullPipeline:
    async def test_full_pipeline_happy_path(
        self,
        db_engine: AsyncEngine,
        test_org: uuid.UUID,
        test_user: uuid.UUID,
    ) -> None:
        """Uses a private pipeline/trigger to avoid state pollution from other tests."""
        factory = async_sessionmaker(db_engine, expire_on_commit=False)
        hmac_secret = "whsec_full_test_secret"
        trigger_id, pipeline_id, snapshot_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()

        async with factory() as session, session.begin():
            await set_rls_org(session, test_org)
            await session.execute(
                text(
                    "INSERT INTO pipelines (id, organisation_id, name, account_id, "
                    "max_concurrent_runs, lock_wait_timeout_seconds, "
                    "node_timeout_seconds, run_context_defaults, graph_nodes_json) "
                    "VALUES (:id, :oid, :name, :uid, 10, 30, 300, '{}'::json, '[]'::json)",
                ),
                {"id": str(pipeline_id), "oid": str(test_org), "name": "Full Pipeline Test", "uid": str(test_user)},
            )
            await session.execute(
                text(
                    "INSERT INTO pipeline_snapshots (id, pipeline_id, organisation_id, "
                    "snapshot_version, graph_json, connector_bindings_json, "
                    "schema_pins_json, prompt_pins_json, model_backend_pins_json, "
                    "run_context_defaults, config_json) "
                    "VALUES (:id, :pid, :oid, 1, '{}'::json, '[]'::json, "
                    "'[]'::json, '[]'::json, '[]'::json, '{}'::json, '{}'::json)",
                ),
                {"id": str(snapshot_id), "pid": str(pipeline_id), "oid": str(test_org)},
            )
            await session.execute(
                text(
                    "INSERT INTO triggers (name, id, organisation_id, pipeline_id, "
                    "trigger_type, active, max_concurrent_runs, config_json, account_id) "
                    "VALUES (gen_random_uuid()::text, :id, :oid, :pid, 'webhook', true, 20, (:config)::json, :uid)",
                ),
                {
                    "id": str(trigger_id),
                    "oid": str(test_org),
                    "pid": str(pipeline_id),
                    "config": json.dumps({"hmac_secret": hmac_secret}),
                    "uid": str(test_user),
                },
            )

        engine = TriggerEngine()
        body = b'{"action": "opened", "issue": {"number": 42}}'
        ts = _valid_timestamp()
        sig = _hmac_sign(body, hmac_secret, int(ts))

        async with factory() as session, session.begin():
            await set_rls_org(session, test_org)
            run, event, _ = await engine.handle_webhook(
                session,
                trigger_id=trigger_id,
                org_id=test_org,
                raw_body=body,
                raw_payload={"action": "opened", "issue": {"number": 42}},
                hmac_signature=sig,
                modulo_timestamp=ts,
                snapshot_id=snapshot_id,
            )

            assert run.status == "pending"
            assert run.trigger_type == "webhook"
            assert run.organisation_id == test_org
            assert run.snapshot_id == snapshot_id

            assert event.validation_result == "accepted"
            assert event.trigger_id == trigger_id
            assert event.run_id == run.id

            # Verify dedup hash was stored (canonical POST-guardrail hash, FAR-214)
            payload_hash = canonical_payload_hash({"action": "opened", "issue": {"number": 42}})
            result = await session.execute(
                text(
                    "SELECT payload_hash, expires_at FROM webhook_dedup_hashes "
                    "WHERE trigger_id = :tid AND payload_hash = :hash",
                ),
                {"tid": str(trigger_id), "hash": payload_hash},
            )
            row = result.fetchone()
            assert row is not None, "Dedup hash should exist"
            assert row.expires_at is not None


# ---------------------------------------------------------------------------
# FAR-1034 CI-failure coalesce identity — round-trip through ingestion
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture(scope="module")
async def ci_failure_mapped_trigger(
    db_engine: AsyncEngine,
    test_org: uuid.UUID,
    test_pipeline: uuid.UUID,
    test_user: uuid.UUID,
) -> tuple[uuid.UUID, str]:
    """A webhook trigger whose ``payload_mapping`` REPLACES the payload wholesale.

    ``prNumber`` / ``headSha`` are renamed away, which is exactly the config that
    made the in-flight leg silently inert before the engine-stamped key: the
    stored payload no longer carries the fields the leg used to re-derive from.
    """
    trigger_id = uuid.uuid4()
    hmac_secret = "whsec_ci_mapping_secret_1234567890"
    async with db_engine.connect() as conn, conn.begin():
        await conn.execute(
            text(
                "INSERT INTO triggers (name, id, organisation_id, pipeline_id, "
                "trigger_type, active, max_concurrent_runs, config_json, account_id) "
                "VALUES (gen_random_uuid()::text, :id, :oid, :pid, 'webhook', true, 20, (:config)::json, :uid)",
            ),
            {
                "id": str(trigger_id),
                "oid": str(test_org),
                "pid": str(test_pipeline),
                "config": json.dumps(
                    {
                        "hmac_secret": hmac_secret,
                        "payload_mapping": {"pr": "prNumber", "sha": "headSha"},
                    }
                ),
                "uid": str(test_user),
            },
        )
    return trigger_id, hmac_secret


class TestCiFailureKeySurvivesPayloadMapping:
    async def test_stored_row_key_matches_incoming_key(
        self,
        db_engine: AsyncEngine,
        test_org: uuid.UUID,
        test_pipeline: uuid.UUID,
        test_snapshot: uuid.UUID,
        ci_failure_mapped_trigger: tuple[uuid.UUID, str],
    ) -> None:
        trigger_id, hmac_secret = ci_failure_mapped_trigger
        raw_payload = {
            "branchName": "prompt-pr-777",
            "prNumber": 777,
            "headSha": "abc123",
            "runUrl": "https://github.com/farnalabs/modulo/actions/runs/1",
            "failureDescription": "CI checks failed on PR #777",
        }
        body = json.dumps(raw_payload).encode()
        ts = _valid_timestamp()
        sig = _hmac_sign(body, hmac_secret, int(ts))
        ci_key = "ci-failure:pr:777:sha:abc123"

        factory = async_sessionmaker(db_engine, expire_on_commit=False)
        async with factory() as session, session.begin():
            await set_rls_org(session, test_org)
            run, event, input_payload = await TriggerEngine().handle_webhook(
                session,
                trigger_id=trigger_id,
                org_id=test_org,
                raw_body=body,
                raw_payload=raw_payload,
                hmac_signature=sig,
                modulo_timestamp=ts,
                snapshot_id=test_snapshot,
            )
            assert event.validation_result == "accepted"

            # The mapping replaced the payload: the raw CI fields are gone.
            assert input_payload == {"pr": 777, "sha": "abc123"}

            # ...but the run carries the engine-stamped identity.
            stored = (await session.execute(select(Run.input_payload).where(Run.id == run.id))).scalar_one()
            assert "prNumber" not in stored
            assert stored[CI_FAILURE_COALESCE_KEY_FIELD] == ci_key

            # The in-flight leg matches the incoming key off the STORED row.
            assert await has_in_flight_ci_failure_run(
                session,
                pipeline_id=test_pipeline,
                coalesce_key=ci_key,
            )
