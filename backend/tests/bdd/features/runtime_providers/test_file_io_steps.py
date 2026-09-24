"""BDD step definitions: runtime provider file-I/O primitives (feat-core-runtime-provider-core).

Wires ``file_io.feature`` to the REAL ``modulo.core.runtime_provider`` exec-based
file-I/O defaults (FAR-1050 R2a) — network-free and DB-free. A real
``LocalRuntimeProvider`` (whose ``exec_command`` runs actual subprocesses with
the host temp-dir workspace as ``cwd``) drives the ABC's ``read_file`` /
``write_file`` / ``list_files`` / ``get_info`` defaults, so the base64
round-trip, the ``mkdir -p`` parent creation, the ``ls``-parse, the ``stat``-parse
and the typed ``RuntimeProviderError`` on a non-zero exec exit are all exercised
for real, exactly as the ``provider_matrix.feature`` pattern drives the real
hub / resolve / initialise seams.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Callable
from typing import Any

from pytest_bdd import given, parsers, scenarios, then, when

from modulo.core.runtime_provider import RuntimeProviderError, WorkspaceFileInfo, WorkspaceSpec
from modulo.core.runtime_provider.local import LocalRuntimeProvider

scenarios("file_io.feature")


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def _state(request: Any) -> dict:
    state = getattr(request.node, "_file_io_state", None)
    if state is None:
        raise RuntimeError(
            "file_io BDD state not initialised — the Background 'a local runtime workspace' step must run first"
        )
    return state


def _provider(request: Any) -> LocalRuntimeProvider:
    return _state(request)["provider"]


def _ref(request: Any) -> str:
    return _state(request)["ref"]


def _write(request: Any, path: str, data: bytes) -> None:
    _run(_provider(request).write_file(_ref(request), path, data))


def _guarded(request: Any, call: Callable[[], Any]) -> None:
    """Run *call*, recording the result or the raised exception for the Then."""
    state = _state(request)
    state["result"] = None
    state["error"] = None
    try:
        state["result"] = call()
    except Exception as exc:  # BDD verdicts assert on the exact typed error
        state["error"] = exc


# -- Given ---------------------------------------------------------------


@given("a local runtime workspace")
def step_local_workspace(request: Any) -> None:
    provider = LocalRuntimeProvider(max_concurrency=2)
    spec = WorkspaceSpec(environment_profile_id=uuid.uuid4(), organisation_id=uuid.uuid4())
    ref = _run(provider.create_workspace(spec))
    request.node._file_io_state = {"provider": provider, "ref": ref}

    def _cleanup() -> None:
        _run(provider.destroy_workspace(ref))
        _run(provider.close())

    request.addfinalizer(_cleanup)


@given(parsers.parse('a file "{path}" containing "{text}"'))
def step_file_containing_text(request: Any, path: str, text: str) -> None:
    _write(request, path, text.encode("utf-8"))


@given(parsers.parse('a file "{path}" that is {size:d} bytes long'))
def step_file_of_size(request: Any, path: str, size: int) -> None:
    _write(request, path, b"x" * size)


@given(parsers.parse('a directory "{path}" with a file inside'))
def step_directory_with_file(request: Any, path: str) -> None:
    _write(request, f"{path}/.keep", b"")


# -- When ----------------------------------------------------------------


@when(parsers.parse('I write "{text}" to file "{path}"'))
def step_write_text(request: Any, text: str, path: str) -> None:
    _write(request, path, text.encode("utf-8"))


@when(parsers.parse('I write {size:d} binary bytes to file "{path}"'))
def step_write_binary(request: Any, size: int, path: str) -> None:
    _write(request, path, bytes(range(size)))


@when(parsers.parse('I read missing file "{path}"'))
def step_read_missing(request: Any, path: str) -> None:
    provider, ref = _provider(request), _ref(request)
    _guarded(request, lambda: _run(provider.read_file(ref, path)))


@when(parsers.parse('I list files in directory "{path}"'))
def step_list_files(request: Any, path: str) -> None:
    provider, ref = _provider(request), _ref(request)
    _guarded(request, lambda: _run(provider.list_files(ref, path)))


@when(parsers.parse('I get info for "{path}"'))
def step_get_info(request: Any, path: str) -> None:
    provider, ref = _provider(request), _ref(request)
    _guarded(request, lambda: _run(provider.get_info(ref, path)))


# -- Then ----------------------------------------------------------------


@then(parsers.parse('reading file "{path}" returns "{text}"'))
def step_read_returns_text(request: Any, path: str, text: str) -> None:
    data = _run(_provider(request).read_file(_ref(request), path))
    assert data == text.encode("utf-8"), f"expected {text!r}, got {data!r}"


@then(parsers.parse('reading file "{path}" returns {size:d} binary bytes'))
def step_read_returns_binary(request: Any, path: str, size: int) -> None:
    data = _run(_provider(request).read_file(_ref(request), path))
    assert data == bytes(range(size)), f"expected {size} binary bytes, got {data!r}"


@then("the file operation fails with the typed runtime provider error")
def step_file_operation_failed(request: Any) -> None:
    error = _state(request)["error"]
    assert error is not None, "expected the file operation to fail, but it succeeded"
    assert isinstance(error, RuntimeProviderError), f"expected RuntimeProviderError, got {error!r}"


@then(parsers.parse("the listing is exactly {expected}"))
def step_listing_exact(request: Any, expected: str) -> None:
    listing = _state(request)["result"]
    assert listing is not None, "expected a listing, but the previous step failed"
    wanted = [item.strip("\"' ") for item in expected.split(",")]
    assert listing == wanted, f"expected listing {wanted!r}, got {listing!r}"


@then(parsers.parse("the info shows a size of {size:d} bytes and is not a directory"))
def step_info_file(request: Any, size: int) -> None:
    info = _state(request)["result"]
    assert isinstance(info, WorkspaceFileInfo), f"expected WorkspaceFileInfo, got {info!r}"
    assert info.size == size, f"expected size {size}, got {info.size}"
    assert not info.is_dir, f"expected a file, got is_dir={info.is_dir}"


@then("the info path is a directory")
def step_info_directory(request: Any) -> None:
    info = _state(request)["result"]
    assert isinstance(info, WorkspaceFileInfo), f"expected WorkspaceFileInfo, got {info!r}"
    assert info.is_dir, f"expected a directory, got is_dir=False for {info.path!r}"
