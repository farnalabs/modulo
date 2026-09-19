"""BDD step definitions: at-rest secret storage (feat-core-secrets-backend).

Drives the REAL ``modulo.core.secrets_backend`` seams network-free and
DB-free — the ``FernetSecretsBackend`` persists and reads real rows in an
in-memory aiosqlite engine (the community-tier default store), organisation
scoping goes through the real ``WHERE organisation_id = :oid`` SQL the backend
emits, key validation / RLS-context / no-session failures go through the real
``validate_key`` and ``_read_org_id_from_session`` paths, the rotation fallback
decrypts under a real old key, and the factory exercises its real default /
unknown-name / unlicensed-fallback logic (the unlicensed ``vault`` fallback is
forced by patching ``_check_external_secrets_licensed`` exactly as the unit
suite does, so the scenario is deterministic in any tier). External ``vault``
/ ``aws`` construction stays unit-tested — those backends call out to live
services.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Callable
from unittest.mock import patch

from cryptography.fernet import Fernet
from pytest_bdd import given, parsers, scenarios, then, when
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from modulo.core import secrets_backend as secrets_module
from modulo.core.secrets_backend import create_secrets_backend
from modulo.core.secrets_backend.fernet import FernetSecretsBackend
from modulo.db.models.base import Base
from modulo.db.models.secret import Secret

logging.getLogger("modulo.core.secrets_backend").setLevel(logging.CRITICAL)

scenarios("../features/infra/secrets_backend.feature")

_ORG_A = uuid.UUID("00000000-0000-0000-0000-0000000000aa")
_ORG_B = uuid.UUID("00000000-0000-0000-0000-0000000000bb")
_ORG_ID_BY_LABEL = {"A": _ORG_A, "B": _ORG_B}


def _run(coro) -> object:
    """Drive a coroutine to completion outside pytest's sync step context."""
    return asyncio.run(coro)


def _capture(request, call: Callable[[], object]) -> None:
    """Run ``call`` and record the raised exception (or ``None`` on success)."""
    try:
        call()
    except Exception as exc:  # BDD verdicts assert on the exact type
        request.node._secret_error = exc
    else:
        request.node._secret_error = None


def _new_store(request) -> FernetSecretsBackend:
    """Build a real FernetSecretsBackend against an in-memory aiosqlite engine.

    One engine backs every session, so organisations A and B share the same
    database while remaining scoped by the ``organisation_id`` the backend
    bakes into every statement. Rows are scoped through
    *session.info['org_id']* — the non-Postgres tenant fallback the backend
    reads when ``current_setting()`` is unavailable.
    """
    key = Fernet.generate_key().decode()
    engine = create_async_engine("sqlite+aiosqlite://")
    asyncio.run(_create_table(engine))

    factory = async_sessionmaker(engine, expire_on_commit=False)
    stores: dict[str, FernetSecretsBackend] = {}
    for label, org_id in _ORG_ID_BY_LABEL.items():
        session = factory()
        session.info["org_id"] = org_id
        stores[label] = FernetSecretsBackend(fernet_key=key, session=session)

    request.node._secret_engine = engine
    request.node._secret_engine_key = key
    request.node._secret_stores = stores
    request.node._secret_current_store = stores["A"]
    return stores["A"]


async def _create_table(engine) -> None:
    async with engine.begin() as conn:
        await conn.run_sync(lambda sync: Base.metadata.create_all(sync, tables=[Secret.__table__]))


def _store(request) -> FernetSecretsBackend:
    return request.node._secret_current_store


def _commit_current(request) -> None:
    session = _store(request)._session
    if session is not None:
        _run(session.commit())


def _seed_row(request, *, key: str, ciphertext: bytes) -> None:
    """Persist a raw Secret row under organisation A via the real store session."""

    async def _seed() -> None:
        async with _store(request)._session.begin():
            _store(request)._session.add(
                Secret(id=uuid.uuid4(), organisation_id=_ORG_A, key=key, encrypted_value=ciphertext)
            )

    _run(_seed())


# -- Given: store variants ---------------------------------------------------


@given("a Fernet secret store")
def step_store_default(request) -> None:
    _new_store(request)


@given("a Fernet secret store shared by organisations A and B")
def step_store_shared(request) -> None:
    _new_store(request)


@given("a Fernet secret store with no DB session")
def step_store_no_session(request) -> None:
    _new_store(request)
    request.node._secret_current_store = FernetSecretsBackend(fernet_key=Fernet.generate_key().decode())


@given("a Fernet secret store with no RLS organisation context")
def step_store_no_rls_context(request) -> None:
    _new_store(request)
    session = request.node._secret_stores["A"]._session
    session.info.clear()
    request.node._secret_current_store = FernetSecretsBackend(
        fernet_key=Fernet.generate_key().decode(), session=session
    )


@given("the store holds a secret encrypted under a rotated-out key")
def step_store_rotated_out(request) -> None:
    _new_store(request)
    old_key = Fernet.generate_key().decode()
    _seed_row(request, key="rotated-key", ciphertext=Fernet(old_key.encode()).encrypt(b"old-value"))
    request.node._secret_current_store = FernetSecretsBackend(
        fernet_key=Fernet.generate_key().decode(),
        old_key=old_key,
        session=request.node._secret_stores["A"]._session,
    )


@given("the store holds a secret encrypted under an unknown key")
def step_store_unknown_key(request) -> None:
    _new_store(request)
    _seed_row(request, key="alien-key", ciphertext=Fernet(Fernet.generate_key()).encrypt(b"alien"))


