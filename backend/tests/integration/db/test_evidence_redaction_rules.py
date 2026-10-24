"""Real-Postgres integration tests for the evidence redaction rules (FAR-944).

Behavioural acceptance tests where a real database is required (Docker /
testcontainers): the shipped catch-all seed rule actually redacts an
unclassified key for viewers (AC 11), the admin upsert round-trips through the
real table (AC 4), and the shipped RLS policy isolates organisations under a
non-superuser role.

Mechanics-level coverage lives in
``tests/unit/core/evidence/test_evidence_redaction.py``.

Because the integration database persists across tests (session-scoped engine,
no truncation), every test mints its OWN organisation.
"""

from __future__ import annotations

import importlib.util
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from modulo.api.routes.evidence import _upsert_redaction_rule
from modulo.core.evidence_redaction import (
    REDACTED_SENTINEL,
    apply_redaction,
    load_redaction_rules,
    resolve_redaction_classes_from,
)
from modulo.db.models.evidence_redaction_rule import EvidenceRedactionRule

pytestmark = pytest.mark.integration

_MIGRATION_PATH = (
    Path(__file__).resolve().parents[3]
    / "src"
    / "modulo"
    / "db"
    / "migrations"
    / "versions"
    / "0320_evidence_redaction_rules.py"
)

_BOTH_CLASSES = frozenset({"sensitive_output", "internal_metadata"})


async def _create_org(db_engine: AsyncEngine) -> uuid.UUID:
    """Commit a fresh organisation row (the integration DB never truncates)."""
    org_id = uuid.uuid4()
    async with db_engine.begin() as conn:
        await conn.execute(
            text("INSERT INTO organisations (id, name, slug, settings_json) VALUES (:id, :name, :slug, '{}'::json)"),
            {"id": str(org_id), "name": "Redaction Org", "slug": f"redact-{org_id.hex[:8]}"},
        )
    return org_id


def _load_migration() -> Any:
    spec = importlib.util.spec_from_file_location("mig_0320_evidence_redaction_rules", _MIGRATION_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def _resolve_classes(
    db_session: AsyncSession,
    org_id: uuid.UUID,
    role: str,
    key: str,
) -> frozenset[str]:
    """Load the org's rules, then resolve one ``(role, key)`` from them."""
    rules = await load_redaction_rules(db_session, org_id)
    return resolve_redaction_classes_from(rules, role, key)


async def _insert_rule(
    engine: AsyncEngine,
    org_id: uuid.UUID,
    key_pattern: str,
    role: str,
    redacted_fields: list[str],
) -> uuid.UUID:
    rule_id = uuid.uuid4()
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO evidence_redaction_rules "
                "(id, organisation_id, key_pattern, role, redacted_fields) "
                "VALUES (:id, :org, :kp, :role, :fields)"
            ),
            {"id": str(rule_id), "org": str(org_id), "kp": key_pattern, "role": role, "fields": redacted_fields},
        )
    return rule_id


# ---------------------------------------------------------------------------
# AC 11 — the shipped catch-all seed redacts unclassified keys for viewers
# ---------------------------------------------------------------------------


