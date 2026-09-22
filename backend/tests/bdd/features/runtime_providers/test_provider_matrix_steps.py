"""BDD step definitions: runtime provider platform matrix (feat-core-runtime-provider-core).

Wires ``provider_matrix.feature`` to the REAL ``modulo.core.runtime_provider``
seams network-free and DB-free — the ``build_hub`` env-gated factory
registration matrix (local always; e2b only when ``MODULO_E2B_API_KEY`` is set;
the docker family only when ``MODULO_DOCKER_HOST`` or ``DOCKER_HOST`` is set,
and never on an unrelated ``MODULO_RUNNER_*`` var, FAR-996), the real
``RuntimeProviderHub.resolve`` matrix (hint-wins, direct/alias match through
the real ``matches_provider_type`` identity, known-but-unregistered →
``ProviderNotConfiguredError`` naming the remediation env var, unknown type →
``UnknownProviderTypeError`` naming the valid vocabulary, missing type →
unresolvable), and the real factory ``initialise`` config-driven registry.
Constructors (``DockerRuntimeProvider`` / ``E2BRuntimeProvider`` /
``LocalRuntimeProvider``) open no network connections, so every scenario is
deterministic locally and in CI.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

from pytest_bdd import given, parsers, scenarios, then, when

from modulo.core.runtime_provider import (
    ProviderNotConfiguredError,
    RuntimeProvider,
    UnknownProviderTypeError,
    build_hub,
)
from modulo.core.runtime_provider.docker import DockerRuntimeProvider
from modulo.core.runtime_provider.e2b import E2BRuntimeProvider
from modulo.core.runtime_provider.hub import RuntimeProviderHub
from modulo.core.runtime_provider.local import LocalRuntimeProvider
from modulo.db.models.environment_profile import PROVIDER_TYPES

scenarios("provider_matrix.feature")

_ENV_SIGNALS = ("MODULO_E2B_API_KEY", "MODULO_DOCKER_HOST", "DOCKER_HOST")


def _clean_env(monkeypatch) -> None:
    for var in _ENV_SIGNALS:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.delenv("MODULO_RUNNER_TEMPLATE_ID", raising=False)


def _hub_state(request) -> dict:
    state = getattr(request.node, "_provider_state", None)
    if state is None:
        state = {
            "hub": RuntimeProviderHub(),
            "profile": None,
            "resolved": None,
            "resolve_error": None,
            "registered": {},
        }
        request.node._provider_state = state
    return state


def _profile(*, provider_type: str | None = None, provider_hint: str | None = None) -> SimpleNamespace:
    return SimpleNamespace(provider_type=provider_type, provider_hint=provider_hint)


def _reset_resolve(request) -> None:
    state = _hub_state(request)
    state["resolved"] = None
    state["resolve_error"] = None


def _request_resolve(request, hub) -> None:
    state = _hub_state(request)
    _reset_resolve(request)
    try:
        state["resolved"] = hub.resolve(state["profile"])
    except Exception as exc:  # BDD verdicts assert on the exact typed error
        state["resolve_error"] = exc


def _error(request) -> Exception | None:
    return _hub_state(request)["resolve_error"] or _hub_state(request).get("initialise_error")


# -- Given ---------------------------------------------------------------


@given("the runtime provider environment is clean")
def step_env_clean(monkeypatch) -> None:
    _clean_env(monkeypatch)


@given("no runtime provider environment signals are set")
def step_env_no_signals(monkeypatch) -> None:
    _clean_env(monkeypatch)


@given(parsers.parse('MODULO_E2B_API_KEY is set to "{value}"'))
def step_env_e2b_key(monkeypatch, value: str) -> None:
    _clean_env(monkeypatch)
    monkeypatch.setenv("MODULO_E2B_API_KEY", value)


@given(parsers.parse('MODULO_DOCKER_HOST is set to "{value}"'))
def step_env_modulo_docker_host(monkeypatch, value: str) -> None:
    _clean_env(monkeypatch)
    monkeypatch.setenv("MODULO_DOCKER_HOST", value)


@given(parsers.parse('DOCKER_HOST is set to "{value}"'))
def step_env_docker_host(monkeypatch, value: str) -> None:
    _clean_env(monkeypatch)
    monkeypatch.setenv("DOCKER_HOST", value)


@given(parsers.parse('MODULO_RUNNER_TEMPLATE_ID is set to "{value}"'))
def step_env_unrelated_runner(monkeypatch, value: str) -> None:
    _clean_env(monkeypatch)
    monkeypatch.setenv("MODULO_RUNNER_TEMPLATE_ID", value)


@given("an empty runtime provider hub")
def step_empty_hub(request) -> None:
    state = _hub_state(request)
    state["hub"] = RuntimeProviderHub()


@given(parsers.parse('a hub with "{first}" and "{second}" providers registered'))
def step_hub_two_registered(request, first: str, second: str) -> None:
    state = _hub_state(request)
    hub = RuntimeProviderHub()
    for name in (first, second):
        hub.register(name, _real_provider(name))
    state["hub"] = hub


@given(parsers.parse('a hub with only the "{name}" provider registered'))
def step_hub_single_registered(request, name: str) -> None:
    state = _hub_state(request)
    hub = RuntimeProviderHub()
    hub.register(name, _real_provider(name))
    state["hub"] = hub


def _real_provider(name: str) -> RuntimeProvider:
    provider = _hub_provider_fixture().get(name)
    if provider is None:
        raise ValueError(f"test helper has no provider fixture for {name!r}")
    return provider


@given(parsers.parse('an environment profile with provider_hint "{hint}" and provider_type "{ptype}"'))
def step_profile_hint_and_type(request, hint: str, ptype: str) -> None:
    _hub_state(request)["profile"] = _profile(provider_type=ptype, provider_hint=hint)


@given(parsers.parse('an environment profile with provider_type "{ptype}" and no provider_hint'))
def step_profile_type_only(request, ptype: str) -> None:
    _hub_state(request)["profile"] = _profile(provider_type=ptype)


@given("an environment profile with no provider_type and no provider_hint")
def step_profile_none(request) -> None:
    _hub_state(request)["profile"] = _profile()


# -- When ----------------------------------------------------------------


@when("I build the runtime provider hub from the environment")
def step_build_hub(request) -> None:
    _hub_state(request)["hub"] = build_hub()


@when("I resolve the profile against the hub")
def step_resolve(request) -> None:
    state = _hub_state(request)
    _request_resolve(request, state["hub"])


@when(parsers.parse("I initialise the hub from config {json_text}"))
def step_initialise(request, json_text: str) -> None:
    state = _hub_state(request)
    config = json.loads(json_text)
    _reset_resolve(request)
    try:
        import asyncio

        asyncio.run(state["hub"].initialise(config))
    except Exception as exc:  # BDD verdicts assert on the exact typed error
        state["initialise_error"] = exc


# -- Then ----------------------------------------------------------------


@then(parsers.parse('the hub has a "{name}" provider'))
@then(parsers.parse('the hub has an "{name}" provider'))
def step_hub_has(request, name: str) -> None:
    provider = _hub_state(request)["hub"].get(name)
    assert provider is not None, f"expected provider {name!r} to be registered"
    _hub_state(request)["registered"][name] = provider


@then(parsers.parse('the hub has no "{name}" provider'))
def step_hub_has_not(request, name: str) -> None:
    provider = _hub_state(request)["hub"].get(name)
    assert provider is None, f"expected provider {name!r} to be absent, got {provider!r}"


@then(parsers.parse('an environment profile requesting provider_type "{ptype}" resolves to provider "{expected}"'))
def step_type_resolves_to(request, ptype: str, expected: str) -> None:
    state = _hub_state(request)
    state["profile"] = _profile(provider_type=ptype)
    _request_resolve(request, state["hub"])
    error = state["resolve_error"]
    resolved = state["resolved"]
    assert error is None, f"expected resolve to succeed, got error: {error!r}"
    assert resolved is not None, "expected a resolved provider"
    assert resolved.provider_id == expected, (
        f"expected provider_type {ptype!r} to resolve to {expected!r}, got {resolved.provider_id!r}"
    )


@then(
    parsers.parse(
        'an environment profile requesting provider_type "{ptype}" fails with '
        'ProviderNotConfiguredError mentioning "{fragment}"'
    )
)
def step_type_fails_mentioning(request, ptype: str, fragment: str) -> None:
    state = _hub_state(request)
    state["profile"] = _profile(provider_type=ptype)
    _request_resolve(request, state["hub"])
    error = state["resolve_error"]
    assert isinstance(error, ProviderNotConfiguredError), (
        f"expected ProviderNotConfiguredError for {ptype!r}, got {error!r}"
    )
    assert fragment.lower() in str(error).lower(), f"expected {fragment!r} in the remediation copy, got: {error!r}"


@then(parsers.parse('every docker-family alias resolves to the same "{name}" provider'))
def step_docker_aliases(request, name: str) -> None:
    state = _hub_state(request)
    hub = state["hub"]
    canonical = hub.get(name)
    assert canonical is not None, f"expected provider {name!r} to be registered"
    resolved_ids = {
        alias: hub.resolve(_profile(provider_type=alias)).provider_id for alias in ("docker", "local_docker", name)
    }
    assert set(resolved_ids.values()) == {name}, (
        f"docker-family aliases resolved to different providers: {resolved_ids}"
    )
    state["registered"][name] = canonical


@then(parsers.parse('the resolved provider is "{expected}"'))
def step_resolved_is(request, expected: str) -> None:
    state = _hub_state(request)
    assert state["resolve_error"] is None, f"expected resolve to succeed, got: {state['resolve_error']!r}"
    assert state["resolved"] is not None, "expected a resolved provider"
    assert state["resolved"].provider_id == expected, (
        f"expected the resolved provider to be {expected!r}, got {state['resolved'].provider_id!r}"
    )


@then(parsers.parse('resolve fails with ProviderNotConfiguredError for provider_type "{ptype}"'))
def step_resolve_fails_configured(request, ptype: str) -> None:
    state = _hub_state(request)
    error = state["resolve_error"]
    assert isinstance(error, ProviderNotConfiguredError), f"expected ProviderNotConfiguredError, got {error!r}"
    assert error.provider_type == ptype, f"expected error provider_type {ptype!r}, got {error.provider_type!r}"


@then(parsers.parse('the error\'s env_var is "{expected}"'))
def step_error_env_var(request, expected: str) -> None:
    error = _error(request)
    assert isinstance(error, ProviderNotConfiguredError), f"expected ProviderNotConfiguredError, got {error!r}"
    assert getattr(error, "env_var", None) == expected, (
        f"expected env_var {expected!r}, got {getattr(error, 'env_var', None)!r}"
    )


@then(parsers.parse('resolve fails with UnknownProviderTypeError for provider_type "{ptype}"'))
def step_resolve_fails_unknown(request, ptype: str) -> None:
    state = _hub_state(request)
    error = state["resolve_error"]
    assert isinstance(error, UnknownProviderTypeError), f"expected UnknownProviderTypeError, got {error!r}"
    assert error.provider_type == ptype, f"expected error provider_type {ptype!r}, got {error.provider_type!r}"


@then("the error names the valid provider types")
def step_error_names_valid_types(request) -> None:
    error = _error(request)
    assert isinstance(error, UnknownProviderTypeError), f"expected UnknownProviderTypeError, got {error!r}"
    for ptype in sorted(PROVIDER_TYPES):
        assert ptype in str(error), f"expected valid type {ptype!r} in the error, got: {error!r}"


@then("resolve fails with ProviderNotConfiguredError")
def step_resolve_fails_bare(request) -> None:
    state = _hub_state(request)
    assert isinstance(state["resolve_error"], ProviderNotConfiguredError), (
        f"expected ProviderNotConfiguredError, got {state['resolve_error']!r}"
    )


@then(parsers.parse('resolving provider_type "{ptype}" against the hub yields provider "{expected}"'))
def step_config_resolve_yields(request, ptype: str, expected: str) -> None:
    state = _hub_state(request)
    resolved = state["hub"].resolve(_profile(provider_type=ptype))
    assert resolved.provider_id == expected, (
        f"expected provider_type {ptype!r} to resolve to {expected!r}, got {resolved.provider_id!r}"
    )


@then(parsers.parse('initialise fails with UnknownProviderTypeError naming "{ptype}"'))
def step_initialise_fails_unknown(request, ptype: str) -> None:
    state = _hub_state(request)
    error = state.get("initialise_error")
    assert isinstance(error, UnknownProviderTypeError), f"expected UnknownProviderTypeError, got {error!r}"
    assert ptype in str(error), f"expected {ptype!r} in the error, got: {error!r}"


def _hub_provider_fixture() -> dict[str, RuntimeProvider]:
    """Real provider instances used by the given-steps hub fixtures.

    Constructors are network-free; the E2B store is given a real key so the
    hub can register it alongside the host-process and container providers.
    """
    return {
        "local": LocalRuntimeProvider(max_concurrency=2),
        "e2b": E2BRuntimeProvider(api_key="test-key"),
        "runner_docker": DockerRuntimeProvider(),
    }
