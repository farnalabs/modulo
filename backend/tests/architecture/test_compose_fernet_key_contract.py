"""Architecture test: both compose paths require a *valid* Fernet key.

FAR-2181. Both shipped compose files carried a fixed ``FERNET_KEY`` that is not
a Fernet key at all:

* ``docker-compose.yml`` - ``modulo-docker-compose-fernet-key-min-32-bytes!!``
  (47 chars). ``Settings`` validates only the LENGTH (>= 32 bytes), so this
  passed boot and then failed on the first credential write: the checkpointer
  never initialised (``startup.checkpointer_init_failed``, so ``checkpoints`` /
  ``checkpoint_migrations`` were never created and ``/healthz/ready`` reported
  ``checkpointer: degraded``) and ``POST /api/v1/model-backends`` returned 500.
  Login still returned 200, so the stack looked healthy.
* ``docker-compose.local.yml`` - ``${FERNET_KEY:-local-dev-only-change-me}``
  (24 bytes), below the ``Settings`` minimum, so ``saq-runner`` and
  ``saq-system`` both exited 1 and no run or cron ever executed.

A Fernet key is url-safe base64 of exactly 32 bytes (``Fernet.generate_key()``),
so it can never be a literal in a compose file: the only correct shape is a
required ``${FERNET_KEY:?...}`` interpolation whose message says how to
generate one. These tests pin that shape on BOTH files, prove the message is
actionable, and prove a generated value actually satisfies ``Settings`` and
Fernet while the two historical literals do not.

Run from ``backend/``::

    uv run pytest tests/architecture/test_compose_fernet_key_contract.py
"""

from __future__ import annotations

import base64
import binascii
import os
import re
from pathlib import Path

import pytest
import yaml
from cryptography.fernet import Fernet

from modulo.settings import Settings

# tests/architecture/ -> tests/ -> backend/ -> repo root.
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
_ROOT_COMPOSE = _REPO_ROOT / "docker-compose.yml"
_LOCAL_COMPOSE = _REPO_ROOT / "docker-compose.local.yml"

#: Both documented bring-up paths: the root demo stack and the local-dev stack.
_COMPOSE_FILES = (_ROOT_COMPOSE, _LOCAL_COMPOSE)

#: The two values that shipped and broke each path (FAR-2181). Kept as data so
#: the "a fixed literal cannot work" proof cannot silently rot into asserting
#: nothing.
_HISTORICAL_LITERALS = (
    "modulo-docker-compose-fernet-key-min-32-bytes!!",
    "local-dev-only-change-me",
)

#: ``${VAR:?message}`` - the only interpolation form allowed for the key.
_REQUIRED_REF = re.compile(r"^\$\{FERNET_KEY:\?(?P<message>.+)\}$")