async def test_catch_all_seed_redacts_unclassified_key_for_viewer(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    org_id = await _create_org(db_engine)

    # Run the migration's OWN seed statement (idempotent, guarded by NOT EXISTS).
    migration = _load_migration()
    async with db_engine.begin() as conn:
        await conn.execute(text(migration._SEED_CATCH_ALL_SQL))

    seeded = (
        (
            await db_session.execute(
                select(EvidenceRedactionRule).where(
                    EvidenceRedactionRule.organisation_id == org_id,
                    EvidenceRedactionRule.key_pattern == "*",
                    EvidenceRedactionRule.role == "viewer",
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(seeded) == 1
    assert set(seeded[0].redacted_fields) == set(_BOTH_CLASSES)

    # An unclassified key resolves to BOTH classes via the catch-all.
    classes = await _resolve_classes(db_session, org_id, "viewer", "brand.new.unclassified.key")
    assert classes == _BOTH_CLASSES

    redacted = apply_redaction({"raw_output": "secret", "config": {"a": 1}, "status": "complete"}, classes)
    assert redacted is not None
    assert redacted["raw_output"] == REDACTED_SENTINEL
    assert redacted["config"] == REDACTED_SENTINEL
    assert redacted["status"] == "complete"

    # An admin request for the same unclassified key is not redacted (admin is
    # a TRUSTED role, so the no-rule default is fail-open for it).
    admin_classes = await _resolve_classes(db_session, org_id, "admin", "brand.new.unclassified.key")
    assert not admin_classes
    admin_view = apply_redaction({"raw_output": "secret"}, admin_classes)
    assert admin_view is not None
    assert admin_view["raw_output"] == "secret"

    # Regression (FAR-944 review): runner has no rule and is NOT trusted, so it
    # fails CLOSED — both classes redacted, unlike the trusted admin above.
    runner_classes = await _resolve_classes(db_session, org_id, "runner", "brand.new.unclassified.key")
    assert runner_classes == _BOTH_CLASSES
    runner_view = apply_redaction({"raw_output": "secret"}, runner_classes)
    assert runner_view["raw_output"] == REDACTED_SENTINEL


# ---------------------------------------------------------------------------
# AC 4 — upsert then read reflects the rule
# ---------------------------------------------------------------------------


async def test_upsert_then_resolve_reflects_rule(db_engine: AsyncEngine, db_session: AsyncSession) -> None:
    org_id = await _create_org(db_engine)

    await _upsert_redaction_rule(db_session, org_id, "model_judge.*", "viewer", ["sensitive_output"])
    await db_session.commit()

    matched = await _resolve_classes(db_session, org_id, "viewer", "model_judge.grade")
    assert matched == frozenset({"sensitive_output"})

    # Upserting the same (key_pattern, role) updates in place.
    await _upsert_redaction_rule(db_session, org_id, "model_judge.*", "viewer", ["internal_metadata"])
    await db_session.commit()

    updated = await _resolve_classes(db_session, org_id, "viewer", "model_judge.grade")
    assert updated == frozenset({"internal_metadata"})

    # A more specific pattern wins over the broader one.
    await _upsert_redaction_rule(db_session, org_id, "model_judge.grade", "viewer", ["sensitive_output"])
    await db_session.commit()

    specific = await _resolve_classes(db_session, org_id, "viewer", "model_judge.grade")
    assert specific == frozenset({"sensitive_output"})

    # A different key under the broader pattern still uses the broader rule.
    broader = await _resolve_classes(db_session, org_id, "viewer", "model_judge.raw")
    assert broader == frozenset({"internal_metadata"})


async def test_upsert_restores_a_soft_deleted_rule(db_engine: AsyncEngine, db_session: AsyncSession) -> None:
    org_id = await _create_org(db_engine)

    created = await _upsert_redaction_rule(db_session, org_id, "llm.*", "viewer", ["sensitive_output"])
    await db_session.commit()
    created_id = created.id

    created.deleted_at = datetime.now(UTC)
    await db_session.commit()

    restored = await _upsert_redaction_rule(db_session, org_id, "llm.*", "viewer", ["internal_metadata"])
    await db_session.commit()

    assert restored.id == created_id
    assert restored.deleted_at is None
    assert restored.redacted_fields == ["internal_metadata"]


# ---------------------------------------------------------------------------
# RLS org isolation on the shipped policy
# ---------------------------------------------------------------------------


async def test_rls_org_isolation_hides_other_org_rules(db_engine: AsyncEngine, app_engine: AsyncEngine) -> None:
    org_a = await _create_org(db_engine)
    org_b = await _create_org(db_engine)

    await _insert_rule(db_engine, org_a, "*", "viewer", ["sensitive_output"])
    await _insert_rule(db_engine, org_b, "*", "viewer", ["internal_metadata"])

    factory = async_sessionmaker(app_engine, expire_on_commit=False)
    async with factory() as session, session.begin():
        await session.execute(
            text("SELECT set_config('app.organisation_id', :oid, true)"),
            {"oid": str(org_a)},
        )
        rows = list((await session.execute(select(EvidenceRedactionRule))).scalars().all())

    visible_org_ids = {row.organisation_id for row in rows}
    assert org_a in visible_org_ids
    assert org_b not in visible_org_ids
