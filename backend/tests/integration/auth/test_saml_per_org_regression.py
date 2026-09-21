"""Multi-org SAML regression + legacy parity (FAR-1007).

Extends ``test_saml_rls_resolution.py`` (FAR-1058) — same harness, same
real-Postgres posture. Why integration (mocked sessions CANNOT catch this
defect class): per-provider SAML routing is only meaningful when the provider
rows live behind the REAL RLS policy (``modulo_app`` NOBYPASSRLS /
``modulo_system`` BYPASSRLS) and the HTTP routes resolve through it. A mocked
provider list can never prove that org B's ACS resolves org B's row, that
org A's SP entity is invisible to org B, or that the legacy singleton routes
still resolve the SAME first-enabled provider row.

Proven here (FAR-1001/FAR-1004 multi-org story):
1. Each org's ``/api/v1/auth/org-login/{slug}`` lists ONLY its own SAML
   provider, and the ``saml`` boolean is per-org.
2. ``/saml/{provider_id}/metadata`` emits each provider's OWN Entity ID and
   per-provider ACS Location.
3. An assertion audited for org A's SP entity is rejected at org B's
   per-provider ACS with 401 — audience containment through the URL path —
   with an affirmative control (org B-audited response ACCEPTED at org B's
   ACS), through the real python3-saml strict validation.
4. A signed RelayState minted for org A is rejected at org B's ACS
   (defense-in-depth HMAC containment, FAR-1003).
5. IdP-initiated (unsolicited) SSO with NO RelayState succeeds on the
   per-provider ACS.
6. Legacy parity: ``/saml/login`` (no RelayState), ``/saml/metadata``
   (instance-wide ACS URL, entity from the legacy first-enabled resolution),
   and ``/saml/acs`` round-trip behave exactly as the pre-per-provider
   singleton contract — existing single-tenant IdP configs untouched.

Signature honesty (Windows): xmlsec cannot produce sign/verify round-trips
locally, so ONLY the XML signature-validation step is mocked
(``OneLogin_Saml2_Utils.validate_sign`` + ``process_signed_elements``, the
same scoped patches the unit suite uses). Destination, conditions, audience
restriction and the hard-fail per-provider containment check all run for
real. The audience-reject and RelayState tests stop before JIT provisioning
so only the signature step needs mocking there; the two ACCEPT-path tests
additionally mock ``jit_provision_user`` and ``issue_sso_tokens`` (JIT
account creation + token minting against the real DB is a separate, already
unit-covered surface — and its absence keeps every assertion here strictly
provider-resolution-scoped).
"""

import base64
import os
import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from onelogin.saml2.response import OneLogin_Saml2_Response
from onelogin.saml2.utils import OneLogin_Saml2_Utils
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

pytestmark = pytest.mark.integration

_IDP_METADATA = (
    '<?xml version="1.0"?>'
    '<md:EntityDescriptor xmlns:md="urn:oasis:names:tc:SAML:2.0:metadata"'
    ' entityID="https://idp.example.com">'
    '  <md:IDPSSODescriptor protocolSupportEnumeration="urn:oasis:names:tc:SAML:2.0:protocol">'
    "    <md:SingleSignOnService"
    '     Binding="urn:oasis:names:tc:SAML:2.0:bindings:HTTP-Redirect"'
    '     Location="https://idp.example.com/sso"/>'
    "  </md:IDPSSODescriptor>"
    "</md:EntityDescriptor>"
)

_NS_SAML = "urn:oasis:names:tc:SAML:2.0:assertion"
_NS_SAMLP = "urn:oasis:names:tc:SAML:2.0:protocol"
_NS_STATUS = "urn:oasis:names:tc:SAML:2.0:status"


# ---------------------------------------------------------------------------
# Seed helpers (superuser engine — bypasses RLS for setup)
# ---------------------------------------------------------------------------


async def _create_org(engine: AsyncEngine, label: str, *, created_at_offset_seconds: int = 0) -> tuple[uuid.UUID, str]:
    """Create a login-active org; slug returned for URL use."""
    org_id = uuid.uuid4()
    slug = f"far1007-{label}-{org_id.hex[:8]}"
    created_at = datetime.now(UTC) - timedelta(seconds=created_at_offset_seconds)
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO organisations (id, name, slug, settings_json, created_at) "
                "VALUES (:id, :name, :slug, '{}'::json, :created_at)"
            ),
            {"id": str(org_id), "name": f"Org {label}", "slug": slug, "created_at": created_at},
        )
    return org_id, slug


