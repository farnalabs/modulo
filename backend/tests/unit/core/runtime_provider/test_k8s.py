"""Unit tests for the Kubernetes runtime provider (FAR-1051).

Coverage (the Kubernetes client is MOCKED throughout — no live cluster):

* registration / absence: the env-gated ``build_hub`` matrix, the remediation
  env-var mapping, and the missing-SDK skip (boot never crashes);
* ``exec_command`` -> ``ExecResult`` mapping (exit-code resolution from the
  exec subresource's error-channel payload, timeout, typed stream failures);
* ``exec_command_stream`` lifecycle: chunks, healthy exit codes, and the
  stream-error XOR (an error carries ``exit_code=None`` — never a fabricated 0);
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
from typing import Any, Self, cast
from unittest.mock import AsyncMock, MagicMock, patch

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


def _close_frame() -> SimpleNamespace:
    return SimpleNamespace(type=WSMsgType.CLOSE, data=None)


class _FakeWs:
    """Minimal aiohttp-WebSocket stand-in for the exec subresource."""

    def __init__(self, messages: list[SimpleNamespace], *, receive_exc: BaseException | None = None) -> None:
        self._messages = list(messages)
        self._receive_exc = receive_exc
        self.closed = False

    async def recv(self) -> SimpleNamespace:
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
    """A ws whose recv never completes (for the cmd_timeout path)."""

    def __init__(self) -> None:
        self.closed = False

    async def recv(self) -> SimpleNamespace:
        await asyncio.Event().wait()  # never set — blocks until wait_for cancels
        raise AssertionError("unreachable")

    def close(self) -> None:
        self.closed = True


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

        async def _awaitable() -> _FakeWs:
            return ws

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

    async def test_read_log_tail_is_bounded_and_never_raises(self) -> None:
        core = AsyncMock()
        core.read_namespaced_pod_log.return_value = "0123456789abcdefghij"
        provider = _provider(core=core)

        tail = await provider.read_log_tail("modulo-ws-abc", max_bytes=5)
        assert tail == b"fghij"

        core.read_namespaced_pod_log.side_effect = ApiException(status=404, reason="Not Found")
        assert not await provider.read_log_tail("modulo-ws-gone", max_bytes=5)

        assert not await provider.read_log_tail("", max_bytes=5)


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
# Migration 0279 structural parity (mirrors the 0178 migration test)
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


# ---------------------------------------------------------------------------
# Changed-lines coverage remediation (Branch Fixer): defensive / error arms.
# Every test below pins a real behaviour on an otherwise-unexercised branch
# of the FAR-1051 Kubernetes provider (all clients mocked — no live cluster).
# ---------------------------------------------------------------------------


class _CoroutineCloseWs:
    """A ws whose ``close()`` returns an awaitable (aiohttp's real shape)."""

    def __init__(self, *, exc: BaseException | None = None) -> None:
        self._exc = exc
        self.closed = False

    def close(self) -> Any:
        if self._exc is not None:
            raise self._exc

        async def _done() -> None:
            self.closed = True

        return _done()


class TestExitStatusResolution:
    def test_unparseable_error_payload_is_reported_not_fabricated(self) -> None:
        """A garbage error-channel payload yields ``(None, note)`` — never a 0."""
        exit_code, note = k8s_mod._resolve_exit_code("{not json")

        assert exit_code is None
        assert note is not None
        assert "unparseable" in note


class TestConfigurationResolution:
    async def test_configuration_is_cached_after_the_first_load(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("KUBERNETES_SERVICE_HOST", raising=False)
        loads: list[dict[str, Any]] = []

        async def _kube(**kwargs: Any) -> None:
            loads.append(kwargs)

        monkeypatch.setattr(k8s_mod.k8s_config, "load_kube_config", _kube)
        provider = KubernetesRuntimeProvider()

        first = await provider._get_configuration()
        second = await provider._get_configuration()

        assert first is second
        assert len(loads) == 1  # the fast path returned the cached configuration

    async def test_configuration_cancellation_propagates(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("KUBERNETES_SERVICE_HOST", raising=False)

        async def _cancel(**kwargs: Any) -> None:
            raise asyncio.CancelledError

        monkeypatch.setattr(k8s_mod.k8s_config, "load_kube_config", _cancel)
        provider = KubernetesRuntimeProvider()

        with pytest.raises(asyncio.CancelledError):
            await provider._get_configuration()

    async def test_configuration_already_set_inside_the_lock_is_returned(self) -> None:
        """Double-checked locking: a racer winning inside the lock is honoured."""
        provider = KubernetesRuntimeProvider()
        sentinel = object()

        class _RacingLock:
            async def __aenter__(self) -> Self:
                provider._configuration = sentinel
                return self

            async def __aexit__(self, *exc: object) -> bool:
                return False

        provider._client_lock = _RacingLock()  # type: ignore[assignment]

        assert await provider._get_configuration() is sentinel


class TestClientConstruction:
    async def test_get_core_builds_the_plain_client_once(self, monkeypatch: pytest.MonkeyPatch) -> None:
        provider = KubernetesRuntimeProvider()
        configuration = object()
        provider._get_configuration = AsyncMock(return_value=configuration)  # type: ignore[method-assign]
        api_client = object()
        core_api = object()
        monkeypatch.setattr(k8s_mod.k8s_client, "ApiClient", MagicMock(return_value=api_client))
        monkeypatch.setattr(k8s_mod.k8s_client, "CoreV1Api", MagicMock(return_value=core_api))

        assert await provider._get_core() is core_api
        assert provider._api_client is api_client
        assert await provider._get_core() is core_api  # cached

    async def test_get_ws_core_builds_the_ws_client_once(self, monkeypatch: pytest.MonkeyPatch) -> None:
        provider = KubernetesRuntimeProvider()
        configuration = object()
        provider._get_configuration = AsyncMock(return_value=configuration)  # type: ignore[method-assign]
        ws_api_client = object()
        ws_core = object()
        monkeypatch.setattr(k8s_mod, "WsApiClient", MagicMock(return_value=ws_api_client))
        monkeypatch.setattr(k8s_mod.k8s_client, "CoreV1Api", MagicMock(return_value=ws_core))

        assert await provider._get_ws_core() is ws_core
        assert provider._ws_api_client is ws_api_client
        assert await provider._get_ws_core() is ws_core  # cached

    async def test_get_core_returns_a_client_set_by_a_racer_inside_the_lock(self) -> None:
        provider = KubernetesRuntimeProvider()
        provider._get_configuration = AsyncMock(return_value=object())  # type: ignore[method-assign]
        sentinel = object()

        class _RacingLock:
            async def __aenter__(self) -> Self:
                provider._core_api = sentinel
                return self

            async def __aexit__(self, *exc: object) -> bool:
                return False

        provider._client_lock = _RacingLock()  # type: ignore[assignment]

        assert await provider._get_core() is sentinel

    async def test_get_ws_core_returns_a_client_set_by_a_racer_inside_the_lock(self) -> None:
        provider = KubernetesRuntimeProvider()
        provider._get_configuration = AsyncMock(return_value=object())  # type: ignore[method-assign]
        sentinel = object()

        class _RacingLock:
            async def __aenter__(self) -> Self:
                provider._ws_core_api = sentinel
                return self

            async def __aexit__(self, *exc: object) -> bool:
                return False

        provider._client_lock = _RacingLock()  # type: ignore[assignment]

        assert await provider._get_ws_core() is sentinel


class TestSpecMappingArms:
    def test_non_int_memory_falls_back_to_the_default(self) -> None:
        assert KubernetesRuntimeProvider._resolve_memory_mb("abc") == k8s_mod._DEFAULT_MEMORY_MB
        assert KubernetesRuntimeProvider._resolve_memory_mb(None) == k8s_mod._DEFAULT_MEMORY_MB

    def test_control_char_env_entry_is_skipped_with_the_rest_kept(self) -> None:
        env = KubernetesRuntimeProvider._build_container_env({"A": "line\nbreak", "B": "ok"})

        assert [e.name for e in env] == ["B"]

    def test_invalid_label_key_is_skipped_in_labels_but_kept_in_annotations(self) -> None:
        provider = KubernetesRuntimeProvider()

        labels, annotations = provider._build_metadata(
            _spec(workspace_metadata={"bad key!": "v", "modulo.run.id": "run-1"})
        )

        assert "bad key!" not in labels
        assert labels["modulo.run.id"] == "run-1"
        # Annotations carry the raw metadata (nothing is lossy there).
        assert annotations["bad key!"] == "v"

    async def test_allow_root_user_skips_the_non_root_stamp(self) -> None:
        core = AsyncMock()
        core.read_namespaced_pod.return_value = _pod("Running")
        provider = _provider(core=core)

        await provider.create_workspace(_spec(allow_root_user=True))

        body = core.create_namespaced_pod.await_args.kwargs["body"]
        security = body.spec.containers[0].security_context
        assert security.run_as_non_root is None
        assert security.run_as_user is None
        assert body.spec.security_context is None

    def test_command_with_only_invalid_env_names_is_left_untouched(self) -> None:
        command = KubernetesRuntimeProvider._command_with_environment(["echo", "hi"], {"BAD-NAME": "x"})

        assert command == ["echo", "hi"]


class TestPodPhaseAndNote:
    def test_terminated_state_note_includes_reason_and_exit_code(self) -> None:
        pod = SimpleNamespace(
            status=SimpleNamespace(
                phase="Failed",
                container_statuses=[
                    SimpleNamespace(
                        state=SimpleNamespace(
                            waiting=None,
                            terminated=SimpleNamespace(reason="OOMKilled", exit_code=137),
                        )
                    )
                ],
            )
        )

        phase, note = KubernetesRuntimeProvider._pod_phase_and_note(pod)

        assert phase == "failed"
        assert "OOMKilled" in note
        assert "137" in note

    def test_empty_waiting_reason_falls_through_to_terminated_note(self) -> None:
        pod = SimpleNamespace(
            status=SimpleNamespace(
                phase="Failed",
                container_statuses=[
                    SimpleNamespace(
                        state=SimpleNamespace(
                            waiting=SimpleNamespace(reason="", message=""),
                            terminated=SimpleNamespace(reason="Error", exit_code=1),
                        )
                    )
                ],
            )
        )

        phase, note = KubernetesRuntimeProvider._pod_phase_and_note(pod)

        assert phase == "failed"
        assert note.startswith("Error")

    def test_terminated_note_loop_continues_to_the_next_container_status(self) -> None:
        pod = SimpleNamespace(
            status=SimpleNamespace(
                phase="Failed",
                container_statuses=[
                    SimpleNamespace(
                        state=SimpleNamespace(
                            waiting=None,
                            terminated=SimpleNamespace(reason="", exit_code=None),
                        )
                    ),
                    SimpleNamespace(
                        state=SimpleNamespace(
                            waiting=None,
                            terminated=SimpleNamespace(reason="OOMKilled", exit_code=137),
                        )
                    ),
                ],
            )
        )

        _phase, note = KubernetesRuntimeProvider._pod_phase_and_note(pod)

        assert "OOMKilled" in note


class TestCreateAndDeleteArms:
    async def test_create_cancellation_propagates(self) -> None:
        core = AsyncMock()
        core.create_namespaced_pod.side_effect = asyncio.CancelledError
        provider = _provider(core=core)

        with pytest.raises(asyncio.CancelledError):
            await provider.create_workspace(_spec())

    async def test_pod_disappearing_mid_provision_raises(self) -> None:
        core = AsyncMock()
        core.read_namespaced_pod.return_value = None
        provider = _provider(core=core)

        with pytest.raises(RuntimeProviderError, match="disappeared"):
            await provider.create_workspace(_spec())

        core.delete_namespaced_pod.assert_awaited_once()

    async def test_read_pod_cancellation_propagates(self) -> None:
        core = AsyncMock()
        core.read_namespaced_pod.side_effect = asyncio.CancelledError
        provider = _provider(core=core)

        with pytest.raises(asyncio.CancelledError):
            await provider._read_pod("modulo-ws-abc")

    async def test_delete_best_effort_swallows_404_and_logs_other_failures(self) -> None:
        core = AsyncMock()
        provider = _provider(core=core)

        core.delete_namespaced_pod.side_effect = ApiException(status=404, reason="Not Found")
        await provider._delete_pod_best_effort("modulo-ws-gone")

        core.delete_namespaced_pod.side_effect = ApiException(status=500, reason="Boom")
        await provider._delete_pod_best_effort("modulo-ws-abc")

        core.delete_namespaced_pod.side_effect = ConnectionRefusedError("apiserver down")
        await provider._delete_pod_best_effort("modulo-ws-abc")

    async def test_delete_best_effort_cancellation_propagates(self) -> None:
        core = AsyncMock()
        core.delete_namespaced_pod.side_effect = asyncio.CancelledError
        provider = _provider(core=core)

        with pytest.raises(asyncio.CancelledError):
            await provider._delete_pod_best_effort("modulo-ws-abc")


class TestExecArms:
    async def test_exec_command_cancellation_propagates(self) -> None:
        provider = _provider(ws_core=_FakeWsCore(_FakeWs([], receive_exc=asyncio.CancelledError())))

        with pytest.raises(asyncio.CancelledError):
            await provider.exec_command("modulo-ws-abc", ["sh"])

    async def test_exec_command_unexpected_frame_failure_is_backend_unreachable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        provider = _provider(ws_core=_FakeWsCore(_FakeWs([])))

        async def _boom(ws: Any, state: Any) -> Any:
            raise RuntimeError("frame reader exploded")
            yield  # pragma: no cover - unreachable, marks an async generator

        monkeypatch.setattr(provider, "_read_exec_frames", _boom)

        with pytest.raises(BackendUnreachableError, match="frame reader exploded"):
            await provider.exec_command("modulo-ws-abc", ["sh"])

    async def test_open_exec_cancellation_propagates(self) -> None:
        provider = _provider(ws_core=_FakeWsCore(exc=asyncio.CancelledError()))

        with pytest.raises(asyncio.CancelledError):
            await provider._open_exec("modulo-ws-abc", ["sh"])

    async def test_open_exec_non_404_failure_is_backend_unreachable(self) -> None:
        provider = _provider(ws_core=_FakeWsCore(exc=ApiException(status=500, reason="Boom")))

        with pytest.raises(BackendUnreachableError, match="unreachable"):
            await provider._open_exec("modulo-ws-abc", ["sh"])

    async def test_read_exec_frames_control_frames_unknown_channel_and_error(self) -> None:
        provider = _provider()
        state = k8s_mod._ExecStreamState()
        messages = [
            SimpleNamespace(type=WSMsgType.PING, data=None),
            SimpleNamespace(type=WSMsgType.TEXT, data=""),
            SimpleNamespace(type=WSMsgType.BINARY, data=bytes([1])),
            _frame(9, "unknown-channel"),
            SimpleNamespace(type=WSMsgType.ERROR, data=RuntimeError("transport error")),
        ]
        ws = _FakeWs(messages)
        seen: list[tuple[str, str]] = []

        async for item in provider._read_exec_frames(ws, state):
            seen.append(item)

        assert seen == [("stderr", "unknown-channel")]
        assert state.error is not None
        assert "transport error" in state.error

    async def test_close_ws_awaits_a_coroutine_close(self) -> None:
        provider = _provider()
        ws = _CoroutineCloseWs()

        await provider._close_ws(ws)

        assert ws.closed is True

    async def test_close_ws_swallows_a_close_failure(self) -> None:
        provider = _provider()

        await provider._close_ws(_CoroutineCloseWs(exc=RuntimeError("close boom")))  # must not raise

    async def test_close_ws_cancellation_propagates(self) -> None:
        provider = _provider()

        with pytest.raises(asyncio.CancelledError):
            await provider._close_ws(_CoroutineCloseWs(exc=asyncio.CancelledError()))


class TestDestroyByRefArms:
    async def test_read_cancellation_propagates(self) -> None:
        core = AsyncMock()
        core.read_namespaced_pod.side_effect = asyncio.CancelledError
        provider = _provider(core=core)

        with pytest.raises(asyncio.CancelledError):
            await provider.destroy_workspace_by_ref("modulo-ws-abc")

    async def test_delete_cancellation_propagates(self) -> None:
        core = AsyncMock()
        core.read_namespaced_pod.return_value = _pod("Running")
        core.delete_namespaced_pod.side_effect = asyncio.CancelledError
        provider = _provider(core=core)

        with pytest.raises(asyncio.CancelledError):
            await provider.destroy_workspace_by_ref("modulo-ws-abc")

    async def test_delete_404_is_idempotent_success(self) -> None:
        core = AsyncMock()
        core.read_namespaced_pod.return_value = _pod("Running")
        core.delete_namespaced_pod.side_effect = ApiException(status=404, reason="Not Found")
        provider = _provider(core=core)
        provider._workspaces.add("modulo-ws-abc")

        assert await provider.destroy_workspace_by_ref("modulo-ws-abc") is True
        assert "modulo-ws-abc" not in provider._workspaces

    async def test_delete_generic_failure_returns_false(self) -> None:
        core = AsyncMock()
        core.read_namespaced_pod.return_value = _pod("Running")
        core.delete_namespaced_pod.side_effect = ConnectionRefusedError("apiserver down")
        provider = _provider(core=core)

        assert await provider.destroy_workspace_by_ref("modulo-ws-abc") is False


class TestListWorkspacePodsArms:
    async def test_skips_no_metadata_nameless_and_unparseable_age(self) -> None:
        core = AsyncMock()
        identity = KubernetesRuntimeProvider._deployment_identity()
        items = [
            SimpleNamespace(metadata=None),
            _listed_pod(
                "",
                labels={"modulo.created_at": "not-a-number"},
                annotations={k8s_mod._MACHINE_ANNOTATION: identity},
            ),
            _listed_pod(
                "modulo-ws-ok",
                labels={"modulo.created_at": "not-a-number"},
                annotations={k8s_mod._MACHINE_ANNOTATION: identity},
            ),
        ]
        core.list_namespaced_pod.return_value = SimpleNamespace(items=items)
        provider = _provider(core=core)

        entries = await provider.list_workspace_pods()

        assert [entry.ref for entry in entries] == ["modulo-ws-ok"]
        assert entries[0].created_age_s == 0.0


class TestStatusAndLogArms:
    async def test_status_cancellation_propagates(self) -> None:
        core = AsyncMock()
        core.read_namespaced_pod.side_effect = asyncio.CancelledError
        provider = _provider(core=core)

        with pytest.raises(asyncio.CancelledError):
            await provider.get_workspace_status("modulo-ws-abc")

    async def test_log_tail_cancellation_propagates(self) -> None:
        core = AsyncMock()
        core.read_namespaced_pod_log.side_effect = asyncio.CancelledError
        provider = _provider(core=core)

        with pytest.raises(asyncio.CancelledError):
            await provider.read_log_tail("modulo-ws-abc", max_bytes=5)


class TestIsolationArms:
    async def test_git_credentials_none_runs_the_none_script(self, monkeypatch: pytest.MonkeyPatch) -> None:
        provider = _provider()
        calls: list[list[str]] = []

        async def _fake_exec(provider_ref: str, command: list[str], *, cmd_timeout: int | None = None) -> ExecResult:
            calls.append(command)
            return ExecResult(exit_code=0, stdout="", stderr="", duration_ms=1)

        monkeypatch.setattr(provider, "exec_command", _fake_exec)

        status = await provider.apply_isolation(
            "modulo-ws-abc",
            _spec(),
            IsolationPolicy(git_credentials="none", command_timeout=5.0),
        )

        assert status is None
        assert len(calls) == 1

    async def test_guard_install_detail_is_logged(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        provider = _provider()

        async def _fake_exec(provider_ref: str, command: list[str], *, cmd_timeout: int | None = None) -> ExecResult:
            return ExecResult(exit_code=0, stdout="", stderr="", duration_ms=1)

        monkeypatch.setattr(provider, "exec_command", _fake_exec)
        monkeypatch.setattr(
            "modulo.core.pipeline_engine.sandbox_policy.install_gh_pr_guard_via_exec",
            AsyncMock(return_value=SimpleNamespace(status="installed", detail="guard note")),
        )

        with caplog.at_level("WARNING"):
            status = await provider.apply_isolation(
                "modulo-ws-abc",
                _spec(),
                IsolationPolicy(single_pr_per_run=True, command_timeout=5.0),
            )

        assert status == "installed"
        assert "guard note" in caplog.text


class TestCloseArms:
    async def test_close_destroys_tracked_workspaces_and_closes_clients(self, monkeypatch: pytest.MonkeyPatch) -> None:
        provider = _provider()
        provider._workspaces.add("modulo-ws-abc")
        destroy = AsyncMock()
        monkeypatch.setattr(provider, "destroy_workspace", destroy)
        api_client = SimpleNamespace(close=AsyncMock())
        ws_api_client = SimpleNamespace(close=AsyncMock())
        provider._api_client = api_client
        provider._ws_api_client = ws_api_client

        await provider.close()

        destroy.assert_awaited_once_with("modulo-ws-abc")
        api_client.close.assert_awaited_once()
        ws_api_client.close.assert_awaited_once()
        assert provider._api_client is None
        assert provider._ws_api_client is None

    async def test_close_cancellation_from_destroy_propagates(self, monkeypatch: pytest.MonkeyPatch) -> None:
        provider = _provider()
        provider._workspaces.add("modulo-ws-abc")
        monkeypatch.setattr(provider, "destroy_workspace", AsyncMock(side_effect=asyncio.CancelledError))

        with pytest.raises(asyncio.CancelledError):
            await provider.close()

    async def test_close_times_out_destroy_and_force_drops_it(self, monkeypatch: pytest.MonkeyPatch) -> None:
        provider = _provider()
        provider._workspaces.add("modulo-ws-abc")
        monkeypatch.setattr(provider, "destroy_workspace", AsyncMock(side_effect=asyncio.TimeoutError))

        await provider.close()

        assert "modulo-ws-abc" not in provider._workspaces

    async def test_close_swallows_a_destroy_failure(self, monkeypatch: pytest.MonkeyPatch) -> None:
        provider = _provider()
        provider._workspaces.add("modulo-ws-abc")
        monkeypatch.setattr(provider, "destroy_workspace", AsyncMock(side_effect=RuntimeError("boom")))

        await provider.close()  # must not raise

    async def test_close_client_cancellation_propagates(self) -> None:
        provider = _provider()
        provider._api_client = SimpleNamespace(close=AsyncMock(side_effect=asyncio.CancelledError))

        with pytest.raises(asyncio.CancelledError):
            await provider.close()

    async def test_close_client_timeout_is_swallowed(self) -> None:
        provider = _provider()
        provider._api_client = SimpleNamespace(close=AsyncMock(side_effect=asyncio.TimeoutError))

        await provider.close()

        assert provider._api_client is None

    async def test_close_client_generic_failure_is_swallowed(self) -> None:
        provider = _provider()
        provider._ws_api_client = SimpleNamespace(close=AsyncMock(side_effect=RuntimeError("boom")))

        await provider.close()

        assert provider._ws_api_client is None
