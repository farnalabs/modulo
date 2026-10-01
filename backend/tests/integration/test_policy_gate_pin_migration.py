"""Integration tests for FAR-967 chunk 10: migration 0272 round-trip + live CHECK.

Runs against a real Postgres (testcontainers) with real Alembic migrations,
using a private database cloned from the shared container's database (an
isolated DB name) so the shared session schema is never touched (mirrors
``test_migration_0191_bundled_runner_seed_backfill.py``).

Covers criterion 13 (upgrade/downgrade round-trip of migration
``0272_policy_gate_pin_fingerprint_operator_control``, including the
symmetric CHECK behaviour and the nullable legacy-compat snapshot columns)
and the live-DB half of criteria 11/12 (column types/nullability as the
migration actually ships them).
"""

import json
import uuid
from pathlib import Path

import pytest
import pytest_asyncio
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

from modulo.core.eval_engine.policy_gate import fingerprint_policy_gate_pins

pytestmark = [pytest.mark.integration]

BACKEND_ROOT = Path(__file__).parents[2]  # backend/
MIGRATION_REV = "0272_policy_gate_pin_fingerprint_operator_control"
PREV_REV = "0271_org_api_keys_revocation_sweep_indexes"


def _alembic_config(db_url: str) -> Config:
    config = Config(BACKEND_ROOT / "alembic.ini")
    config.set_main_option("sqlalchemy.url", db_url)
    config.set_main_option("script_location", str(BACKEND_ROOT / "src" / "modulo" / "db" / "migrations"))
    config.config_file_name = None
    return config


def _swap_db_name(db_url: str, new_db: str) -> str:
    from urllib.parse import urlparse, urlunparse

    parsed = urlparse(db_url)
    return urlunparse(parsed._replace(path=f"/{new_db}"))


def _alembic_cmd(iso_url: str, cmd: str, target: str) -> None:
    """Run an alembic command against the isolated DB via the Python API.

    Deliberately does NOT inject ``cmd_opts``: env.py's
    ``_invocation_is_upgrade`` now infers direction from the active Alembic
    EnvironmentContext (``context._proxy.context_opts["fn"].__name__``) when
    ``cmd_opts`` is absent, so a real ``command.downgrade`` on an at-head DB
    runs instead of being fast-path-skipped (FAR-967 F2).  These round-trip
    tests double as the regression proof — the old
    ``cfg.cmd_opts = SimpleNamespace(command=...)`` workaround is gone."""
    cfg = _alembic_config(iso_url)
    assert getattr(cfg, "cmd_opts", None) is None, "Python-API invocation must carry no cmd_opts"
    getattr(command, cmd)(cfg, target)


@pytest_asyncio.fixture
async def isolated_db_url(db_url: str, monkeypatch: pytest.MonkeyPatch):
    """A fresh, private Postgres database migrated only up to ``PREV_REV``.

    ``env.py`` resolves the target DB from ``DATABASE_URL`` /
    ``DATABASE_ADMIN_URL``, so both are pinned for every alembic call."""
    admin_engine = create_async_engine(db_url, poolclass=NullPool, execution_options={"isolation_level": "AUTOCOMMIT"})
    db_name = f"m0272_iso_{uuid.uuid4().hex[:10]}"
    async with admin_engine.connect() as conn:
        await conn.execute(text(f'CREATE DATABASE "{db_name}" WITH TEMPLATE template0'))
    await admin_engine.dispose()

    iso_url = _swap_db_name(db_url, db_name)
    engine = create_async_engine(iso_url, poolclass=NullPool)
    async with engine.connect() as conn:
        await conn.execute(
            text("CREATE TABLE IF NOT EXISTS alembic_version (version_num VARCHAR(255) NOT NULL PRIMARY KEY)")
        )
    await engine.dispose()

    monkeypatch.setenv("DATABASE_URL", iso_url)
    monkeypatch.setenv("DATABASE_ADMIN_URL", iso_url)
    with pytest.MonkeyPatch().context() as mp:
        mp.setenv("DATABASE_URL", iso_url)
        mp.setenv("DATABASE_ADMIN_URL", iso_url)
        _alembic_cmd(iso_url, "upgrade", PREV_REV)

    yield iso_url

    admin_engine = create_async_engine(db_url, poolclass=NullPool, execution_options={"isolation_level": "AUTOCOMMIT"})
    async with admin_engine.connect() as conn:
        await conn.execute(text(f'DROP DATABASE IF EXISTS "{db_name}"'))
    await admin_engine.dispose()


