"""Real Keycloak testcontainers harness for the auth coverage tier (FAR-1126).

Replaces the httpx-stub IdP suites with a real Keycloak container, real
browser-form drive, and real HTTP redirects — no stub IdP on the wire.

Every fixture step encoded here was verified against a live Keycloak 26.1.0
container during the FAR-1126 spike (realm boot, admin REST bootstrap, OIDC
browser drive end-to-end, SAML browser drive + a python3-saml round-trip
through the REAL ``ModuloSamlAuth.process_response`` under xmlsec).

Key verified facts baked into this code:

- Keycloak 26.1.0 accepts the command line::

    start-dev --hostname-strict=false \\
        --https-certificate-file=/certs/server.pem \\
        --https-certificate-key-file=/certs/server-key.pem

  where ``server.pem`` is the LEAF + issuer-CA chain concatenated (the
  HTTPS connector needs the full chain, not the bare leaf).
- ``--hostname-strict=false`` is required so the discovered issuer echoes
  whatever ``Host`` the harness sends — the test host:port round-trip
  converges (the issuer echoes the port it was reached on).
- The admin token is a password grant against ``admin-cli`` on the
  ``master`` realm, using the ``KC_BOOTSTRAP_ADMIN_*`` env credentials.
- A SAML client MUST have the ``role_list`` client scope REMOVED (default +
  optional) and get user-property mappers for ``email``/``firstName``/
  ``lastName`` instead — Keycloak 26.1.0 emits one ``<Attribute
  Name="Role">`` per realm role which python3-saml refuses with
  "Found an Attribute element with duplicated Name".
- ``WantAuthnRequestsSigned`` in the IdP descriptor is signalled but NOT
  enforced — a redirect-bound unsigned AuthnRequest from Modulo is
  accepted and the assertion comes back signed. No SP key is needed.
"""

from __future__ import annotations

import ipaddress
import re
import ssl
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urljoin, urlsplit

import httpx
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from tests.helpers.testcontainers_harness import (
    ContainerHandle,
    ContainerSpec,
    Tier1bFixtureError,
    start_tier1b_container,
)

KEYCLOAK_IMAGE = "quay.io/keycloak/keycloak:26.1.0"

# The fake Modulo bases the REAL under-test code path derives its
# redirect/entity/ACS URLs from. Defined ONCE here so the conftest's realm
# bootstrap and every test module's settings agree by construction (a drift
# between the two would hand Keycloak a redirect_uri the app never presents).
HARNESS_PUBLIC_BASE = "https://api.example.com"
HARNESS_FRONTEND_BASE = "https://spa.example.com"

# In-container paths the Keycloak HTTPS connector reads from.
_KC_CERT_FILE = "/certs/server.pem"
_KC_CERT_KEY_FILE = "/certs/server-key.pem"

# Bootstrap-admin credentials (mapped to the image's KC_BOOTSTRAP_ADMIN_* env).
KC_ADMIN_USERNAME = "kcadmin"
KC_ADMIN_PASSWORD = "kc-harness-admin-pw-1"

# The Modulo-side user under test: a real, browser-driveable user in the
# IdP realm whose email lives on a fixed allowlist-friendly domain.
KC_USER_EMAIL_DOMAIN = "idp-test.local"
KC_USER_PASSWORD = "kc-harness-user-pw-1"


@dataclass
class KeycloakOidcTenant:
    """One OIDC realm + client + user, built by :func:`bootstrap_oidc_realm`."""

    realm: str
    issuer: str
    discovery_url: str
    provider_id: str
    client_id: str
    client_secret: str
    redirect_uri: str
    user_email: str
    user_password: str


@dataclass
class KeycloakSamlTenant:
    """One SAML realm + client + user, built by :func:`bootstrap_saml_realm`."""

    realm: str
    idp_entity_id: str
    descriptor_url: str
    provider_id: str
    idp_base: str
    sp_entity_id: str
    acs_url: str
    user_email: str
    user_password: str


