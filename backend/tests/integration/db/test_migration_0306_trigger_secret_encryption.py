"""Integration test for migration 0306_trigger_secret_encryption (FAR-1732).

Against REAL Postgres (testcontainers, private database per test — the
``test_migration_0302`` / ``test_migration_0191`` harness), unlike the
structural unit test whose bind is a MagicMock and whose SQL never executes:

* seed three pre-existing trigger rows at ``0305_guardrail_escalate_action``
  (the deployed shape where ``hmac_secret`` may still be PLAINTEXT):
  - ``plain``  — plaintext ``hmac_secret`` + ``signing_secret`` + non-secret keys,
  - ``enc``    — a secret already stored under a Fernet token (the write path's
    shape), ``masked`` — the sensitive-value mask sentinel;
* the REAL alembic ``upgrade`` of ``0306_trigger_secret_encryption``:
  - exactly the PLAINTEXT row's ``config_json`` changes — the already-encrypted
    and masked rows are byte-for-byte untouched (``config_json::text`` compare),
  - the rewritten secret decrypts back to the original plaintext under the
    application's own ``fernet_key``,
  - every non-secret key of that row is preserved,
  - the already-encrypted row still decrypts (not double-encrypted, not clobbered);
* idempotency on REAL data: an alembic downgrade (0306's deliberate no-op) and a
  second ``upgrade`` re-run leaves all three rows byte-for-byte identical to the
  first run — a re-encryption would produce a fresh Fernet token (timestamped)
  and fail this comparison.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncGenerator
from pathlib import Path
from urllib.parse import urlparse, urlunparse

import pytest
import pytest_asyncio
from alembic import command
from alembic.config import Config
from cryptography.fernet import Fernet
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

from modulo.core.secret_patterns import SENSITIVE_VALUE_MASK
from modulo.settings import get_settings

pytestmark = [pytest.mark.integration]

BACKEND_ROOT = Path(__file__).parents[3]  # backend/

MIGRATION_REV = "0306_trigger_secret_encryption"
PREV_REV = "0305_guardrail_escalate_action"

_PLAIN_HMAC = "plain-hmac-secret"
_PLAIN_SIGNING = "plain-signing-secret"
_ENC_SECRET = b"already-encrypted-secret"
_SECRET_KEYS = ("hmac_secret", "signing_secret")


# ---------------------------------------------------------------------------
# Harness (mirrors test_migration_0302_runs_run_grouping.py)
# ---------------------------------------------------------------------------


def _alembic_config(db_url: str) -> Config:
    config = Config(BACKEND_ROOT / "alembic.ini")
    config.set_main_option("sqlalchemy.url", db_url)
    config.set_main_option("script_location", str(BACKEND_ROOT / "src" / "modulo" / "db" / "migrations"))
    config.config_file_name = None
    return config


def _swap_db_name(db_url: str, new_db: str) -> str:
    parsed = urlparse(db_url)
    return urlunparse(parsed._replace(path=f"/{new_db}"))


def _alembic_cmd(iso_url: str, cmd: str, target: str) -> None:
    """Run an alembic command against the isolated DB via the Python API.

    Deliberately does NOT inject ``cmd_opts`` (the FAR-967 F2 shape): env.py
    infers direction from the active EnvironmentContext instead.
    """
    cfg = _alembic_config(iso_url)
    assert getattr(cfg, "cmd_opts", None) is None, "Python-API invocation must carry no cmd_opts"
    getattr(command, cmd)(cfg, target)


async def _fresh_db(db_url: str, prefix: str) -> tuple[str, str]:
    """Create an empty private database; return ``(name, async url)``."""
    admin_engine = create_async_engine(db_url, poolclass=NullPool, execution_options={"isolation_level": "AUTOCOMMIT"})
    db_name = f"{prefix}_{uuid.uuid4().hex[:10]}"
    async with admin_engine.connect() as conn:
        await conn.execute(text(f'CREATE DATABASE "{db_name}" WITH TEMPLATE template0'))
    await admin_engine.dispose()
    return db_name, _swap_db_name(db_url, db_name)


async def _drop_db(db_url: str, db_name: str) -> None:
    admin_engine = create_async_engine(db_url, poolclass=NullPool, execution_options={"isolation_level": "AUTOCOMMIT"})
    async with admin_engine.connect() as conn:
        await conn.execute(text(f'DROP DATABASE IF EXISTS "{db_name}"'))
    await admin_engine.dispose()


def _migrate(iso_url: str, target: str) -> None:
    """Run an alembic upgrade with env.py's URL resolution pinned to ``iso_url``."""
    with pytest.MonkeyPatch().context() as mp:
        mp.setenv("DATABASE_URL", iso_url)
        mp.setenv("DATABASE_ADMIN_URL", iso_url)
        _alembic_cmd(iso_url, "upgrade", target)