async def _migrate_to_target(iso_url: str) -> None:
    with pytest.MonkeyPatch().context() as mp:
        mp.setenv("DATABASE_URL", iso_url)
        mp.setenv("DATABASE_ADMIN_URL", iso_url)
        _alembic_cmd(iso_url, "upgrade", MIGRATION_REV)


async def _seed(engine: AsyncEngine) -> dict[str, uuid.UUID]:
    """Minimal org/account/pipeline/eval/policy_gate rows.

    At ``PREV_REV`` (pre-0272) the ``enabled`` operator-control columns do
    not exist; after 0272 they do and the symmetric CHECK requires
    ``enabled=true ⟹ enabled_at`` set. Detects which world it is in."""
    org_id = uuid.uuid4()
    slug = f"m0272-{org_id.hex[:8]}"
    account_id = uuid.uuid4()
    pipe_id = uuid.uuid4()
    node_id = uuid.uuid4()
    eval_ids = [uuid.uuid4() for _ in range(4)]
    gate_ids = [uuid.uuid4() for _ in range(3)]
    snapshot_id = uuid.uuid4()
    async with engine.begin() as conn:
        await conn.execute(
            text("INSERT INTO organisations (id, name, slug, settings_json) VALUES (:i, :n, :s, '{}'::json)"),
            {"i": str(org_id), "n": slug, "s": slug},
        )
        await conn.execute(
            text(
                "INSERT INTO accounts (id, email, display_name, password_hash, auth_provider, active) "
                "VALUES (:i, :e, :n, 'hash', 'local', true)"
            ),
            {"i": str(account_id), "e": f"{slug}@test.com", "n": slug},
        )
        await conn.execute(
            text(
                "INSERT INTO pipelines (id, organisation_id, name, account_id, "
                "max_concurrent_runs, lock_wait_timeout_seconds, node_timeout_seconds, "
                "run_context_defaults, graph_nodes_json, visibility) "
                "VALUES (:i, :oid, :n, :aid, 10, 30, 300, '{}'::json, '[]'::json, 'org')"
            ),
            {"i": str(pipe_id), "oid": str(org_id), "n": f"{slug}-pipe", "aid": str(account_id)},
        )
        for eid in eval_ids:
            await conn.execute(
                text(
                    "INSERT INTO evals (id, organisation_id, pipeline_id, account_id, "
                    "node_id, name, eval_type, config_json, version) "
                    "VALUES (:i, :oid, :pid, :aid, :nid, :n, 'regex', '{}'::jsonb, 1)"
                ),
                {
                    "i": str(eid),
                    "oid": str(org_id),
                    "pid": str(pipe_id),
                    "aid": str(account_id),
                    "nid": str(node_id),
                    "n": f"{slug}-eval",
                },
            )
        post_migration = (
            await conn.execute(
                text(
                    "SELECT COUNT(*) FROM information_schema.columns "
                    "WHERE table_name = 'policy_gates' AND column_name = 'enabled'"
                )
            )
        ).scalar_one()
        for gid, eid in zip(gate_ids, eval_ids[:3], strict=True):
            gate_sql = (
                "INSERT INTO policy_gates (id, organisation_id, eval_id, node_id, "
                "action, version) VALUES (:i, :oid, :eid, :nid, 'block', 1)"
                if not post_migration
                else "INSERT INTO policy_gates (id, organisation_id, eval_id, node_id, "
                "action, enabled, enabled_at, disabled_at, version) "
                "VALUES (:i, :oid, :eid, :nid, 'block', true, now(), NULL, 1)"
            )
            await conn.execute(
                text(gate_sql),
                {"i": str(gid), "oid": str(org_id), "eid": str(eid), "nid": str(node_id)},
            )
        await conn.execute(
            text(
                "INSERT INTO pipeline_snapshots (id, pipeline_id, organisation_id, "
                "snapshot_version, graph_json, connector_bindings_json, schema_pins_json, "
                "prompt_pins_json, model_backend_pins_json, config_json, run_context_defaults) "
                "VALUES (:i, :pid, :oid, 1, CAST(:graph AS json), '[]'::json, '[]'::json, "
                "'[]'::json, '[]'::json, '{}'::json, '{}'::json)"
            ),
            {
                "i": str(snapshot_id),
                "pid": str(pipe_id),
                "oid": str(org_id),
                "graph": '{"nodes": [], "edges": []}',
            },
        )
    return {
        "org_id": org_id,
        "pipe_id": pipe_id,
        "eval_ids": eval_ids,
        "gate_ids": gate_ids,
        "snapshot_id": snapshot_id,
        "node_id": node_id,
    }


