"""FAR-2040 / ADR 066 S1: migration ``0328_eval_verdict_contract`` against real Postgres.

Covers the migration's own contract, on a private database per test (the
``test_migration_0308`` harness):

* the pre-upgrade shape (``0323``) has no ``outcome`` / ``judge_result_json`` /
  ``judge_confidence_kind`` / ``error_code`` and still stores ``llm_judge``;
* the REAL upgrade renames the stored vocabulary, backfills ``outcome`` from
  ``passed``, and lands the EXPAND contract — ``outcome`` NULLABLE (the old
  image is still serving during the rollout), ``judge_result_json`` physical
  ``jsonb``, the closed confidence vocabulary, and a TRANSITIONAL eval-type
  CHECK that accepts both stored names;
* an unknown outcome / confidence kind violates its CHECK;
* a SINGLE ``alembic upgrade 0323 -> 0328`` invocation completes (the
  self-hosted rollout path, where 0324, 0325_run_seam_coverage and 0328 share
  one transaction — the side-connection deadlock reproduction);
* the ``jsonb`` envelope round-trips as an object;
* the downgrade restores the previous shape and vocabulary.
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
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

pytestmark = [pytest.mark.integration]

BACKEND_ROOT = Path(__file__).parents[2]  # backend/
MIGRATION_REV = "0328_eval_verdict_contract"
PREV_REV = "0325_run_seam_coverage"

_ORG = "d0c00000-0000-0000-0000-000000000001"
_PIPELINE = "d0c00000-0000-0000-0000-000000000002"
_ACCOUNT = "d0c00000-0000-0000-0000-000000000003"
_EVAL = "d0c00000-0000-0000-0000-000000000004"
_PASS_RESULT = "d0c00000-0000-0000-0000-000000000005"
_FAIL_RESULT = "d0c00000-0000-0000-0000-000000000006"
_RUN = "d0c00000-0000-0000-0000-000000000007"

_NEW_COLUMNS = ("outcome", "judge_result_json", "judge_confidence_kind", "error_code")


def _alembic_config(db_url: str) -> Config:
    config = Config(BACKEND_ROOT / "alembic.ini")
    config.set_main_option("sqlalchemy.url", db_url)
    config.set_main_option("script_location", str(BACKEND_ROOT / "src" / "modulo" / "db" / "migrations"))
    config.config_file_name = None
    return config


def _swap_db_name(db_url: str, new_db: str) -> str:
    parsed = urlparse(db_url)
    return urlunparse(parsed._replace(path=f"/{new_db}"))


def _alembic_cmd(cmd: str, target: str) -> None:
    """Run an alembic command; env.py resolves the URL from the environment."""
    config = _alembic_config("postgresql+asyncpg://unused/unused")
    assert getattr(config, "cmd_opts", None) is None
    getattr(command, cmd)(config, target)


async def _fresh_db(db_url: str, prefix: str) -> tuple[str, str]:
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


def _migrate(iso_url: str, target: str, cmd: str = "upgrade") -> None:
    with pytest.MonkeyPatch().context() as mp:
        mp.setenv("DATABASE_URL", iso_url)
        mp.setenv("DATABASE_ADMIN_URL", iso_url)
        _alembic_cmd(cmd, target)


async def _seed_pre_upgrade(iso_url: str) -> None:
    """Seed one legacy ``llm_judge`` eval and two result rows under ``0323``.

    ``session_replication_role = replica`` switches FK triggers off for the
    seed connection only, so the fixture does not have to construct a whole
    organisation/pipeline graph; the migration's data behaviour is the subject.
    """
    engine = create_async_engine(iso_url, poolclass=NullPool)
    async with engine.begin() as conn:
        await conn.execute(text("SET session_replication_role = replica"))
        await conn.execute(
            text(
                "INSERT INTO evals (id, organisation_id, pipeline_id, account_id, eval_type, config_json, version) "
                "VALUES (:id, :org, :pipeline, :account, 'llm_judge', '{}'::jsonb, 1)"
            ),
            {"id": _EVAL, "org": _ORG, "pipeline": _PIPELINE, "account": _ACCOUNT},
        )
        for result_id, passed in ((_PASS_RESULT, True), (_FAIL_RESULT, False)):
            await conn.execute(
                text(
                    "INSERT INTO eval_results (id, organisation_id, run_id, eval_id, passed, observed) "
                    "VALUES (:id, :org, :run, :eval, :passed, true)"
                ),
                {"id": result_id, "org": _ORG, "run": _RUN, "eval": _EVAL, "passed": passed},
            )
    await engine.dispose()


async def _column_shapes(engine: AsyncEngine) -> dict[str, dict[str, str | None]]:
    async with engine.connect() as conn:
        rows = (
            await conn.execute(
                text(
                    "SELECT column_name, is_nullable, column_default, data_type "
                    "FROM information_schema.columns "
                    "WHERE table_schema = 'public' AND table_name = 'eval_results' "
                    "AND column_name IN ('outcome', 'judge_result_json', "
                    "'judge_confidence_kind', 'error_code')"
                )
            )
        ).all()
    return {str(r[0]): {"nullable": str(r[1]), "default": r[2], "type": str(r[3])} for r in rows}


@pytest_asyncio.fixture
async def pre_upgrade_db_url(db_url: str) -> AsyncGenerator[str, None]:
    db_name, iso_url = await _fresh_db(db_url, "m0324_pre")
    try:
        _migrate(iso_url, PREV_REV)
        await _seed_pre_upgrade(iso_url)
        yield iso_url
    finally:
        await _drop_db(db_url, db_name)


async def _insert_eval(engine: AsyncEngine, eval_type: str) -> None:
    """Insert an eval row, bypassing FK triggers (a CHECK-only probe)."""
    async with engine.begin() as conn:
        await conn.execute(text("SET session_replication_role = replica"))
        await conn.execute(
            text(
                "INSERT INTO evals "
                "(id, organisation_id, pipeline_id, account_id, eval_type, config_json, version) "
                "VALUES (gen_random_uuid(), :org, :pipeline, :account, :eval_type, '{}'::jsonb, 1)"
            ),
            {"org": _ORG, "pipeline": _PIPELINE, "account": _ACCOUNT, "eval_type": eval_type},
        )


@pytest_asyncio.fixture
async def one_shot_db_url(db_url: str) -> AsyncGenerator[str, None]:
    """A database at 0323, upgraded to 0328 in ONE alembic invocation.

    This is the self-hosted rollout path: the release's single
    ``alembic upgrade heads`` applies 0324_purge, 0325_run_seam_coverage and 0328 in the SAME
    transaction, which is where a side connection taking a lock on ``evals``
    would self-deadlock.
    """
    db_name, iso_url = await _fresh_db(db_url, "m0328_shot")
    try:
        _migrate(iso_url, "0323_pipeline_snapshots_substantive_version_index")
        yield iso_url
    finally:
        await _drop_db(db_url, db_name)


class TestMigration0328:
    async def test_pre_upgrade_has_no_verdict_columns(self, pre_upgrade_db_url: str) -> None:
        """NEGATIVE CONTROL: the columns do not exist before the upgrade."""
        engine = create_async_engine(pre_upgrade_db_url, poolclass=NullPool)
        try:
            assert not await _column_shapes(engine)
            async with engine.connect() as conn:
                eval_type = (
                    await conn.execute(text("SELECT eval_type FROM evals WHERE id = :id"), {"id": _EVAL})
                ).scalar_one()
            assert eval_type == "llm_judge"
        finally:
            await engine.dispose()

    async def test_upgrade_renames_backfills_and_lands_the_contract(self, pre_upgrade_db_url: str) -> None:
        _migrate(pre_upgrade_db_url, MIGRATION_REV)
        engine = create_async_engine(pre_upgrade_db_url, poolclass=NullPool)
        try:
            async with engine.connect() as conn:
                eval_type = (
                    await conn.execute(text("SELECT eval_type FROM evals WHERE id = :id"), {"id": _EVAL})
                ).scalar_one()
                outcomes = dict(
                    (
                        await conn.execute(
                            text("SELECT id::text, outcome FROM eval_results WHERE id IN (:p, :f) ORDER BY id"),
                            {"p": _PASS_RESULT, "f": _FAIL_RESULT},
                        )
                    ).all()
                )
            assert eval_type == "model_judge"
            assert outcomes[_PASS_RESULT] == "pass"
            assert outcomes[_FAIL_RESULT] == "fail"

            shapes = await _column_shapes(engine)
            # EXPAND release: nullable while the old image is still serving.
            # Stage 2 flips this to NOT NULL after the rollout.
            assert shapes["outcome"]["nullable"] == "YES"
            assert shapes["outcome"]["default"] is None
            assert shapes["judge_result_json"]["type"] == "jsonb"
            assert shapes["judge_confidence_kind"]["nullable"] == "YES"
            assert shapes["error_code"]["nullable"] == "YES"

            async with engine.connect() as conn:
                temp_check = (
                    await conn.execute(
                        text("SELECT COUNT(*) FROM pg_constraint WHERE conname = 'ck_eval_results_outcome_not_null'")
                    )
                ).scalar_one()
                vocabulary_valid = (
                    await conn.execute(
                        text(
                            "SELECT convalidated FROM pg_constraint WHERE conname = 'ck_evals_type' "
                            "AND conrelid = 'public.evals'::regclass"
                        )
                    )
                ).scalar_one()
            # The temporary NOT NULL-proof check is dropped after SET NOT NULL.
            assert temp_check == 0
            # The swapped vocabulary CHECK is VALID, not left NOT VALID.
            assert vocabulary_valid is True
        finally:
            await engine.dispose()

    async def test_the_transitional_vocabulary_accepts_both_names(self, pre_upgrade_db_url: str) -> None:
        """EXPAND: the old image's ``llm_judge`` writes stay valid."""
        _migrate(pre_upgrade_db_url, MIGRATION_REV)
        engine = create_async_engine(pre_upgrade_db_url, poolclass=NullPool)
        try:
            await _insert_eval(engine, "llm_judge")
            await _insert_eval(engine, "model_judge")
            with pytest.raises(IntegrityError):
                await _insert_eval(engine, "bogus_judge")
        finally:
            await engine.dispose()

    async def test_single_invocation_0323_to_0328_completes(self, one_shot_db_url: str) -> None:
        """The self-hosted path: 0324 + 0325 + 0328 in ONE transaction, no deadlock."""
        _migrate(one_shot_db_url, MIGRATION_REV)

        engine = create_async_engine(one_shot_db_url, poolclass=NullPool)
        try:
            async with engine.connect() as conn:
                version = (await conn.execute(text("SELECT version_num FROM alembic_version"))).scalar_one()
                legacy_table = (await conn.execute(text("SELECT to_regclass('public.eval_definitions')"))).scalar_one()
            shapes = await _column_shapes(engine)
            assert version == MIGRATION_REV
            assert legacy_table is None
            assert shapes["outcome"]["nullable"] == "YES"
            assert shapes["judge_result_json"]["type"] == "jsonb"
        finally:
            await engine.dispose()

    async def test_the_expand_database_still_allows_a_null_outcome(self, pre_upgrade_db_url: str) -> None:
        """EXPAND contract: the old image (which has no ``outcome``) can insert.

        The application-layer ``before_insert`` guard is what prevents the NEW
        image from introducing a NULL; stage 2 makes the DB enforce it.
        """
        _migrate(pre_upgrade_db_url, MIGRATION_REV)
        engine = create_async_engine(pre_upgrade_db_url, poolclass=NullPool)
        try:
            async with engine.begin() as conn:
                await conn.execute(text("SET session_replication_role = replica"))
                await conn.execute(
                    text(
                        "INSERT INTO eval_results "
                        "(id, organisation_id, run_id, eval_id, passed, observed) "
                        "VALUES (gen_random_uuid(), :org, :run, :eval, true, true)"
                    ),
                    {"org": _ORG, "run": _RUN, "eval": _EVAL},
                )
            async with engine.connect() as conn:
                null_count = (
                    await conn.execute(text("SELECT count(*) FROM eval_results WHERE outcome IS NULL"))
                ).scalar_one()
            assert null_count == 1
        finally:
            await engine.dispose()

    async def test_check_constraints_reject_unknown_values(self, pre_upgrade_db_url: str) -> None:
        _migrate(pre_upgrade_db_url, MIGRATION_REV)
        engine = create_async_engine(pre_upgrade_db_url, poolclass=NullPool)
        try:
            with pytest.raises(IntegrityError):
                async with engine.begin() as conn:
                    await conn.execute(
                        text(
                            "INSERT INTO eval_results "
                            "(id, organisation_id, run_id, eval_id, outcome, passed, observed) "
                            "VALUES (gen_random_uuid(), :org, :run, :eval, 'bogus', true, true)"
                        ),
                        {"org": _ORG, "run": _RUN, "eval": _EVAL},
                    )
            with pytest.raises(IntegrityError):
                async with engine.begin() as conn:
                    await conn.execute(
                        text("UPDATE eval_results SET judge_confidence_kind = 'bogus' WHERE id = :id"),
                        {"id": _PASS_RESULT},
                    )
        finally:
            await engine.dispose()

    async def test_jsonb_envelope_round_trips(self, pre_upgrade_db_url: str) -> None:
        _migrate(pre_upgrade_db_url, MIGRATION_REV)
        engine = create_async_engine(pre_upgrade_db_url, poolclass=NullPool)
        envelope = {
            "schema_version": 1,
            "outcome": "error",
            "score": None,
            "error_code": "judge.unavailable",
            "confidence_kind": "unavailable",
            "questions": [],
        }
        try:
            async with engine.begin() as conn:
                await conn.execute(
                    text(
                        "UPDATE eval_results SET judge_result_json = CAST(:payload AS jsonb), "
                        "judge_confidence_kind = 'unavailable', error_code = 'judge.unavailable' "
                        "WHERE id = :id"
                    ),
                    {"payload": json.dumps(envelope), "id": _FAIL_RESULT},
                )
            async with engine.connect() as conn:
                row = (
                    await conn.execute(
                        text("SELECT judge_result_json, error_code FROM eval_results WHERE id = :id"),
                        {"id": _FAIL_RESULT},
                    )
                ).one()
            stored = row[0]
            if isinstance(stored, str):
                stored = json.loads(stored)
            assert stored["error_code"] == "judge.unavailable"
            assert stored["score"] is None
            assert row[1] == "judge.unavailable"
        finally:
            await engine.dispose()

    async def test_downgrade_restores_the_previous_shape(self, pre_upgrade_db_url: str) -> None:
        _migrate(pre_upgrade_db_url, MIGRATION_REV)
        _migrate(pre_upgrade_db_url, PREV_REV, cmd="downgrade")
        engine = create_async_engine(pre_upgrade_db_url, poolclass=NullPool)
        try:
            assert not await _column_shapes(engine)
            async with engine.connect() as conn:
                eval_type = (
                    await conn.execute(text("SELECT eval_type FROM evals WHERE id = :id"), {"id": _EVAL})
                ).scalar_one()
            assert eval_type == "llm_judge"
        finally:
            await engine.dispose()
