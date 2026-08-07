"""Unit tests for the individual housekeeping scanners (modulo.core.housekeeping).

Each scanner executes a real SQL query against an in-memory SQLite database so
the WHERE clauses and JOINs are actually exercised (no ORM tenant-filter
listener is active — session.info carries no org context here).

The scanner suite previously had no direct coverage: only the orchestration
layer (``scan_all``) and the metadata mappings were tested. This file closes
that gap by asserting the exact candidate set each scanner returns for a
controlled org-scoped dataset.
"""

import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from modulo.core.housekeeping import (
    _scan_duplicate_triggers,
    _scan_empty_lifecycle_maps,
    _scan_empty_teams,
    _scan_expired_webhook_dedups,
    _scan_inactive_triggers,
    _scan_orphan_secrets,
    _scan_orphan_snapshots,
    _scan_stale_api_keys,
    _scan_stale_pipelines,
    _scan_unbound_connectors,
    _scan_untriggered_pipelines,
    _scan_unused_environment_profiles,
    _scan_unused_model_backends,
    _scan_unused_parameter_schemas,
    _scan_unused_schemas,
    _scan_unused_sso_providers,
)
from modulo.db.models.account import Account
from modulo.db.models.agent import Agent
from modulo.db.models.api_key import OrgApiKey
from modulo.db.models.base import Base
from modulo.db.models.connector_instance import ConnectorInstance
from modulo.db.models.environment_profile import EnvironmentProfile
from modulo.db.models.lifecycle_map import LifecycleMap
from modulo.db.models.model_backend import ModelBackend
from modulo.db.models.org_membership import OrgMembership
from modulo.db.models.parameter_schema import ParameterSchema
from modulo.db.models.pipeline import Pipeline
from modulo.db.models.pipeline_snapshot import PipelineSnapshot
from modulo.db.models.run import Run
from modulo.db.models.schema import Schema
from modulo.db.models.secret import Secret
from modulo.db.models.snapshot_schema_pin import SnapshotSchemaPin
from modulo.db.models.sso_provider import SsoProvider
from modulo.db.models.team import Team
from modulo.db.models.team_membership import TeamMembership
from modulo.db.models.trigger import Trigger
from modulo.db.models.webhook import WebhookDedupHash

# ``onboarding_progress`` uses a Postgres-only ARRAY column and cannot be
# rendered on SQLite — exclude it, the scanners never touch it.
_TABLE_NAMES = {
    "accounts",
    "agents",
    "connector_instances",
    "environment_profiles",
    "lifecycle_maps",
    "model_backends",
    "org_api_keys",
    "org_memberships",
    "parameter_schemas",
    "pipelines",
    "pipeline_snapshots",
    "runs",
    "schemas",
    "secrets",
    "snapshot_schema_pins",
    "sso_providers",
    "team_memberships",
    "teams",
    "triggers",
    "webhook_dedup_hashes",
}

_ORG_A = uuid.UUID("00000000-0000-0000-0000-00000000000a")
_ORG_B = uuid.UUID("00000000-0000-0000-0000-00000000000b")
_ACCOUNT = uuid.UUID("00000000-0000-0000-0000-0000000000a1")


@pytest.fixture
async def engine() -> AsyncGenerator[AsyncEngine, None]:
    eng = create_async_engine("sqlite+aiosqlite://", echo=False)
    async with eng.begin() as conn:
        tables = [t for name, t in Base.metadata.tables.items() if name in _TABLE_NAMES]
        await conn.run_sync(lambda sync_conn: Base.metadata.create_all(sync_conn, tables=tables))
        await conn.exec_driver_sql("PRAGMA foreign_keys = OFF")
    yield eng
    await eng.dispose()


@pytest.fixture
async def session(engine: AsyncEngine) -> AsyncGenerator[AsyncSession, None]:
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as s:
        yield s


def _candidate_names(candidates: list) -> list[str]:
    return sorted(c.name for c in candidates)