async def _scalar(engine: AsyncEngine, sql: str, params: dict | None = None) -> object:
    async with engine.connect() as conn:
        return (await conn.execute(text(sql), params or {})).scalar()


# ---------------------------------------------------------------------------
# Round-trip (C13) + live schema contract (C11/C12)
# ---------------------------------------------------------------------------


_GATE_COLUMNS_SQL = (
    "SELECT column_name, data_type, character_maximum_length, is_nullable "
    "FROM information_schema.columns WHERE table_name = 'policy_gates'"
)


async def _engine_connect(iso_url: str) -> AsyncEngine:
    return create_async_engine(iso_url, poolclass=NullPool)


class TestC13MigrationRoundTrip:
    @pytest.mark.asyncio
    async def test_upgrade_adds_columns_backfills_and_check(self, isolated_db_url: str) -> None:
        """Upgrade 0271 → 0272 adds the operator-control + pin columns and
        creates a VALIDATED symmetric CHECK (C11)."""
        engine = await _engine_connect(isolated_db_url)
        try:
            await _seed(engine)
            await _migrate_to_target(isolated_db_url)

            async with engine.connect() as conn:
                rows = (await conn.execute(text(_GATE_COLUMNS_SQL))).fetchall()
            gate_cols = {r[0]: r for r in rows}
            enabled = gate_cols["enabled"]
            assert enabled[1] == "boolean", f"enabled must be BOOLEAN, got {enabled[1]}"
            assert enabled[3] == "NO", "enabled must be NOT NULL"
            assert gate_cols["enabled_at"][1] == "timestamp with time zone"
            assert gate_cols["disabled_at"][3] == "YES"

            # Snapshots: pin columns ship nullable legacy-compat (C12).
            async with engine.connect() as conn:
                snap_rows = (
                    await conn.execute(
                        text(
                            "SELECT column_name, data_type, character_maximum_length, is_nullable "
                            "FROM information_schema.columns WHERE table_name = 'pipeline_snapshots' "
                            "AND column_name IN ('policy_gate_pins_json', 'policy_gate_pins_fingerprint')"
                        )
                    )
                ).fetchall()
            snap_cols = {r[0]: r for r in snap_rows}
            fingerprint = snap_cols["policy_gate_pins_fingerprint"]
            assert fingerprint[1] == "character varying"
            assert fingerprint[2] == 64
            assert fingerprint[3] == "YES"
            assert snap_cols["policy_gate_pins_json"][3] == "YES"
        finally:
            await engine.dispose()

    @pytest.mark.asyncio
    async def test_upgrade_backfills_enabled_at_for_existing_rows(self, isolated_db_url: str) -> None:
        """Backfill: rows seeded pre-migration read back enabled=true with
        ``enabled_at = created_at`` and NULL ``disabled_at``."""
        engine = await _engine_connect(isolated_db_url)
        try:
            await _seed(engine)
            await _migrate_to_target(isolated_db_url)
            bad = await _scalar(
                engine,
                "SELECT COUNT(*) FROM policy_gates WHERE NOT enabled OR enabled_at IS NULL OR disabled_at IS NOT NULL",
            )
            assert bad == 0
        finally:
            await engine.dispose()

    @pytest.mark.asyncio
    async def test_check_constraint_created_and_validated(self, isolated_db_url: str) -> None:
        engine = await _engine_connect(isolated_db_url)
        try:
            await _migrate_to_target(isolated_db_url)
            convalidated = await _scalar(
                engine,
                "SELECT convalidated FROM pg_constraint "
                "WHERE conname = 'ck_policy_gates_enabled_timestamps' "
                "AND conrelid = 'public.policy_gates'::regclass",
            )
            assert convalidated is True
        finally:
            await engine.dispose()

    @pytest.mark.asyncio
    async def test_downgrade_removes_columns_and_check_keeps_rows(self, isolated_db_url: str) -> None:
        """Downgrade 0272 → 0271 drops the columns AND the CHECK while
        pre-existing rows survive (data never shaped by the dropped cols)."""
        engine = await _engine_connect(isolated_db_url)
        try:
            await _seed(engine)
            await _migrate_to_target(isolated_db_url)
            with pytest.MonkeyPatch().context() as mp:
                mp.setenv("DATABASE_URL", isolated_db_url)
                mp.setenv("DATABASE_ADMIN_URL", isolated_db_url)
                _alembic_cmd(isolated_db_url, "downgrade", PREV_REV)

            async with engine.connect() as conn:
                gate_rows = (await conn.execute(text(_GATE_COLUMNS_SQL))).fetchall()
            gate_names = {r[0] for r in gate_rows}
            assert not {"enabled", "enabled_at", "disabled_at"} & gate_names
            snap_names = await _scalar(
                engine,
                "SELECT COUNT(*) FROM information_schema.columns "
                "WHERE table_name = 'pipeline_snapshots' "
                "AND column_name IN ('policy_gate_pins_json', 'policy_gate_pins_fingerprint')",
            )
            assert snap_names == 0
            check_gone = await _scalar(
                engine,
                "SELECT COUNT(*) FROM pg_constraint WHERE conname = 'ck_policy_gates_enabled_timestamps'",
            )
            assert check_gone == 0
        finally:
            await engine.dispose()

    @pytest.mark.asyncio
    async def test_full_round_trip_restores_revision_with_columns(self, isolated_db_url: str) -> None:
        """upgrade → downgrade → upgrade: the second upgrade re-adds the
        columns and the CHECK, and alembic_version returns to 0272."""
        engine = await _engine_connect(isolated_db_url)
        try:
            await _seed(engine)
            await _migrate_to_target(isolated_db_url)
            with pytest.MonkeyPatch().context() as mp:
                mp.setenv("DATABASE_URL", isolated_db_url)
                mp.setenv("DATABASE_ADMIN_URL", isolated_db_url)
                _alembic_cmd(isolated_db_url, "downgrade", PREV_REV)
                _alembic_cmd(isolated_db_url, "upgrade", MIGRATION_REV)

            async with engine.connect() as conn:
                version = (await conn.execute(text("SELECT version_num FROM alembic_version"))).scalar_one()
            assert version == MIGRATION_REV
            columns = await _scalar(
                engine,
                "SELECT COUNT(*) FROM information_schema.columns "
                "WHERE table_name = 'policy_gates' "
                "AND column_name IN ('enabled', 'enabled_at', 'disabled_at')",
            )
            assert columns == 3
        finally:
            await engine.dispose()