# ---------------------------------------------------------------------------
# TLS cert generation (CA + localhost leaf, Keycloak's HTTPS mount)
# ---------------------------------------------------------------------------


def generate_tls_certs(cert_dir: Path) -> Path:
    """Write a self-signed CA + localhost leaf suitable for Keycloak's HTTPS.

    ``server.pem`` is leaf + root concatenated — the format the HTTPS
    connector expects (verified live; a bare leaf without the CA makes the
    connector refuse the connection).
    """
    cert_dir.mkdir(parents=True, exist_ok=True)
    root_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    root_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "kc-test-ca")])
    root_cert = (
        x509.CertificateBuilder()
        .subject_name(root_name)
        .issuer_name(root_name)
        .public_key(root_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.now(UTC) - timedelta(days=1))
        .not_valid_after(datetime.now(UTC) + timedelta(days=365))
        .add_extension(x509.BasicConstraints(ca=True, path_length=1), critical=True)
        .sign(root_key, hashes.SHA256())
    )
    leaf_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    leaf_cert = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")]))
        .issuer_name(root_cert.subject)
        .public_key(leaf_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.now(UTC) - timedelta(days=1))
        .not_valid_after(datetime.now(UTC) + timedelta(days=365))
        .add_extension(
            x509.SubjectAlternativeName(
                [
                    x509.DNSName("localhost"),
                    x509.DNSName("*.localhost"),
                    x509.IPAddress(ipaddress.ip_address("127.0.0.1")),
                ]
            ),
            critical=False,
        )
        .sign(root_key, hashes.SHA256())
    )
    leaf_pem = leaf_cert.public_bytes(serialization.Encoding.PEM)
    root_pem = root_cert.public_bytes(serialization.Encoding.PEM)
    (cert_dir / "server.pem").write_bytes(leaf_pem + root_pem)
    key_pem = leaf_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    (cert_dir / "server-key.pem").write_bytes(key_pem)
    (cert_dir / "ca.pem").write_bytes(root_pem)
    # This dir is bind-mounted at /certs and read by Keycloak, which runs as
    # UID 1000 inside the container. A bind mount preserves the host inode's
    # ownership AND mode, and pytest's tmp_path_factory.mktemp() creates the
    # dir mode 0700 owned by the CI runner (UID 1001 on GitHub Actions). UID
    # 1000 cannot traverse a 0700 dir it does not own, so it cannot open
    # server.pem / server-key.pem, Keycloak exits 2 with "AccessDeniedException:
    # https-...", and the readiness probe sees ConnectError until it times out
    # (observed on PR #1703). Make the dir world-traversable and every file
    # world-readable; these are throwaway, per-run, self-signed files in a temp
    # dir. This is a FILE- and DIR-ownership issue, not a mount mode.
    (cert_dir / "server.pem").chmod(0o644)
    (cert_dir / "server-key.pem").chmod(0o644)
    (cert_dir / "ca.pem").chmod(0o644)
    cert_dir.chmod(0o755)
    return cert_dir


def keycloak_ssl_context(ca_file: str | Path) -> ssl.SSLContext:
    """A real TLS-verifying context that trusts the harness CA."""
    return ssl.create_default_context(cafile=str(ca_file))


def _kc_probe(ca_file: str) -> Callable[[int | None], str | None]:
    """HTTPS readiness probe: master realm's discovery doc must answer 200.

    Probes ``127.0.0.1`` (the literal loopback Docker publishes the mapped
    port on, and a SAN on the harness leaf) rather than ``localhost`` — the
    same host the other Tier 1b fixtures probe, so readiness never depends on
    how the runner resolves ``localhost`` (IPv6-first would otherwise add a
    spurious connection-failure surface).
    """

    def _probe(port: int | None) -> str | None:
        if port is None:
            return "Keycloak container started without a mapped 8443 port"
        try:
            resp = httpx.get(
                f"https://127.0.0.1:{port}/realms/master/.well-known/openid-configuration",
                verify=keycloak_ssl_context(ca_file),
                timeout=10.0,
            )
        except httpx.HTTPError as exc:
            return f"Keycloak discovery probe not reachable yet: {type(exc).__name__}"
        if resp.status_code == 200:
            return None
        return f"Keycloak discovery probe returned HTTP {resp.status_code} (expected 200)"

    return _probe