def _snapshot(
    *,
    organisation_id: uuid.UUID,
    pipeline_id: uuid.UUID,
    version: int = 1,
    environment_profile_id: uuid.UUID | None = None,
    connector_bindings_json: list[dict] | None = None,
) -> PipelineSnapshot:
    return PipelineSnapshot(
        id=uuid.uuid4(),
        organisation_id=organisation_id,
        pipeline_id=pipeline_id,
        snapshot_version=version,
        account_id=_ACCOUNT,
        environment_profile_id=environment_profile_id,
        graph_json={},
        connector_bindings_json=connector_bindings_json or [],
        schema_pins_json=[],
        prompt_pins_json=[],
        model_backend_pins_json=[],
    )


def _agent(
    *,
    organisation_id: uuid.UUID,
    name: str,
    model_backend_id: uuid.UUID,
    parameter_schema_id: uuid.UUID | None = None,
    input_schema_id: uuid.UUID | None = None,
    output_schema_id: uuid.UUID | None = None,
) -> Agent:
    return Agent(
        id=uuid.uuid4(),
        organisation_id=organisation_id,
        account_id=_ACCOUNT,
        name=name,
        input_schema_id=input_schema_id or uuid.uuid4(),
        input_schema_version="1",
        output_schema_id=output_schema_id or uuid.uuid4(),
        output_schema_version="1",
        prompt_template="prompt",
        model_backend_id=model_backend_id,
        connector_type_refs=[],
        parameter_schema_id=parameter_schema_id,
    )


class TestExpiredWebhookDedups:
    async def test_flags_only_expired_hashes_in_org(self, session: AsyncSession) -> None:
        now = datetime.now(UTC)
        session.add(
            WebhookDedupHash(
                id=uuid.uuid4(),
                organisation_id=_ORG_A,
                trigger_id=uuid.uuid4(),
                payload_hash="a" * 64,
                expires_at=now - timedelta(hours=1),
            )
        )
        session.add(
            WebhookDedupHash(
                id=uuid.uuid4(),
                organisation_id=_ORG_A,
                trigger_id=uuid.uuid4(),
                payload_hash="b" * 64,
                expires_at=now + timedelta(hours=1),
            )
        )
        session.add(
            WebhookDedupHash(
                id=uuid.uuid4(),
                organisation_id=_ORG_B,
                trigger_id=uuid.uuid4(),
                payload_hash="c" * 64,
                expires_at=now - timedelta(hours=1),
            )
        )
        await session.commit()

        candidates = await _scan_expired_webhook_dedups(session, _ORG_A)

        assert len(candidates) == 1
        assert candidates[0].detail == "Expired webhook deduplication hash"

    async def test_returns_empty_when_nothing_expired(self, session: AsyncSession) -> None:
        now = datetime.now(UTC)
        session.add(
            WebhookDedupHash(
                id=uuid.uuid4(),
                organisation_id=_ORG_A,
                trigger_id=uuid.uuid4(),
                payload_hash="a" * 64,
                expires_at=now + timedelta(hours=1),
            )
        )
        await session.commit()

        assert await _scan_expired_webhook_dedups(session, _ORG_A) == []


class TestInactiveTriggers:
    async def test_flags_inactive_triggers_that_never_fired(self, session: AsyncSession) -> None:
        now = datetime.now(UTC)
        session.add(
            Trigger(
                id=uuid.uuid4(),
                organisation_id=_ORG_A,
                pipeline_id=uuid.uuid4(),
                trigger_type="cron",
                active=False,
                account_id=_ACCOUNT,
            )
        )
        session.add(
            Trigger(
                id=uuid.uuid4(),
                organisation_id=_ORG_A,
                pipeline_id=uuid.uuid4(),
                trigger_type="cron",
                active=False,
                account_id=_ACCOUNT,
                last_fired_at=now - timedelta(days=1),
            )
        )
        session.add(
            Trigger(
                id=uuid.uuid4(),
                organisation_id=_ORG_A,
                pipeline_id=uuid.uuid4(),
                trigger_type="cron",
                active=True,
                account_id=_ACCOUNT,
            )
        )
        session.add(
            Trigger(
                id=uuid.uuid4(),
                organisation_id=_ORG_A,
                pipeline_id=uuid.uuid4(),
                trigger_type="cron",
                active=False,
                account_id=_ACCOUNT,
                deleted_at=now,
            )
        )
        session.add(
            Trigger(
                id=uuid.uuid4(),
                organisation_id=_ORG_B,
                pipeline_id=uuid.uuid4(),
                trigger_type="cron",
                active=False,
                account_id=_ACCOUNT,
            )
        )
        await session.commit()

        candidates = await _scan_inactive_triggers(session, _ORG_A)

        assert len(candidates) == 1
        assert "cron" in candidates[0].name