class TestC12LegacyCompat:
    @pytest.mark.asyncio
    async def test_pre_upgrade_snapshot_rows_read_back_null_pins(self, isolated_db_url: str) -> None:
        """C12 legacy-compat: a snapshot inserted at PREV_REV (no pin columns)
        reads back with NULL pins + NULL fingerprint after the upgrade."""
        engine = await _engine_connect(isolated_db_url)
        try:
            seeded = await _seed(engine)
            await _migrate_to_target(isolated_db_url)
            async with engine.connect() as conn:
                rows = (
                    await conn.execute(
                        text(
                            "SELECT policy_gate_pins_json, policy_gate_pins_fingerprint "
                            "FROM pipeline_snapshots WHERE id = :sid"
                        ),
                        {"sid": str(seeded["snapshot_id"])},
                    )
                ).fetchall()
            assert rows
            stored_pins, stored_fingerprint = rows[0]
            assert stored_pins is None
            assert stored_fingerprint is None
        finally:
            await engine.dispose()


class TestC11CheckBehaviour:
    """Symmetric CHECK (§4.3) enforced live: enabled ⟹ enabled_at NOT NULL
    AND disabled_at NULL; NOT enabled ⟹ disabled_at NOT NULL AND
    enabled_at NULL. Probed via UPDATE on seeded rows — CHECKs fire on
    UPDATE too, without FK/unique-index hazards of fresh inserts."""

    @pytest.mark.asyncio
    async def test_valid_disabled_state_accepted(self, isolated_db_url: str) -> None:
        engine = await _engine_connect(isolated_db_url)
        await _migrate_to_target(isolated_db_url)
        try:
            seeded = await _seed(engine)
            gate_id = str(seeded["gate_ids"][0])
            async with engine.begin() as conn:
                await conn.execute(
                    text(
                        "UPDATE policy_gates SET enabled = false, disabled_at = now(), enabled_at = NULL WHERE id = :g"
                    ),
                    {"g": gate_id},
                )
            state = await _scalar(
                engine,
                "SELECT enabled FROM policy_gates WHERE id = :g",
                {"g": gate_id},
            )
            assert state is False
        finally:
            await engine.dispose()

    @pytest.mark.asyncio
    async def test_enabled_false_without_disabled_at_rejected(self, isolated_db_url: str) -> None:
        engine = await _engine_connect(isolated_db_url)
        await _migrate_to_target(isolated_db_url)
        try:
            seeded = await _seed(engine)
            gate_id = str(seeded["gate_ids"][0])
            async with engine.begin() as conn:
                await conn.execute(
                    text("UPDATE policy_gates SET enabled_at = now(), disabled_at = NULL WHERE id = :g"),
                    {"g": gate_id},
                )
            with pytest.raises(IntegrityError):
                async with engine.begin() as conn:
                    await conn.execute(
                        text("UPDATE policy_gates SET enabled = false WHERE id = :g"),
                        {"g": gate_id},
                    )
        finally:
            await engine.dispose()

    @pytest.mark.asyncio
    async def test_enabled_true_with_disabled_at_rejected(self, isolated_db_url: str) -> None:
        engine = await _engine_connect(isolated_db_url)
        await _migrate_to_target(isolated_db_url)
        try:
            seeded = await _seed(engine)
            gate_id = str(seeded["gate_ids"][0])
            async with engine.begin() as conn:
                await conn.execute(
                    text(
                        "UPDATE policy_gates SET enabled = false, disabled_at = now(), enabled_at = NULL WHERE id = :g"
                    ),
                    {"g": gate_id},
                )
            with pytest.raises(IntegrityError):
                async with engine.begin() as conn:
                    await conn.execute(
                        text("UPDATE policy_gates SET enabled = true WHERE id = :g"),
                        {"g": gate_id},
                    )
        finally:
            await engine.dispose()

    @pytest.mark.asyncio
    async def test_enabled_state_accepted_on_fresh_insert(self, isolated_db_url: str) -> None:
        """The creation state accepted by the CHECK: a brand-new row with
        enabled=true, enabled_at set, disabled_at NULL."""
        engine = await _engine_connect(isolated_db_url)
        await _migrate_to_target(isolated_db_url)
        try:
            seeded = await _seed(engine)
            new_gate = uuid.uuid4()
            async with engine.begin() as conn:
                await conn.execute(
                    text(
                        "INSERT INTO policy_gates (id, organisation_id, eval_id, node_id, "
                        "action, enabled, enabled_at, disabled_at, version) "
                        "VALUES (:i, :oid, :eid, :nid, 'warn', true, now(), NULL, 1)"
                    ),
                    {
                        "i": str(new_gate),
                        "oid": str(seeded["org_id"]),
                        "eid": str(seeded["eval_ids"][3]),
                        "nid": str(seeded["node_id"]),
                    },
                )
            stored_state = await _scalar(
                engine,
                "SELECT enabled FROM policy_gates WHERE id = :g",
                {"g": str(new_gate)},
            )
            assert stored_state is True
        finally:
            await engine.dispose()