async def _create_saml_provider(
    engine: AsyncEngine,
    *,
    org_id: uuid.UUID,
    created_at_offset_seconds: int = 0,
) -> str:
    """Create an ENABLED SAML provider; the globally unique slug is returned.

    ``created_at`` is explicit so the legacy first-enabled-wins resolution
    (``order_by(created_at) limit(1)``) is deterministic in these tests.
    """
    slug = f"saml-{uuid.uuid4().hex[:12]}"
    created_at = datetime.now(UTC) - timedelta(seconds=created_at_offset_seconds)
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO sso_providers "
                "(id, organisation_id, provider_type, name, provider_id, entity_id, "
                "metadata_xml, enabled, auto_provision, allowed_domains, "
                "default_role, group_mappings, preset, created_at) "
                "VALUES (:id, :oid, 'saml', :name, :pid, :eid, :meta, true, false, "
                "CAST('[]' AS json), 'runner', '[]'::json, 'custom', :created_at)"
            ),
            {
                "id": str(uuid.uuid4()),
                "oid": str(org_id),
                "name": f"SAML {slug}",
                "pid": slug,
                "eid": f"urn:modulo:sp:{org_id.hex[:8]}",
                "meta": _IDP_METADATA,
                "created_at": created_at,
            },
        )
    return slug


# Two committed orgs, each owning exactly ONE enabled SAML provider.
# Org A's PROVIDER is backdated a decade so it is reliably the legacy
# "first enabled" singleton for the legacy-parity tests. The ORGS are NOT
# backdated: the integration suite runs against one shared Postgres, and
# ``_set_default_rls_org`` (``Organisation.created_at`` asc, limit 1) is a
# global resource claimed by ``test_saml_rls_resolution``'s first-org fallback
# test. Backdating these orgs too would steal that first-org slot and break the
# older test (observed on main run 35537837965).
@pytest_asyncio.fixture(scope="module")
async def two_saml_orgs(db_engine: AsyncEngine) -> dict[str, str]:
    org_a_id, org_a_slug = await _create_org(db_engine, "a")
    org_b_id, org_b_slug = await _create_org(db_engine, "b")
    saml_a = await _create_saml_provider(db_engine, org_id=org_a_id, created_at_offset_seconds=315_360_000)
    saml_b = await _create_saml_provider(db_engine, org_id=org_b_id, created_at_offset_seconds=0)
    return {
        "org_a_id": str(org_a_id),
        "org_a_slug": org_a_slug,
        "org_b_id": str(org_b_id),
        "org_b_slug": org_b_slug,
        "saml_a": saml_a,
        "saml_b": saml_b,
        "entity_a": f"urn:modulo:sp:{org_a_id.hex[:8]}",
        "entity_b": f"urn:modulo:sp:{org_b_id.hex[:8]}",
    }


# ---------------------------------------------------------------------------
# Real-role session fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def system_engine() -> AsyncGenerator[AsyncEngine, None]:
    """Engine on the real ``modulo_system`` role (BYPASSRLS), like production."""
    engine = create_async_engine(
        os.environ["MODULO_SYSTEM_DATABASE_URL"],
        echo=False,
        poolclass=NullPool,
    )
    yield engine
    await engine.dispose()


@pytest.fixture
def system_session(system_engine: AsyncEngine) -> AsyncSession:
    """Session on the ``modulo_system`` role — mirrors ``get_system_db_session``."""
    return AsyncSession(bind=system_engine, autobegin=False, expire_on_commit=False)


class _AllFeatures:
    def feature_enabled(self, name: str) -> bool:
        return True

    def list_enabled_features(self) -> list:
        return []

    def tier(self) -> str:
        return "enterprise"

    def has_license_key(self) -> bool:
        return True


_PUBLIC_URL = "https://staging.example.com"