class TestEmptyTeams:
    async def test_flags_team_without_memberships(self, session: AsyncSession) -> None:
        empty = Team(id=uuid.uuid4(), organisation_id=_ORG_A, name="empty", account_id=_ACCOUNT)
        has_member = Team(id=uuid.uuid4(), organisation_id=_ORG_A, name="has-member", account_id=_ACCOUNT)
        session.add_all([empty, has_member])
        session.add(
            TeamMembership(
                id=uuid.uuid4(),
                organisation_id=_ORG_A,
                team_id=has_member.id,
                account_id=_ACCOUNT,
                role="viewer",
            )
        )
        session.add(Team(id=uuid.uuid4(), organisation_id=_ORG_B, name="other-org", account_id=_ACCOUNT))
        await session.commit()

        candidates = await _scan_empty_teams(session, _ORG_A)

        assert _candidate_names(candidates) == ["empty"]

    async def test_skips_soft_deleted_teams(self, session: AsyncSession) -> None:
        now = datetime.now(UTC)
        empty = Team(id=uuid.uuid4(), organisation_id=_ORG_A, name="empty", account_id=_ACCOUNT)
        deleted = Team(
            id=uuid.uuid4(),
            organisation_id=_ORG_A,
            name="deleted",
            account_id=_ACCOUNT,
            deleted_at=now,
        )
        session.add_all([empty, deleted])
        await session.commit()

        candidates = await _scan_empty_teams(session, _ORG_A)

        assert _candidate_names(candidates) == ["empty"]


class TestStaleApiKeys:
    async def test_flags_never_used_and_stale_keys(self, session: AsyncSession) -> None:
        now = datetime.now(UTC)
        session.add(
            OrgApiKey(
                id=uuid.uuid4(),
                organisation_id=_ORG_A,
                name="never-used",
                lookup_prefix="ab",
                hashed_secret="x",
                role="operator",
                account_id=_ACCOUNT,
                expires_at=now + timedelta(days=30),
            )
        )
        session.add(
            OrgApiKey(
                id=uuid.uuid4(),
                organisation_id=_ORG_A,
                name="stale",
                lookup_prefix="cd",
                hashed_secret="y",
                role="operator",
                account_id=_ACCOUNT,
                expires_at=now + timedelta(days=30),
                last_used_at=now - timedelta(weeks=5),
            )
        )
        session.add(
            OrgApiKey(
                id=uuid.uuid4(),
                organisation_id=_ORG_A,
                name="recent",
                lookup_prefix="ef",
                hashed_secret="z",
                role="operator",
                account_id=_ACCOUNT,
                expires_at=now + timedelta(days=30),
                last_used_at=now - timedelta(days=1),
            )
        )
        session.add(
            OrgApiKey(
                id=uuid.uuid4(),
                organisation_id=_ORG_A,
                name="revoked",
                lookup_prefix="gh",
                hashed_secret="w",
                role="operator",
                account_id=_ACCOUNT,
                expires_at=now + timedelta(days=30),
                revoked_at=now,
            )
        )
        await session.commit()

        candidates = await _scan_stale_api_keys(session, _ORG_A)

        assert _candidate_names(candidates) == ["never-used", "stale"]