def _load(path: Path) -> dict:
    if not path.is_file():
        pytest.skip(f"compose file not present: {path.name} (running outside a repo checkout)")
    with path.open(encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def _fernet_values(path: Path) -> dict[str, str]:
    """service name -> raw ``FERNET_KEY`` value, for services that set one."""
    values: dict[str, str] = {}
    for service, definition in (_load(path).get("services") or {}).items():
        environment = (definition or {}).get("environment") or {}
        if "FERNET_KEY" in environment:
            values[service] = str(environment["FERNET_KEY"])
    return values


@pytest.mark.parametrize("path", _COMPOSE_FILES, ids=lambda p: p.name)
def test_every_service_passes_a_required_fernet_key(path: Path):
    """No compose service may pin the key to a literal or give it a default."""
    values = _fernet_values(path)

    assert values, f"{path.name} passes FERNET_KEY to no service - did the services get renamed?"

    literal = sorted(service for service, value in values.items() if _REQUIRED_REF.match(value) is None)
    assert not literal, (
        f"{path.name} pins FERNET_KEY to a non-interpolated value for {literal}. A fixed "
        "string cannot be a valid Fernet key (url-safe base64 of exactly 32 bytes), and "
        "Settings only checks the length - so the stack boots and then fails on the first "
        "credential write (FAR-2181). Use ${FERNET_KEY:?<how to generate it>}."
    )


@pytest.mark.parametrize("path", _COMPOSE_FILES, ids=lambda p: p.name)
def test_the_failure_message_says_how_to_generate_the_key(path: Path):
    """A fail-fast error is only useful if it names the fix (FAR-2181)."""
    for service, value in _fernet_values(path).items():
        match = _REQUIRED_REF.match(value)
        assert match is not None, f"{path.name}:{service} is not a required interpolation"
        message = match.group("message")
        assert "openssl rand -base64 32" in message, (
            f"{path.name}:{service} fails fast without a generation command - put "
            "`openssl rand -base64 32` in the message so the operator has a copy-pasteable fix"
        )
        assert ".env" in message, (
            f"{path.name}:{service} does not say WHERE to put the key - compose reads `.env` "
            "from the project directory, so name it"
        )


@pytest.mark.parametrize("path", _COMPOSE_FILES, ids=lambda p: p.name)
def test_the_broken_placeholders_are_not_used_as_a_fernet_key(path: Path):
    """The two values that shipped must not come back as the key.

    Checked against the parsed ``environment`` values, not the raw file text: a
    comment that names the historical placeholder to explain the fix is
    legitimate, a service actually resolving to it is not.
    """
    values = _fernet_values(path)
    used = sorted(service for service, value in values.items() if any(bad in value for bad in _HISTORICAL_LITERALS))

    assert not used, (
        f"{path.name} resolves FERNET_KEY to a FAR-2181 placeholder for {used} - it passes the "
        "Settings length check and then breaks the first credential write"
    )


@pytest.mark.parametrize("path", _COMPOSE_FILES, ids=lambda p: p.name)
def test_a_generated_key_boots_settings_and_is_a_real_fernet_key(path: Path, monkeypatch: pytest.MonkeyPatch):
    """The value an operator puts in ``.env`` must satisfy Settings AND Fernet."""
    generated = Fernet.generate_key().decode()
    values = _fernet_values(path)
    assert values, f"{path.name} passes FERNET_KEY to no service"

    for service, value in values.items():
        match = _REQUIRED_REF.match(value)
        assert match is not None, f"{path.name}:{service} is not a required interpolation"
        # Resolve the reference the way compose does, from the operator's .env.
        environment = _service_environment(path, service)
        environment["FERNET_KEY"] = generated
        monkeypatch.setattr(os, "environ", environment)
        settings = Settings(_env_file=None)

        assert settings.fernet_key == generated
        # Settings' own validator checks length only; Fernet checks the shape.
        # Decoding is what cryptography does on first credential use.
        assert len(base64.urlsafe_b64decode(settings.fernet_key)) == 32, (
            f"{path.name}:{service} accepted a key Fernet cannot use"
        )


def _service_environment(path: Path, service: str) -> dict[str, str]:
    environment = (_load(path)["services"][service].get("environment")) or {}
    return {str(name): str(raw) for name, raw in environment.items()}


def _decodes_to_32_bytes(value: str) -> bool:
    """True when ``value`` is url-safe base64 of exactly 32 bytes."""
    try:
        return len(base64.urlsafe_b64decode(value)) == 32
    except (binascii.Error, ValueError):
        return False


@pytest.mark.parametrize("literal", _HISTORICAL_LITERALS)
def test_the_historical_placeholders_are_not_valid_fernet_keys(literal: str):
    """Proof the fix is necessary: neither shipped value decodes to 32 bytes.

    This is the regression test for the *class* - a placeholder that satisfies
    the length check but not Fernet. If this ever passes for a placeholder, the
    contract above is no longer protecting anything.
    """
    assert not _decodes_to_32_bytes(literal), (
        f"{literal!r} decodes to 32 bytes, so it would be a usable Fernet key - this test's "
        "premise (that it cannot be) is wrong and must be re-derived"
    )