def start_keycloak(cert_dir: Path) -> tuple[ContainerHandle, str]:
    """Start a real Keycloak container; return ``(handle, https_base)``.

    ``https_base`` is the URL the OUTSIDE world reaches (the test process).
    The mapped host port is unique per test run so parallel suites never
    race on a fixed port.
    """
    handle = start_tier1b_container(
        ContainerSpec(
            image=KEYCLOAK_IMAGE,
            container_port=8443,
            env={
                "KC_BOOTSTRAP_ADMIN_USERNAME": KC_ADMIN_USERNAME,
                "KC_BOOTSTRAP_ADMIN_PASSWORD": KC_ADMIN_PASSWORD,
            },
            command=[
                "start-dev",
                "--hostname-strict=false",
                f"--https-certificate-file={_KC_CERT_FILE}",
                f"--https-certificate-key-file={_KC_CERT_KEY_FILE}",
            ],
            volumes=[(str(cert_dir), "/certs")],
            volume_mode="ro",
            ready_timeout_seconds=600,
            poll_interval_seconds=4.0,
            probe=_kc_probe(str(cert_dir / "ca.pem")),
        )
    )
    return handle, f"https://localhost:{handle.host_port}"


def stop_keycloak(handle: ContainerHandle) -> None:
    """Best-effort stop through the tier1b handle (never masks a failure)."""
    handle.stop()


# ---------------------------------------------------------------------------
# Admin REST bootstrap (all shapes verified live on Keycloak 26.1.0)
# ---------------------------------------------------------------------------