class TestEmptyLifecycleMaps:
    async def test_flags_empty_content_maps(self, session: AsyncSession) -> None:
        session.add(
            LifecycleMap(id=uuid.uuid4(), organisation_id=_ORG_A, account_id=_ACCOUNT, name="empty", content_json={})
        )
        session.add(
            LifecycleMap(
                id=uuid.uuid4(),
                organisation_id=_ORG_A,
                account_id=_ACCOUNT,
                name="full",
                content_json={"stages": ["stage-a"]},
            )
        )
        session.add(
            LifecycleMap(
                id=uuid.uuid4(),
                organisation_id=_ORG_B,
                account_id=_ACCOUNT,
                name="other-org",
                content_json={},
            )
        )
        await session.commit()

        candidates = await _scan_empty_lifecycle_maps(session, _ORG_A)

        assert _candidate_names(candidates) == ["empty"]


class TestDuplicateTriggers:
    async def test_flags_duplicate_trigger_type_on_same_pipeline(self, session: AsyncSession) -> None:
        now = datetime.now(UTC)
        pipeline_id = uuid.uuid4()
        session.add(
            Trigger(
                id=uuid.uuid4(),
                organisation_id=_ORG_A,
                pipeline_id=pipeline_id,
                trigger_type="cron",
                active=True,
                account_id=_ACCOUNT,
            )
        )
        session.add(
            Trigger(
                id=uuid.uuid4(),
                organisation_id=_ORG_A,
                pipeline_id=pipeline_id,
                trigger_type="cron",
                active=True,
                account_id=_ACCOUNT,
            )
        )
        session.add(
            Trigger(
                id=uuid.uuid4(),
                organisation_id=_ORG_A,
                pipeline_id=pipeline_id,
                trigger_type="webhook",
                active=True,
                account_id=_ACCOUNT,
            )
        )
        session.add(
            Trigger(
                id=uuid.uuid4(),
                organisation_id=_ORG_A,
                pipeline_id=pipeline_id,
                trigger_type="cron",
                active=True,
                account_id=_ACCOUNT,
                deleted_at=now,
            )
        )
        await session.commit()

        candidates = await _scan_duplicate_triggers(session, _ORG_A)

        # Only the second live cron trigger is a duplicate; webhook type and
        # the soft-deleted cron trigger are excluded.
        assert len(candidates) == 1
        assert "2 total on this pipeline" in candidates[0].detail

    async def test_returns_empty_when_no_duplicates(self, session: AsyncSession) -> None:
        session.add(
            Trigger(
                id=uuid.uuid4(),
                organisation_id=_ORG_A,
                pipeline_id=uuid.uuid4(),
                trigger_type="cron",
                active=True,
                account_id=_ACCOUNT,
            )
        )
        await session.commit()

        assert await _scan_duplicate_triggers(session, _ORG_A) == []


class TestOrphanSnapshots:
    async def test_flags_snapshot_whose_pipeline_is_gone(self, session: AsyncSession) -> None:
        pipeline = Pipeline(id=uuid.uuid4(), organisation_id=_ORG_A, name="alive", account_id=_ACCOUNT)
        session.add(pipeline)
        session.add(_snapshot(organisation_id=_ORG_A, pipeline_id=uuid.uuid4()))
        session.add(_snapshot(organisation_id=_ORG_A, pipeline_id=pipeline.id))
        await session.commit()

        candidates = await _scan_orphan_snapshots(session, _ORG_A)

        assert len(candidates) == 1
        assert "Orphan snapshot" in candidates[0].detail


