"""Kubernetes runtime-provider conformance against a REAL cluster (FAR-1053).

Runs the shared checks from ``_conformance.py`` (the ADR 040 ABC contract:
create/exec/stream/log-tail/destroy/status) plus the Kubernetes-specific
clauses the portable suite cannot express (pod Ready condition, PSA
``restricted`` admission, typed egress refusal, no post-destroy log
retention), plus the NEGATIVE suite:

1. a deliberately broken adapter (fabricates exit-code 0 for every command)
   must FAIL the suite - that is what proves the gate has teeth;
2. a kill-before-collect must be DETECTED (stream error, ``exit_code`` still
   ``None``) and must never produce a synthetic successful completion.

DESELECTED BY DEFAULT: the module marker plus the ``addopts`` clause in
``backend/pyproject.toml`` keep these tests out of every normal run - they
need a live cluster. A normal run shows them DESELECTED (never skipped and
counted green); the k8s-conformance workflow selects them explicitly:

    uv run pytest tests/conformance/ -m runtime_provider_conformance ...
"""

from __future__ import annotations

import asyncio
import uuid
from typing import TYPE_CHECKING

import pytest

from modulo.core.runtime_provider import (
    ExecResult,
    IsolationPolicy,
    ProviderCapabilityUnsupportedError,
)
from modulo.core.runtime_provider.k8s import KubernetesRuntimeProvider
from tests.conformance.runtime_provider._conformance import (
    assert_destroy_by_ref_idempotent,
    assert_exec_exit_zero,
    assert_exec_nonzero_exit_code_mapped,
    assert_log_tail_after_process_end,
    assert_stream_kill_before_collect_detected,
    assert_stream_ordered_with_clean_exit,
    assert_workspace_created_and_running,
    await_workspace_status,
    conformance_workspace_spec,
    destroy_workspace_quietly,
    get_registered_checks,
    run_conformance_checks,
)

if TYPE_CHECKING:
    from tests.conformance.runtime_provider.conftest import K8sConformanceCluster

pytestmark = pytest.mark.runtime_provider_conformance


# ── Shared ABC-conformance checks (thin wrappers; one contract clause each) ─


async def test_create_workspace_reaches_running(k8s_cluster: K8sConformanceCluster) -> None:
    """create_workspace returns a ref whose status reads ``running``."""
    await assert_workspace_created_and_running(k8s_cluster.provider)


async def test_exec_command_exit_zero(k8s_cluster: K8sConformanceCluster) -> None:
    """An exit-0 command reports exit_code 0 with stdout delivered."""
    await assert_exec_exit_zero(k8s_cluster.provider)


async def test_exec_command_nonzero_exit_is_mapped(k8s_cluster: K8sConformanceCluster) -> None:
    """A real non-zero exit maps to its own code - never fabricated as 0."""
    await assert_exec_nonzero_exit_code_mapped(k8s_cluster.provider)


async def test_exec_command_stream_ordered_with_clean_exit(k8s_cluster: K8sConformanceCluster) -> None:
    """Streaming delivers ordered per-stream chunks and a clean exit."""
    await assert_stream_ordered_with_clean_exit(k8s_cluster.provider)


async def test_stream_kill_before_collect_is_detected(k8s_cluster: K8sConformanceCluster) -> None:
    """Kill-before-collect surfaces an error and NEVER a synthetic success."""
    await assert_stream_kill_before_collect_detected(k8s_cluster.provider)


async def test_read_log_tail_after_process_end(k8s_cluster: K8sConformanceCluster) -> None:
    """read_log_tail works after an exec'd process ends: bounded raw bytes."""
    await assert_log_tail_after_process_end(k8s_cluster.provider)


async def test_destroy_workspace_by_ref_is_idempotent(k8s_cluster: K8sConformanceCluster) -> None:
    """First destroy confirms; the second (and a foreign ref) is a clean no-op."""
    await assert_destroy_by_ref_idempotent(k8s_cluster.provider)


# ── Kubernetes-substrate clauses (what the portable suite cannot express) ──


async def test_workspace_pod_reaches_ready_condition(k8s_cluster: K8sConformanceCluster) -> None:
    """The workspace pod is ADMITTED under PSA ``restricted`` and reports Ready=True.

    Admission is implicit proof: the session namespace enforces ``restricted``
    (see conftest), so a spec violating it would have been rejected at create
    time before this assertion could run.
    """
    provider = k8s_cluster.provider
    ref = await provider.create_workspace(conformance_workspace_spec())
    try:
        pod = await asyncio.wait_for(
            k8s_cluster.core.read_namespaced_pod(name=ref, namespace=k8s_cluster.namespace),
            timeout=30,
        )
        conditions = {c.type: c.status for c in (pod.status.conditions or [])}
        assert conditions.get("Ready") == "True", f"workspace pod {ref} is not Ready: {conditions}"
    finally:
        await destroy_workspace_quietly(provider, ref)


