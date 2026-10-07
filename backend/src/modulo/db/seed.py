"""System seeding: system schemas (org creation) + MODULO_USERS boot seeding.

The MODULO_USERS seeder was promoted from ``modulo.api.main`` (FAR-671 /
ADR 031 Decision 2) so the native launcher and the container boot share one
implementation. The DB layer stays free of api imports (import-linter
``db-does-not-import-core``): the ORCHESTRATOR takes an already-built session
factory — the caller (API boot wrapper, native launcher) resolves the
engine/factory from its own layer. The single-entry and rehash seeders are
pure DB logic (lazy model/password imports, same pattern the API version
used).

FAR-1561: seeding a user GRANTS a credential and (for the admin emails) the
``admin`` role, so this module also appends a SYSTEM-actor audit event. That
is the one ``db -> core`` seam this module carries, exempted in
``backend/.importlinter`` next to the four ``db.crud.* -> core.audit_logger``
exemptions: the seed only invokes the append; audit-chain semantics stay in
core.
"""

import asyncio
import logging
import uuid
from collections.abc import Callable
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.db.models.schema import Schema, SchemaVersion
from modulo.db.rls import set_rls_org

_log = logging.getLogger(__name__)

#: ``audit_events.actor_source`` stamped on every MODULO_USERS seed append —
#: the honest machine actor for a grant made with no HTTP principal in scope.
_SEED_ACTOR_SOURCE = "boot_seed"
_SEED_AUDIT_LOG_KEY = "db.seed.boot_user_audit_failed"