class TestUntriggeredPipelines:
    async def test_flags_pipeline_with_no_trigger_and_no_runs(self, session: AsyncSession) -> None:
        now = datetime.now(UTC)
        untouched = Pipeline(id=uuid.uuid4(), organisation_id=_ORG_A, name="untouched", account_id=_ACCOUNT)
        deleted = Pipeline(id=uuid.uuid4(), organisation_id=_ORG_A, name="deleted", account_id=_ACCOUNT, deleted_at=now)
        has_trigger = Pipeline(id=uuid.uuid4(), organisation_id=_ORG_A, name="has-trigger", account_id=_ACCOUNT)
        has_run = Pipeline(id=uuid.uuid4(), organisation_id=_ORG_A, name="has-run", account_id=_ACCOUNT)
        session.add_all([untouched, deleted, has_trigger, has_run])
        session.add(
            Trigger(
                id=uuid.uuid4(),
                organisation_id=_ORG_A,
                pipeline_id=has_trigger.id,
                trigger_type="cron",
                active=True,
                account_id=_ACCOUNT,
            )
        )
        snap = _snapshot(organisation_id=_ORG_A, pipeline_id=has_run.id)
        session.add(snap)
        session.add(
            Run(
                id=uuid.uuid4(),
                organisation_id=_ORG_A,
                pipeline_id=has_run.id,
                snapshot_id=snap.id,
                trigger_type="manual",
                run_number=1,
                account_id=_ACCOUNT,
                input_hash="h",
                langgraph_thread_id="thread-has-run",
            )
        )
        session.add(Pipeline(id=uuid.uuid4(), organisation_id=_ORG_B, name="other-org", account_id=_ACCOUNT))
        await session.commit()

        candidates = await _scan_untriggered_pipelines(session, _ORG_A)

        assert _candidate_names(candidates) == ["untouched"]


class TestUnusedEnvironmentProfiles:
    async def test_flags_profiles_not_referenced_by_snapshots(self, session: AsyncSession) -> None:
        now = datetime.now(UTC)
        unused = EnvironmentProfile(id=uuid.uuid4(), organisation_id=_ORG_A, name="unused", account_id=_ACCOUNT)
        used = EnvironmentProfile(id=uuid.uuid4(), organisation_id=_ORG_A, name="used", account_id=_ACCOUNT)
        deleted = EnvironmentProfile(id=uuid.uuid4(), organisation_id=_ORG_A, name="deleted", account_id=_ACCOUNT)
        session.add_all([unused, used, deleted])
        session.add(_snapshot(organisation_id=_ORG_A, pipeline_id=uuid.uuid4(), environment_profile_id=used.id))
        session.add(
            EnvironmentProfile(
                id=uuid.uuid4(),
                organisation_id=_ORG_A,
                name="deleted-unused",
                account_id=_ACCOUNT,
                deleted_at=now,
            )
        )
        session.add(_snapshot(organisation_id=_ORG_B, pipeline_id=uuid.uuid4(), environment_profile_id=unused.id))
        await session.commit()

        candidates = await _scan_unused_environment_profiles(session, _ORG_A)

        # The "unused" profile is referenced only by another org's snapshot and
        # "deleted" is a live row with no snapshot — both flagged.
        assert _candidate_names(candidates) == ["deleted", "unused"]


