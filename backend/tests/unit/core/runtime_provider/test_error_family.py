"""FAR-1050 slice 1: the additive ``RuntimeProviderError`` family (ADR 040).

Covers:

1. ``isinstance`` relationships of the NEW hierarchy (every member is a
   ``RuntimeProviderError`` / ``RuntimeError``).
2. The DELIBERATE DUAL HIERARCHY — the untouched
   ``ProviderNotConfiguredError`` / ``UnknownProviderTypeError`` tree is
   neither re-parented under the new base nor caught by it.
3. The ABC's ``exec_command_stream`` default raises the typed
   ``StreamingUnsupportedError`` (ADR 040 "Error honesty" carve-out).
4. Per-site catchability reconciliation: every production
   ``except ProviderNotConfiguredError`` handler around provider dispatch
   is exercised to prove the new family is NOT swallowed by it (and, where
   the site's semantics require, that it surfaces through the site's own
   deliberate outer path instead).
5. The untouched hierarchy's handlers behave exactly as before for the
   untouched errors (hub resolve + config-error remediation).
"""

import uuid
from contextlib import suppress
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from modulo.core.runtime_provider import (
    ArtifactTooLargeError,
    BackendUnreachableError,
    ProviderCapabilityUnsupportedError,
    ProviderNotConfiguredError,
    ProvisionTimeoutError,
    RateLimitedError,
    RuntimeProvider,
    RuntimeProviderError,
    SdkMissingError,
    StreamingUnsupportedError,
    UnknownProviderTypeError,
    UnknownRefError,
    WorkspaceGoneError,
    WorkspaceNetworkValidationError,
    WorkspaceSpec,
)
from modulo.core.runtime_provider.hub import RuntimeProviderHub
from modulo.core.runtime_provider.local import LocalRuntimeProvider

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")

#: Exactly the set ADR 040 "Error family" names — no more, no less.
ADR040_NEW_FAMILY: tuple[type[RuntimeProviderError], ...] = (
    ProviderCapabilityUnsupportedError,
    WorkspaceGoneError,
    StreamingUnsupportedError,
    ArtifactTooLargeError,
    RateLimitedError,
    SdkMissingError,
    ProvisionTimeoutError,
    UnknownRefError,
    BackendUnreachableError,
)


# ---------------------------------------------------------------------------
# 1. isinstance relationships of the new hierarchy
# ---------------------------------------------------------------------------


def test_every_new_member_subclasses_runtime_provider_error() -> None:
    for member in ADR040_NEW_FAMILY:
        assert issubclass(member, RuntimeProviderError), member.__name__
        assert issubclass(member, RuntimeError), member.__name__
        err = member(f"{member.__name__} fired")
        assert isinstance(err, RuntimeProviderError), member.__name__


def test_runtime_provider_error_is_a_runtime_error() -> None:
    assert issubclass(RuntimeProviderError, RuntimeError)
    assert not issubclass(RuntimeProviderError, ProviderNotConfiguredError)


def test_new_family_catchable_as_runtime_provider_error() -> None:
    """A handler catching the new base catches every ADR 040 member."""
    for member in ADR040_NEW_FAMILY:
        caught = False
        try:
            raise member(f"{member.__name__} fired")
        except RuntimeProviderError:
            caught = True
        assert caught, member.__name__


# ---------------------------------------------------------------------------
# 2. The deliberate dual hierarchy
# ---------------------------------------------------------------------------


def test_config_error_hierarchy_unchanged() -> None:
    """The untouched tree keeps its exact shape (ADR 040: unification is a
    separate GA-gated ticket)."""
    assert issubclass(UnknownProviderTypeError, ProviderNotConfiguredError)
    assert issubclass(ProviderNotConfiguredError, RuntimeError)
    assert not issubclass(ProviderNotConfiguredError, RuntimeProviderError)
    assert not issubclass(UnknownProviderTypeError, RuntimeProviderError)


def test_new_family_is_not_caught_by_config_error_handlers() -> None:
    """The sibling trap: ``except ProviderNotConfiguredError`` must NOT catch
    any new-family member.

    Pattern: raise inside a suppress block acting as the config-error handler.
    If that handler swallowed the member, nothing would propagate and
    ``pytest.raises`` would fail with DID NOT RAISE.
    """
    for member in ADR040_NEW_FAMILY:
        with pytest.raises(member), suppress(ProviderNotConfiguredError):
            raise member("runtime failure")


