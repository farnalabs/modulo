"""BDD step definitions for the ModelBackendHub runtime registry.

Exercises ModelBackendHub directly (no HTTP layer) — registration, health
resolution, failover/rotation, audit events, and one-decrypt-per-run.
"""

import asyncio
import contextlib
import uuid
from typing import Any
from unittest.mock import AsyncMock

import pytest
from pytest_bdd import given, parsers, scenarios, then, when

from modulo.core.model_backend_hub import (
    BackendNotFoundError,
    BackendUnavailableError,
    ModelBackendHub,
)
from modulo.model_backends.stub.backend import StubModelBackend

with contextlib.suppress(FileNotFoundError, OSError):
    scenarios("../../bdd/features/model_backends/hub.feature")


@pytest.fixture
def hub_ctx():
    """Shared mutable context for hub step definitions."""
    return {
        "hub": None,
        "backend_ids": {},  # name -> uuid
        "backend_objs": {},  # name -> backend instance
        "fallbacks": {},
        "healthy": {},
        "audit_events": [],
        "audit_enabled": False,
        "error": None,
        "resolved": None,
        "secret_reads": {},
    }


@pytest.fixture
def hub(hub_ctx):
    return hub_ctx["hub"]


def _new_hub() -> ModelBackendHub:
    h = ModelBackendHub()
    asyncio.run(h.__aenter__())
    return h


# ---------------------------------------------------------------------------
# Given
# ---------------------------------------------------------------------------


@given("an empty ModelBackendHub")
def empty_hub(hub_ctx):
    hub_ctx["hub"] = _new_hub()


@given(parsers.parse('a backend "{name}" is registered'))
def backend_registered(name: str, hub_ctx):
    _ensure_hub(hub_ctx)
    bid = uuid.uuid4()
    backend = StubModelBackend()
    hub_ctx["hub"].register(bid, backend)
    hub_ctx["backend_ids"][name] = bid
    hub_ctx["backend_objs"][name] = backend
    hub_ctx["healthy"][name] = True


@given(parsers.parse('a backend "{name}" is registered and healthy'))
def backend_registered_healthy(name: str, hub_ctx):
    backend_registered(name, hub_ctx)


@given(parsers.parse('backend "{name}" is configured with fallback "{fallback}"'))
def backend_fallback(name: str, fallback: str, hub_ctx):
    _ensure_hub(hub_ctx)
    hub_ctx["hub"]._fallbacks[hub_ctx["backend_ids"][name]] = [hub_ctx["backend_ids"][fallback]]


@given(parsers.parse('backend "{name}" is unhealthy'))
def backend_unhealthy(name: str, hub_ctx):
    _ensure_hub(hub_ctx)
    hub_ctx["hub"].mark_unhealthy(hub_ctx["backend_ids"][name])
    hub_ctx["healthy"][name] = False


@given("failover audit logging is enabled")
def audit_enabled(hub_ctx):
    hub_ctx["audit_enabled"] = True


@given(parsers.parse('backend "{name}" has encrypted credentials stored in the secret backend'))
def backend_encrypted(name: str, hub_ctx):
    _ensure_hub(hub_ctx)
    hub_ctx["backend_ids"][name] = uuid.uuid4()
    hub_ctx["secret_reads"][name] = 0


# ---------------------------------------------------------------------------
# When
# ---------------------------------------------------------------------------


@when(parsers.parse('I resolve backend "{name}"'))
def resolve_backend(name: str, hub_ctx):
    _ensure_hub(hub_ctx)
    bid = hub_ctx["backend_ids"].get(name)
    if bid is None:
        try:
            asyncio.run(hub_ctx["hub"].get(uuid.uuid4(), audit_logger=_audit_logger(hub_ctx)))
        except (BackendNotFoundError, BackendUnavailableError) as exc:
            hub_ctx["error"] = exc
        return
    try:
        hub_ctx["resolved"] = asyncio.run(hub_ctx["hub"].get(bid, audit_logger=_audit_logger(hub_ctx)))
    except (BackendNotFoundError, BackendUnavailableError) as exc:
        hub_ctx["error"] = exc