class TestStalePipelines:
    async def test_flags_pipeline_whose_last_run_is_over_four_weeks_old(self, session: AsyncSession) -> None:
        now = datetime.now(UTC)
        stale = Pipeline(id=uuid.uuid4(), organisation_id=_ORG_A, name="stale", account_id=_ACCOUNT)
        recent = Pipeline(id=uuid.uuid4(), organisation_id=_ORG_A, name="recent", account_id=_ACCOUNT)
        deleted = Pipeline(id=uuid.uuid4(), organisation_id=_ORG_A, name="deleted", account_id=_ACCOUNT, deleted_at=now)
        session.add_all([stale, recent, deleted])
        stale_snap = _snapshot(organisation_id=_ORG_A, pipeline_id=stale.id)
        recent_snap = _snapshot(organisation_id=_ORG_A, pipeline_id=recent.id)
        deleted_snap = _snapshot(organisation_id=_ORG_A, pipeline_id=deleted.id)
        session.add_all([stale_snap, recent_snap, deleted_snap])
        session.add(
            Run(
                id=uuid.uuid4(),
                organisation_id=_ORG_A,
                pipeline_id=stale.id,
                snapshot_id=stale_snap.id,
                trigger_type="manual",
                run_number=1,
                account_id=_ACCOUNT,
                input_hash="h1",
                langgraph_thread_id="thread-stale",
                created_at=now - timedelta(days=40),
            )
        )
        session.add(
            Run(
                id=uuid.uuid4(),
                organisation_id=_ORG_A,
                pipeline_id=recent.id,
                snapshot_id=recent_snap.id,
                trigger_type="manual",
                run_number=2,
                account_id=_ACCOUNT,
                input_hash="h2",
                langgraph_thread_id="thread-recent",
                created_at=now - timedelta(days=1),
            )
        )
        session.add(
            Run(
                id=uuid.uuid4(),
                organisation_id=_ORG_A,
                pipeline_id=deleted.id,
                snapshot_id=deleted_snap.id,
                trigger_type="manual",
                run_number=3,
                account_id=_ACCOUNT,
                input_hash="h3",
                langgraph_thread_id="thread-deleted",
                created_at=now - timedelta(days=40),
            )
        )
        await session.commit()

        candidates = await _scan_stale_pipelines(session, _ORG_A)

        assert _candidate_names(candidates) == ["stale"]


class TestOrphanSecrets:
    async def test_flags_secrets_referenced_by_no_connector_or_agent(self, session: AsyncSession) -> None:
        session.add(Secret(id=uuid.uuid4(), organisation_id=_ORG_A, key="conn_secret", encrypted_value=b"x"))
        session.add(Secret(id=uuid.uuid4(), organisation_id=_ORG_A, key="agent_secret", encrypted_value=b"x"))
        session.add(Secret(id=uuid.uuid4(), organisation_id=_ORG_A, key="orphan", encrypted_value=b"x"))
        session.add(
            ConnectorInstance(
                id=uuid.uuid4(),
                organisation_id=_ORG_A,
                name="conn",
                connector_type_id="github",
                config_json={"token": "conn_secret"},
                credentials_ciphertext=b"x",
                account_id=_ACCOUNT,
            )
        )
        session.add(
            Agent(
                id=uuid.uuid4(),
                organisation_id=_ORG_A,
                account_id=_ACCOUNT,
                name="agent",
                input_schema_id=uuid.uuid4(),
                input_schema_version="1",
                output_schema_id=uuid.uuid4(),
                output_schema_version="1",
                prompt_template="prompt",
                model_backend_id=uuid.uuid4(),
                connector_type_refs=[{"secret_key": "agent_secret"}],
            )
        )
        await session.commit()

        candidates = await _scan_orphan_secrets(session, _ORG_A)

        assert _candidate_names(candidates) == ["orphan"]