@given("the store holds corrupted ciphertext for a secret")
def step_store_corrupted(request) -> None:
    _new_store(request)
    _seed_row(request, key="corrupt-key", ciphertext=b"\x00\x00\x00\x00")


# -- When: operations --------------------------------------------------------


@when(parsers.parse('I store the secret "{key}" as "{value}"'))
def step_store_secret(request, key: str, value: str) -> None:
    _capture(request, lambda: _run(_store(request).set_secret(key, value)))
    if request.node._secret_error is None:
        _commit_current(request)


@when("I store a secret with a blank key")
def step_store_blank_key(request) -> None:
    _capture(request, lambda: _run(_store(request).set_secret("", "value")))


@when(parsers.parse('I read the secret "{key}"'))
def step_read_secret(request, key: str) -> None:
    def _read() -> None:
        request.node._secret_value = _run(_store(request).get_secret(key))

    _capture(request, _read)


@when(parsers.parse('I delete the secret "{key}"'))
def step_delete_secret(request, key: str) -> None:
    _capture(request, lambda: _run(_store(request).delete_secret(key)))
    if request.node._secret_error is None:
        _commit_current(request)


@when(parsers.parse('organisation {label} stores the secret "{key}" as "{value}"'))
def step_org_store_secret(request, label: str, key: str, value: str) -> None:
    store = request.node._secret_stores[label]
    _capture(request, lambda: _run(store.set_secret(key, value)))
    if request.node._secret_error is None:
        _run(store._session.commit())


@when(parsers.parse('organisation {label} reads the secret "{key}"'))
def step_org_read_secret(request, label: str, key: str) -> None:
    store = request.node._secret_stores[label]

    def _read() -> None:
        request.node._secret_value = _run(store.get_secret(key))

    _capture(request, _read)


# -- When: factory -----------------------------------------------------------


@when("I ask the factory for the default secret backend")
def step_factory_default(request, monkeypatch) -> None:
    monkeypatch.delenv("MODULO_SECRETS_BACKEND", raising=False)
    _capture_factory(
        request,
        lambda: create_secrets_backend(fernet_key=Fernet.generate_key().decode()),
    )


@when(parsers.parse('I ask the factory for the "{name}" secret backend'))
def step_factory_named(request, name: str) -> None:
    _capture_factory(
        request,
        lambda: create_secrets_backend(fernet_key=Fernet.generate_key().decode(), backend_name=name),
    )


@when(parsers.parse('I ask the factory for the "{name}" secret backend without a license'))
def step_factory_named_unlicensed(request, name: str) -> None:
    with patch.object(secrets_module, "_check_external_secrets_licensed", return_value=False):
        _capture_factory(
            request,
            lambda: create_secrets_backend(fernet_key=Fernet.generate_key().decode(), backend_name=name),
        )


def _capture_factory(request, call: Callable[[], object]) -> None:
    try:
        request.node._secret_factory_backend = call()
    except Exception as exc:  # BDD verdicts assert on the exact type
        request.node._secret_factory_backend = None
        request.node._secret_error = exc
    else:
        request.node._secret_error = None


# -- Then: verdicts ----------------------------------------------------------


@then(parsers.parse('the secret "{key}" is "{value}"'))
def step_secret_is(request, key: str, value: str) -> None:
    error = request.node._secret_error
    assert error is None, f"expected a successful read of {key!r}, got: {error!r}"
    assert request.node._secret_value == value, (
        f"expected {key!r} to hold {value!r}, got {request.node._secret_value!r}"
    )


@then(parsers.parse('organisation {label} can read "{key}" as "{value}"'))
def step_org_secret_is(request, label: str, key: str, value: str) -> None:
    store = request.node._secret_stores[label]

    def _read() -> None:
        request.node._secret_value = _run(store.get_secret(key))

    _capture(request, _read)
    step_secret_is(request, key, value)


@then("the read fails with KeyError")
def step_read_key_error(request) -> None:
    error = request.node._secret_error
    assert isinstance(error, KeyError), f"expected a KeyError, got {error!r}"


@then(parsers.parse('the read fails with a ValueError mentioning "{fragment}"'))
def step_read_value_error(request, fragment: str) -> None:
    step_value_error(request, fragment)


@then(parsers.parse('a ValueError is raised mentioning "{fragment}"'))
def step_value_error(request, fragment: str) -> None:
    error = request.node._secret_error
    assert isinstance(error, ValueError), f"expected a ValueError, got {error!r}"
    assert fragment.lower() in str(error).lower(), f"expected {fragment!r} in the error, got: {error!r}"


@then(parsers.parse('a RuntimeError is raised mentioning "{fragment}"'))
def step_runtime_error(request, fragment: str) -> None:
    error = request.node._secret_error
    assert isinstance(error, RuntimeError), f"expected a RuntimeError, got {error!r}"
    assert fragment.lower() in str(error).lower(), f"expected {fragment!r} in the error, got: {error!r}"


@then("the factory returns a Fernet backend")
def step_factory_fernet(request) -> None:
    error = request.node._secret_error
    assert error is None, f"expected the factory to succeed, got: {error!r}"
    assert isinstance(request.node._secret_factory_backend, FernetSecretsBackend), (
        f"expected a FernetSecretsBackend, got {type(request.node._secret_factory_backend).__name__}"
    )