def _downgrade(iso_url: str, target: str) -> None:
    with pytest.MonkeyPatch().context() as mp:
        mp.setenv("DATABASE_URL", iso_url)
        mp.setenv("DATABASE_ADMIN_URL", iso_url)
        _alembic_cmd(iso_url, "downgrade", target)


@pytest_asyncio.fixture
async def pre_upgrade_db_url(db_url: str) -> AsyncGenerator[str, None]:
    """A private Postgres database migrated to ``PREV_REV`` — the deployed
    shape where plaintext trigger secrets can still exist."""
    db_name, iso_url = await _fresh_db(db_url, "m0306_pre")
    try:
        _migrate(iso_url, PREV_REV)
        yield iso_url
    finally:
        await _drop_db(db_url, db_name)


async def _engine(iso_url: str) -> AsyncEngine:
    return create_async_engine(iso_url, poolclass=NullPool)


async def _alembic_version(engine: AsyncEngine) -> str:
    async with engine.connect() as conn:
        return str((await conn.execute(text("SELECT version_num FROM alembic_version"))).scalar_one())


async def _seed(engine: AsyncEngine) -> dict[str, uuid.UUID]:
    """Seed account/org/pipeline + the three trigger rows under test.

    ``triggers.config_json`` is NOT NULL with a Python-side ``default=dict``
    (no SQL server default), so the raw INSERT supplies it — the same seed
    shape ``test_migration_0302`` uses for its rows.
    """
    ids = {
        "account": uuid.uuid4(),
        "org": uuid.uuid4(),
        "pipeline": uuid.uuid4(),
        "plain": uuid.uuid4(),
        "enc": uuid.uuid4(),
        "masked": uuid.uuid4(),
    }
    enc_token = Fernet(get_settings().fernet_key.encode()).encrypt(_ENC_SECRET).decode()
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO accounts (id, email, display_name, password_hash, auth_provider, active) "
                "VALUES (:id, 'f0305@example.com', 'f0305', 'hash', 'local', true)"
            ),
            {"id": str(ids["account"])},
        )
        await conn.execute(
            text(
                "INSERT INTO organisations (id, name, slug, settings_json) VALUES (:id, 'f0305', 'f0305', '{}'::json)"
            ),
            {"id": str(ids["org"])},
        )
        await conn.execute(
            text(
                "INSERT INTO pipelines (id, organisation_id, account_id, name, "
                "max_concurrent_runs, lock_wait_timeout_seconds, node_timeout_seconds, "
                "run_context_defaults, graph_nodes_json) "
                "VALUES (:id, :oid, :aid, 'f0305 pipeline', 10, 30, 300, '{}'::json, '[]'::json)"
            ),
            {"id": str(ids["pipeline"]), "oid": str(ids["org"]), "aid": str(ids["account"])},
        )
        seeds: list[tuple[str, dict[str, object]]] = [
            (
                "plain",
                {
                    "url": "https://example.com",
                    "hmac_secret": _PLAIN_HMAC,
                    "signing_secret": _PLAIN_SIGNING,
                    "event_filters": {"accepted_events": ["push"]},
                },
            ),
            (
                "enc",
                {
                    "url": "https://enc.example.com",
                    "hmac_secret": enc_token,
                    "work_item_ref_paths": [".agents"],
                },
            ),
            (
                "masked",
                {
                    "url": "https://masked.example.com",
                    "hmac_secret": SENSITIVE_VALUE_MASK,
                },
            ),
        ]
        for key, config in seeds:
            await conn.execute(
                text(
                    "INSERT INTO triggers (id, organisation_id, pipeline_id, account_id, "
                    "trigger_type, config_json) "
                    "VALUES (:id, :oid, :pid, :aid, 'webhook', CAST(:cfg AS jsonb))"
                ),
                {
                    "id": str(ids[key]),
                    "oid": str(ids["org"]),
                    "pid": str(ids["pipeline"]),
                    "aid": str(ids["account"]),
                    "cfg": json.dumps(config),
                },
            )
    return ids