class TestUnboundConnectors:
    async def test_flags_connectors_not_bound_to_any_snapshot(self, session: AsyncSession) -> None:
        bound = ConnectorInstance(
            id=uuid.uuid4(),
            organisation_id=_ORG_A,
            name="bound",
            connector_type_id="github",
            config_json={},
            credentials_ciphertext=b"x",
            account_id=_ACCOUNT,
        )
        legacy_key = ConnectorInstance(
            id=uuid.uuid4(),
            organisation_id=_ORG_A,
            name="legacy-key",
            connector_type_id="github",
            config_json={},
            credentials_ciphertext=b"x",
            account_id=_ACCOUNT,
        )
        unbound = ConnectorInstance(
            id=uuid.uuid4(),
            organisation_id=_ORG_A,
            name="unbound",
            connector_type_id="github",
            config_json={},
            credentials_ciphertext=b"x",
            account_id=_ACCOUNT,
        )
        other_org = ConnectorInstance(
            id=uuid.uuid4(),
            organisation_id=_ORG_B,
            name="other-org",
            connector_type_id="github",
            config_json={},
            credentials_ciphertext=b"x",
            account_id=_ACCOUNT,
        )
        session.add_all([bound, legacy_key, unbound, other_org])
        session.add(
            _snapshot(
                organisation_id=_ORG_A,
                pipeline_id=uuid.uuid4(),
                connector_bindings_json=[
                    {"connector_instance_id": str(bound.id)},
                    {"connector_id": str(legacy_key.id)},
                ],
            )
        )
        # A snapshot in another org that binds "unbound" must not shield it.
        session.add(
            _snapshot(
                organisation_id=_ORG_B,
                pipeline_id=uuid.uuid4(),
                connector_bindings_json=[{"connector_instance_id": str(unbound.id)}],
            )
        )
        await session.commit()

        candidates = await _scan_unbound_connectors(session, _ORG_A)

        assert _candidate_names(candidates) == ["unbound"]

    async def test_malformed_binding_id_is_ignored(self, session: AsyncSession) -> None:
        connector = ConnectorInstance(
            id=uuid.uuid4(),
            organisation_id=_ORG_A,
            name="conn",
            connector_type_id="github",
            config_json={},
            credentials_ciphertext=b"x",
            account_id=_ACCOUNT,
        )
        session.add(connector)
        session.add(
            _snapshot(
                organisation_id=_ORG_A,
                pipeline_id=uuid.uuid4(),
                connector_bindings_json=[{"connector_instance_id": "not-a-uuid"}],
            )
        )
        await session.commit()

        candidates = await _scan_unbound_connectors(session, _ORG_A)

        # The malformed binding reference cannot be resolved, so the connector
        # is still treated as unbound rather than crashing the scan.
        assert _candidate_names(candidates) == ["conn"]


class TestUnusedModelBackends:
    async def test_flags_backends_not_assigned_to_any_agent(self, session: AsyncSession) -> None:
        used = ModelBackend(
            id=uuid.uuid4(),
            organisation_id=_ORG_A,
            name="used",
            display_name="Used",
            provider="openai",
            model_id="gpt-4o",
            credentials_ciphertext=b"x",
            account_id=_ACCOUNT,
        )
        unused = ModelBackend(
            id=uuid.uuid4(),
            organisation_id=_ORG_A,
            name="unused",
            display_name="Unused",
            provider="anthropic",
            model_id="claude-sonnet-4",
            credentials_ciphertext=b"x",
            account_id=_ACCOUNT,
        )
        other_org = ModelBackend(
            id=uuid.uuid4(),
            organisation_id=_ORG_B,
            name="other-org",
            display_name="Other Org",
            provider="openai",
            model_id="gpt-4o",
            credentials_ciphertext=b"x",
            account_id=_ACCOUNT,
        )
        session.add_all([used, unused, other_org])
        session.add(_agent(organisation_id=_ORG_A, name="agent", model_backend_id=used.id))
        await session.commit()

        candidates = await _scan_unused_model_backends(session, _ORG_A)

        assert _candidate_names(candidates) == ["unused"]


class TestUnusedSsoProviders:
    async def test_flags_providers_with_no_sso_accounts(self, session: AsyncSession) -> None:
        session.add(SsoProvider(id=uuid.uuid4(), organisation_id=_ORG_A, provider_type="oidc", name="unused"))
        session.add(SsoProvider(id=uuid.uuid4(), organisation_id=_ORG_B, provider_type="saml", name="other-org"))
        await session.commit()

        candidates = await _scan_unused_sso_providers(session, _ORG_A)

        assert _candidate_names(candidates) == ["unused"]

    async def test_skips_when_sso_accounts_exist(self, session: AsyncSession) -> None:
        sso_account = Account(id=uuid.uuid4(), email="sso@example.com", display_name="SSO User", auth_provider="oidc")
        provider = SsoProvider(id=uuid.uuid4(), organisation_id=_ORG_A, provider_type="oidc", name="used")
        session.add_all([sso_account, provider])
        session.add(OrgMembership(id=uuid.uuid4(), organisation_id=_ORG_A, account_id=sso_account.id, role="runner"))
        await session.commit()

        assert await _scan_unused_sso_providers(session, _ORG_A) == []

    async def test_local_auth_does_not_count_as_sso(self, session: AsyncSession) -> None:
        local_account = Account(
            id=uuid.uuid4(), email="local@example.com", display_name="Local User", auth_provider="local"
        )
        provider = SsoProvider(id=uuid.uuid4(), organisation_id=_ORG_A, provider_type="oidc", name="used")
        session.add_all([local_account, provider])
        session.add(OrgMembership(id=uuid.uuid4(), organisation_id=_ORG_A, account_id=local_account.id, role="runner"))
        await session.commit()

        candidates = await _scan_unused_sso_providers(session, _ORG_A)

        # Only oidc/saml/scim memberships suppress the warning; a local-only
        # membership leaves the provider flagged as unused.
        assert _candidate_names(candidates) == ["used"]