@when(parsers.parse('I resolve backend "{name}" with rotation'))
def resolve_backend_rotation(name: str, hub_ctx):
    _ensure_hub(hub_ctx)
    bid = hub_ctx["backend_ids"].get(name)
    if bid is None:
        try:
            asyncio.run(hub_ctx["hub"].get_with_rotation(uuid.uuid4(), audit_logger=_audit_logger(hub_ctx)))
        except (BackendNotFoundError, BackendUnavailableError) as exc:
            hub_ctx["error"] = exc
        return
    try:
        result = asyncio.run(hub_ctx["hub"].get_with_rotation(bid, audit_logger=_audit_logger(hub_ctx)))
        hub_ctx["resolved"] = result.backend
    except (BackendNotFoundError, BackendUnavailableError) as exc:
        hub_ctx["error"] = exc


@when(parsers.parse('the hub initialises with backend "{name}"'))
def hub_initialises(name: str, hub_ctx):
    _ensure_hub(hub_ctx)
    bid = hub_ctx["backend_ids"][name]

    async def _init():
        secrets = AsyncMock()

        async def _get_secret(_key: str) -> str:
            hub_ctx["secret_reads"][name] = hub_ctx["secret_reads"].get(name, 0) + 1
            return '{"api_key": "sk-test"}'

        secrets.get_secret = _get_secret
        row = _FakeRow(bid, "ollama", "llama3")
        await hub_ctx["hub"].initialise([row], secrets_backend=secrets)

    asyncio.run(_init())


# ---------------------------------------------------------------------------
# Then
# ---------------------------------------------------------------------------


@then(parsers.parse('the resolved backend is "{name}"'))
def resolved_backend(name: str, hub_ctx):
    assert hub_ctx["resolved"] is hub_ctx["backend_objs"][name], (
        f"expected backend {name!r}, got {hub_ctx['resolved']!r}"
    )


@then("an unavailable error is raised")
def unavailable_error(hub_ctx):
    assert isinstance(hub_ctx["error"], BackendUnavailableError), hub_ctx["error"]


@then("a not-found error is raised")
def not_found_error(hub_ctx):
    assert isinstance(hub_ctx["error"], BackendNotFoundError), hub_ctx["error"]


@then(parsers.parse('a model_failover audit event records primary "{primary}" and fallback "{fallback}"'))
def audit_event_recorded(primary: str, fallback: str, hub_ctx):
    primary_id = str(hub_ctx["backend_ids"][primary])
    fallback_id = str(hub_ctx["backend_ids"][fallback])
    assert any(
        e.get("event_type") == "model_failover"
        and e.get("primary_id") == primary_id
        and e.get("fallback_id") == fallback_id
        for e in hub_ctx["audit_events"]
    ), f"no model_failover event {primary_id} -> {fallback_id} in {hub_ctx['audit_events']}"


@then(parsers.parse('the secret backend was read exactly once for "{name}"'))
def secret_read_once(name: str, hub_ctx):
    assert hub_ctx["secret_reads"].get(name, 0) == 1, (
        f"expected exactly one secret read for {name!r}, got {hub_ctx['secret_reads'].get(name, 0)}"
    )


@then(parsers.parse('backend "{name}" is registered in the hub'))
def backend_registered_in_hub(name: str, hub_ctx):
    assert hub_ctx["backend_ids"][name] in hub_ctx["hub"].backend_ids


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ensure_hub(hub_ctx) -> None:
    if hub_ctx["hub"] is None:
        hub_ctx["hub"] = _new_hub()


def _audit_logger(hub_ctx):
    if not hub_ctx["audit_enabled"]:
        return None

    async def _log(event: dict[str, Any]) -> None:
        hub_ctx["audit_events"].append(event)

    return _log


class _FakeRow:
    """Minimal duck-typed ModelBackend ORM row for initialise()."""

    def __init__(self, bid: uuid.UUID, provider: str, model_id: str) -> None:
        self.id = bid
        self.provider = provider
        self.model_id = model_id
        self.credentials_ciphertext = b"unused"
        self.default_params = {}
        self.fallback_backend_ids = None