async def _read_configs(engine: AsyncEngine) -> dict[str, str]:
    """Every trigger's ``config_json::text`` keyed by id — byte-level compare."""
    async with engine.connect() as conn:
        rows = await conn.execute(text("SELECT id::text, config_json::text FROM triggers ORDER BY id::text"))
        return {str(row[0]): str(row[1]) for row in rows.all()}


def _without_secret_keys(config: dict[str, object]) -> dict[str, object]:
    return {k: v for k, v in config.items() if k not in _SECRET_KEYS}


# ---------------------------------------------------------------------------
# Upgrade over a populated database
# ---------------------------------------------------------------------------


async def test_upgrade_encrypts_only_the_plaintext_row_and_is_idempotent(pre_upgrade_db_url: str) -> None:
    engine = await _engine(pre_upgrade_db_url)
    try:
        ids = await _seed(engine)
        # _read_configs keys are id::text — index with the string form.
        row_id = {k: str(v) for k, v in ids.items()}
        before = await _read_configs(engine)
        assert len(before) == 3, f"expected the three seeded rows, got {len(before)}"

        _migrate(pre_upgrade_db_url, MIGRATION_REV)
        assert await _alembic_version(engine) == MIGRATION_REV

        after = await _read_configs(engine)
        # EXACTLY the plaintext row changed: the already-encrypted and masked
        # rows are byte-for-byte untouched.
        assert after[row_id["enc"]] == before[row_id["enc"]], "an already-encrypted row must never be rewritten"
        assert after[row_id["masked"]] == before[row_id["masked"]], "a masked sentinel row must never be rewritten"
        assert after[row_id["plain"]] != before[row_id["plain"]], "the plaintext secret row must be rewritten"

        plain = json.loads(after[row_id["plain"]])
        plain_before = json.loads(before[row_id["plain"]])
        # Non-secret keys preserved.
        assert _without_secret_keys(plain) == _without_secret_keys(plain_before)
        assert plain["url"] == "https://example.com"
        assert plain["event_filters"] == {"accepted_events": ["push"]}
        # The rewritten secrets decrypt back to the originals under the
        # application's own fernet key (the SAME key the write path uses).
        fern = Fernet(get_settings().fernet_key.encode())
        assert fern.decrypt(plain["hmac_secret"].encode()) == _PLAIN_HMAC.encode()
        assert fern.decrypt(plain["signing_secret"].encode()) == _PLAIN_SIGNING.encode()
        # The already-encrypted row still decrypts to ITS original value —
        # not double-encrypted, not clobbered.
        still_enc = json.loads(after[row_id["enc"]])
        assert fern.decrypt(still_enc["hmac_secret"].encode()) == _ENC_SECRET

        # Idempotency on real data: 0305's downgrade is a deliberate no-op, so
        # a downgrade + second upgrade re-runs upgrade() over the now-encrypted
        # rows. All three rows must be byte-for-byte identical to the first
        # run — a re-encryption would mint a fresh (timestamped) Fernet token
        # and change the bytes.
        _downgrade(pre_upgrade_db_url, PREV_REV)
        assert await _alembic_version(engine) == PREV_REV
        _migrate(pre_upgrade_db_url, MIGRATION_REV)
        assert await _alembic_version(engine) == MIGRATION_REV
        assert await _read_configs(engine) == after, "a second upgrade run must be a byte-for-byte no-op"
    finally:
        await engine.dispose()