SYSTEM_SCHEMAS = [
    {
        "abstract_name": "_system.schema_freeform",
        "name": "schema_freeform",
        "version": "v1",
        "definition": {"type": "object"},
    },
    {
        "abstract_name": "_system.schema_text",
        "name": "schema_text",
        "version": "v1",
        "definition": {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
    },
    {
        "abstract_name": "_system.schema_trigger_payload",
        "name": "schema_trigger_payload",
        "version": "v1",
        "definition": {
            "type": "object",
            "properties": {},
            "additionalProperties": True,
        },
    },
]


async def seed_system_schemas(session: AsyncSession, org_id: uuid.UUID, account_id: uuid.UUID) -> None:
    """Create system schemas for an organisation if they don't already exist."""
    for spec in SYSTEM_SCHEMAS:
        existing = await session.execute(
            select(Schema).where(
                Schema.organisation_id == org_id,
                Schema.name == spec["name"],
            )
        )
        if existing.scalar_one_or_none() is not None:
            continue

        schema = Schema(
            organisation_id=org_id,
            name=spec["name"],
            abstract_name=spec["abstract_name"],
            account_id=account_id,
            description=f"System schema: {spec['name']}",
            system=True,
        )
        session.add(schema)
        await session.flush()

        schema_version = SchemaVersion(
            organisation_id=org_id,
            schema_id=schema.id,
            version=spec["version"],
            version_number=1,
            definition_json=spec["definition"],
            account_id=account_id,
            published=True,
        )
        session.add(schema_version)
        _log.info("seed.system_schema_created", extra={"org_id": str(org_id), "schema_name": spec["name"]})


async def seed_bundled_runner_profile(session: AsyncSession, org_id: uuid.UUID, account_id: uuid.UUID) -> None:
    """Seed the per-org "Bundled Runner (Docker)" EnvironmentProfile (FAR-590 D4).

    Shipped-template seeding at org-creation (owned by the org's account) —
    idempotent: an org that already carries a live Bundled Runner profile row
    keeps it (operator-pinned older digests survive; template updates are
    surfaced live by the drift helper, never silently applied).
    """
    from sqlalchemy import text

    from modulo.db.bundled_runner_template import (
        TEMPLATE_PROFILE_NAME,
        build_bundled_runner_profile_values,
    )
    from modulo.db.models.environment_profile import EnvironmentProfile

    existing = await session.execute(
        text(
            "SELECT id FROM environment_profiles "
            "WHERE organisation_id = :oid AND provider_type = 'runner_docker' "
            "AND deleted_at IS NULL LIMIT 1"
        ),
        {"oid": str(org_id)},
    )
    if existing.fetchone() is not None:
        return
    values = build_bundled_runner_profile_values()
    profile = EnvironmentProfile(
        organisation_id=org_id,
        account_id=account_id,
        name=values["name"],
        description=values["description"],
        provider_type=values["provider_type"],
        image_ref=values["image_ref"],
        capabilities_json=values["capabilities_json"],
        config_json=values["config_json"],
        network_policy=values["network_policy"],
        initialisation_strategy=values["initialisation_strategy"],
        secret_refs_json=values["secret_refs_json"],
        persistence_policy=values["persistence_policy"],
        visibility="org",
    )
    session.add(profile)
    await session.flush()
    _log.info(
        "seed.bundled_runner_profile_created",
        extra={"org_id": str(org_id), "profile_name": TEMPLATE_PROFILE_NAME},
    )


async def seed_modulo_users(session_factory: Callable[[], Any], modulo_users: str) -> None:
    """Seed MODULO_USERS env var entries into the account + membership tables.

    Accepts both bcrypt hashes (user1:$2b$12$hash) and plaintext passwords
    (admin:admin). Plaintext passwords are auto-hashed with bcrypt at seed
    time. Skips when *modulo_users* is empty or no organisation exists.

    Takes the session FACTORY (not Settings) so this DB-layer module never
    imports the API layer — the API boot wrapper and the native launcher each
    resolve their own engine/factory (FAR-671).
    """
    if not modulo_users:
        return

    from modulo.db.models.organisation import Organisation

    async with session_factory() as session, session.begin():
        org_result = await session.execute(select(Organisation).order_by(Organisation.created_at).limit(1))
        org = org_result.scalar_one_or_none()
        if org is None:
            _log.warning("startup.no_org_for_user_seed")
            return

        for entry in modulo_users.split(","):
            await seed_modulo_user(session, org, entry)


async def _append_boot_user_audit(
    session: Any,
    *,
    org_id: uuid.UUID,
    event_type: str,
    account_id: uuid.UUID | None,
    payload: dict[str, Any],
) -> None:
    """Record a MODULO_USERS seed grant on the organisation's audit chain (FAR-1561).

    A seed entry creates a login credential and — for ``admin`` /
    ``admin@modulo.run`` — the ``admin`` role, with no HTTP principal in scope.
    The event therefore carries the SYSTEM actor plus an honest
    ``actor_source`` (``boot_seed``), the same convention
    ``core.audit_logger.background`` enforces for every other background write.

    Appended IN the seeding transaction (the seam ``db.crud.*`` audit appends
    use): the grant and its record commit atomically, and
    ``append_audit_event`` isolates the append in a savepoint, so a failed
    record can never discard the grant it describes. ``set_rls_org`` runs
    first — ``audit_events`` carries the STRICT org-only RLS policy (the
    NULL-context fallback ``org_memberships`` enjoys does not apply to it), so
    an unbound session would fail-closed to 42501 on PostgreSQL.

    Fail-open with a loud log (the grant is already made; mirrors the admin
    create-user route): ``CancelledError`` always propagates.
    """
    from modulo.core.audit_logger import append_audit_event
    from modulo.core.audit_logger.labels import SYSTEM_ACTOR

    try:
        await set_rls_org(session, org_id)
        await append_audit_event(
            session,
            org_id=org_id,
            event_type=event_type,
            actor_user_id=None,
            resource_type="user",
            resource_id=account_id,
            payload_json={"actor": SYSTEM_ACTOR, "actor_source": _SEED_ACTOR_SOURCE, **payload},
        )
    except asyncio.CancelledError:
        raise
    except Exception:
        _log.exception(
            _SEED_AUDIT_LOG_KEY,
            extra={"event_type": event_type, "org_id": str(org_id), "account_id": str(account_id)},
        )


async def seed_modulo_user(session: Any, org: Any, entry: str) -> None:
    """Seed a single MODULO_USERS entry (``email:password``) into the account + membership tables.

    Accepts both bcrypt hashes (user1:$2b$12$hash) and plaintext passwords
    (admin:admin). Plaintext passwords are auto-hashed with bcrypt at seed time.
    """
    from modulo.auth.passwords import hash_password
    from modulo.db.models.account import Account
    from modulo.db.models.org_membership import OrgMembership

    entry = entry.strip()
    if not entry:
        return
    colon = entry.find(":")
    if colon < 1:
        return
    email = entry[:colon]
    pw_part = entry[colon + 1 :]

    result = await session.execute(select(Account).where(Account.email == email))
    existing_account = result.scalar_one_or_none()
    pw_hash = pw_part if pw_part.startswith("$2") else hash_password(pw_part)

    if existing_account is not None and (
        not existing_account.password_hash or not existing_account.password_hash.startswith("$2")
    ):
        await rehash_existing_user(session, org, existing_account, email, pw_hash)
        return

    if existing_account is not None:
        _log.info("startup.user_exists", extra={"email": email})
        return

    account = Account(
        email=email,
        display_name=email.split("@")[0],
        password_hash=pw_hash,
        auth_provider="local",
    )
    session.add(account)
    await session.flush()

    membership = OrgMembership(
        account_id=account.id,
        organisation_id=org.id,
        role="admin" if email in ("admin", "admin@modulo.run") else "runner",
    )
    session.add(membership)
    _log.info("startup.user_seeded", extra={"email": email})
    await _append_boot_user_audit(
        session,
        org_id=org.id,
        event_type="user_seeded",
        account_id=account.id,
        payload={
            "summary": f"User {email} seeded with role {membership.role}",
            "email": email,
            "role": membership.role,
        },
    )


async def rehash_existing_user(session: Any, org: Any, existing_account: Any, email: str, pw_hash: str) -> None:
    """Rehash an existing account's plaintext password and ensure its org membership."""
    from modulo.db.models.org_membership import OrgMembership

    existing_account.password_hash = pw_hash
    _log.info("startup.user_rehashed", extra={"email": email})

    # Ensure OrgMembership exists and role is correct
    mem_result = await session.execute(
        select(OrgMembership).where(
            OrgMembership.account_id == existing_account.id,
            OrgMembership.organisation_id == org.id,
        )
    )
    membership = mem_result.scalar_one_or_none()
    admin_role = "admin" if email in ("admin", "admin@modulo.run") else None
    if membership is not None:
        if admin_role and membership.role != "admin":
            membership.role = "admin"
            _log.info("startup.user_role_set_admin", extra={"email": email})
        else:
            _log.info("startup.user_exists", extra={"email": email})
        final_role = membership.role
    else:
        new_membership = OrgMembership(
            account_id=existing_account.id,
            organisation_id=org.id,
            role=admin_role or "runner",
        )
        session.add(new_membership)
        _log.info("startup.user_membership_created", extra={"email": email})
        final_role = new_membership.role

    # A rehash is also a privilege decision: the seeded password can ESCALATE
    # the account to ``admin`` (see ``admin_role`` above), so the credential
    # rotation and the role it landed on are recorded together (FAR-1561).
    await _append_boot_user_audit(
        session,
        org_id=org.id,
        event_type="user_rehashed",
        account_id=existing_account.id,
        payload={
            "summary": f"User {email} credential rehashed with role {final_role}",
            "email": email,
            "role": final_role,
            "role_granted": bool(admin_role),
        },
    )
