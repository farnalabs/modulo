"""FAR-1126 auth coverage tier: the REAL Keycloak container fixtures.

Owns one Keycloak 26.1.0 testcontainers instance shared by the auth tier's
REAL-IdP test modules (currently the OIDC and SAML HTTP-flow suits). Each
bootstrap creates its own realm, so a failure in one flow cannot corrupt the
other and teardown is a single container stop.

Follows the Tier 1b harness pattern (FAR-934):

- the container is session-scoped (a real container costs 30-90s to boot);
- readiness is a REAL HTTPS probe against the master realm's discovery doc
  (never a fixed sleep);
- a Docker-less workstation SKIPs the suite loudly rather than pretending
  green;
- the SSRF loopback opt-in is the TEST MODULES' responsibility (imported at
  module level from ``tests.helpers.tier1b_backends``, mirroring the Tier 1b
  modules): granting it conftest-wide here would silently lax the auth
  stub suites too, which deliberately test the guard without consent;
- ``SSL_CERT_FILE`` + ``SSRF_TRUST_PROXY=1`` are set from the generated CA
  chain at session scope so every client the REAL sso.py code paths
  constructs lazily trusts the container's harness-signed HTTPS. The pinned
  transport resolves ``trust_env`` via ``default_trust_env()`` (env var
  ``SSRF_TRUST_PROXY``); without it httpx 0.28.1's ``create_ssl_context``
  fails closed to certifi roots and silently ignores ``SSL_CERT_FILE``, so
  BOTH must be set (reproduced 2026-10-12) — no product symbol is patched
  and no proxy env var exists in the test env, so the proxy hop is inert.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest

from tests.helpers.testcontainers_harness import Tier1bFixtureError
from tests.integration.auth._keycloak_harness import KeycloakAdminApi


@dataclass
class KeycloakWorld:
    """A live Keycloak container + its admin API handle + TLS material dir."""

    idp_base: str
    cert_dir: Path
    admin: KeycloakAdminApi

    def close(self) -> None:
        self.admin.close()


@pytest.fixture(scope="session")
def keycloak_cert_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Generated CA + localhost leaf cert dir, torn down with the session."""
    from tests.integration.auth._keycloak_harness import generate_tls_certs

    return generate_tls_certs(tmp_path_factory.mktemp("kc-tls"))


@pytest.fixture(scope="session", autouse=True)
def keycloak_tls_trust_env(
    keycloak_cert_dir: Path,
    session_monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Trust the harness CA for every default TLS context built this session.

    ``SSRF_TRUST_PROXY=1`` is required alongside ``SSL_CERT_FILE``: the pinned
    transport resolves ``trust_env=False`` by default, and httpx's
    ``create_ssl_context(verify=True, trust_env=False)`` then ignores
    ``SSL_CERT_FILE`` and loads ONLY certifi roots (reproduced 2026-10-12).
    """
    session_monkeypatch.setenv("SSL_CERT_FILE", str(keycloak_cert_dir / "ca.pem"))
    session_monkeypatch.setenv("SSRF_TRUST_PROXY", "1")


@pytest.fixture(scope="session")
def keycloak_world(keycloak_cert_dir: Path) -> Iterator[KeycloakWorld]:
    """Start the container once for the whole auth tier.

    If the container cannot be provisioned (no Docker daemon, image pull
    failure, probe deadline), the whole tier SKIPs with the recorded reason —
    never a "green suite with a silently missing fixture".
    """
    from tests.integration.auth._keycloak_harness import (
        KeycloakAdminApi,
        start_keycloak,
        stop_keycloak,
    )

    try:
        handle, idp_base = start_keycloak(keycloak_cert_dir)
    except Tier1bFixtureError as exc:
        if os.environ.get("CI"):
            # In CI a provisioned-but-broken container must FAIL the job rather
            # than degrade to a green zero-coverage skip: this tier's coverage
            # IS the deliverable (FAR-1126). Local runs keep the loud skip —
            # Docker is genuinely absent on some workstations.
            raise
        pytest.skip(f"Keycloak container fixture unavailable: {exc}")

    world = KeycloakWorld(
        idp_base=idp_base,
        cert_dir=keycloak_cert_dir,
        admin=KeycloakAdminApi(idp_base, keycloak_cert_dir / "ca.pem"),
    )
    try:
        yield world
    finally:
        world.close()
        stop_keycloak(handle)


@pytest.fixture(scope="session")
def keycloak_tenants(
    keycloak_world: KeycloakWorld,
) -> tuple[object, object]:
    """Bootstrap BOTH realms (OIDC + SAML) on the one container.

    Yields ``(KeycloakOidcTenant, KeycloakSamlTenant)``. Bootstrapping both up
    front avoids one flow's admin session closing working state the other
    flow still needs, and the two realms are independent by construction.
    """
    from tests.integration.auth._keycloak_harness import (
        HARNESS_PUBLIC_BASE,
        bootstrap_oidc_realm,
        bootstrap_saml_realm,
    )

    admin = keycloak_world.admin
    oidc_tenant = bootstrap_oidc_realm(
        admin,
        idp_base=keycloak_world.idp_base,
        public_base=HARNESS_PUBLIC_BASE,
    )
    saml_tenant = bootstrap_saml_realm(
        admin,
        idp_base=keycloak_world.idp_base,
        public_base=HARNESS_PUBLIC_BASE,
    )
    return oidc_tenant, saml_tenant
