"""Unit tests for the demo-org seed framework (FAR-450).

These exercise the seeding logic and per-org signed-license wiring without a
live database: a tiny fake async session records added entities and answers the
existence queries seed_demo_org issues, while the real license signing/verification
helpers (encode_license_key / generate_team_license / parse_and_verify) run
end-to-end with a generated Ed25519 test keypair.

resolve_plan_context is exercised against the real license on the org by
stubbing the DB-backed tier-catalog lookup (FeatureFlagRegistry.from_db) — the
org license path is exactly what FAR-450 is about.
"""

from __future__ import annotations

import operator
import types
from collections.abc import Generator
from typing import Self

import pytest

from modulo.core.license import (
    _LICENSE_PUBLIC_KEY_HEX,
    parse_and_verify,
    set_public_key,
)
from modulo.core.license_signing import LicenseSigningError
from modulo.core.registry.crypto import generate_keypair
from modulo.core.seed_data import demo_data as demo_mod
from modulo.core.seed_data.demo_data import DEMO_ORGS, seed_demo_org
from modulo.db.models.account import Account
from modulo.db.models.org_membership import OrgMembership
from modulo.db.models.organisation import Organisation

_KP = generate_keypair()
_TEST_PRIV = _KP["private_key"]
_TEST_PUB = _KP["public_key"]
set_public_key(_TEST_PUB)


@pytest.fixture(autouse=True)
def _patch_get_settings(monkeypatch) -> None:
    # seed_demo_org now resolves the signing key via get_settings(); point it at
    # the test keypair so the real signing helpers run end-to-end.
    monkeypatch.setattr(demo_mod, "get_settings", lambda: _settings())


@pytest.fixture(autouse=True)
def _use_test_license_key() -> Generator[None, None, None]:
    # The license public key is global module state in modulo.core.license and is
    # clobbered by other test modules' autouse fixtures (e.g. stripe fulfilment
    # resets it to the builtin dev key). Pin it per-test so this module's signed
    # licenses always verify against the matching test public key, regardless of
    # collection order.
    set_public_key(_TEST_PUB)
    yield
    set_public_key(_LICENSE_PUBLIC_KEY_HEX)


# ---------------------------------------------------------------------------
# Fake async session
# ---------------------------------------------------------------------------
class _FakeResult:
    def __init__(self, rows: list) -> None:
        self._rows = rows

    def scalar_one_or_none(self):
        return self._rows[0] if self._rows else None

    def scalars(self):
        return self

    def all(self):
        return self._rows

    def first(self):
        return self._rows[0] if self._rows else None


def _col_name(left) -> str | None:
    for attr in ("key", "name"):
        val = getattr(left, attr, None)
        if val:
            return val
    return None


def _extract_predicates(clause) -> list[tuple[str, object]]:
    preds: list[tuple[str, object]] = []
    if clause is None:
        return preds
    if hasattr(clause, "clauses"):  # and_ / BooleanClauseList
        for sub in clause.clauses:
            preds.extend(_extract_predicates(sub))
        return preds
    if getattr(clause, "operator", None) is operator.eq:
        name = _col_name(clause.left)
        val = getattr(clause.right, "value", None)
        if name is not None:
            preds.append((name, val))
    return preds


class _FakeSavepoint:
    """No-op stand-in for an AsyncSessionTransaction savepoint."""

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc) -> bool:
        return False


class FakeAsyncSession:
    """Minimal in-memory async session for seeding logic."""

    def __init__(self) -> None:
        self._store: dict[type, list] = {}
        self.added: list = []

    def add(self, obj) -> None:
        self.added.append(obj)
        self._store.setdefault(type(obj), []).append(obj)

    async def execute(self, stmt):
        entity = stmt.column_descriptions[0]["entity"]
        preds = _extract_predicates(stmt.whereclause)
        matches = [
            obj for obj in self._store.get(entity, []) if all(getattr(obj, name, None) == val for (name, val) in preds)
        ]
        return _FakeResult(matches)

    async def flush(self) -> None:
        pass

    def begin_nested(self) -> _FakeSavepoint:
        return _FakeSavepoint()

    async def commit(self) -> None:
        pass

    async def rollback(self) -> None:
        pass


def _settings(private_key: str = _TEST_PRIV):
    return types.SimpleNamespace(
        modulo_license_private_key=private_key,
        modulo_license_key="",
    )


def _count(store: dict, cls: type) -> int:
    return len(store.get(cls, []))


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------
async def test_seed_demo_org_community_creates_entities_and_license() -> None:
    session = FakeAsyncSession()
    await seed_demo_org(
        session,
        slug="demo-community",
        tier="community",
        full=False,
        admin_email="admin@demo.example",
        admin_password="secret123",
    )

    assert _count(session._store, Organisation) == 1
    assert _count(session._store, Account) == 1
    assert _count(session._store, OrgMembership) == 1

    org = session._store[Organisation][0]
    account = session._store[Account][0]
    membership = session._store[OrgMembership][0]

    assert org.slug == "demo-community"
    assert account.email == "admin@demo.example"
    assert account.password_hash.startswith("$2")  # bcrypt hash, not plaintext
    assert account.auth_provider == "local"
    assert membership.account_id == account.id
    assert membership.organisation_id == org.id
    assert membership.role == "admin"

    license_key = org.settings_json["license_key"]
    validation = parse_and_verify(license_key)
    assert validation.valid
    assert validation.license_data.tier == "community"
    assert org.settings_json["demo"]["full"] is False