def test_config_errors_are_not_caught_by_new_family_handlers() -> None:
    """And the converse: ``except RuntimeProviderError`` must not catch the
    untouched hierarchy — existing handlers keep owning those errors."""
    config_errors: tuple[BaseException, ...] = (
        ProviderNotConfiguredError("e2b", "MODULO_E2B_API_KEY"),
        UnknownProviderTypeError("kubernetes", frozenset({"local", "e2b"})),
    )
    for exc in config_errors:
        with pytest.raises(type(exc)), suppress(RuntimeProviderError):
            raise exc


def test_config_error_hierarchy_handlers_behave_exactly_as_before() -> None:
    """Catchability for the UNTOUCHED errors: hub.resolve raises the config
    family, and ``except ProviderNotConfiguredError`` still catches the
    unknown-type subclass (the FIX 1 contract)."""
    hub = RuntimeProviderHub()
    hub.register("local", LocalRuntimeProvider())

    with pytest.raises(ProviderNotConfiguredError, match="MODULO_E2B_API_KEY"):
        hub.resolve(SimpleNamespace(provider_type="e2b"))

    with pytest.raises(ProviderNotConfiguredError) as exc_info:
        hub.resolve(SimpleNamespace(provider_type="kubernetes"))
    assert isinstance(exc_info.value, UnknownProviderTypeError)
    assert exc_info.value.provider_type == "kubernetes"

    # The new-family handler must NOT catch the untouched errors either —
    # if it did, nothing would propagate and pytest.raises would fail.
    with pytest.raises(UnknownProviderTypeError), suppress(RuntimeProviderError):
        hub.resolve(SimpleNamespace(provider_type="kubernetes"))


# ---------------------------------------------------------------------------
# 3. The ABC's typed exec_command_stream default (ADR 040 carve-out)
# ---------------------------------------------------------------------------


class _BareProvider(RuntimeProvider):
    """Minimal concrete provider that does NOT override exec_command_stream."""

    provider_id = "bare"

    async def create_workspace(self, spec: WorkspaceSpec) -> str:
        return "ref"

    async def exec_command(
        self,
        provider_ref: str,
        command: list[str],
        *,
        cmd_timeout: int | None = None,
    ):
        raise AssertionError("exec_command must not be called in this test")

    async def destroy_workspace(self, provider_ref: str) -> None:
        return None

    async def get_workspace_status(self, provider_ref: str) -> str:
        return "running"


async def test_abc_default_exec_command_stream_raises_typed_streaming_unsupported() -> None:
    """ADR 040 "Error honesty": the default raises the typed
    ``StreamingUnsupportedError``, not a raw ``NotImplementedError``."""
    with pytest.raises(StreamingUnsupportedError, match="exec_command_stream") as exc_info:
        await _BareProvider().exec_command_stream("ref", ["echo"])
    # The typed error is a RuntimeProviderError member and is NOT the raw
    # NotImplementedError the pre-ADR default raised.
    assert isinstance(exc_info.value, RuntimeProviderError)
    assert not isinstance(exc_info.value, NotImplementedError)
    assert "BareProvider" in str(exc_info.value)


# ---------------------------------------------------------------------------
# 4. Per-site catchability reconciliation
# ---------------------------------------------------------------------------
#
# Site map (production ``except ProviderNotConfiguredError`` around provider
# dispatch), each decision documented:
#
#   (A) runner_dispatch.resolve_sandbox_dispatch_route
#       -> LEAVE the handler (it wraps hub build/resolve, which raise only
#          the config family). DELIBERATE: a new-family error raised inside
#          the guarded block must PROPAGATE as itself, never be rewrapped
#          as SandboxDispatchUnboundError. Tested below.
#   (B) connectors.shell.ShellConnector._ensure_runtime_provider
#       -> LEAVE the handler (config errors fail soft to ValueError per the
#          connector's contract). DELIBERATE: a new-family error from
#          resolve must NOT be flattened to ValueError. Tested below.
#   (C) api.routes.environment_profiles._sandbox_test_stream (inner resolve
#       handler)
#       -> LEAVE the handler (it exists to attach config-remediation copy to
#          the SSE failed event). DELIBERATE: a new-family error is surfaced
#          by the stream's EXISTING outer catch-all as a generic failed
#          event — not swallowed, not given config-remediation copy.
#          Tested below; the untouched PNCE path keeps its remediation copy
#          (covered by the pre-existing route test
#          test_profile_test_unconfigured_provider_streams_failed_event).
#   (D) tests/bdd/steps/test_environments.py resolve step
#       -> LEAVE unchanged: test-only fixture mirroring hub.resolve's config
#          errors; hub.resolve never raises the new family. Outside this
#          slice's allowlist; BDD suite is CI-only (deferred).
#   (E) ``except NotImplementedError`` sites
#       -> db/crud/pagination.py catches SQLAlchemy's column python_type
#          NotImplementedError — NOT provider dispatch; unrelated, unchanged.
#       -> tests/unit/core/bundled_runner/test_streaming.py asserted the OLD
#          ABC default (raw NotImplementedError); updated in place to the
#          typed error (ADR 040 carve-out) — reconciliation, not deletion.
#       -> No production ``except NotImplementedError`` guards a provider
#          dispatch call site (grep-verified), so the carve-out cannot strand
#          an existing handler.