async def test_read_log_tail_after_destroy_is_empty(k8s_cluster: K8sConformanceCluster) -> None:
    """k8s retains pod logs only while the pod exists: post-destroy is empty, never an error."""
    provider = k8s_cluster.provider
    ref = await provider.create_workspace(conformance_workspace_spec())
    await provider.destroy_workspace_by_ref(ref)
    status = await await_workspace_status(provider, ref, "terminated")
    assert status == "terminated", f"pod {ref} must be gone before the post-destroy read, got {status!r}"
    tail = await provider.read_log_tail(ref, max_bytes=1024)
    assert not tail, f"a post-destroy log read must be empty on k8s (no retention window), got {tail!r}"


async def test_egress_none_refused_with_typed_error(k8s_cluster: K8sConformanceCluster) -> None:
    """Error-envelope clause: unenforceable egress fails TYPED, never a silent downgrade."""
    spec = conformance_workspace_spec()
    spec.egress_policy = "none"
    with pytest.raises(ProviderCapabilityUnsupportedError, match="NetworkPolicy"):
        await k8s_cluster.provider.create_workspace(spec)
    policy = IsolationPolicy(egress_policy="none")
    with pytest.raises(ProviderCapabilityUnsupportedError, match="NetworkPolicy"):
        await k8s_cluster.provider.apply_isolation("unused-ref", spec, policy)


async def test_get_workspace_status_unknown_ref_is_terminated(k8s_cluster: K8sConformanceCluster) -> None:
    """A ref this run never created reads ``terminated`` (the pod does not exist)."""
    ref = f"modulo-ws-{uuid.uuid4().hex[:16]}"
    status = await k8s_cluster.provider.get_workspace_status(ref)
    assert status == "terminated", f"an unknown ref must read as terminated, got {status!r}"


# ── Negative suite: the gate must have teeth in BOTH directions ────────────


class _FabricatesExitZeroAdapter(KubernetesRuntimeProvider):
    """Deliberately NON-conforming adapter, kept permanently in the suite (ADR 040).

    Breaks exactly one method: ``exec_command`` always reports ``exit_code=0``
    regardless of what the command actually did - the classic synthetic
    success. A suite without teeth would pass this adapter; the test below is
    what proves the gate can fail.
    """

    async def exec_command(
        self,
        provider_ref: str,
        command: list[str],
        *,
        cmd_timeout: int | None = None,
    ) -> ExecResult:
        result = await super().exec_command(provider_ref, command, cmd_timeout=cmd_timeout)
        return ExecResult(
            exit_code=0,
            stdout=result.stdout,
            stderr=result.stderr,
            duration_ms=result.duration_ms,
        )


async def test_broken_adapter_fails_the_suite(k8s_cluster: K8sConformanceCluster) -> None:
    """Negative suite (FAR-1053): a conforming provider passes; the broken one FAILS.

    Both directions are asserted so the runner itself is verified: a runner
    that blanket-fails trips the second assertion, and a runner that silently
    passes everything trips the first.
    """
    provider = k8s_cluster.provider

    conforming_failures = await run_conformance_checks(provider)
    assert not conforming_failures, (
        f"a conforming provider must pass every registered check, got: {conforming_failures}"
    )

    broken = _FabricatesExitZeroAdapter(namespace=k8s_cluster.namespace)
    try:
        broken_failures = await run_conformance_checks(broken)
    finally:
        await broken.close()
    failed_names = {entry.partition(":")[0] for entry in broken_failures}
    assert "exec_nonzero_exit_mapping" in failed_names, (
        "the suite must FAIL an adapter that fabricates exit-code 0 for every command "
        f"(that is what proves the gate is not vacuous); failures were: {broken_failures}"
    )
    assert "stream_ordered_clean_exit" not in failed_names, (
        "the runner must collect, not blanket-fail: the stream checks are untouched by the "
        f"broken exec_command and must pass on this adapter; failures were: {broken_failures}"
    )


def test_kubernetes_checks_are_registered() -> None:
    """The registry is populated at import - a silently empty suite would pass vacuously."""
    registered = get_registered_checks()
    assert "exec_nonzero_exit_mapping" in registered, f"registry missing the exec check: {registered}"
    assert "stream_kill_before_collect" in registered, f"registry missing the kill check: {registered}"
