"""Idempotent demo-org seed for the /demo auto-login experience (FAR-535).

GATED: runs only when ``MODULO_DEMO_ENABLED`` is truthy AND both
``MODULO_DEMO_USER`` (email) and ``MODULO_DEMO_PASSWORD`` are non-empty
(see ``modulo.core.demo`` — the neutral gate both this seed and the auth
route share). Default off — the seed is a no-op otherwise, so the
default release path behaviour is unchanged.

Creates (idempotently, NO Alembic migrations):

* the demo organisation (slug ``demo``),
* the demo user account (email/password from env; the password hash is
  re-stamped to match the env on every run so rotating the secret works),
* a ``viewer``-role org membership (read-only permission set — the org-role
  hierarchy viewer < runner < operator < admin is the enforcement boundary for
  every route via ``require_permission``; the seed also forces the role BACK to
  viewer if it drifted, and forces ``is_system_admin`` off),
* minimal benign sample data, all clearly prefixed "Demo": two published
  schemas, one two-node pipeline (+ snapshot v1), and two synthetic terminal
  runs (one complete, one failed).

Safety:
* No migrations — pure runtime ORM inserts.
* Org-scoped writes run under ``set_rls_org`` + ``set_rls_execution_context``
  (the documented boot/execution context) so Postgres RLS admits them.
* Account/email writes follow the same pattern as the boot-time
  ``_seed_modulo_users`` seed (system-context transaction, no RLS org).
* Idempotent by natural keys (org slug, account email, schema name, pipeline
  name, per-org run_number) — re-running never duplicates.

Runnable standalone (entrypoint-style, mirrors ``modulo.db.bootstrap_role``):

    python -m modulo.db.seed_demo
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import sys
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from modulo.core.demo import DEMO_ORG_ROLE, DEMO_ORG_SLUG, demo_login_config
from modulo.db.models.account import Account
from modulo.db.models.org_membership import OrgMembership
from modulo.db.models.organisation import Organisation
from modulo.db.models.pipeline import Pipeline
from modulo.db.models.pipeline_snapshot import PipelineSnapshot
from modulo.db.models.run import Run
from modulo.db.models.schema import Schema, SchemaVersion
from modulo.db.rls import set_rls_execution_context, set_rls_org
from modulo.settings import get_settings

_log = logging.getLogger(__name__)

DEMO_ORG_NAME = "Demo"

# SQLAlchemy DBAPIError/StatementError str() and repr() embed the failed
# statement's bind parameters as a "[parameters: (...)]" section. The demo
# account INSERT binds include the demo user's bcrypt password_hash, so every
# seed-failure log/print goes through _safe_exc_text — never the raw
# exception text or repr.
_PARAMETERS_SECTION_RE = re.compile(r"\[parameters:\s*.*?\]", re.DOTALL)


def _safe_exc_text(exc: BaseException) -> str:
    """Type + message for a seed-failure log, with bind parameters stripped.

    SQLAlchemy's DBAPIError/StatementError string and repr forms embed
    ``[parameters: (...)]``; for the demo account INSERT those bind params
    contain the demo account's bcrypt password_hash. The section is removed
    entirely (not masked) so neither the hash nor a "[parameters:" marker
    survives in any log/stdout surface.
    """
    text = f"{type(exc).__name__}: {exc}"
    return _PARAMETERS_SECTION_RE.sub("", text)


class DemoSeedError(Exception):
    """Raised by main.py's demo-seed wrapper when the demo seed fails.

    The message is always ``_safe_exc_text`` output: ``_boot_seed`` prints
    ``repr(exc)`` to stdout and logs the traceback, and SQLAlchemy exception
    reprs embed bind parameters (this seed's include the demo password hash),
    so the original exception is deliberately NOT chained here.
    """


def _demo_pipeline_graph() -> list[dict[str, object]]:
    """Two-node demo pipeline graph (the shape PipelinesView renders)."""
    return [
        {
            "id": "demo-intake",
            "node_type": "agent",
            "label": "Demo: Intake",
            "position": {"x": 100, "y": 100},
            "config": {"agent_prompt": "Summarise the demo input payload."},
        },
        {
            "id": "demo-report",
            "node_type": "agent",
            "label": "Demo: Report",
            "position": {"x": 400, "y": 100},
            "config": {"agent_prompt": "Write a short demo report from the summary."},
        },
    ]


def _demo_graph_json(nodes: list[dict[str, object]]) -> dict[str, object]:
    return {"nodes": nodes, "edges": [{"id": "demo-edge-1", "source": "demo-intake", "target": "demo-report"}]}


async def _select_demo_org(session: AsyncSession) -> Organisation | None:
    """Deterministic single-row lookup for the demo org slug.

    ``organisations.slug`` uniqueness is a PARTIAL index (``deleted_at IS
    NULL``), so multiple soft-deleted 'demo' rows can coexist and a bare
    ``scalar_one_or_none`` would raise ``MultipleResultsFound`` on every boot.
    Mirrors the ``crud.organisation.get_organisation_by_slug`` defence:
    order live rows first, then soft-deleted rows most-recent first, and take
    one. (Organisation carries no ``updated_at``, so ``created_at`` is the
    recency tiebreaker.)
    """
    result = await session.execute(
        select(Organisation)
        .where(Organisation.slug == DEMO_ORG_SLUG)
        .order_by(Organisation.deleted_at.is_not(None), Organisation.created_at.desc())
        .limit(1)
    )
    return result.scalars().first()


def _undelete_demo_org(org: Organisation) -> Organisation:
    """Revive a soft-deleted demo org (the seed owns the slug's live row)."""
    if org.deleted_at is not None:
        org.deleted_at = None
        _log.info("demo_seed.org_undeleted", extra={"slug": DEMO_ORG_SLUG})
    return org


async def _get_or_create_demo_org(session: AsyncSession) -> Organisation:
    """Idempotently create the demo organisation (slug-unique, race-safe)."""
    org = await _select_demo_org(session)
    if org is not None:
        return _undelete_demo_org(org)
    org = Organisation(name=DEMO_ORG_NAME, slug=DEMO_ORG_SLUG, settings_json={})
    try:
        # Savepoint so a concurrent boot that already committed the slug only
        # rolls back this insert, not the surrounding seed transaction.
        async with session.begin_nested():
            session.add(org)
            await session.flush()
    except IntegrityError:
        org = await _select_demo_org(session)
        if org is None:
            raise
        _log.info("demo_seed.org_recovered_after_conflict", extra={"slug": DEMO_ORG_SLUG})
    else:
        _log.info("demo_seed.org_created", extra={"slug": DEMO_ORG_SLUG})
    # The recovered row may be soft-deleted too — the undelete repair applies
    # to every adoption path, not just the primary lookup.
    return _undelete_demo_org(org)


async def _seed_demo_account(session: AsyncSession, email: str, password: str) -> Account:
    """Idempotently create/update the demo user (password re-stamped from env).

    The insert is race-safe across multi-instance boots: a concurrent boot that
    already committed the email rolls back only this savepoint, then the seed
    adopts the winner row and runs the same drift-repair path on it.
    """
    from modulo.auth.passwords import hash_password, verify_password

    result = await session.execute(select(Account).where(Account.email == email))
    account = result.scalar_one_or_none()
    if account is None:
        account = Account(
            email=email,
            display_name="Demo",
            password_hash=hash_password(password),
            auth_provider="local",
            active=True,
            is_system_admin=False,
            must_change_password=False,
        )
        try:
            # Savepoint so a concurrent boot that already committed the email
            # only rolls back this insert, not the surrounding seed transaction.
            async with session.begin_nested():
                session.add(account)
                await session.flush()
        except IntegrityError:
            result = await session.execute(select(Account).where(Account.email == email))
            account = result.scalar_one_or_none()
            if account is None:
                raise
            _log.info("demo_seed.account_recovered_after_conflict", extra={"email": email})
        else:
            _log.info("demo_seed.account_created", extra={"email": email})
            return account

    changed = False
    # Re-stamp the hash every run when the stored hash no longer verifies
    # against the env password, so rotating MODULO_DEMO_PASSWORD takes effect
    # on the next boot without manual DB surgery. bcrypt hashes are salted, so
    # the comparison must go through verify_password (never hash-to-hash).
    # Corrupt-hash safety: a malformed stored hash must not crash the boot
    # seed — verify_password swallows bcrypt's ValueError, and the guard below
    # catches anything unexpected (e.g. a non-string hash read shape) and
    # re-stamps from env instead, matching the rotation intent.
    try:
        stored_hash = account.password_hash or ""
        stored_hash_verifies = bool(stored_hash) and verify_password(password, stored_hash)
    except Exception:
        _log.warning("demo_seed.account_hash_corrupt", extra={"email": email})
        stored_hash_verifies = False
    if not stored_hash_verifies:
        account.password_hash = hash_password(password)
        changed = True
    if account.active is not True:
        account.active = True
        changed = True
    # Defense: the demo account must never be a system admin.
    if account.is_system_admin is True:
        account.is_system_admin = False
        changed = True
    # A pre-existing account with must_change_password set would trap the demo
    # viewer in ForceChangePasswordView, whose mutation is viewer-denied — the
    # demo account must always be immediately usable.
    if account.must_change_password is not False:
        account.must_change_password = False
        changed = True
    if changed:
        _log.info("demo_seed.account_updated", extra={"email": email})
    return account


async def _seed_demo_membership(session: AsyncSession, account: Account, org: Organisation) -> None:
    """Idempotent viewer-role membership; forces a drifted role back to viewer.

    The insert is race-safe across multi-instance boots (savepoint +
    IntegrityError recovery on the (account, org) unique key), and the drift
    warning reports the role captured BEFORE the overwrite.
    """
    result = await session.execute(
        select(OrgMembership).where(
            OrgMembership.account_id == account.id,
            OrgMembership.organisation_id == org.id,
        )
    )
    membership = result.scalar_one_or_none()
    if membership is None:
        membership = OrgMembership(
            account_id=account.id,
            organisation_id=org.id,
            role=DEMO_ORG_ROLE,
        )
        try:
            # Savepoint so a concurrent boot that already committed the
            # (account, org) pair only rolls back this insert, not the
            # surrounding seed transaction.
            async with session.begin_nested():
                session.add(membership)
                await session.flush()
        except IntegrityError:
            result = await session.execute(
                select(OrgMembership).where(
                    OrgMembership.account_id == account.id,
                    OrgMembership.organisation_id == org.id,
                )
            )
            membership = result.scalar_one_or_none()
            if membership is None:
                raise
            _log.info("demo_seed.membership_recovered_after_conflict", extra={"email": account.email})
        else:
            _log.info("demo_seed.membership_created", extra={"email": account.email, "role": DEMO_ORG_ROLE})
            return
    if membership.role != DEMO_ORG_ROLE:
        # Capture BEFORE overwriting so the warning reports the actual
        # previous role, not the role we just wrote.
        previous_role = membership.role
        membership.role = DEMO_ORG_ROLE
        _log.warning(
            "demo_seed.membership_role_reset",
            extra={"email": account.email, "previous_role": previous_role, "role": DEMO_ORG_ROLE},
        )


async def _seed_demo_schemas(session: AsyncSession, org: Organisation, account: Account) -> None:
    """Two published demo schemas (idempotent by (organisation, name)).

    Race-safe across multi-instance boots like the org/account/membership
    inserts: each insert runs in a savepoint; a concurrent boot that already
    committed the natural key only rolls back that savepoint, and the seed
    adopts the winner row and continues.
    """
    specs = [
        {
            "name": "Demo Intake",
            "description": "Demo sample: intake payload",
            "definition": {
                "type": "object",
                "properties": {"title": {"type": "string"}, "summary": {"type": "string"}},
                "required": ["title"],
            },
        },
        {
            "name": "Demo Report",
            "description": "Demo sample: report payload",
            "definition": {
                "type": "object",
                "properties": {"report": {"type": "string"}},
                "required": ["report"],
            },
        },
    ]
    for spec in specs:
        result = await session.execute(
            select(Schema).where(Schema.organisation_id == org.id, Schema.name == spec["name"])
        )
        schema = result.scalar_one_or_none()
        if schema is None:
            schema = Schema(
                organisation_id=org.id,
                name=spec["name"],
                account_id=account.id,
                description=spec["description"],
            )
            try:
                # Savepoint: a concurrent boot that committed the same
                # (organisation, name) only rolls back this insert.
                async with session.begin_nested():
                    session.add(schema)
                    await session.flush()
            except IntegrityError:
                result = await session.execute(
                    select(Schema).where(Schema.organisation_id == org.id, Schema.name == spec["name"])
                )
                schema = result.scalar_one_or_none()
                if schema is None:
                    raise
                _log.info("demo_seed.schema_recovered_after_conflict", extra={"schema_name": spec["name"]})
            else:
                _log.info("demo_seed.schema_created", extra={"schema_name": spec["name"]})
        version_result = await session.execute(
            select(SchemaVersion).where(
                SchemaVersion.schema_id == schema.id,
                SchemaVersion.version == "v1",
                SchemaVersion.organisation_id == org.id,
            )
        )
        if version_result.scalar_one_or_none() is not None:
            continue
        try:
            # Savepoint: same multi-boot protection for the version row.
            async with session.begin_nested():
                session.add(
                    SchemaVersion(
                        organisation_id=org.id,
                        schema_id=schema.id,
                        version="v1",
                        version_number=1,
                        definition_json=spec["definition"],
                        published=True,
                        account_id=account.id,
                    )
                )
                await session.flush()
        except IntegrityError:
            version_result = await session.execute(
                select(SchemaVersion).where(
                    SchemaVersion.schema_id == schema.id,
                    SchemaVersion.version == "v1",
                    SchemaVersion.organisation_id == org.id,
                )
            )
            if version_result.scalar_one_or_none() is None:
                raise
            _log.info("demo_seed.schema_version_recovered_after_conflict", extra={"schema_name": spec["name"]})
        else:
            _log.info("demo_seed.schema_version_created", extra={"schema_name": spec["name"], "version": "v1"})


async def _seed_demo_pipeline_and_runs(session: AsyncSession, org: Organisation, account: Account) -> None:
    """One demo pipeline (+ snapshot v1) and two terminal demo runs.

    Race-safe across multi-instance boots like the org/account/membership
    inserts: each insert runs in a savepoint with IntegrityError recovery on
    its natural key (see _seed_demo_schemas).
    """
    pipeline_result = await session.execute(
        select(Pipeline).where(Pipeline.organisation_id == org.id, Pipeline.name == "Demo Governance Pipeline")
    )
    pipeline = pipeline_result.scalar_one_or_none()
    if pipeline is None:
        nodes = _demo_pipeline_graph()
        pipeline = Pipeline(
            organisation_id=org.id,
            name="Demo Governance Pipeline",
            description="Demo sample pipeline — read-only demo data (FAR-535).",
            account_id=account.id,
            visibility="org",
            graph_nodes_json=nodes,
            default_autonomy_level="manual_approval",
        )
        try:
            # Savepoint: a concurrent boot that committed the same
            # (organisation, name) only rolls back this insert.
            async with session.begin_nested():
                session.add(pipeline)
                await session.flush()
        except IntegrityError:
            pipeline_result = await session.execute(
                select(Pipeline).where(Pipeline.organisation_id == org.id, Pipeline.name == "Demo Governance Pipeline")
            )
            pipeline = pipeline_result.scalar_one_or_none()
            if pipeline is None:
                raise
            _log.info("demo_seed.pipeline_recovered_after_conflict", extra={"org_id": str(org.id)})
        else:
            _log.info("demo_seed.pipeline_created", extra={"pipeline_id": str(pipeline.id)})

    snapshot_result = await session.execute(
        select(PipelineSnapshot).where(
            PipelineSnapshot.pipeline_id == pipeline.id,
            PipelineSnapshot.snapshot_version == 1,
        )
    )
    snapshot = snapshot_result.scalar_one_or_none()
    if snapshot is None:
        snapshot = PipelineSnapshot(
            organisation_id=org.id,
            pipeline_id=pipeline.id,
            snapshot_version=1,
            account_id=account.id,
            graph_json=_demo_graph_json(pipeline.graph_nodes_json),
            connector_bindings_json=[],
            schema_pins_json=[],
            prompt_pins_json=[],
            model_backend_pins_json=[],
        )
        try:
            # Savepoint: same multi-boot protection for the snapshot row.
            async with session.begin_nested():
                session.add(snapshot)
                await session.flush()
        except IntegrityError:
            snapshot_result = await session.execute(
                select(PipelineSnapshot).where(
                    PipelineSnapshot.pipeline_id == pipeline.id,
                    PipelineSnapshot.snapshot_version == 1,
                )
            )
            snapshot = snapshot_result.scalar_one_or_none()
            if snapshot is None:
                raise
            _log.info("demo_seed.snapshot_recovered_after_conflict", extra={"pipeline_id": str(pipeline.id)})
        else:
            _log.info("demo_seed.snapshot_created", extra={"snapshot_id": str(snapshot.id)})

    # Deterministic, idempotent runs: fixed run_numbers with per-number
    # existence checks. The demo org is seed-owned (the viewer demo user cannot
    # trigger runs), so a missing number can only mean a partial/absent seed.
    # (run_number, status, total_tokens, total_cost_usd)
    run_specs: list[tuple[int, str, int, float]] = [
        (1, "complete", 1840, 0.0042),
        (2, "failed", 210, 0.0005),
    ]
    for run_number, status, total_tokens, total_cost_usd in run_specs:
        existing_result = await session.execute(
            select(Run.id).where(Run.organisation_id == org.id, Run.run_number == run_number)
        )
        if existing_result.scalar_one_or_none() is not None:
            continue
        thread_id = f"demo-seed-{org.id}-{run_number}"
        run = Run(
            organisation_id=org.id,
            pipeline_id=pipeline.id,
            snapshot_id=snapshot.id,
            account_id=account.id,
            trigger_type="manual",
            status=status,
            run_number=run_number,
            input_hash=hashlib.sha256(thread_id.encode()).hexdigest(),
            langgraph_thread_id=thread_id,
            started_at=datetime.now(UTC) - timedelta(hours=2 * run_number),
            completed_at=datetime.now(UTC) - timedelta(hours=2 * run_number - 1),
            total_tokens=total_tokens,
            total_cost_usd=total_cost_usd,
            error_detail="Demo sample failure — no real work was performed." if status == "failed" else None,
            error_code="DEMO_SAMPLE" if status == "failed" else None,
            outputs_json={"demo": "Sample demo output — synthetic, no agent execution."},
        )
        try:
            # Savepoint: same multi-boot protection for the run row
            # ((organisation, run_number) unique key).
            async with session.begin_nested():
                session.add(run)
                await session.flush()
        except IntegrityError:
            existing_result = await session.execute(
                select(Run.id).where(Run.organisation_id == org.id, Run.run_number == run_number)
            )
            if existing_result.scalar_one_or_none() is None:
                raise
            _log.info("demo_seed.run_recovered_after_conflict", extra={"run_number": run_number})
        else:
            _log.info("demo_seed.run_created", extra={"run_number": run_number, "status": status})


async def seed_demo(session: AsyncSession) -> str | None:
    """Seed the demo org, demo user, and sample data. Idempotent.

    Returns a summary string when the demo feature is configured (something may
    have been created or already existed), or ``None`` when the feature is not
    configured (a deliberate no-op — the caller logs its own outcome).
    """
    settings = get_settings()
    config = demo_login_config(settings)
    if config is None:
        _log.info("demo_seed.disabled")
        return None
    email, password = config

    # Org/account/membership writes follow the boot-seed pattern (system
    # context, no org RLS) used by _seed_modulo_users / seed_demo_org.
    org = await _get_or_create_demo_org(session)
    account = await _seed_demo_account(session, email, password)
    await _seed_demo_membership(session, account, org)

    # Operator-misconfiguration observability: MODULO_DEMO_USER should name a
    # dedicated account. If it named an existing account with memberships in
    # other orgs, the demo endpoint still only ever mints the demo-org viewer
    # session (see auth._resolve_demo_org_membership) — but flag the stray
    # memberships loudly so the operator can point the env at a fresh account.
    # The join filters soft-deleted orgs (deleted_at IS NULL) so resurrected
    # or expired memberships cannot inflate other_org_count.
    stray_org_ids = (
        (
            await session.execute(
                select(OrgMembership.organisation_id)
                .join(Organisation, Organisation.id == OrgMembership.organisation_id)
                .where(
                    OrgMembership.account_id == account.id,
                    OrgMembership.organisation_id != org.id,
                    Organisation.deleted_at.is_(None),
                )
            )
        )
        .scalars()
        .all()
    )
    if stray_org_ids:
        _log.warning(
            "demo_seed.account_has_memberships_outside_demo_org",
            extra={"email": email, "other_org_count": len(stray_org_ids)},
        )

    # Org-scoped entity writes run under the documented execution context so
    # Postgres RLS admits them (mirrors seed_cost_components_for_org).
    await set_rls_org(session, org.id)
    await set_rls_execution_context(session)
    await _seed_demo_schemas(session, org, account)
    await _seed_demo_pipeline_and_runs(session, org, account)

    return f"org={DEMO_ORG_SLUG} user={email}"


async def seed_demo_runtime(session_factory: async_sessionmaker[AsyncSession] | None = None) -> str | None:
    """Run ``seed_demo`` in its own transaction on a session factory.

    The single transaction wrapper for both callers: main.py's boot lifespan
    passes its DI ``get_or_create_session_factory`` engine-backed factory (one
    engine path per caller), while the ``python -m modulo.db.seed_demo``
    entry point below falls back to the shared module-level
    ``AsyncSessionLocal``.
    """
    factory = session_factory
    if factory is None:
        from modulo.db.session import AsyncSessionLocal

        factory = AsyncSessionLocal
    async with factory() as session, session.begin():
        return await seed_demo(session)


def main() -> None:
    """Standalone entry: seed the demo org/user when configured, then exit."""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    if demo_login_config(get_settings()) is None:
        print("[demo-seed] disabled — set MODULO_DEMO_ENABLED, MODULO_DEMO_USER, MODULO_DEMO_PASSWORD")  # noqa: T201
        return
    try:
        summary = asyncio.run(seed_demo_runtime())
    except Exception as exc:
        detail = _safe_exc_text(exc)
        # Sanitized type + message only — NO exc_info. SQLAlchemy reprs embed
        # bind params (this seed's include the demo password hash), and
        # traceback rendering re-embeds them via str(exc), so the raw
        # exception/traceback must never reach stdout or the logs.
        _log.error("demo_seed.failed", extra={"error": detail})
        print(f"[demo-seed] FAILED ({detail})", flush=True)  # noqa: T201
        sys.exit(1)
    print(f"[demo-seed] ok ({summary})", flush=True)  # noqa: T201


if __name__ == "__main__":
    main()
