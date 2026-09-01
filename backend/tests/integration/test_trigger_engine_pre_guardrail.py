"""Integration tests for the FAR-214 pre-trigger guardrail pass (real Postgres).

Exercises the REAL ``TriggerEngine.handle_webhook`` path against testcontainers
Postgres with guardrail rows bound to a dedicated pipeline:

  * a block-action guardrail → the delivery is rejected
    (``GuardrailBlockedAtIntakeError``), a ``guardrail_blocked`` TriggerEvent
    row is committed, no run is created and no dedup slot is consumed;
  * a redact-action guardrail → the persisted run's ``input_payload`` is
    POST-redaction;
  * canonical dedup — logically identical payloads delivered with different raw
    encodings dedup (the FAR-214 encoding-bypass closure), while different
    payloads do not.

Each test uses its OWN pipeline (fresh UUIDs) so bound guardrails never leak
into the shared conftest pipeline consumed by other integration tests.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
import uuid
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from modulo.core.trigger_engine import DuplicateWebhookError, TriggerEngine
from modulo.core.trigger_engine.pre_guardrail import (
    GuardrailBlockedAtIntakeError,
    canonical_payload_hash,
    run_pre_trigger_guardrail_pass,
)
from modulo.db.rls import set_rls_org

pytestmark = pytest.mark.integration


def _hmac_sign(body: bytes, secret: str, ts: int) -> str:
    payload = f"{ts}.".encode() + body
    return "sha256=" + hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()


def _valid_timestamp() -> str:
    return str(int(time.time()))


@pytest_asyncio.fixture
async def intake_rig(
    db_engine: AsyncEngine,
    test_org: uuid.UUID,
    test_user: uuid.UUID,
) -> dict[str, uuid.UUID | str]:
    """A dedicated pipeline + snapshot + webhook trigger for guardrail intake.

    Using a fresh pipeline per test keeps the bound guardrails from leaking
    into the shared conftest pipeline consumed by other integration tests.
    """
    pipeline_id = uuid.uuid4()
    snapshot_id = uuid.uuid4()
    trigger_id = uuid.uuid4()
    hmac_secret = "whsec_far214_intake_secret"
    async with db_engine.connect() as conn, conn.begin():
        await conn.execute(
            text(
                "INSERT INTO pipelines (id, organisation_id, name, account_id, "
                "max_concurrent_runs, lock_wait_timeout_seconds, node_timeout_seconds, "
                "run_context_defaults, graph_nodes_json) "
                "VALUES (:id, :oid, :name, :uid, 10, 30, 300, '{}'::json, '[]'::json)",
            ),
            {
                "id": str(pipeline_id),
                "oid": str(test_org),
                "name": f"FAR-214 Intake Pipeline {pipeline_id.hex[:8]}",
                "uid": str(test_user),
            },
        )
        await conn.execute(
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
        await conn.execute(
            text(
                "INSERT INTO triggers (id, organisation_id, pipeline_id, "
                "trigger_type, active, max_concurrent_runs, config_json, account_id) "
                "VALUES (:id, :oid, :pid, 'webhook', true, 5, (:config)::json, :uid)",
            ),
            {
                "id": str(trigger_id),
                "oid": str(test_org),
                "pid": str(pipeline_id),
                "config": json.dumps({"hmac_secret": hmac_secret}),
                "uid": str(test_user),
            },
        )
    return {
        "org_id": test_org,
        "pipeline_id": pipeline_id,
        "snapshot_id": snapshot_id,
        "trigger_id": trigger_id,
        "hmac_secret": hmac_secret,
    }


async def _seed_guardrail(
    db_engine: AsyncEngine,
    *,
    org_id: uuid.UUID,
    pipeline_id: uuid.UUID,
    account_id: uuid.UUID,
    name: str,
    action: str,
    config: dict[str, object] | None = None,
) -> None:
    cfg: dict[str, object] = {
        "action": action,
        "interception_point": "input",
        "type": "regex",
        "field": "body",
        "pattern": r"SECRET_[A-Z0-9]{8}",
    }
    if config:
        cfg.update(config)
    async with db_engine.connect() as conn, conn.begin():
        await conn.execute(
            text(
                "INSERT INTO eval_definitions (id, organisation_id, pipeline_id, node_id, name, "
                "eval_type, config_json, failure_behaviour, account_id) "
                "VALUES (:id, :oid, :pid, NULL, :name, 'guardrail', (:cfg)::json, 'warn', :aid)",
            ),
            {
                "id": str(uuid.uuid4()),
                "oid": str(org_id),
                "pid": str(pipeline_id),
                "name": name,
                "cfg": json.dumps(cfg),
                "aid": str(account_id),
            },
        )


async def _deliver(
    db_engine: AsyncEngine,
    rig: dict[str, uuid.UUID | str],
    *,
    body: bytes,
    raw_payload: dict[str, object],
) -> tuple[Any, Any, Any]:
    """Deliver a webhook through the REAL engine, mirroring the route's
    transaction semantics: a ``GuardrailBlockedAtIntakeError`` is caught INSIDE
    the transaction so the ``guardrail_blocked`` TriggerEvent + stored raw
    payload COMMIT (the route's in-transaction catch), then re-raised after the
    commit so the caller observes the rejection."""
    ts = _valid_timestamp()
    sig = _hmac_sign(body, rig["hmac_secret"], int(ts))
    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    engine = TriggerEngine()
    blocked: GuardrailBlockedAtIntakeError | None = None
    result: tuple[Any, Any, Any] | None = None
    async with factory() as session, session.begin():
        await set_rls_org(session, rig["org_id"])
        try:
            result = await engine.handle_webhook(
                session,
                trigger_id=rig["trigger_id"],
                org_id=rig["org_id"],
                raw_body=body,
                raw_payload=raw_payload,
                hmac_signature=sig,
                modulo_timestamp=ts,
                snapshot_id=rig["snapshot_id"],
            )
        except GuardrailBlockedAtIntakeError as exc:
            blocked = exc
    if blocked is not None:
        raise blocked
    assert result is not None
    return result


async def _guardrail_blocked_events(db_engine: AsyncEngine, trigger_id: uuid.UUID) -> list[str]:
    async with db_engine.connect() as conn:
        result = await conn.execute(
            text(
                "SELECT error_detail FROM trigger_events "
                "WHERE trigger_id = :tid AND validation_result = 'guardrail_blocked'"
            ),
            {"tid": str(trigger_id)},
        )
        return list(result.scalars())


async def _dedup_rows(db_engine: AsyncEngine, trigger_id: uuid.UUID) -> list[tuple[str, object]]:
    async with db_engine.connect() as conn:
        result = await conn.execute(
            text("SELECT payload_hash, expires_at FROM webhook_dedup_hashes WHERE trigger_id = :tid"),
            {"tid": str(trigger_id)},
        )
        return [(row[0], row[1]) for row in result.fetchall()]


class TestGuardrailBlockAtIntake:
    async def test_block_rejects_with_event_no_run_no_dedup(
        self,
        db_engine: AsyncEngine,
        test_user: uuid.UUID,
        intake_rig: dict[str, uuid.UUID | str],
    ) -> None:
        await _seed_guardrail(
            db_engine,
            org_id=intake_rig["org_id"],
            pipeline_id=intake_rig["pipeline_id"],
            account_id=test_user,
            name="no-secrets",
            action="block",
        )

        body = b'{"body": "leak SECRET_ABC12345"}'
        with pytest.raises(GuardrailBlockedAtIntakeError) as exc_info:
            await _deliver(
                db_engine,
                intake_rig,
                body=body,
                raw_payload={"body": "leak SECRET_ABC12345"},
            )
        assert "no-secrets" in exc_info.value.detail

        # The ``guardrail_blocked`` TriggerEvent was COMMITTED.
        blocked = await _guardrail_blocked_events(db_engine, intake_rig["trigger_id"])
        assert len(blocked) == 1
        assert "no-secrets" in (blocked[0] or "")

        # No run was created.
        async with db_engine.connect() as conn:
            run_count = (
                await conn.execute(
                    text("SELECT count(*) FROM runs WHERE trigger_id = :tid"),
                    {"tid": str(intake_rig["trigger_id"])},
                )
            ).scalar_one()
        assert run_count == 0

        # No dedup slot was consumed.
        assert not await _dedup_rows(db_engine, intake_rig["trigger_id"])

        # The raw payload was stored for replay (the sender can retry after
        # fixing): a webhook_payloads row is bound to the blocked event.
        async with db_engine.connect() as conn:
            stored = (
                await conn.execute(
                    text(
                        "SELECT count(*) FROM webhook_payloads wp "
                        "JOIN trigger_events te ON te.id = wp.trigger_event_id "
                        "WHERE te.validation_result = 'guardrail_blocked'"
                    )
                )
            ).scalar_one()
        assert stored == 1

    async def test_block_then_clean_delivery_creates_run(
        self,
        db_engine: AsyncEngine,
        test_user: uuid.UUID,
        intake_rig: dict[str, uuid.UUID | str],
    ) -> None:
        """After a block, the SAME trigger accepts a clean delivery — proving the
        blocked delivery consumed no dedup slot and created no run."""
        await _seed_guardrail(
            db_engine,
            org_id=intake_rig["org_id"],
            pipeline_id=intake_rig["pipeline_id"],
            account_id=test_user,
            name="no-secrets",
            action="block",
        )

        with pytest.raises(GuardrailBlockedAtIntakeError):
            await _deliver(
                db_engine,
                intake_rig,
                body=b'{"body": "leak SECRET_ABC12345"}',
                raw_payload={"body": "leak SECRET_ABC12345"},
            )

        run, event, _ = await _deliver(
            db_engine,
            intake_rig,
            body=b'{"body": "clean text"}',
            raw_payload={"body": "clean text"},
        )
        assert event.validation_result == "accepted"
        assert run is not None


class TestGuardrailRedactAtIntake:
    async def test_redact_masks_before_run_creation(
        self,
        db_engine: AsyncEngine,
        test_user: uuid.UUID,
        intake_rig: dict[str, uuid.UUID | str],
    ) -> None:
        await _seed_guardrail(
            db_engine,
            org_id=intake_rig["org_id"],
            pipeline_id=intake_rig["pipeline_id"],
            account_id=test_user,
            name="redact-key",
            action="redact",
            config={"redaction": [{"path": "credentials.api_key", "mode": "transform"}]},
        )

        run, event, _ = await _deliver(
            db_engine,
            intake_rig,
            body=b'{"body": "clean", "credentials": {"api_key": "sk-live-123"}}',
            raw_payload={"body": "clean", "credentials": {"api_key": "sk-live-123"}},
        )
        assert event.validation_result == "accepted"
        # The persisted run input_payload is POST-redaction (T1 contract).
        async with db_engine.connect() as conn:
            persisted = (
                await conn.execute(
                    text("SELECT input_payload FROM runs WHERE id = :rid"),
                    {"rid": str(run.id)},
                )
            ).scalar_one()
        assert persisted["credentials"]["api_key"] == "\u2022\u2022\u2022\u2022\u2022\u2022"
        assert persisted["body"] == "clean"
        assert "sk-live-123" not in json.dumps(persisted)


class TestCanonicalDedupAcrossEncodings:
    async def test_same_logical_payload_dedups_across_encodings(
        self,
        db_engine: AsyncEngine,
        intake_rig: dict[str, uuid.UUID | str],
    ) -> None:
        """Same logical payload with different raw encodings (key order +
        whitespace) must dedup — the FAR-214 raw-body-hash encoding-bypass
        closure. No guardrails bound."""
        payload = {"event": "push", "ref": "refs/heads/main"}

        _run, event, _ = await _deliver(
            db_engine,
            intake_rig,
            body=b'{"event": "push", "ref": "refs/heads/main"}',
            raw_payload=payload,
        )
        assert event.validation_result == "accepted"

        with pytest.raises(DuplicateWebhookError):
            await _deliver(
                db_engine,
                intake_rig,
                body=b'{"ref":"refs/heads/main","event":"push"}',
                raw_payload={"ref": "refs/heads/main", "event": "push"},
            )

        # Only one run was created.
        async with db_engine.connect() as conn:
            run_count = (
                await conn.execute(
                    text("SELECT count(*) FROM runs WHERE trigger_id = :tid"),
                    {"tid": str(intake_rig["trigger_id"])},
                )
            ).scalar_one()
        assert run_count == 1

        # Exactly one dedup hash row for the trigger.
        rows = await _dedup_rows(db_engine, intake_rig["trigger_id"])
        assert len(rows) == 1
        assert rows[0][0] == canonical_payload_hash(payload)

    async def test_different_payloads_do_not_dedup(
        self,
        db_engine: AsyncEngine,
        intake_rig: dict[str, uuid.UUID | str],
    ) -> None:
        await _deliver(
            db_engine,
            intake_rig,
            body=b'{"event": "push", "ref": "refs/heads/main"}',
            raw_payload={"event": "push", "ref": "refs/heads/main"},
        )
        run, event, _ = await _deliver(
            db_engine,
            intake_rig,
            body=b'{"event": "push", "ref": "refs/heads/dev"}',
            raw_payload={"event": "push", "ref": "refs/heads/dev"},
        )
        assert event.validation_result == "accepted"
        assert run is not None


class TestGuardrailRLSScoping:
    async def test_guardrail_is_org_scoped(
        self,
        db_engine: AsyncEngine,
        test_user: uuid.UUID,
        intake_rig: dict[str, uuid.UUID | str],
    ) -> None:
        """The pre-trigger guardrail lookup is org-scoped: a guardrail bound to
        a pipeline in ONE org must never fire for a delivery in ANOTHER org —
        even when that other org's delivery targets the SAME pipeline_id (the
        RLS row-level filter hides the foreign org's guardrail row).

        This closes the cross-org leak class: without the org scope, org B's
        pipeline would inherit org A's block policy and reject org B's
        deliveries. The pass returns the payload unmodified (not blocked)."""
        other_org = uuid.uuid4()
        other_account = uuid.uuid4()
        async with db_engine.connect() as conn, conn.begin():
            await conn.execute(
                text(
                    "INSERT INTO organisations (id, name, slug, settings_json) VALUES (:id, :name, :slug, '{}'::json)"
                ),
                {"id": str(other_org), "name": "FAR-214 Other Org", "slug": f"other-{other_org.hex[:8]}"},
            )
            await conn.execute(
                text(
                    "INSERT INTO accounts (id, email, display_name, password_hash, "
                    "auth_provider, active) "
                    "VALUES (:id, :email, :name, 'hash', 'local', true)"
                ),
                {"id": str(other_account), "email": f"other-{other_org.hex[:8]}@example.com", "name": "Other User"},
            )

        # A block guardrail bound to the SHARED pipeline in org A (the rig org).
        await _seed_guardrail(
            db_engine,
            org_id=intake_rig["org_id"],
            pipeline_id=intake_rig["pipeline_id"],
            account_id=test_user,
            name="no-secrets-orgA",
            action="block",
        )

        # Org B runs the pass against the SAME pipeline_id through its OWN
        # session + RLS context — the foreign guardrail must be invisible.
        factory = async_sessionmaker(db_engine, expire_on_commit=False)
        async with factory() as session, session.begin():
            await set_rls_org(session, other_org)
            outcome = await run_pre_trigger_guardrail_pass(
                session,
                org_id=other_org,
                pipeline_id=intake_rig["pipeline_id"],
                raw_payload={"body": "leak SECRET_ABC12345"},
            )
        assert outcome.blocked is False
        assert outcome.payload == {"body": "leak SECRET_ABC12345"}

    async def test_guardrail_only_blocks_own_org_deliveries(
        self,
        db_engine: AsyncEngine,
        test_user: uuid.UUID,
        intake_rig: dict[str, uuid.UUID | str],
    ) -> None:
        """End-to-end: the SAME violating payload is blocked for org A's own
        trigger but accepted for an org B trigger on its own pipeline — the
        pass never applies another org's guardrail."""
        await _seed_guardrail(
            db_engine,
            org_id=intake_rig["org_id"],
            pipeline_id=intake_rig["pipeline_id"],
            account_id=test_user,
            name="no-secrets",
            action="block",
        )

        with pytest.raises(GuardrailBlockedAtIntakeError):
            await _deliver(
                db_engine,
                intake_rig,
                body=b'{"body": "leak SECRET_ABC12345"}',
                raw_payload={"body": "leak SECRET_ABC12345"},
            )

        other_org = uuid.uuid4()
        other_pipeline = uuid.uuid4()
        other_snapshot = uuid.uuid4()
        other_trigger = uuid.uuid4()
        hmac_secret = "whsec_far214_other"
        async with db_engine.connect() as conn, conn.begin():
            await conn.execute(
                text(
                    "INSERT INTO organisations (id, name, slug, settings_json) VALUES (:id, :name, :slug, '{}'::json)"
                ),
                {"id": str(other_org), "name": "FAR-214 Org B", "slug": f"orgb-{other_org.hex[:8]}"},
            )
            await conn.execute(
                text(
                    "INSERT INTO pipelines (id, organisation_id, name, account_id, "
                    "max_concurrent_runs, lock_wait_timeout_seconds, node_timeout_seconds, "
                    "run_context_defaults, graph_nodes_json) "
                    "VALUES (:id, :oid, :name, :uid, 10, 30, 300, '{}'::json, '[]'::json)"
                ),
                {
                    "id": str(other_pipeline),
                    "oid": str(other_org),
                    "name": "FAR-214 Org B Pipeline",
                    "uid": str(test_user),
                },
            )
            await conn.execute(
                text(
                    "INSERT INTO pipeline_snapshots (id, pipeline_id, organisation_id, "
                    "snapshot_version, graph_json, connector_bindings_json, "
                    "schema_pins_json, prompt_pins_json, model_backend_pins_json, "
                    "run_context_defaults, config_json) "
                    "VALUES (:id, :pid, :oid, 1, '{}'::json, '[]'::json, "
                    "'[]'::json, '[]'::json, '[]'::json, '{}'::json, '{}'::json)"
                ),
                {"id": str(other_snapshot), "pid": str(other_pipeline), "oid": str(other_org)},
            )
            await conn.execute(
                text(
                    "INSERT INTO triggers (id, organisation_id, pipeline_id, "
                    "trigger_type, active, max_concurrent_runs, config_json, account_id) "
                    "VALUES (:id, :oid, :pid, 'webhook', true, 5, (:config)::json, :uid)"
                ),
                {
                    "id": str(other_trigger),
                    "oid": str(other_org),
                    "pid": str(other_pipeline),
                    "config": json.dumps({"hmac_secret": hmac_secret}),
                    "uid": str(test_user),
                },
            )

        other_rig = {
            "org_id": other_org,
            "pipeline_id": other_pipeline,
            "snapshot_id": other_snapshot,
            "trigger_id": other_trigger,
            "hmac_secret": hmac_secret,
        }
        run, event, _ = await _deliver(
            db_engine,
            other_rig,
            body=b'{"body": "leak SECRET_ABC12345"}',
            raw_payload={"body": "leak SECRET_ABC12345"},
        )
        assert event.validation_result == "accepted"
        assert run is not None
