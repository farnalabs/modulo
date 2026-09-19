"""Architecture test: default compose publishes no port on a non-loopback address.

Verifies FAR-1035: every host-published port in the default compose files
(docker-compose.yml, docker-compose.local.yml, deploy/compose/*.yml) must
bind to 127.0.0.1.  Services that expose ports on all interfaces are a
workspace-egress exposure — an egress-permitted workspace can reach the
host and therefore those ports (control-plane DB, queue broker, app server).

The guard is deliberately scoped to compose files that ship with the
product.  CI-only overlays (runner-ci.yml) and production overrides
(docker-compose.prod.yml) that intentionally expose services on all
interfaces are excluded; their port policy is documented in
docs/security/bundled-runner-trust-boundary.md instead.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

# Repo root (three levels up from this test file: tests/architecture/ → tests/ → backend/ → repo root).
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent

# Compose files under security policy — these MUST publish loopback-only.
_COMPOSE_FILES: list[Path] = [
    _REPO_ROOT / "docker-compose.yml",
    _REPO_ROOT / "docker-compose.local.yml",
    _REPO_ROOT / "deploy" / "compose" / "docker-compose.test.yml",
]

# docker-compose.prod.yml is EXCLUDED: the modulo service publishes on
# ${PORT:-80} by design (the app must be reachable from outside).  Its
# port policy is documented in the trust-boundary doc.


def _parse_host_binding(port_str: str) -> str:
    """Return the host part of a Docker port mapping.

    Docker port formats handled:
      "5432:5432"           → host = ""
      "127.0.0.1:5432:5432" → host = "127.0.0.1"
      "127.0.0.1::2375"     → host = "127.0.0.1"
      "8000:8000/tcp"       → host = ""
      "127.0.0.1:5432:5432/tcp" → host = "127.0.0.1"
    """
    # Strip protocol suffix if present (e.g. "/tcp", "/udp").
    port_str = re.sub(r"/.+$", "", port_str.strip())
    parts = port_str.split(":")
    if len(parts) >= 3:
        # host:container or host:host:container
        return parts[0]
    return ""


def _load_compose(path: Path) -> dict:
    """Load a compose file, returning the parsed YAML dict."""
    with path.open(encoding="utf-8") as fh:
        return yaml.safe_load(fh)


@pytest.mark.parametrize(
    "compose_path",
    _COMPOSE_FILES,
    ids=[str(p.relative_to(_REPO_ROOT)) for p in _COMPOSE_FILES],
)
def test_default_compose_ports_are_loopback_only(compose_path: Path) -> None:
    """Every host-published port in the default compose must bind to 127.0.0.1."""
    if not compose_path.exists():
        pytest.skip(f"Compose file not present: {compose_path.relative_to(_REPO_ROOT)}")

    compose = _load_compose(compose_path)
    services = compose.get("services", {})
    violations: list[str] = []

    for svc_name, svc_def in services.items():
        ports = svc_def.get("ports", [])
        for port_entry in ports:
            if isinstance(port_entry, int):
                # Bare integer — publishes on all interfaces.
                violations.append(f"  {svc_name}: port {port_entry} has no host binding")
                continue
            host = _parse_host_binding(str(port_entry))
            if host != "127.0.0.1":
                violations.append(f"  {svc_name}: '{port_entry}' binds to '{host or '<all>'}' (must be 127.0.0.1)")

    assert not violations, (
        f"Non-loopback port bindings found in {compose_path.relative_to(_REPO_ROOT)}:\n"
        + "\n".join(violations)
        + "\n\nSee docs/security/bundled-runner-trust-boundary.md — FAR-1035."
    )
