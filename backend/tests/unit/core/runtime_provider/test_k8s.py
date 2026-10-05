"""Unit tests for the Kubernetes runtime provider (FAR-1051).

Coverage (the Kubernetes client is MOCKED throughout — no live cluster):

* registration / absence: the env-gated ``build_hub`` matrix, the remediation
  env-var mapping, and the missing-SDK skip (boot never crashes);
* ``exec_command`` -> ``ExecResult`` mapping (exit-code resolution from the
  exec subresource's error-channel payload, timeout, typed stream failures);
* ``exec_command_stream`` lifecycle: chunks, healthy exit codes, and the
  stream-error XOR (an error carries ``exit_code=None`` — never a fabricated 0);
* exec WebSocket contract (FAR-1504): ``_open_exec`` resolves the client's
  two-await shape to the real websocket, and frame reading uses aiohttp 3.14's
  ``receive()`` (``recv()`` no longer exists) over a full TEXT frame sequence;
* destroy idempotency: tracked destroy, by-ref 404 / foreign-pod / failure;
* pod listing: the reconciler's own selector + deployment-identity scoping
  exercised against the REAL filtering code (client mocked, never a cluster);
* reserved identity labels: operator ``workspace_metadata`` cannot re-stamp
  ``modulo.provider`` / ``modulo.created_at`` / ``modulo.machine.id``;
* typed-error paths: capability refusals (egress), provision timeout,
  unreachable backend, unknown ref;
* structural parity for the CHECK-widening migration 0281 (mirrors the 0178
  migration test's contract assertions, without a database).
"""

from __future__ import annotations

import asyncio
import importlib.util
import socket
import time
import uuid
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, patch

import pytest
from aiohttp import WSMsgType
from kubernetes_asyncio.client.exceptions import ApiException

import modulo.core.runtime_provider.k8s as k8s_mod
from modulo.core.runtime_provider import (
    BackendUnreachableError,
    ExecResult,
    IsolationPolicy,
    ProviderCapabilityUnsupportedError,
    ProviderNotConfiguredError,
    ProvisionTimeoutError,
    RuntimeProviderError,
    UnknownProviderTypeError,
    UnknownRefError,
    WorkspaceSpec,
    build_hub,
)
from modulo.core.runtime_provider.hub import RuntimeProviderHub
from modulo.core.runtime_provider.k8s import KubernetesRuntimeProvider
from modulo.core.runtime_provider.local import LocalRuntimeProvider
from modulo.db.models.environment_profile import PROVIDER_TYPES, EnvironmentProfile

_EXIT_SUCCESS = '{"metadata":{},"status":"Success"}'


def _exit_failure(code: int) -> str:
    return (
        '{"metadata":{},"status":"Failure","message":"command terminated with non-zero exit code",'
        f'"reason":"NonZeroExitCode","details":{{"causes":[{{"reason":"ExitCode","message":"{code}"}}]}}}}'
    )


def _frame(channel: int, text: str) -> SimpleNamespace:
    """One exec WebSocket frame: first byte = channel, rest = payload."""
    return SimpleNamespace(type=WSMsgType.BINARY, data=bytes([channel]) + text.encode("utf-8"))


def _text_frame(channel: int, text: str) -> SimpleNamespace:
    """One TEXT exec frame — aiohttp hands decoded ``str`` payloads for TEXT."""
    return SimpleNamespace(type=WSMsgType.TEXT, data=chr(channel) + text)


def _close_frame() -> SimpleNamespace:
    return SimpleNamespace(type=WSMsgType.CLOSE, data=None)


class _FakeWs:
    """Minimal aiohttp-WebSocket stand-in for the exec subresource.

    Mirrors the aiohttp 3.14 ``ClientWebSocketResponse`` interface: it
    exposes ``receive()`` and deliberately NOT ``recv()``, which aiohttp 3.14
    removed (FAR-1504). A provider that still calls ``recv`` raises
    ``AttributeError`` inside ``_read_exec_frames``, which the stream turns
    into ``state.error`` — so every healthy-exit assertion in this module
    fails if the provider regresses.
    """

    def __init__(self, messages: list[SimpleNamespace], *, receive_exc: BaseException | None = None) -> None:
        self._messages = list(messages)
        self._receive_exc = receive_exc
        self.closed = False

    async def receive(self) -> SimpleNamespace:
        if self._messages:
            return self._messages.pop(0)
        if self._receive_exc is not None:
            raise self._receive_exc
        # aiohttp raises once the connection is gone and no CLOSE frame
        # was consumed — an abrupt drop mid-stream.
        raise ConnectionResetError("Connection closed")

    def close(self) -> None:
        self.closed = True


class _BlockingWs:
    """A ws whose receive never completes (for the cmd_timeout path)."""

    def __init__(self) -> None:
        self.closed = False

    async def receive(self) -> SimpleNamespace:
        await asyncio.Event().wait()  # never set — blocks until wait_for cancels
        raise AssertionError("unreachable")

    def close(self) -> None:
        self.closed = True


class _WsConnectContextManager:
    """Stand-in for aiohttp's ``_WSRequestContextManager``.

    One await lands here (kubernetes-asyncio's ``WsApiClient.request``
    returns ``ClientSession.ws_connect(...)`` without awaiting it); a second
    await yields the websocket — the real two-await shape against a cluster
    (FAR-1504 Bug A).
    """

    __slots__ = ("_ws",)

    def __init__(self, ws: _FakeWs) -> None:
        self._ws = ws

    def __await__(self) -> Any:
        async def _enter() -> _FakeWs:
            return self._ws

        return _enter().__await__()