@pytest_asyncio.fixture
async def multi_org_client(db_url: str, app_engine: AsyncEngine, system_session: AsyncSession) -> AsyncClient:
    """ASGI client (mirrors ``test_saml_rls_resolution.multi_org_client``):
    - ``modulo_multi_org_enabled=True`` (org-login routes otherwise 404 per ADR 005),
    - a REAL system-session override (exercises the modulo_system leg),
    - an anonymous plan-context override (pre-auth feature gate)."""
    from modulo.api.dependencies import _get_engine, get_anonymous_plan_context, get_db_session, get_system_db_session
    from modulo.api.main import app
    from modulo.settings import Settings, get_settings

    settings = Settings(
        database_url=db_url,
        secret_key="a" * 32,
        fernet_key="a" * 32,
        modulo_csrf_enabled=False,
        modulo_auth_rate_limit_enabled=False,
        modulo_multi_org_enabled=True,
        modulo_public_url=_PUBLIC_URL,
        redis_url="",
        modulo_admin_password="",
    )

    async def override_session() -> AsyncGenerator[AsyncSession, None]:
        factory = async_sessionmaker(app_engine, expire_on_commit=False)
        async with factory() as session:
            yield session

    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[_get_engine] = lambda: app_engine
    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[get_system_db_session] = lambda: system_session
    app.dependency_overrides[get_anonymous_plan_context] = lambda: _AllFeatures()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test", timeout=30.0) as client:
        yield client
    app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# SAML Response builder (XSD-compliant, fresh timestamps) — same shape the
# unit suite's ``_build_xsd_compliant_saml_response`` produces; duplicated
# here because unit-test helpers are private and not import-stable.
# ---------------------------------------------------------------------------