_ORG = uuid.uuid4()
_PROFILE_ID = uuid.uuid4()


def _runner_docker_profile() -> SimpleNamespace:
    return SimpleNamespace(
        id=_PROFILE_ID,
        organisation_id=_ORG,
        name="Bundled Runner (Docker)",
        provider_type="runner_docker",
        image_ref="modulo-runner:opencode@sha256:" + "a" * 64,
        persistence_policy="ephemeral",
        network_policy="outbound",
        capabilities_json=[],
        config_json={
            "timeout_seconds": 1800,
            "memory_mb": 1024,
            "workspace_network": "modulo-runner-workspace",
        },
    )


class _SessionCM:
    """Async context manager standing in for ``session_factory()`` (same
    shape as the dispatch-route test's fake session)."""

    def __init__(self, profile: object) -> None:
        self._session = SimpleNamespace(
            execute=AsyncMock(return_value=SimpleNamespace(scalar_one_or_none=lambda: profile)),
            in_transaction=lambda: True,
            get_bind=lambda: SimpleNamespace(dialect=SimpleNamespace(name="sqlite")),
            info={},
        )

    async def __aenter__(self) -> SimpleNamespace:
        return self._session

    async def __aexit__(self, *args: object) -> None:
        return None


def _session_factory_returning(profile: object):
    cm = _SessionCM(profile)
    return lambda: cm


async def test_dispatch_route_new_family_error_propagates_unwrapped() -> None:
    """Site A: ``except ProviderNotConfiguredError`` in
    ``resolve_sandbox_dispatch_route`` must NOT rewrap a new-family error as
    ``SandboxDispatchUnboundError``."""
    from modulo.core.bundled_runner import runner_dispatch

    def _boom_hub(**_kwargs: object):
        raise RateLimitedError("substrate rate limit / quota exceeded")

    with (
        patch.object(runner_dispatch, "build_hub", side_effect=_boom_hub),
        pytest.raises(RateLimitedError, match="rate limit"),
    ):
        await runner_dispatch.resolve_sandbox_dispatch_route(
            _session_factory_returning(_runner_docker_profile()),
            _ORG,
            _PROFILE_ID,
        )


async def test_dispatch_route_config_error_still_becomes_dispatch_unbound() -> None:
    """Site A, untouched side: ``ProviderNotConfiguredError`` from the hub is
    still converted to ``SandboxDispatchUnboundError`` (behaviour unchanged)."""
    from modulo.core.bundled_runner import runner_dispatch
    from modulo.core.bundled_runner.runner_dispatch import SandboxDispatchUnboundError

    def _config_boom(**_kwargs: object):
        raise ProviderNotConfiguredError("runner_docker", "MODULO_DOCKER_HOST")

    with (
        patch.object(runner_dispatch, "build_hub", side_effect=_config_boom),
        pytest.raises(SandboxDispatchUnboundError, match="MODULO_DOCKER_HOST"),
    ):
        await runner_dispatch.resolve_sandbox_dispatch_route(
            _session_factory_returning(_runner_docker_profile()),
            _ORG,
            _PROFILE_ID,
        )


async def test_shell_connector_new_family_error_not_flattened_to_value_error() -> None:
    """Site B: the shell connector's fail-soft ``except
    ProviderNotConfiguredError -> ValueError`` must NOT flatten a new-family
    error."""
    from modulo.connectors.shell import ShellConnector

    class _GoneHub:
        def resolve(self, profile: object) -> object:
            raise WorkspaceGoneError("workspace reclaimed by substrate")

    connector = ShellConnector(
        runtime_provider=None,
        runtime_provider_hub=_GoneHub(),
        environment_profile_id=uuid.uuid4(),
    )
    connector._resolve_profile_from_hub = AsyncMock(  # type: ignore[method-assign]
        return_value=SimpleNamespace(provider_type="runner_docker")
    )

    with pytest.raises(WorkspaceGoneError, match="reclaimed"):
        await connector._ensure_runtime_provider()