class _FakeWsCore:
    """CoreV1Api stand-in exposing only the exec subresource."""

    def __init__(self, ws: _FakeWs | None = None, exc: BaseException | None = None) -> None:
        self._ws = ws
        self._exc = exc
        self.calls: list[dict[str, Any]] = []

    def connect_get_namespaced_pod_exec(self, *args: Any, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if self._exc is not None:
            raise self._exc
        ws = self._ws
        assert ws is not None

        async def _awaitable() -> _WsConnectContextManager:
            return _WsConnectContextManager(ws)

        return _awaitable()


def _spec(**overrides: Any) -> WorkspaceSpec:
    defaults: dict[str, Any] = {
        "environment_profile_id": uuid.uuid4(),
        "organisation_id": uuid.uuid4(),
        "image_ref": "ghcr.io/acme/runner:1.2.3",
        "timeout_seconds": 30,
    }
    defaults.update(overrides)
    return WorkspaceSpec(**defaults)


def _pod(phase: str = "Running", labels: dict[str, str] | None = None, note: str = "") -> SimpleNamespace:
    statuses: list[SimpleNamespace] | None = None
    if note:
        statuses = [
            SimpleNamespace(
                state=SimpleNamespace(waiting=SimpleNamespace(reason=note, message="back-off")),
            )
        ]
    return SimpleNamespace(
        status=SimpleNamespace(phase=phase, container_statuses=statuses),
        metadata=SimpleNamespace(labels=labels if labels is not None else {"modulo.provider": "kubernetes"}),
    )


def _listed_pod(
    name: str,
    *,
    labels: dict[str, str] | None = None,
    annotations: dict[str, str] | None = None,
) -> SimpleNamespace:
    """One pod as ``list_namespaced_pod`` hands it back (name + labels + annotations)."""
    return SimpleNamespace(
        metadata=SimpleNamespace(name=name, labels=labels if labels is not None else {}, annotations=annotations or {}),
    )


def _provider(core: Any = None, ws_core: Any = None) -> KubernetesRuntimeProvider:
    provider = KubernetesRuntimeProvider()
    if core is not None:
        provider._core_api = core
    if ws_core is not None:
        provider._ws_core_api = ws_core
    return provider


async def _drain(process: Any) -> list[Any]:
    chunks = []
    async for chunk in process.chunks:
        chunks.append(chunk)
    assert process.done.is_set()
    return chunks


# ---------------------------------------------------------------------------
# Registration / absence (build_hub env gate + remediation mapping)
# ---------------------------------------------------------------------------


class TestRegistration:
    def test_not_registered_without_env_signal(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("MODULO_KUBERNETES_ENABLED", raising=False)

        hub = build_hub()

        assert hub.get("kubernetes") is None
        assert isinstance(hub.get("local"), LocalRuntimeProvider)

    @pytest.mark.parametrize("signal", ["1", "true", "yes", "on", "registration-signal"])
    def test_registered_when_env_signal_set(self, monkeypatch: pytest.MonkeyPatch, signal: str) -> None:
        monkeypatch.setenv("MODULO_KUBERNETES_ENABLED", signal)

        hub = build_hub()
        provider = hub.get("kubernetes")

        assert isinstance(provider, KubernetesRuntimeProvider)
        resolved = hub.resolve(SimpleNamespace(provider_type="kubernetes", provider_hint=None))
        assert resolved.provider_id == "kubernetes"

    @pytest.mark.parametrize("signal", ["0", "false", "no", "off", ""])
    def test_not_registered_when_env_signal_is_falsy(self, monkeypatch: pytest.MonkeyPatch, signal: str) -> None:
        monkeypatch.setenv("MODULO_KUBERNETES_ENABLED", signal)

        hub = build_hub()

        assert hub.get("kubernetes") is None

    def test_not_registered_when_sdk_missing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A missing client SDK logs a warning and stays unregistered — boot never crashes."""
        monkeypatch.setenv("MODULO_KUBERNETES_ENABLED", "1")
        real_import = cast(Any, __import__)

        def _fake_import(name: str, *args: object, **kwargs: object) -> object:
            if name == "modulo.core.runtime_provider.k8s":
                raise ImportError("kubernetes-asyncio not installed")
            return real_import(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=_fake_import):
            hub = build_hub()

        assert hub.get("kubernetes") is None
        assert isinstance(hub.get("local"), LocalRuntimeProvider)

    def test_provider_identity_and_alias(self) -> None:
        provider = KubernetesRuntimeProvider()
        assert provider.provider_id == "kubernetes"
        assert provider.matches_provider_type("kubernetes")
        assert provider.matches_provider_type("k8s")

    def test_env_var_mapping_names_remediation(self) -> None:
        """env_var_for_provider_type('kubernetes') returns the registration var."""
        from modulo.core.runtime_provider import env_var_for_provider_type

        assert env_var_for_provider_type("kubernetes") == "MODULO_KUBERNETES_ENABLED"
        assert env_var_for_provider_type("KUBERNETES") == "MODULO_KUBERNETES_ENABLED"

    def test_resolve_unregistered_kubernetes_is_known_type_not_unknown(self) -> None:
        """A vocabulary member resolves to ProviderNotConfiguredError with its env var —
        never UnknownProviderTypeError (the vocabulary now contains kubernetes)."""
        hub = RuntimeProviderHub()
        hub.register("local", LocalRuntimeProvider())

        with pytest.raises(ProviderNotConfiguredError, match="MODULO_KUBERNETES_ENABLED") as exc_info:
            hub.resolve(SimpleNamespace(provider_type="kubernetes", provider_hint=None))

        assert exc_info.value.provider_type == "kubernetes"
        assert not isinstance(exc_info.value, UnknownProviderTypeError)


# ---------------------------------------------------------------------------
# create_workspace
# ---------------------------------------------------------------------------


class TestCreateWorkspace:
    async def test_egress_none_refuses_with_typed_capability_error(self) -> None:
        """An egress 'none' claim this tier cannot enforce fails closed at provision."""
        provider = _provider(core=AsyncMock())

        with pytest.raises(ProviderCapabilityUnsupportedError, match="NetworkPolicy"):
            await provider.create_workspace(_spec(egress_policy="none"))

    async def test_create_builds_hardened_pod_and_returns_tracked_ref(self) -> None:
        core = AsyncMock()
        core.read_namespaced_pod.return_value = _pod("Running")
        provider = _provider(core=core)

        ref = await provider.create_workspace(
            _spec(
                labels={"NODE_TOKEN": "abc123"},
                workspace_metadata={"modulo.run.id": "7d9f0000-0000-4000-8000-000000000001"},
                resource_limits={"memory_mb": 512},
            )
        )

        assert ref.startswith("modulo-ws-")
        assert ref in provider._workspaces
        body = core.create_namespaced_pod.await_args.kwargs["body"]
        container = body.spec.containers[0]
        assert container.image == "ghcr.io/acme/runner:1.2.3"
        assert container.resources.limits["memory"] == "512Mi"
        assert container.resources.limits["cpu"] == "1"
        # Env-var injection from spec.labels (FAR-595).
        assert any(env.name == "NODE_TOKEN" and env.value == "abc123" for env in container.env)
        # Pod Security restricted posture.
        security = container.security_context
        assert security.run_as_non_root is True
        assert security.run_as_user == 1001
        assert security.allow_privilege_escalation is False
        assert security.read_only_root_filesystem is True
        assert security.capabilities.drop == ["ALL"]
        assert security.seccomp_profile.type == "RuntimeDefault"
        # Namespaced credential, no mounted API token, customer SA default.
        assert body.spec.automount_service_account_token is False
        assert body.spec.service_account_name == "default"
        # Metadata: raw annotations + sanitised labels.
        assert body.metadata.annotations["modulo.run.id"] == "7d9f0000-0000-4000-8000-000000000001"
        assert body.metadata.annotations["modulo.provider"] == "kubernetes"
        assert body.metadata.labels["modulo.provider"] == "kubernetes"
        assert "modulo.run.id" in body.metadata.labels

    async def test_metadata_cannot_restamp_a_reserved_identity_label(self) -> None:
        """Reserved identity keys are stamped AFTER the metadata loop (F2).

        Operator-supplied ``workspace_metadata`` carrying ``modulo.provider`` /
        ``modulo.created_at`` / ``modulo.machine.id`` must never re-stamp them:
        a pod labelled ``modulo.provider=local_docker`` is invisible to
        ``destroy_workspace_by_ref`` and ``list_workspace_pods`` (both key on
        ``modulo.provider=kubernetes``), so the pod could never be reclaimed —
        a permanent leak. Non-reserved attribution keys still flow through.
        """
        core = AsyncMock()
        core.read_namespaced_pod.return_value = _pod("Running")
        provider = _provider(core=core)

        ref = await provider.create_workspace(
            _spec(
                workspace_metadata={
                    "modulo.provider": "local_docker",
                    "modulo.created_at": "1",
                    "modulo.machine.id": "evil-deployment",
                    "modulo.run.id": "7d9f0000-0000-4000-8000-000000000001",
                }
            )
        )

        body = core.create_namespaced_pod.await_args.kwargs["body"]
        labels = body.metadata.labels
        annotations = body.metadata.annotations
        # The identity keys win outright — assignment, not setdefault.
        assert labels["modulo.provider"] == "kubernetes"
        assert annotations["modulo.provider"] == "kubernetes"
        assert annotations["modulo.machine.id"] == provider._deployment_identity()
        # A fresh creation stamp, never the caller's backdated value.
        assert int(labels["modulo.created_at"]) > 1
        # Non-reserved attribution keys still round-trip.
        assert labels["modulo.run.id"] == "7d9f0000-0000-4000-8000-000000000001"

        # Behavioural proof: the pod the API would hand back (carrying exactly
        # those labels) is still recognised as ours by the reclamation
        # primitive — the leak the ordering bug opened is closed.
        core.read_namespaced_pod.return_value = SimpleNamespace(
            metadata=SimpleNamespace(labels=dict(labels)),
        )
        assert await provider.destroy_workspace_by_ref(ref) is True
        core.delete_namespaced_pod.assert_awaited_once()

    async def test_provision_timeout_is_typed_and_reclaims_the_pod(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(k8s_mod, "_PROVISION_POLL_INTERVAL", 0.01)
        core = AsyncMock()
        core.read_namespaced_pod.return_value = _pod("Pending", note="ImagePullBackOff")
        provider = _provider(core=core)

        with pytest.raises(ProvisionTimeoutError, match="ImagePullBackOff"):
            await provider.create_workspace(_spec(timeout_seconds=1))

        core.delete_namespaced_pod.assert_awaited_once()
        assert not provider._workspaces

    async def test_failed_phase_is_typed_and_reclaims_the_pod(self) -> None:
        core = AsyncMock()
        core.read_namespaced_pod.return_value = _pod("Failed", note="OOMKilled")
        provider = _provider(core=core)

        with pytest.raises(RuntimeProviderError, match="failed"):
            await provider.create_workspace(_spec())

        core.delete_namespaced_pod.assert_awaited_once()
        assert not provider._workspaces

    def test_container_note_wins_over_pod_conditions(self) -> None:
        pod = SimpleNamespace(
            status=SimpleNamespace(
                phase="Pending",
                container_statuses=[
                    SimpleNamespace(
                        state=SimpleNamespace(
                            waiting=SimpleNamespace(reason="ImagePullBackOff", message="back-off"),
                        ),
                    )
                ],
                conditions=[
                    SimpleNamespace(type="PodScheduled", status="False", reason="Unschedulable", message="no nodes"),
                ],
            )
        )

        assert KubernetesRuntimeProvider._pod_phase_and_note(pod) == ("pending", "ImagePullBackOff: back-off")

    def test_condition_note_surfaces_when_no_container_status(self) -> None:
        pod = SimpleNamespace(
            status=SimpleNamespace(
                phase="Pending",
                container_statuses=None,
                conditions=[
                    SimpleNamespace(type="Initialized", status="True", reason="", message=""),
                    SimpleNamespace(type="PodScheduled", status=False, reason="Unschedulable", message="no nodes"),
                ],
            )
        )

        assert KubernetesRuntimeProvider._pod_phase_and_note(pod) == (
            "pending",
            "PodScheduled Unschedulable no nodes",
        )

    def test_all_true_conditions_yield_empty_note(self) -> None:
        pod = SimpleNamespace(
            status=SimpleNamespace(
                phase="Pending",
                container_statuses=None,
                conditions=[SimpleNamespace(type="Ready", status="True", reason="", message="")],
            )
        )

        assert KubernetesRuntimeProvider._pod_phase_and_note(pod) == ("pending", "")

    def test_empty_condition_fields_yield_empty_note(self) -> None:
        pod = SimpleNamespace(
            status=SimpleNamespace(
                phase="Pending",
                container_statuses=None,
                conditions=[SimpleNamespace(type="", status="False", reason="", message="")],
            )
        )

        assert KubernetesRuntimeProvider._pod_phase_and_note(pod) == ("pending", "")

    async def test_provision_timeout_surfaces_unschedulable_condition(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(k8s_mod, "_PROVISION_POLL_INTERVAL", 0.01)
        core = AsyncMock()
        core.read_namespaced_pod.return_value = SimpleNamespace(
            status=SimpleNamespace(
                phase="Pending",
                container_statuses=None,
                conditions=[
                    SimpleNamespace(type="PodScheduled", status="False", reason="Unschedulable", message="no nodes"),
                ],
            ),
            metadata=SimpleNamespace(labels={"modulo.provider": "kubernetes"}),
        )
        provider = _provider(core=core)

        with pytest.raises(ProvisionTimeoutError, match="Unschedulable"):
            await provider.create_workspace(_spec(timeout_seconds=1))

        core.delete_namespaced_pod.assert_awaited_once()
        assert not provider._workspaces

    async def test_api_rejection_is_typed(self) -> None:
        core = AsyncMock()
        core.create_namespaced_pod.side_effect = ApiException(status=422, reason="Unprocessable")
        provider = _provider(core=core)

        with pytest.raises(RuntimeProviderError, match="HTTP 422"):
            await provider.create_workspace(_spec())

    async def test_transport_failure_is_backend_unreachable(self) -> None:
        core = AsyncMock()
        core.create_namespaced_pod.side_effect = ConnectionRefusedError("no route to apiserver")
        provider = _provider(core=core)

        with pytest.raises(BackendUnreachableError, match="unreachable"):
            await provider.create_workspace(_spec())


# ---------------------------------------------------------------------------
# exec_command (collect-then-return)
# ---------------------------------------------------------------------------


class TestExecCommand:
    async def test_collects_stdout_stderr_and_zero_exit(self) -> None:
        ws = _FakeWs(
            [
                _frame(1, "hello"),
                _frame(2, "warn"),
                _frame(3, _EXIT_SUCCESS),
                _close_frame(),
            ]
        )
        provider = _provider(ws_core=_FakeWsCore(ws))

        result = await provider.exec_command("modulo-ws-abc", ["sh", "-c", "echo hi"])

        assert result.exit_code == 0
        assert result.stdout == "hello"
        assert result.stderr == "warn"
        assert isinstance(result.duration_ms, int)

    async def test_non_zero_exit_is_a_result_not_a_stream_error(self) -> None:
        ws = _FakeWs([_frame(1, "out"), _frame(3, _exit_failure(5)), _close_frame()])
        provider = _provider(ws_core=_FakeWsCore(ws))

        result = await provider.exec_command("modulo-ws-abc", ["false"])

        assert result.exit_code == 5
        assert result.stdout == "out"

    async def test_missing_exit_status_is_minus_one_never_zero(self) -> None:
        ws = _FakeWs([_frame(1, "partial"), _close_frame()])
        provider = _provider(ws_core=_FakeWsCore(ws))

        result = await provider.exec_command("modulo-ws-abc", ["sh"])

        assert result.exit_code == -1
        assert "exit-status" in result.stderr

    async def test_timeout_returns_minus_one_and_closes_stream(self) -> None:
        provider = _provider(ws_core=_FakeWsCore(_BlockingWs()))

        result = await provider.exec_command("modulo-ws-abc", ["sleep", "999"], cmd_timeout=1)

        assert result.exit_code == -1
        assert result.stderr == "Command timed out"

    async def test_mid_stream_drop_raises_backend_unreachable(self) -> None:
        ws = _FakeWs([_frame(1, "partial")], receive_exc=ConnectionResetError("proxy died"))
        provider = _provider(ws_core=_FakeWsCore(ws))

        with pytest.raises(BackendUnreachableError, match=r"proxy died|ConnectionResetError"):
            await provider.exec_command("modulo-ws-abc", ["sh"])

    async def test_unknown_ref_is_typed(self) -> None:
        ws_core = _FakeWsCore(exc=ApiException(status=404, reason="Not Found"))
        provider = _provider(ws_core=ws_core)

        with pytest.raises(UnknownRefError, match="modulo-ws-abc"):
            await provider.exec_command("modulo-ws-abc", ["true"])

    async def test_environment_is_applied_via_posix_sh_prefix(self) -> None:
        ws = _FakeWs([_frame(3, _EXIT_SUCCESS), _close_frame()])
        ws_core = _FakeWsCore(ws)
        provider = _provider(ws_core=ws_core)

        await provider.exec_command_stream(
            "modulo-ws-abc", ["printenv", "RUN_TOKEN"], environment={"RUN_TOKEN": "s3cret value"}
        )

        command = ws_core.calls[0]["command"]
        assert command[0] == "sh"
        assert "RUN_TOKEN='s3cret value'" in command[2] or "RUN_TOKEN=s3cret\\ value" in command[2]
        assert command[-2:] == ["printenv", "RUN_TOKEN"]


# ---------------------------------------------------------------------------
# exec_command_stream (D4 lifecycle)
# ---------------------------------------------------------------------------


class TestExecCommandStream:
    async def test_streams_chunks_then_healthy_zero_exit(self) -> None:
        ws = _FakeWs(
            [
                _frame(1, "line1\n"),
                _frame(2, "err\n"),
                _frame(1, "line2\n"),
                _frame(3, _EXIT_SUCCESS),
                _close_frame(),
            ]
        )
        provider = _provider(ws_core=_FakeWsCore(ws))

        process = await provider.exec_command_stream("modulo-ws-abc", ["sh"])
        chunks = await _drain(process)

        assert [c.stream for c in chunks] == ["stdout", "stderr", "stdout"]
        assert chunks[0].data == "line1\n"
        assert process.exit_code == 0
        assert process.error is None

    async def test_non_zero_exit_populates_exit_code_without_error(self) -> None:
        ws = _FakeWs([_frame(1, "out"), _frame(3, _exit_failure(3)), _close_frame()])
        provider = _provider(ws_core=_FakeWsCore(ws))

        process = await provider.exec_command_stream("modulo-ws-abc", ["sh"])
        await _drain(process)

        assert process.exit_code == 3
        assert process.error is None

    async def test_stream_error_sets_error_and_never_fabricates_zero(self) -> None:
        """The ADR 040 XOR: an error carries exit_code=None — never a fabricated 0."""
        ws = _FakeWs([_frame(1, "partial output")], receive_exc=ConnectionResetError("engine died"))
        provider = _provider(ws_core=_FakeWsCore(ws))

        process = await provider.exec_command_stream("modulo-ws-abc", ["sh"])
        chunks = await _drain(process)

        assert chunks[0].data == "partial output"
        assert process.error is not None
        assert "engine died" in process.error
        assert process.exit_code is None

    async def test_healthy_end_without_exit_status_sets_error_not_zero(self) -> None:
        ws = _FakeWs([_frame(1, "output"), _close_frame()])
        provider = _provider(ws_core=_FakeWsCore(ws))

        process = await provider.exec_command_stream("modulo-ws-abc", ["sh"])
        await _drain(process)

        assert process.error is not None
        assert "exit-status" in process.error
        assert process.exit_code is None

    async def test_kill_closes_the_websocket(self) -> None:
        ws = _FakeWs([_frame(1, "x"), _close_frame()])
        provider = _provider(ws_core=_FakeWsCore(ws))

        process = await provider.exec_command_stream("modulo-ws-abc", ["sh"])
        await process.kill()

        assert ws.closed is True


# ---------------------------------------------------------------------------
# exec WebSocket contract (FAR-1504 — real-cluster shape, mocked client)
# ---------------------------------------------------------------------------


class TestExecWebSocketContract:
    """Regression tests for the two exec-path breakages found on a real cluster.

    The fakes model aiohttp 3.14 / kubernetes-asyncio 36.1.0 honestly:

    * ``connect_get_namespaced_pod_exec`` resolves over **two** awaits — the
      ``ws_connect`` context manager first, the websocket second (Bug A);
    * the websocket exposes ``receive()`` and **not** ``recv()``, which
      aiohttp 3.14 removed (Bug B).

    The unit suite faked a one-await, ``recv()``-shaped world, so it stayed
    green while every real exec died. Either bug reappearing fails these
    tests (and the whole module — a ``recv`` call becomes ``state.error``,
    which XORs away every healthy exit code).
    """

    async def test_open_exec_returns_the_websocket_not_the_context_manager(self) -> None:
        ws = _FakeWs([_frame(3, _EXIT_SUCCESS), _close_frame()])
        provider = _provider(ws_core=_FakeWsCore(ws))

        opened = await provider._open_exec("modulo-ws-abc", ["sh"])

        # One await deep lands on _WsConnectContextManager (no receive());
        # the second lands on the websocket the readers expect.
        assert opened is ws
        assert callable(getattr(opened, "receive", None))

    async def test_text_frames_stream_in_order_with_parsed_exit_status(self) -> None:
        ws = _FakeWs(
            [
                _text_frame(1, "first line\n"),
                _text_frame(2, "a warning\n"),
                _text_frame(1, "second line\n"),
                _text_frame(3, _exit_failure(7)),
                _close_frame(),
            ]
        )
        provider = _provider(ws_core=_FakeWsCore(ws))

        process = await provider.exec_command_stream("modulo-ws-abc", ["sh"])
        chunks = await _drain(process)

        assert [(c.stream, c.data) for c in chunks] == [
            ("stdout", "first line\n"),
            ("stderr", "a warning\n"),
            ("stdout", "second line\n"),
        ]
        assert process.exit_code == 7
        assert process.error is None

    async def test_collect_then_return_exec_reads_text_frames(self) -> None:
        ws = _FakeWs([_text_frame(1, "hello"), _text_frame(2, "warn"), _text_frame(3, _EXIT_SUCCESS), _close_frame()])
        provider = _provider(ws_core=_FakeWsCore(ws))

        result = await provider.exec_command("modulo-ws-abc", ["sh", "-c", "echo hi"])

        assert result.exit_code == 0
        assert result.stdout == "hello"
        assert result.stderr == "warn"

    async def test_stream_error_still_never_fabricates_a_zero(self) -> None:
        ws = _FakeWs([_text_frame(1, "partial")], receive_exc=ConnectionResetError("proxy died"))
        provider = _provider(ws_core=_FakeWsCore(ws))

        process = await provider.exec_command_stream("modulo-ws-abc", ["sh"])
        chunks = await _drain(process)

        assert [c.data for c in chunks] == ["partial"]
        assert process.error is not None
        assert process.exit_code is None

    def test_fake_websocket_is_receive_only(self) -> None:
        """The fake must not re-grow ``recv`` — that hollows out the guard."""
        ws = _FakeWs([])
        assert hasattr(ws, "receive")
        with pytest.raises(AttributeError):
            cast(Any, ws).recv()


# ---------------------------------------------------------------------------
# destroy_workspace / destroy_workspace_by_ref / status / log tail
# ---------------------------------------------------------------------------


class TestDestroyAndStatus:
    async def test_destroy_untracked_ref_is_a_noop(self) -> None:
        core = AsyncMock()
        provider = _provider(core=core)

        await provider.destroy_workspace("modulo-ws-foreign")

        assert core.delete_namespaced_pod.await_count == 0

    async def test_destroy_tracked_pod_deletes_and_untracks(self) -> None:
        core = AsyncMock()
        provider = _provider(core=core)
        provider._workspaces.add("modulo-ws-abc")

        await provider.destroy_workspace("modulo-ws-abc")

        core.delete_namespaced_pod.assert_awaited_once_with(name="modulo-ws-abc", namespace="modulo")
        assert "modulo-ws-abc" not in provider._workspaces

    async def test_destroy_by_ref_already_gone_is_idempotent_success(self) -> None:
        core = AsyncMock()
        core.read_namespaced_pod.side_effect = ApiException(status=404, reason="Not Found")
        provider = _provider(core=core)

        assert await provider.destroy_workspace_by_ref("modulo-ws-gone") is True
        assert core.delete_namespaced_pod.await_count == 0

    async def test_destroy_by_ref_foreign_pod_is_left_alone(self) -> None:
        core = AsyncMock()
        core.read_namespaced_pod.return_value = _pod("Running", labels={"modulo.provider": "someone-else"})
        provider = _provider(core=core)

        assert await provider.destroy_workspace_by_ref("modulo-ws-foreign") is True
        assert core.delete_namespaced_pod.await_count == 0

    async def test_destroy_by_ref_deletes_our_pod(self) -> None:
        core = AsyncMock()
        core.read_namespaced_pod.return_value = _pod("Running")
        provider = _provider(core=core)

        assert await provider.destroy_workspace_by_ref("modulo-ws-abc") is True
        core.delete_namespaced_pod.assert_awaited_once()

    async def test_destroy_by_ref_delete_failure_returns_false(self) -> None:
        core = AsyncMock()
        core.read_namespaced_pod.return_value = _pod("Running")
        core.delete_namespaced_pod.side_effect = ApiException(status=500, reason="Boom")
        provider = _provider(core=core)

        assert await provider.destroy_workspace_by_ref("modulo-ws-abc") is False

    async def test_destroy_by_ref_read_failure_returns_false_never_raises(self) -> None:
        core = AsyncMock()
        core.read_namespaced_pod.side_effect = ConnectionRefusedError("apiserver gone")
        provider = _provider(core=core)

        assert await provider.destroy_workspace_by_ref("modulo-ws-abc") is False

    async def test_status_maps_phase_gone_and_error(self) -> None:
        core = AsyncMock()
        core.read_namespaced_pod.return_value = _pod("Running")
        provider = _provider(core=core)
        assert await provider.get_workspace_status("modulo-ws-abc") == "running"

        core.read_namespaced_pod.side_effect = ApiException(status=404, reason="Not Found")
        assert await provider.get_workspace_status("modulo-ws-gone") == "terminated"

        core.read_namespaced_pod.side_effect = ApiException(status=500, reason="Boom")
        assert await provider.get_workspace_status("modulo-ws-abc") == "unknown"

    async def test_read_log_tail_reads_agent_log_and_is_bounded(self) -> None:
        provider = _provider()
        provider.exec_command = AsyncMock(  # type: ignore[method-assign]
            return_value=ExecResult(exit_code=0, stdout="0123456789abcdefghij", stderr="", duration_ms=1)
        )

        tail = await provider.read_log_tail("modulo-ws-abc", max_bytes=5)
        assert tail == b"fghij"
        # The read targets the dispatcher's agent-log FILE (the pod's own
        # container log is the empty keep-alive wait loop), bounded by tail -c.
        await_args = provider.exec_command.await_args
        assert await_args is not None
        command = await_args.args[1]
        assert command[0] == "sh"
        assert "tail -c 20 /home/user/agent.log" in command[2]

        provider.exec_command.side_effect = UnknownRefError("gone")
        assert not await provider.read_log_tail("modulo-ws-gone", max_bytes=5)

        provider.exec_command.reset_mock(side_effect=True)
        assert not await provider.read_log_tail("", max_bytes=5)
        provider.exec_command.assert_not_awaited()


# ---------------------------------------------------------------------------
# deployment identity (reconciler scoping — F4)
# ---------------------------------------------------------------------------


class TestDeploymentIdentity:
    def test_falls_back_to_the_hostname_never_a_shared_constant(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Docker parity: ``MODULO_RUNNER_MACHINE_ID`` first, hostname second.

        The constant fallback this had (``"unspecified"``) collapsed two
        deployments sharing one namespace onto ONE identity, so each
        reconciler would sweep the other's pods as same-machine.
        """
        monkeypatch.delenv("MODULO_RUNNER_MACHINE_ID", raising=False)

        assert KubernetesRuntimeProvider._deployment_identity() == socket.gethostname()

        monkeypatch.setenv("MODULO_RUNNER_MACHINE_ID", "explicit-machine")

        assert KubernetesRuntimeProvider._deployment_identity() == "explicit-machine"


# ---------------------------------------------------------------------------
# list_workspace_pods (the reconciler's listing primitive — behavioural)
# ---------------------------------------------------------------------------


class TestListWorkspacePods:
    """Real selector / identity matching for the reconciler's listing (F6).

    The reconciler tests mock ``list_workspace_pods``; these exercise the
    provider's OWN filtering code instead — the Kubernetes client is still
    mocked (no live cluster), but the scoping logic is not.
    """

    async def test_returns_this_deployment_pods_with_labels_and_age(self) -> None:
        core = AsyncMock()
        identity = KubernetesRuntimeProvider._deployment_identity()
        now = int(time.time())
        ours = _listed_pod(
            "modulo-ws-ours",
            labels={
                "modulo.provider": "kubernetes",
                "modulo.created_at": str(now - 30),
                "modulo.run.id": "run-1",
            },
            annotations={k8s_mod._MACHINE_ANNOTATION: identity},
        )
        core.list_namespaced_pod.return_value = SimpleNamespace(items=[ours])
        provider = _provider(core=core)

        entries = await provider.list_workspace_pods()

        # Server-side selector: foreign pods never reach the client-side pass.
        core.list_namespaced_pod.assert_awaited_once_with(
            namespace="modulo",
            label_selector=f"{k8s_mod._PROVIDER_LABEL}={k8s_mod._PROVIDER_LABEL_VALUE}",
        )
        assert [entry.ref for entry in entries] == ["modulo-ws-ours"]
        # The reconciler's own label vocabulary survives the listing.
        assert entries[0].labels["modulo.run.id"] == "run-1"
        age = entries[0].created_age_s
        assert age >= 25.0
        assert age <= 60.0

    async def test_excludes_foreign_and_identity_less_pods(self) -> None:
        """Client-side deployment-identity match: a same-label pod of ANOTHER
        Modulo deployment (and a pod with no identity annotation at all) is
        never listed — two deployments sharing one namespace stay disjoint."""
        core = AsyncMock()
        identity = KubernetesRuntimeProvider._deployment_identity()
        now = int(time.time())
        items = [
            _listed_pod(
                "modulo-ws-foreign",
                labels={"modulo.provider": "kubernetes", "modulo.created_at": str(now)},
                annotations={k8s_mod._MACHINE_ANNOTATION: "some-other-deployment"},
            ),
            _listed_pod(
                "modulo-ws-no-identity",
                labels={"modulo.provider": "kubernetes", "modulo.created_at": str(now)},
                annotations={},
            ),
            _listed_pod(
                "modulo-ws-ours",
                labels={"modulo.provider": "kubernetes", "modulo.created_at": str(now)},
                annotations={k8s_mod._MACHINE_ANNOTATION: identity},
            ),
        ]
        core.list_namespaced_pod.return_value = SimpleNamespace(items=items)
        provider = _provider(core=core)

        entries = await provider.list_workspace_pods()

        assert [entry.ref for entry in entries] == ["modulo-ws-ours"]

    async def test_listing_failure_propagates_never_a_silent_empty_list(self) -> None:
        """A configured-but-unreachable cluster is a reported sweep failure,
        never an empty listing the sweep would read as "nothing to reclaim"."""
        core = AsyncMock()
        core.list_namespaced_pod.side_effect = ConnectionRefusedError("apiserver gone")
        provider = _provider(core=core)

        with pytest.raises(ConnectionRefusedError):
            await provider.list_workspace_pods()


# ---------------------------------------------------------------------------
# apply_isolation
# ---------------------------------------------------------------------------


class TestApplyIsolation:
    async def test_selected_egress_allowlist_is_refused(self) -> None:
        provider = _provider(core=AsyncMock())
        policy = IsolationPolicy(egress_policy="selected", egress_allowlist=[{"host": "api.github.com"}])

        with pytest.raises(ProviderCapabilityUnsupportedError, match="NetworkPolicy"):
            await provider.apply_isolation("modulo-ws-abc", _spec(), policy)

    async def test_egress_none_is_refused(self) -> None:
        provider = _provider(core=AsyncMock())

        with pytest.raises(ProviderCapabilityUnsupportedError, match="NetworkPolicy"):
            await provider.apply_isolation("modulo-ws-abc", _spec(), IsolationPolicy(egress_policy="none"))

    async def test_git_scope_and_read_only_seal_run_in_order(self, monkeypatch: pytest.MonkeyPatch) -> None:
        provider = _provider()
        calls: list[list[str]] = []

        async def _fake_exec(provider_ref: str, command: list[str], *, cmd_timeout: int | None = None) -> ExecResult:
            calls.append(command)
            return ExecResult(exit_code=0, stdout="", stderr="", duration_ms=1)

        monkeypatch.setattr(provider, "exec_command", _fake_exec)

        status = await provider.apply_isolation(
            "modulo-ws-abc",
            _spec(),
            IsolationPolicy(read_only=True, git_credentials="scoped", command_timeout=5.0),
        )

        assert status is None
        assert len(calls) == 2
        git_script = " ".join(calls[0])
        assert "/home/user/.gitconfig" in git_script
        seal_script = " ".join(calls[1])
        assert "chmod" in seal_script

    async def test_enforcement_failure_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        provider = _provider()

        async def _fake_exec(provider_ref: str, command: list[str], *, cmd_timeout: int | None = None) -> ExecResult:
            return ExecResult(exit_code=1, stdout="", stderr="boom", duration_ms=1)

        monkeypatch.setattr(provider, "exec_command", _fake_exec)

        with pytest.raises(RuntimeProviderError, match="git-credential scope"):
            await provider.apply_isolation(
                "modulo-ws-abc",
                _spec(),
                IsolationPolicy(git_credentials="scoped", command_timeout=5.0),
            )

    async def test_armed_guard_returns_install_status(self, monkeypatch: pytest.MonkeyPatch) -> None:
        provider = _provider()
        calls: list[list[str]] = []

        async def _fake_exec(provider_ref: str, command: list[str], *, cmd_timeout: int | None = None) -> ExecResult:
            calls.append(command)
            return ExecResult(exit_code=0, stdout="", stderr="", duration_ms=1)

        monkeypatch.setattr(provider, "exec_command", _fake_exec)

        status = await provider.apply_isolation(
            "modulo-ws-abc",
            _spec(),
            IsolationPolicy(single_pr_per_run=True, command_timeout=5.0),
        )

        assert status == "installed"
        assert len(calls) == 1
        assert "gh" in " ".join(calls[0])


# ---------------------------------------------------------------------------
# Client configuration (auth: in-cluster else kubeconfig)
# ---------------------------------------------------------------------------


class TestClientConfiguration:
    async def test_kubeconfig_failure_is_typed_backend_unreachable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("KUBERNETES_SERVICE_HOST", raising=False)

        async def _boom(**kwargs: Any) -> None:
            raise RuntimeError("no kubeconfig here")

        monkeypatch.setattr(k8s_mod.k8s_config, "load_kube_config", _boom)

        provider = KubernetesRuntimeProvider()
        with pytest.raises(BackendUnreachableError, match="configuration could not be loaded"):
            await provider._get_core()

    async def test_incluster_config_used_when_service_host_set(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("KUBERNETES_SERVICE_HOST", "10.96.0.1")
        seen: dict[str, Any] = {}

        def _incluster(client_configuration: Any = None, **kwargs: Any) -> None:
            seen["client_configuration"] = client_configuration

        async def _kube(**kwargs: Any) -> None:  # pragma: no cover - must never run in-cluster
            raise AssertionError("kubeconfig must not be loaded in-cluster")

        monkeypatch.setattr(k8s_mod.k8s_config, "load_incluster_config", _incluster)
        monkeypatch.setattr(k8s_mod.k8s_config, "load_kube_config", _kube)

        provider = KubernetesRuntimeProvider()
        configuration = await provider._get_configuration()

        assert seen["client_configuration"] is configuration
        assert provider._configuration is configuration


# ---------------------------------------------------------------------------
# Migration 0281 structural parity (mirrors the 0178 migration test)
# ---------------------------------------------------------------------------

_VERSIONS = Path(__file__).resolve().parents[4] / "src" / "modulo" / "db" / "migrations" / "versions"
_MIGRATION_NAME = "0281_env_profiles_kubernetes"
_MIGRATION_PATH = _VERSIONS / f"{_MIGRATION_NAME}.py"


def _load_migration() -> ModuleType:
    assert _MIGRATION_PATH.exists(), f"Migration file missing: {_MIGRATION_PATH}"
    spec = importlib.util.spec_from_file_location(f"migration_{_MIGRATION_NAME}", _MIGRATION_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _migration_section(section: str) -> str:
    source = _MIGRATION_PATH.read_text(encoding="utf-8")
    parts = source.split('"""', 2)
    code = parts[2] if len(parts) >= 3 else source
    body = code.split(f"def {section}", 1)[1]
    # Cut at the next def so the upgrade section never includes the downgrade.
    if section == "upgrade" and "def downgrade" in body:
        body = body.split("def downgrade", 1)[0]
    return body


class TestMigration0281Parity:
    def test_metadata_pins_chain(self) -> None:
        module = _load_migration()
        assert module.revision == _MIGRATION_NAME
        assert module.down_revision == "0280_runs_node_deadline_watchdog_fired_count"
        assert module.branch_labels is None
        assert module.depends_on is None

    def test_upgrade_widens_check_with_kubernetes(self) -> None:
        upgrade = _migration_section("upgrade")
        assert "DROP CONSTRAINT IF EXISTS ck_env_profiles_provider_type" in upgrade
        assert "'kubernetes'::character varying" in upgrade
        # The four existing members stay valid — nothing is re-pointed on upgrade.
        assert "'local_docker'::character varying" in upgrade
        assert "'runner_docker'::character varying" in upgrade
        assert "'e2b'::character varying" in upgrade
        assert "'local'::character varying" in upgrade

    def test_downgrade_repoints_before_narrowing(self) -> None:
        downgrade = _migration_section("downgrade")
        repoint_at = downgrade.index("SET provider_type = 'local_docker'")
        narrow_at = downgrade.index("DROP CONSTRAINT IF EXISTS ck_env_profiles_provider_type")
        assert repoint_at < narrow_at
        assert "WHERE provider_type = 'kubernetes'" in downgrade
        assert "'kubernetes'::character varying" not in downgrade
        assert "IF NOT EXISTS" in downgrade

    def test_model_vocabulary_and_check_include_kubernetes(self) -> None:
        assert "kubernetes" in PROVIDER_TYPES
        constraint = next(
            c for c in EnvironmentProfile.__table_args__ if getattr(c, "name", None) == "ck_env_profiles_provider_type"
        )
        sqltext = str(getattr(constraint, "sqltext", constraint))
        assert "kubernetes" in sqltext