class TestSnapshotPinColumnsWriteable:
    @pytest.mark.asyncio
    async def test_snapshot_pins_json_roundtrip_through_postgres(self, isolated_db_url: str) -> None:
        """After the migration a snapshot row can carry pins + fingerprint —
        the JSON round-trips through Postgres unchanged."""
        engine = await _engine_connect(isolated_db_url)
        await _migrate_to_target(isolated_db_url)
        try:
            seeded = await _seed(engine)
            pins = [
                {
                    "policy_gate_id": str(gid),
                    "eval_id": str(eid),
                    "action": "block",
                    "node_id": str(seeded["node_id"]),
                }
                for eid, gid in zip(seeded["eval_ids"][:3], seeded["gate_ids"], strict=True)
            ]
            fingerprint = fingerprint_policy_gate_pins(pins)
            async with engine.begin() as conn:
                await conn.execute(
                    text(
                        "UPDATE pipeline_snapshots SET policy_gate_pins_json = CAST(:pins AS json), "
                        "policy_gate_pins_fingerprint = :fp WHERE id = :sid"
                    ),
                    {"pins": json.dumps(pins), "fp": fingerprint, "sid": str(seeded["snapshot_id"])},
                )
            read_back = None
            async with engine.connect() as conn:
                read_back = (
                    await conn.execute(
                        text(
                            "SELECT policy_gate_pins_json, policy_gate_pins_fingerprint "
                            "FROM pipeline_snapshots WHERE id = :sid"
                        ),
                        {"sid": str(seeded["snapshot_id"])},
                    )
                ).fetchall()
            stored_pins, stored_fingerprint = read_back[0]
            assert stored_fingerprint == fingerprint
            assert isinstance(stored_pins, list)
        finally:
            await engine.dispose()