async def test_seed_demo_org_team_license() -> None:
    session = FakeAsyncSession()
    await seed_demo_org(
        session,
        slug="demo-team",
        tier="team",
        full=True,
        admin_email="admin@team.example",
        admin_password="secret123",
    )

    org = session._store[Organisation][0]
    license_key = org.settings_json["license_key"]
    validation = parse_and_verify(license_key)
    assert validation.valid
    assert validation.license_data.tier == "team"
    # Team license grants the team feature set.
    assert "team_rbac" in validation.license_data.features
    assert org.settings_json["demo"]["full"] is True


async def test_seed_demo_org_idempotent() -> None:
    session = FakeAsyncSession()
    kwargs = {
        "slug": "demo-idem",
        "tier": "community",
        "full": False,
        "admin_email": "idem@demo.example",
        "admin_password": "secret123",
    }
    await seed_demo_org(session, **kwargs)
    org_id_after_first = session._store[Organisation][0].id
    await seed_demo_org(session, **kwargs)

    # No duplicates after a second call.
    assert _count(session._store, Organisation) == 1
    assert _count(session._store, Account) == 1
    assert _count(session._store, OrgMembership) == 1
    # The same org object is reused (settings update path, not a new row).
    assert session._store[Organisation][0].id == org_id_after_first


async def test_seed_demo_org_requires_private_key_for_team(monkeypatch) -> None:
    # Override the autouse fixture so the signing key is empty -> must fail closed.
    monkeypatch.setattr(demo_mod, "get_settings", lambda: _settings(private_key=""))
    session = FakeAsyncSession()
    with pytest.raises(LicenseSigningError):
        await seed_demo_org(
            session,
            slug="demo-np",
            tier="team",
            full=False,
            admin_email="np@demo.example",
            admin_password="secret123",
        )


async def test_resolve_plan_context_returns_per_org_tier(monkeypatch) -> None:
    from modulo.core.feature_flags import DbPlanContext, FeatureFlagRegistry, resolve_plan_context

    # Stub the DB tier-catalog lookup — we only care that the ORG license's
    # tier flows through resolve_plan_context unchanged.
    async def _fake_from_db(cls, session, plan_id, has_license_key=False, license_features=None):
        return DbPlanContext(FeatureFlagRegistry(current_tier=plan_id, has_license_key=has_license_key))

    monkeypatch.setattr(
        "modulo.core.feature_flags.DbPlanContext.from_db",
        classmethod(_fake_from_db),
    )

    for tier in ("community", "team"):
        session = FakeAsyncSession()
        await seed_demo_org(
            session,
            slug=f"rpc-{tier}",
            tier=tier,
            full=False,
            admin_email=f"rpc-{tier}@demo.example",
            admin_password="secret123",
        )
        org = session._store[Organisation][0]
        pc = await resolve_plan_context(_settings(), session, org=org)
        assert pc.tier() == tier
        assert pc.has_license_key() is True


def test_seed_demo_orgs_empty_by_default() -> None:
    # FAR-450 foundation: DEMO_ORGS ships empty so nothing seeds until a
    # follow-up ticket populates it.
    assert DEMO_ORGS == []


async def test_seed_demo_org_email_collision_refuses_cross_tenant() -> None:
    session = FakeAsyncSession()
    # A pre-existing account shares the demo email but is NOT an admin member of
    # this org. Seeding must refuse to attach it (cross-tenant escalation guard)
    # and raise, leaving no demo entities committed.
    preexisting = Account(email="admin@demo.example", display_name="real-admin")
    session.add(preexisting)

    with pytest.raises(ValueError):
        await seed_demo_org(
            session,
            slug="collide",
            tier="community",
            full=False,
            admin_email="admin@demo.example",
            admin_password="secret123",
        )

    # The pre-existing account is NOT attached to the demo org: no membership
    # links it, and no second account is created. (The in-memory fake session
    # retains the uncommitted org row, but a real DB rolls the aborted
    # transaction back — the cross-tenant guard is the membership absence.)
    assert _count(session._store, Account) == 1  # only the pre-existing one
    assert _count(session._store, OrgMembership) == 0
    # And the pre-existing account is unchanged (not re-hashed / not a member).
    assert session._store[Account][0] is preexisting


async def test_seed_demo_org_email_reuse_is_idempotent() -> None:
    # Re-running on an org whose admin account we already created (so it already
    # exists AND is an admin member) must NOT raise — idempotency is preserved.
    session = FakeAsyncSession()
    kwargs = {
        "slug": "idem-email",
        "tier": "community",
        "full": False,
        "admin_email": "idem-email@demo.example",
        "admin_password": "secret123",
    }
    await seed_demo_org(session, **kwargs)
    await seed_demo_org(session, **kwargs)
    assert _count(session._store, Organisation) == 1
    assert _count(session._store, Account) == 1
    assert _count(session._store, OrgMembership) == 1
