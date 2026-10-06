"""Live-Kubernetes fixtures for the runtime-provider conformance suite (FAR-1053).

One session-scoped harness that:

- loads client configuration exactly the way the provider does - in-cluster
  when ``KUBERNETES_SERVICE_HOST`` is set, else the standard kubeconfig chain
  (``KUBECONFIG`` / ``~/.kube/config``);
- creates a uniquely-named namespace labelled ``modulo.conformance=true``
  (the cluster-wide sweep selector, so a crashed run's leftovers can still be
  deleted) and labelled with the Pod Security Admission ``restricted``
  standard - kind does not enforce Pod Security by default, so this label is
  where "workspace pods must pass restricted admission" is actually proven
  (see docs/deployment/k8s-conformance-parity.md);
- creates a uniquely-named ServiceAccount for the workspace pods and points
  ``MODULO_KUBERNETES_SERVICE_ACCOUNT`` at it, proving the provider honours
  that setting;
- builds the ``KubernetesRuntimeProvider`` bound to that namespace;
- tears everything down at session end: ``provider.close()`` (tracked pods +
  client close), then namespace deletion - the namespace removes anything a
  failed check left behind.

Nothing happens at import time: a normal (deselected) run never touches a
cluster, and a selected run without a usable kubeconfig fails loudly at
fixture setup instead of skipping.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import pytest
import pytest_asyncio
from kubernetes_asyncio import client as k8s_client
from kubernetes_asyncio import config as k8s_config
from kubernetes_asyncio.client import V1Namespace, V1ObjectMeta, V1ServiceAccount
from kubernetes_asyncio.client.exceptions import ApiException

from modulo.core.runtime_provider.k8s import KubernetesRuntimeProvider

_log = logging.getLogger(__name__)

_NAMESPACE_PREFIX = "modulo-conf-"
_SERVICE_ACCOUNT_PREFIX = "conf-sa-"
# Cluster-wide sweep selector: the workflow's always()-cleanup deletes every
# namespace carrying this label if a run dies before session teardown.
_CLEANUP_LABEL = "modulo.conformance"
# Pod Security Admission: kind ships with no enforcement, so the conformance
# namespace opts in explicitly (the "CI values" the parity doc calls out).
_ENFORCE_LABEL = "pod-security.kubernetes.io/enforce"
_ENFORCE_VALUE = "restricted"
_SETUP_TIMEOUT_S = 60
_TEARDOWN_POLL_BOUND_S = 120.0
_POLL_INTERVAL_S = 1.0


@dataclass(frozen=True)
class K8sConformanceCluster:
    """Everything one conformance session needs against a live cluster."""

    namespace: str
    service_account: str
    provider: KubernetesRuntimeProvider
    core: Any  # raw CoreV1Api - substrate-level assertions (pod conditions)


async def _await_namespace_gone(core: Any, namespace: str) -> bool:
    """True once the namespace reads 404 (deleted), False when the bound elapses."""
    deadline = time.monotonic() + _TEARDOWN_POLL_BOUND_S
    while True:
        try:
            await core.read_namespace(name=namespace)
        except ApiException as exc:
            if exc.status == 404:
                return True
            _log.warning("read_namespace(%s) failed during teardown (HTTP %s)", namespace, exc.status)
            return False
        if time.monotonic() >= deadline:
            return False
        await asyncio.sleep(_POLL_INTERVAL_S)


async def _delete_namespace_bounded(core: Any, namespace: str) -> None:
    """Delete the session namespace; RAISES when the API refuses.

    A leaked namespace on a managed cluster costs money until someone finds
    it, so an API-level delete failure must fail the session loudly - never a
    swallowed best-effort. A namespace that is merely slow to finish its
    finalizers logs a warning instead (Kubernetes will finish it on its own).
    """
    try:
        await asyncio.wait_for(core.delete_namespace(name=namespace), timeout=_SETUP_TIMEOUT_S)
    except Exception as exc:
        raise RuntimeError(
            f"conformance teardown could not delete namespace {namespace!r}: {type(exc).__name__}: {exc}"
        ) from exc
    if not await _await_namespace_gone(core, namespace):
        _log.warning(
            "conformance namespace %s still terminating after %ss (finalizers); it will finish on its own",
            namespace,
            _TEARDOWN_POLL_BOUND_S,
        )


@pytest_asyncio.fixture(scope="session")
async def k8s_cluster() -> AsyncIterator[K8sConformanceCluster]:
    """A live Kubernetes cluster harness: unique namespace + ServiceAccount + provider.

    Session-scoped: one namespace/ServiceAccount/provider pair for the whole
    conformance run, torn down when the session ends.
    """
    configuration = k8s_client.Configuration()
    if os.environ.get("KUBERNETES_SERVICE_HOST"):
        k8s_config.load_incluster_config(client_configuration=configuration)
    else:
        await k8s_config.load_kube_config(config_file=None, client_configuration=configuration)
    api_client = k8s_client.ApiClient(configuration=configuration)
    core = k8s_client.CoreV1Api(api_client=api_client)

    suffix = uuid.uuid4().hex[:8]
    namespace = f"{_NAMESPACE_PREFIX}{suffix}"
    service_account = f"{_SERVICE_ACCOUNT_PREFIX}{suffix}"
    monkeypatch = pytest.MonkeyPatch()
    provider: KubernetesRuntimeProvider | None = None
    namespace_created = False
    try:
        monkeypatch.setenv("MODULO_KUBERNETES_SERVICE_ACCOUNT", service_account)
        provider = KubernetesRuntimeProvider(namespace=namespace)
        labels = {
            _CLEANUP_LABEL: "true",
            _ENFORCE_LABEL: _ENFORCE_VALUE,
        }
        await asyncio.wait_for(
            core.create_namespace(V1Namespace(metadata=V1ObjectMeta(name=namespace, labels=labels))),
            timeout=_SETUP_TIMEOUT_S,
        )
        namespace_created = True
        await asyncio.wait_for(
            core.create_namespaced_service_account(
                namespace, V1ServiceAccount(metadata=V1ObjectMeta(name=service_account))
            ),
            timeout=_SETUP_TIMEOUT_S,
        )
        yield K8sConformanceCluster(
            namespace=namespace,
            service_account=service_account,
            provider=provider,
            core=core,
        )
    finally:
        try:
            if provider is not None:
                await provider.close()
            if namespace_created:
                await _delete_namespace_bounded(core, namespace)
        finally:
            try:
                await api_client.close()
            except Exception:
                _log.exception("failed to close the conformance API client")
            monkeypatch.undo()