class KeycloakAdminApi:
    """Admin REST caller against a real Keycloak master realm.

    All requests run through a CA-trusting client (the container's own TLS
    chain). Any response at an unexpected status fails loudly with the body —
    the admin contract is not "best effort".
    """

    def __init__(self, idp_base: str, ca_file: Path) -> None:
        self._idp_base = idp_base.rstrip("/")
        self._client = httpx.Client(verify=keycloak_ssl_context(ca_file), timeout=30.0)
        self._token: str | None = None

    def close(self) -> None:
        self._client.close()

    def _get_token(self) -> str:
        resp = self._client.post(
            f"{self._idp_base}/realms/master/protocol/openid-connect/token",
            data={
                "grant_type": "password",
                "client_id": "admin-cli",
                # Keycloak's admin token endpoint is its own form, not the
                # FastAPI app login: `username` is the correct field name here
                # (the value is the Keycloak admin user). Not an app-login
                # payload, so `test-login-field-username` does not apply.
                "username": KC_ADMIN_USERNAME,  # nosemgrep: test-login-field-username
                "password": KC_ADMIN_PASSWORD,
            },
        )
        if resp.status_code != 200:
            raise Tier1bFixtureError(f"Keycloak admin password grant failed ({resp.status_code}): {resp.text[:400]}")
        token = resp.json().get("access_token")
        if not isinstance(token, str) or not token:
            raise Tier1bFixtureError("Keycloak admin token response missing access_token")
        return token

    def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        if self._token is None:
            self._token = self._get_token()
        headers = kwargs.pop("headers", {})
        return self._client.request(
            method,
            f"{self._idp_base}{path}",
            headers={"Authorization": f"Bearer {self._token}", **headers},
            **kwargs,
        )

    def _expect(self, method: str, path: str, status: int, **kwargs: Any) -> httpx.Response:
        resp = self._request(method, path, **kwargs)
        if resp.status_code != status:
            raise Tier1bFixtureError(
                f"Keycloak admin {method} {path} -> {resp.status_code} (expected {status}): {resp.text[:600]}"
            )
        return resp

    def _uuid_from_location(self, resp: httpx.Response, what: str) -> str:
        location = resp.headers.get("location", "")
        uuid_value = location.rstrip("/").rsplit("/", 1)[-1]
        if not uuid_value:
            raise Tier1bFixtureError(f"Keycloak {what} creation did not return a Location header ({location!r})")
        return str(uuid_value)

    # -- realm / client / user provisioning --------------------------------
    def create_realm(self, realm: str) -> None:
        self._expect("POST", "/admin/realms", 201, json={"realm": realm, "enabled": True})

    def create_oidc_client(self, realm: str, *, client_id: str, client_secret: str, redirect_uri: str) -> str:
        """Create a confidential OIDC client with a fixed redirect; returns its UUID."""
        resp = self._expect(
            "POST",
            f"/admin/realms/{realm}/clients",
            201,
            json={
                "clientId": client_id,
                "protocol": "openid-connect",
                "publicClient": False,
                "standardFlowEnabled": True,
                "secret": client_secret,
                "redirectUris": [redirect_uri],
                "webOrigins": ["+"],
            },
        )
        return self._uuid_from_location(resp, "oidc client")

    def create_saml_client(self, realm: str, *, entity_id: str, acs_url: str) -> str:
        """Create the SAML SP client Keycloak knows the Modulo app as."""
        resp = self._expect(
            "POST",
            f"/admin/realms/{realm}/clients",
            201,
            json={
                "clientId": entity_id,
                "protocol": "saml",
                "enabled": True,
                "fullScopeAllowed": True,
                "redirectUris": [acs_url],
                "attributes": {
                    "saml.assertion.consumer.service.POST.url": acs_url,
                    "saml_assertion_consumer_service_url": acs_url,
                    "saml.authnrequest.signed": False,
                    # Keycloak's admin API DEFAULTS client-signature-required ON
                    # for SAML clients (and mints a signing keypair), rejecting
                    # unsigned AuthnRequests with "Invalid requester". Modulo's
                    # SAML SP sends unsigned redirect-binding requests, so the
                    # client-signature requirement must be explicitly OFF.
                    "saml.client.signature": False,
                    # POST-only ACS: only the POST-binding ACS URL is
                    # registered, so force the response binding to POST — with
                    # it off Keycloak tries the (unregistered) redirect ACS and
                    # rejects "Invalid redirect uri".
                    "saml.force.post.binding": True,
                    "saml.server.signature": True,
                    "saml_name_id_format": "emailAddress",
                    "saml_force_name_id_format": True,
                    "saml.signature.algorithm": "RSA_SHA256",
                    "saml.encrypt": False,
                },
            },
        )
        return self._uuid_from_location(resp, "saml client")

    def _remove_role_list_scope(self, realm: str, client_uuid: str) -> None:
        """Delete ``role_list`` from this client's default AND optional scopes."""
        scopes = self._expect("GET", f"/admin/realms/{realm}/client-scopes", 200).json()
        role_list_id: str | None = None
        for scope in scopes:
            name, scope_id = scope.get("name"), scope.get("id")
            if name == "role_list" and isinstance(scope_id, str):
                role_list_id = scope_id
                break
        if role_list_id is None:
            raise Tier1bFixtureError(f"role_list client scope not found in realm {realm}")
        self._expect("DELETE", f"/admin/realms/{realm}/clients/{client_uuid}/default-client-scopes/{role_list_id}", 204)
        self._expect(
            "DELETE", f"/admin/realms/{realm}/clients/{client_uuid}/optional-client-scopes/{role_list_id}", 204
        )

    def _add_saml_user_property_mappers(self, realm: str, client_uuid: str) -> None:
        """Insert the three SAML user-property mappers the ACS flow reads.

        ``email``/``firstName``/``lastName`` are the confirmed shape from the
        FAR-1126 spike — they carry the user's REAL email + given name into
        the assertion so ``saml_process_response``'s email/name derivation
        has actual data instead of falling back to ``name_id``.
        """
        for mapper_name, user_attr in (
            ("email", "email"),
            ("firstName", "firstName"),
            ("lastName", "lastName"),
        ):
            self._expect(
                "POST",
                f"/admin/realms/{realm}/clients/{client_uuid}/protocol-mappers/models",
                201,
                json={
                    "name": mapper_name,
                    "protocol": "saml",
                    "protocolMapper": "saml-user-property-mapper",
                    "config": {
                        "user.attribute": user_attr,
                        "attribute.name": mapper_name,
                        "attribute.nameformat": "urn:oasis:names:tc:SAML:2.0:attrname-format:basic",
                        "friendly.name": mapper_name,
                    },
                },
            )

    def create_user_with_password(self, realm: str, *, username: str, email: str, password: str) -> str:
        """Create a user with a REAL, working password for browser-drive login."""
        resp = self._expect(
            "POST",
            f"/admin/realms/{realm}/users",
            201,
            json={
                "username": username,
                "email": email,
                "enabled": True,
                "emailVerified": True,
                "firstName": "Harness",
                "lastName": "User",
            },
        )
        user_uuid = self._uuid_from_location(resp, "user")
        self._expect(
            "PUT",
            f"/admin/realms/{realm}/users/{user_uuid}/reset-password",
            204,
            json={"type": "password", "value": password, "temporary": False},
        )
        return user_uuid