async def test_shell_connector_config_error_still_flattened_to_value_error() -> None:
    """Site B, untouched side: ``ProviderNotConfiguredError`` still becomes
    the connector's fail-soft plain ``ValueError`` (behaviour unchanged)."""
    from modulo.connectors.shell import ShellConnector

    class _UnconfiguredHub:
        def resolve(self, profile: object) -> object:
            raise ProviderNotConfiguredError("runner_docker", "MODULO_DOCKER_HOST")

    connector = ShellConnector(
        runtime_provider=None,
        runtime_provider_hub=_UnconfiguredHub(),
        environment_profile_id=uuid.uuid4(),
    )
    connector._resolve_profile_from_hub = AsyncMock(  # type: ignore[method-assign]
        return_value=SimpleNamespace(provider_type="runner_docker")
    )

    with pytest.raises(ValueError, match="Runtime provider not configured"):
        await connector._ensure_runtime_provider()


async def test_sandbox_test_stream_surfaces_new_family_as_generic_failed_event() -> None:
    """Site C: a new-family error from ``hub.resolve`` inside the inner
    ``except ProviderNotConfiguredError`` block is NOT swallowed by that
    handler — it propagates to the stream's existing outer catch-all, which
    emits the generic failed SSE event (no config-remediation copy)."""
    from modulo.api.routes import environment_profiles as ep

    class _RateHub:
        def resolve(self, profile: object) -> object:
            raise BackendUnreachableError("substrate control plane unreachable")

        async def aclose(self) -> None:
            return None

    profile = SimpleNamespace(id=uuid.uuid4())
    with patch.object(ep, "_get_hub", return_value=_RateHub()):
        events: list[str] = []
        stream = ep._sandbox_test_stream(profile)
        async for line in stream:
            events.append(line)

    body = "".join(events)
    # Not swallowed: the outer catch-all emitted a failed event.
    assert "failed" in body
    # Not treated as a config error: the PNCE remediation copy is absent and
    # the outer handler's generic message is present instead.
    assert "No runtime provider registered" not in body
    assert "check server logs" in body


async def test_sandbox_test_stream_config_error_still_gets_remediation_copy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Site C, untouched side: ``ProviderNotConfiguredError`` still surfaces
    through the INNER handler with its remediation copy (behaviour unchanged)."""
    from modulo.api.routes import environment_profiles as ep

    monkeypatch.delenv("MODULO_E2B_API_KEY", raising=False)
    profile = SimpleNamespace(id=uuid.uuid4(), provider_type="e2b")
    # An empty real hub: resolve("e2b") raises the untouched PNCE with the
    # MODULO_E2B_API_KEY remediation (env var deliberately unset).
    with patch.object(ep, "_get_hub", return_value=RuntimeProviderHub()):
        events: list[str] = []
        stream = ep._sandbox_test_stream(profile)
        async for line in stream:
            events.append(line)

    body = "".join(events)
    assert "failed" in body
    assert "No runtime provider registered" in body
    assert "MODULO_E2B_API_KEY" in body


# ---------------------------------------------------------------------------
# 5. Export surface
# ---------------------------------------------------------------------------


def test_new_family_exported_from_package() -> None:
    import modulo.core.runtime_provider as pkg

    for name in (
        "RuntimeProviderError",
        "ProviderCapabilityUnsupportedError",
        "WorkspaceGoneError",
        "StreamingUnsupportedError",
        "ArtifactTooLargeError",
        "RateLimitedError",
        "SdkMissingError",
        "ProvisionTimeoutError",
        "UnknownRefError",
        "BackendUnreachableError",
    ):
        assert name in pkg.__all__, name
        assert getattr(pkg, name) is not None, name
    # Untouched exports remain present.
    assert "ProviderNotConfiguredError" in pkg.__all__
    assert "UnknownProviderTypeError" in pkg.__all__
    # WorkspaceNetworkValidationError stays a util-level error, not a member.
    assert not issubclass(WorkspaceNetworkValidationError, RuntimeProviderError)