class TestUnusedParameterSchemas:
    async def test_flags_schemas_not_assigned_to_any_agent(self, session: AsyncSession) -> None:
        now = datetime.now(UTC)
        used = ParameterSchema(id=uuid.uuid4(), organisation_id=_ORG_A, name="used", version=1, account_id=_ACCOUNT)
        unused = ParameterSchema(id=uuid.uuid4(), organisation_id=_ORG_A, name="unused", version=1, account_id=_ACCOUNT)
        deleted = ParameterSchema(
            id=uuid.uuid4(),
            organisation_id=_ORG_A,
            name="deleted",
            version=1,
            account_id=_ACCOUNT,
            deleted_at=now,
        )
        other_org = ParameterSchema(
            id=uuid.uuid4(), organisation_id=_ORG_B, name="other-org", version=1, account_id=_ACCOUNT
        )
        session.add_all([used, unused, deleted, other_org])
        session.add(
            _agent(organisation_id=_ORG_A, name="agent", model_backend_id=uuid.uuid4(), parameter_schema_id=used.id)
        )
        await session.commit()

        candidates = await _scan_unused_parameter_schemas(session, _ORG_A)

        # "used" is referenced by an agent, "deleted" is excluded via the
        # deleted_at filter, "other-org" is out of scope.
        assert _candidate_names(candidates) == ["unused"]


class TestUnusedSchemas:
    async def test_flags_schemas_not_used_by_agents_or_pins(self, session: AsyncSession) -> None:
        unused = Schema(id=uuid.uuid4(), organisation_id=_ORG_A, name="unused", account_id=_ACCOUNT)
        system = Schema(id=uuid.uuid4(), organisation_id=_ORG_A, name="system-schema", system=True, account_id=_ACCOUNT)
        used_by_agent = Schema(id=uuid.uuid4(), organisation_id=_ORG_A, name="agent-input", account_id=_ACCOUNT)
        used_by_pin = Schema(id=uuid.uuid4(), organisation_id=_ORG_A, name="pinned", account_id=_ACCOUNT)
        other_org = Schema(id=uuid.uuid4(), organisation_id=_ORG_B, name="other-org", account_id=_ACCOUNT)
        session.add_all([unused, system, used_by_agent, used_by_pin, other_org])
        session.add(
            _agent(
                organisation_id=_ORG_A, name="agent", model_backend_id=uuid.uuid4(), input_schema_id=used_by_agent.id
            )
        )
        session.add(
            SnapshotSchemaPin(
                snapshot_id=uuid.uuid4(),
                organisation_id=_ORG_A,
                node_id=uuid.uuid4(),
                direction="input",
                schema_id=used_by_pin.id,
                schema_version="1",
            )
        )
        await session.commit()

        candidates = await _scan_unused_schemas(session, _ORG_A)

        # "agent-input" is referenced via an agent input pin, "pinned" via a
        # snapshot schema pin, "system-schema" is system-owned, and
        # "other-org" is out of scope.
        assert _candidate_names(candidates) == ["unused"]