def bootstrap_oidc_realm(
    admin: KeycloakAdminApi,
    *,
    idp_base: str,
    public_base: str,
) -> KeycloakOidcTenant:
    """Create the realm, an OIDC client, and a REAL browser-driveable user.

    ``redirect_uri`` is the EXACT per-provider Modulo callback URL the real
    code path derives from ``modulo_public_url`` — a URL the IdP must come
    back to with ``code`` and ``state`` params.
    """
    realm = f"kc-oidc-{uuid.uuid4().hex[:8]}"
    pid = f"oidc-{uuid.uuid4().hex[:8]}"
    redirect_uri = f"{public_base}/api/v1/auth/oidc/{pid}/callback"
    client_secret = f"kc-oidc-secret-{uuid.uuid4().hex[:12]}"

    admin.create_realm(realm)
    admin.create_oidc_client(
        realm,
        client_id=f"oidc-{pid}",
        client_secret=client_secret,
        redirect_uri=redirect_uri,
    )
    user_email = f"oidc-user-{uuid.uuid4().hex[:8]}@{KC_USER_EMAIL_DOMAIN}"
    admin.create_user_with_password(
        realm,
        username=user_email.split("@", maxsplit=1)[0],
        email=user_email,
        password=KC_USER_PASSWORD,
    )
    return KeycloakOidcTenant(
        realm=realm,
        issuer=f"{idp_base}/realms/{realm}",
        discovery_url=f"{idp_base}/realms/{realm}/.well-known/openid-configuration",
        provider_id=pid,
        client_id=f"oidc-{pid}",
        client_secret=client_secret,
        redirect_uri=redirect_uri,
        user_email=user_email,
        user_password=KC_USER_PASSWORD,
    )


def bootstrap_saml_realm(
    admin: KeycloakAdminApi,
    *,
    idp_base: str,
    public_base: str,
) -> KeycloakSamlTenant:
    """Realm + SAML client + user; returns everything needed for seeding."""
    realm = f"kc-saml-{uuid.uuid4().hex[:8]}"
    pid = f"saml-{uuid.uuid4().hex[:8]}"
    # Per-provider SAML surface. Entity IDs match the SP code defaults exactly:
    #   entity_id default = {public}/api/v1/auth/saml/{pid}
    #   ACS default       = {public}/api/v1/auth/saml/acs/{pid}
    sp_entity_id = f"{public_base}/api/v1/auth/saml/{pid}"
    acs_url = f"{public_base}/api/v1/auth/saml/acs/{pid}"

    admin.create_realm(realm)
    client_uuid = admin.create_saml_client(realm, entity_id=sp_entity_id, acs_url=acs_url)
    admin._remove_role_list_scope(realm, client_uuid)
    admin._add_saml_user_property_mappers(realm, client_uuid)
    user_email = f"saml-user-{uuid.uuid4().hex[:8]}@{KC_USER_EMAIL_DOMAIN}"
    admin.create_user_with_password(
        realm,
        username=user_email.split("@", maxsplit=1)[0],
        email=user_email,
        password=KC_USER_PASSWORD,
    )
    return KeycloakSamlTenant(
        realm=realm,
        idp_entity_id=f"{idp_base}/realms/{realm}",
        descriptor_url=f"{idp_base}/realms/{realm}/protocol/saml/descriptor",
        provider_id=pid,
        idp_base=idp_base,
        sp_entity_id=sp_entity_id,
        acs_url=acs_url,
        user_email=user_email,
        user_password=KC_USER_PASSWORD,
    )