def _build_saml_response(
    *, audience: str, destination: str, recipient: str, name_id_value: str = "multiorg@example.com"
) -> str:
    from lxml import etree as lxml_etree

    s = f"{{{_NS_SAML}}}"
    n = f"{{{_NS_SAMLP}}}"
    now = datetime.now(UTC)
    instant = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    nooa = (now + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    nb = (now - timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ")

    dummy = lxml_etree.Element("root")
    r = lxml_etree.SubElement(dummy, f"{n}Response")
    r.set("ID", "_resp_multiorg")
    r.set("Version", "2.0")
    r.set("IssueInstant", instant)
    r.set("Destination", destination)
    st = lxml_etree.SubElement(r, f"{n}Status")
    sc = lxml_etree.SubElement(st, f"{n}StatusCode")
    sc.set("Value", f"{_NS_STATUS}:Success")
    a = lxml_etree.SubElement(r, f"{s}Assertion")
    a.set("ID", "_assert_multiorg")
    a.set("Version", "2.0")
    a.set("IssueInstant", instant)
    lxml_etree.SubElement(a, f"{s}Issuer").text = "https://idp.example.com"
    subj = lxml_etree.SubElement(a, f"{s}Subject")
    nid = lxml_etree.SubElement(subj, f"{s}NameID")
    nid.text = name_id_value
    nid.set("Format", "urn:oasis:names:tc:SAML:1.1:nameid-format:emailAddress")
    sc2 = lxml_etree.SubElement(subj, f"{s}SubjectConfirmation")
    sc2.set("Method", "urn:oasis:names:tc:SAML:2.0:cm:bearer")
    scd = lxml_etree.SubElement(sc2, f"{s}SubjectConfirmationData")
    scd.set("NotOnOrAfter", nooa)
    scd.set("Recipient", recipient)
    conds = lxml_etree.SubElement(a, f"{s}Conditions")
    conds.set("NotBefore", nb)
    conds.set("NotOnOrAfter", nooa)
    ar = lxml_etree.SubElement(conds, f"{s}AudienceRestriction")
    lxml_etree.SubElement(ar, f"{s}Audience").text = audience
    asn = lxml_etree.SubElement(a, f"{s}AuthnStatement")
    asn.set("AuthnInstant", instant)
    asn.set("SessionIndex", "_s1")
    cx = lxml_etree.SubElement(asn, f"{s}AuthnContext")
    cl = lxml_etree.SubElement(cx, f"{s}AuthnContextClassRef")
    cl.text = "urn:oasis:names:tc:SAML:2.0:ac:classes:PasswordProtectedTransport"
    astmt = lxml_etree.SubElement(a, f"{s}AttributeStatement")
    at = lxml_etree.SubElement(astmt, f"{s}Attribute")
    at.set("Name", "email")
    av = lxml_etree.SubElement(at, f"{s}AttributeValue")
    av.text = name_id_value
    atm = lxml_etree.SubElement(astmt, f"{s}Attribute")
    atm.set("Name", "displayName")
    avm = lxml_etree.SubElement(atm, f"{s}AttributeValue")
    avm.text = "Multi Org User"
    dummy.remove(r)
    return lxml_etree.tostring(r, xml_declaration=True, encoding="UTF-8").decode()


def _b64(xml: str) -> str:
    return base64.b64encode(xml.encode()).decode()


def _acs(slug: str) -> str:
    return f"{_PUBLIC_URL}/api/v1/auth/saml/acs/{slug}"


_LEGACY_ACS = f"{_PUBLIC_URL}/api/v1/auth/saml/acs"


# Signature-step-only mocks; xmlsec cannot sign/verify round-trip on Windows.
# Applied as `with _sig1(), _sig2():` — tuple unpacking in `with` is invalid.
def _sig_validate_sign() -> object:
    return patch.object(OneLogin_Saml2_Utils, "validate_sign", return_value=True)


def _sig_signed_elements() -> object:
    return patch.object(
        OneLogin_Saml2_Response,
        "process_signed_elements",
        return_value=["{urn:oasis:names:tc:SAML:2.0:protocol}Response"],
    )


def _accept_mocks() -> tuple[AsyncMock, AsyncMock]:
    """Fresh JIT/token mocks in the same order as ``_ACCEPT_PATCHES``."""
    jit = AsyncMock()
    jit.return_value = (MagicMock(), uuid.uuid4(), "runner")
    tokens = AsyncMock()
    tokens.return_value = {"access_token": "at-multiorg", "refresh_token": "rt-multiorg", "token_type": "bearer"}
    return jit, tokens


# ---------------------------------------------------------------------------
# 1. Org-login provider lists are per-org
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_org_login_lists_only_own_saml_provider_and_flag_is_per_org(
    multi_org_client: AsyncClient,
    two_saml_orgs: dict[str, str],
) -> None:
    """Each org's org-login response surfaces ONLY its own SAML provider and
    its own ``saml`` boolean — through the real app engine (RLS), the
    system-initiated resolution and the per-provider slugs. Both slugs exist
    instance-wide, so any org contamination leaks visibly as an extra entry."""
    resp_a = await multi_org_client.get(f"/api/v1/auth/org-login/{two_saml_orgs['org_a_slug']}")
    assert resp_a.status_code == 200, resp_a.text
    body_a = resp_a.json()
    assert body_a["org"]["slug"] == two_saml_orgs["org_a_slug"]
    assert body_a["saml"] is True
    assert [p["provider_id"] for p in body_a["providers"]] == [two_saml_orgs["saml_a"]]
    assert all(p["type"] == "saml" for p in body_a["providers"])

    resp_b = await multi_org_client.get(f"/api/v1/auth/org-login/{two_saml_orgs['org_b_slug']}")
    assert resp_b.status_code == 200, resp_b.text
    body_b = resp_b.json()
    assert body_b["saml"] is True
    assert [p["provider_id"] for p in body_b["providers"]] == [two_saml_orgs["saml_b"]]
    assert two_saml_orgs["saml_a"] not in [p["provider_id"] for p in body_b["providers"]]


# ---------------------------------------------------------------------------
# 2. Per-provider metadata emits each provider's OWN Entity ID + ACS
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_per_provider_metadata_emits_each_orgs_own_entity_and_acs(
    multi_org_client: AsyncClient,
    two_saml_orgs: dict[str, str],
) -> None:
    """/saml/{provider_id}/metadata for EACH org's provider emits that
    provider's own Entity ID and its own per-provider ACS Location —
    cross-checking that neither provider's identifiers appear in the
    other's document."""
    resp_a = await multi_org_client.get(f"/api/v1/auth/saml/{two_saml_orgs['saml_a']}/metadata")
    assert resp_a.status_code == 200, resp_a.text
    body_a = resp_a.text
    assert f'entityID="{two_saml_orgs["entity_a"]}"' in body_a
    assert f"/api/v1/auth/saml/acs/{two_saml_orgs['saml_a']}" in body_a
    assert two_saml_orgs["saml_b"] not in body_a
    assert two_saml_orgs["entity_b"] not in body_a

    resp_b = await multi_org_client.get(f"/api/v1/auth/saml/{two_saml_orgs['saml_b']}/metadata")
    assert resp_b.status_code == 200, resp_b.text
    body_b = resp_b.text
    assert f'entityID="{two_saml_orgs["entity_b"]}"' in body_b
    assert f"/api/v1/auth/saml/acs/{two_saml_orgs['saml_b']}" in body_b
    assert two_saml_orgs["saml_a"] not in body_b
    assert two_saml_orgs["entity_a"] not in body_b


# ---------------------------------------------------------------------------
# 3. Audience containment: org A's assertion rejected at org B's ACS
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_org_a_assertion_rejected_at_org_b_acs(
    multi_org_client: AsyncClient,
    two_saml_orgs: dict[str, str],
) -> None:
    """A successful assertion audited for org A's SP entity, posted to org B's
    per-provider ACS, is rejected 401 — URL-path routing alone is NOT the
    containment; the resolved org B entity agreement is. The provider row is
    resolved from the REAL DB via the system leg; only the signature step is
    mocked (Windows xmlsec), so strict Destination/conditions/audience run."""
    xml = _build_saml_response(
        audience=two_saml_orgs["entity_a"],
        destination=_acs(two_saml_orgs["saml_b"]),
        recipient=_acs(two_saml_orgs["saml_b"]),
    )
    with _sig_validate_sign(), _sig_signed_elements():
        resp = await multi_org_client.post(
            f"/api/v1/auth/saml/acs/{two_saml_orgs['saml_b']}",
            data={"SAMLResponse": _b64(xml)},
            follow_redirects=False,
        )
    assert resp.status_code == 401, f"expected 401, got {resp.status_code}: {resp.text}"
    assert resp.json()["detail"].startswith("SAML response validation failed")


@pytest.mark.asyncio
async def test_org_b_assertion_accepted_at_org_b_acs_control(
    multi_org_client: AsyncClient,
    two_saml_orgs: dict[str, str],
) -> None:
    """Affirmative control: a correctly-audited response passes org B's ACS.
    Proves the cross-org rejection above is audience-driven, not
    fixture/state-driven. Signature + JIT + token minting mocked (Windows
    xmlsec round-trip impossible; JIT/tokens are separately unit-covered)."""
    xml = _build_saml_response(
        audience=two_saml_orgs["entity_b"],
        destination=_acs(two_saml_orgs["saml_b"]),
        recipient=_acs(two_saml_orgs["saml_b"]),
    )
    jit, tokens = _accept_mocks()
    with (
        _sig_validate_sign(),
        _sig_signed_elements(),
        patch("modulo.auth.sso.jit_provision_user", jit),
        patch("modulo.auth.sso.issue_sso_tokens", tokens),
    ):
        resp = await multi_org_client.post(
            f"/api/v1/auth/saml/acs/{two_saml_orgs['saml_b']}",
            data={"SAMLResponse": _b64(xml)},
            follow_redirects=False,
        )
    assert resp.status_code == 307, resp.text
    assert "access_token=at-multiorg" in resp.headers["location"]


# ---------------------------------------------------------------------------
# 4. RelayState defense-in-depth: A-signed token rejected at B's ACS
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_relay_state_signed_for_org_a_rejected_at_org_b_acs(
    multi_org_client: AsyncClient,
    two_saml_orgs: dict[str, str],
) -> None:
    """A signed RelayState minted for provider A is rejected at B's ACS
    (FAR-1003 HMAC defense-in-depth) BEFORE the response is processed."""
    from modulo.auth.sso import sign_saml_relay_state

    relay = sign_saml_relay_state(two_saml_orgs["saml_a"], "a" * 32)
    xml = _build_saml_response(
        audience=two_saml_orgs["entity_b"],
        destination=_acs(two_saml_orgs["saml_b"]),
        recipient=_acs(two_saml_orgs["saml_b"]),
    )
    with _sig_validate_sign(), _sig_signed_elements():
        resp = await multi_org_client.post(
            f"/api/v1/auth/saml/acs/{two_saml_orgs['saml_b']}",
            data={"SAMLResponse": _b64(xml), "RelayState": relay},
            follow_redirects=False,
        )
    assert resp.status_code == 401
    assert "does not match" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# 5. IdP-initiated (unsolicited) SSO with NO RelayState
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_idp_initiated_sso_without_relay_state_on_per_provider_acs(
    multi_org_client: AsyncClient,
    two_saml_orgs: dict[str, str],
) -> None:
    """Unsolicited SSO with NO RelayState succeeds on the per-provider ACS —
    route resolution is by URL path, RelayState absence must NOT fail
    (FAR-1003). Signature + JIT + tokens mocked (see module docstring)."""
    xml = _build_saml_response(
        audience=two_saml_orgs["entity_b"],
        destination=_acs(two_saml_orgs["saml_b"]),
        recipient=_acs(two_saml_orgs["saml_b"]),
    )
    jit, tokens = _accept_mocks()
    with (
        _sig_validate_sign(),
        _sig_signed_elements(),
        patch("modulo.auth.sso.jit_provision_user", jit),
        patch("modulo.auth.sso.issue_sso_tokens", tokens),
    ):
        resp = await multi_org_client.post(
            f"/api/v1/auth/saml/acs/{two_saml_orgs['saml_b']}",
            data={"SAMLResponse": _b64(xml)},
            follow_redirects=False,
        )
    assert resp.status_code == 307, resp.text
    assert "access_token=at-multiorg" in resp.headers["location"]


# ---------------------------------------------------------------------------
# 6. Legacy singleton parity (single-tenant IdP configs untouched)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_legacy_login_redirects_without_signed_relay_state(multi_org_client: AsyncClient) -> None:
    """GET /saml/login (no provider segment) redirects to the IdP with an
    AuthnRequest for the instance-wide ACS. The legacy route never MINTS the
    signed per-provider RelayState token: python3-saml's RelayState here is
    the SP self URL (the legacy ACS) it derives automatically — NOT one of
    Modulo's opaque signed tokens (which are base64url JSON+HMAC)."""
    resp = await multi_org_client.get("/api/v1/auth/saml/login", follow_redirects=False)
    assert resp.status_code == 307, resp.text
    location = resp.headers["location"]
    assert "idp.example.com" in location
    assert "SAMLRequest" in location
    # The RelayState is python3-saml's self-derived ACS URL, not a signed token.
    from urllib.parse import parse_qs, urlsplit

    query = parse_qs(urlsplit(location).query)
    relay = query.get("RelayState", [""])[0]
    assert relay == "https://staging.example.com/api/v1/auth/saml/acs"


@pytest.mark.asyncio
async def test_legacy_metadata_serves_instance_wide_acs_and_first_enabled_entity(
    multi_org_client: AsyncClient,
    system_session: AsyncSession,
    two_saml_orgs: dict[str, str],
) -> None:
    """/saml/metadata (no provider segment) keeps the legacy singleton shape:
    the ACS Location is the legacy instance-wide ``/saml/acs`` URL (NO
    per-provider segment), and the Entity ID is the legacy first-enabled
    resolution contract (``get_enabled_saml_provider`` — created_at asc,
    limit 1), not a per-provider lookup."""
    from modulo.db.crud.sso_provider import get_enabled_saml_provider

    async with system_session.begin():
        legacy_provider = await get_enabled_saml_provider(system_session)
    assert legacy_provider is not None
    expected_entity = legacy_provider.entity_id

    resp = await multi_org_client.get("/api/v1/auth/saml/metadata")
    assert resp.status_code == 200, resp.text
    body = resp.text
    assert f'entityID="{expected_entity}"' in body
    assert 'Location="https://staging.example.com/api/v1/auth/saml/acs"' in body
    # The legacy document must NOT invert into a per-provider one.
    assert "/api/v1/auth/saml/acs/" not in body  # per-provider ACS segment
    assert two_saml_orgs["saml_a"] not in body
    assert two_saml_orgs["saml_b"] not in body


@pytest.mark.asyncio
async def test_legacy_acs_round_trip_with_legacy_acs_destination(
    multi_org_client: AsyncClient,
    system_session: AsyncSession,
) -> None:
    """POST /saml/acs (no provider segment) round-trips exactly as the legacy
    singleton path did: an assertion audited for the first-enabled provider's
    legacy entity with the instance-wide ACS destination processes and issues
    tokens. No RelayState is supplied or required — the legacy route never
    had RelayState handling. Signature + JIT + tokens mocked (module doc)."""
    from modulo.db.crud.sso_provider import get_enabled_saml_provider

    async with system_session.begin():
        legacy_provider = await get_enabled_saml_provider(system_session)
    assert legacy_provider is not None
    xml = _build_saml_response(
        audience=legacy_provider.entity_id,
        destination=_LEGACY_ACS,
        recipient=_LEGACY_ACS,
    )
    jit, tokens = _accept_mocks()
    with (
        _sig_validate_sign(),
        _sig_signed_elements(),
        patch("modulo.auth.sso.jit_provision_user", jit),
        patch("modulo.auth.sso.issue_sso_tokens", tokens),
    ):
        resp = await multi_org_client.post(
            "/api/v1/auth/saml/acs",
            data={"SAMLResponse": _b64(xml)},
            follow_redirects=False,
        )
    assert resp.status_code == 307, resp.text
    assert "access_token=at-multiorg" in resp.headers["location"]
