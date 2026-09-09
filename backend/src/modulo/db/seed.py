"""System seeding: system schemas (org creation) + MODULO_USERS boot seeding.

The MODULO_USERS seeder was promoted from ``modulo.api.main`` (FAR-671 /
ADR 031 Decision 2) so the native launcher and the container boot share one
implementation. The DB layer stays free of api imports (import-linter
``db-does-not-import-core``): the ORCHESTRATOR takes an already-built session
factory — the caller (API boot wrapper, native launcher) resolves the
engine/factory from its own layer. The single-entry and rehash seeders are
pure DB logic (lazy model/password imports, same pattern the API version
used).
"""

import logging
import uuid
from collections.abc import Callable
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.db.models.schema import Schema, SchemaVersion

_log = logging.getLogger(__name__)

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
    else:
        new_membership = OrgMembership(
            account_id=existing_account.id,
            organisation_id=org.id,
            role=admin_role or "runner",
        )
        session.add(new_membership)
        _log.info("startup.user_membership_created", extra={"email": email})