# ---------------------------------------------------------------------------
# Browser-drive harness (real HTTP walks; no stub IdP on the wire)
# ---------------------------------------------------------------------------

_FORM_ACTION_FINDER = re.compile(r'<form[^>]*action=["\']([^"\']+)["\'][^>]*>', re.IGNORECASE)
_HIDDEN_INPUT_FINDER = re.compile(
    r"<input[^>]*name=['\"]([^'\"]+)['\"][^>]*value=['\"]([^'\"]*)['\"]",
    re.IGNORECASE,
)
_REDIRECT_STATUS = (301, 302, 303, 307, 308)
_MAX_REDIRECT_HOPS = 15


def _parse_form_action(html: str) -> str:
    match = _FORM_ACTION_FINDER.search(html)
    if not match:
        raise Tier1bFixtureError(f"Keycloak drive: no <form action=...> found in page: {html[:600]}")
    return match.group(1)


def _parse_hidden_fields(html: str) -> dict[str, str]:
    return dict(_HIDDEN_INPUT_FINDER.findall(html))


def _assert_is_keycloak_login_form(response: httpx.Response, stage: str) -> None:
    ctype = response.headers.get("content-type", "")
    if "text/html" not in ctype:
        raise Tier1bFixtureError(
            f"Keycloak drive {stage}: expected HTML, got content-type {ctype!r}; body: {response.text[:400]}"
        )


@dataclass
class IdpBrowserDriver:
    """A real httpx walk through the Keycloak login UI on HTTPS.

    Deliberately browser-shaped:
    - redirects are followed ONE AT A TIME so the walk can stop at the FIRST
      response whose Location leaves the IdP host — that is the app's real
      ``code``/``state`` redirect (OIDC) or SAML postback capture point;
    - a 200 HTML response with a form is resolved into the form's action URL;
    - a dead-end response fails the fixture loudly — never "best effort".
    """

    idp_base: str
    ca_file: Path

    def submit_login(self, start_url: str, *, username: str, password: str) -> tuple[httpx.Client, httpx.Response]:
        """Walk ``start_url`` -> login form -> credential POST response.

        Returns the client (so the caller manages teardown AND holds the
        session state Keycloak set during login) and the response after
        credentials were submitted.
        """
        client = httpx.Client(verify=keycloak_ssl_context(self.ca_file), follow_redirects=False, timeout=30.0)
        # Close the client before re-raising a drive failure: callers only close
        # it in a `finally` around the call, which never runs when the call
        # itself raises, so an unclosed Client would leak its connection pool.
        try:
            response = client.get(start_url)
            login_action: str | None = None
            for _ in range(_MAX_REDIRECT_HOPS):
                if response.status_code in _REDIRECT_STATUS:
                    response = client.get(response.headers["location"])
                    continue
                if response.status_code == 200:
                    _assert_is_keycloak_login_form(response, "login form resolve")
                    action = _parse_form_action(response.text)
                    login_action = urljoin(str(response.url), action)
                    break
                raise Tier1bFixtureError(
                    f"Keycloak drive: unexpected response to {start_url}: status {response.status_code}; "
                    f"body: {response.text[:400]}"
                )
            else:
                raise Tier1bFixtureError(f"Keycloak drive: redirect chain exceeded {_MAX_REDIRECT_HOPS} hops")
            submitted = client.post(
                login_action,
                # Keycloak's own login form names the credential field
                # `username` (the value may be an email); `email` is not a valid
                # Keycloak form field. This drives the real IdP UI, not the
                # FastAPI app's login payload, so `test-login-field-username`
                # does not apply.
                data={"username": username, "password": password},  # nosemgrep: test-login-field-username
            )
        except Exception:
            client.close()
            raise
        return client, submitted


def walk_oidc_redirect_params(
    driver: IdpBrowserDriver,
    authorize_url: str,
    *,
    username: str,
    password: str,
) -> dict[str, str]:
    """Drive OIDC auth->login->POST->capture the code redirect.

    Returns query params captured at the (fake, public) app redirect URI —
    realistically ``code``+``state``+``session_state``. The TEST then replays
    them into the REAL ASGI callback route.
    """
    client, response = driver.submit_login(authorize_url, username=username, password=password)
    # Post-credential Keycloak bounces (302) to the app redirect_uri; the
    # response may also be a chain of further redirects first.
    try:
        for _ in range(_MAX_REDIRECT_HOPS):
            if response.status_code in _REDIRECT_STATUS:
                location = response.headers["location"]
                if not location.startswith(driver.idp_base):
                    params = {k: v[0] for k, v in parse_qs(urlsplit(location).query).items() if v}
                    if "code" not in params or "state" not in params:
                        raise Tier1bFixtureError(f"OIDC drive: final redirect {location!r} did not carry code+state")
                    return params
                response = client.get(location)
                continue
            raise Tier1bFixtureError(
                f"OIDC drive: expected a Redirect-to-app after login, got {response.status_code}: {response.text[:400]}"
            )
        raise Tier1bFixtureError("OIDC drive: post-login redirect chain exceeded hops")
    finally:
        client.close()


def walk_saml_postback(
    driver: IdpBrowserDriver,
    sso_url: str,
    *,
    username: str,
    password: str,
) -> dict[str, str]:
    """SAML browser drive: walk auth->login->POST->capture SAML postback.

    Returns ``{"SAMLResponse": ..., "RelayState": ...}`` extracted from the
    auto-submitting HTML form whose action is the (fake, public) ACS URL. We
    do NOT follow the ACS action over-the-wire — the TEST re-POSTs the same
    fields to the REAL ASGI ACS route, which is the code path under test.
    """
    client, response = driver.submit_login(sso_url, username=username, password=password)
    try:
        html: str | None = None
        for _ in range(_MAX_REDIRECT_HOPS):
            if response.status_code in _REDIRECT_STATUS:
                location = response.headers["location"]
                if not location.startswith(driver.idp_base):
                    raise Tier1bFixtureError(
                        f"SAML drive: expected a Keycloak surface after login but got a redirect to {location!r}"
                    )
                response = client.get(location)
                continue
            if response.status_code == 200:
                _assert_is_keycloak_login_form(response, "saml postback")
                html = response.text
                break
            raise Tier1bFixtureError(
                f"SAML drive: unexpected {response.status_code} after login: {response.text[:400]}"
            )
        if html is None:
            raise Tier1bFixtureError("SAML drive: no HTML response carrying the SAML postback form")
        # The SAML postback html has the auto-submitting form actioned to the
        # FAKE public ACS; the harness extracts rather than follows.
        hidden_fields = _parse_hidden_fields(html)
        saml_response = hidden_fields.get("SAMLResponse")
        if not saml_response:
            raise Tier1bFixtureError(f"SAML drive: no SAMLResponse hidden field in form html: {html[:400]}")
        captured = {"SAMLResponse": saml_response}
        if "RelayState" in hidden_fields:
            captured["RelayState"] = hidden_fields["RelayState"]
        else:
            # Some IdP implementations put RelayState in a non-hidden input.
            match = re.search(r"name=['\"]RelayState['\"][^>]*value=['\"]([^'\"]+)['\"]", html, re.IGNORECASE)
            if match is not None:
                captured["RelayState"] = match.group(1)
        return captured
    finally:
        client.close()
