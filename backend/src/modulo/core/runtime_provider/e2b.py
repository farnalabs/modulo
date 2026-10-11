"""E2B RuntimeProvider — sandboxed execution environments via E2B."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shlex
import socket
import time
import urllib.request
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

from modulo.core.runtime_config.key_bridge import get_e2b_api_key
from modulo.core.runtime_provider import (
    ExecProcess,
    ExecResult,
    ExecStreamChunk,
    IsolationPolicy,
    RuntimeProvider,
    WorkspaceFileInfo,
    WorkspaceListEntry,
    WorkspaceMetrics,
    WorkspaceSpec,
)
from modulo.core.runtime_provider.log_tail import combine_log_entries

_log = logging.getLogger(__name__)

_DEFAULT_TEMPLATE_ID = "base"
_DEFAULT_CMD_TIMEOUT = 60
_REPO_CLONE_TIMEOUT = 120
_MAX_PROVISION_TIMEOUT = 120
_KILL_TIMEOUT = 30
# Background/streaming commands have no per-command deadline: the dispatch
# layer owns stall/deadline kills through ExecProcess.kill() (ADR 040 — the
# e2b ``timeout=0`` means "do not limit the command connection time").
_STREAM_CMD_TIMEOUT = 0
# Bound only the START of a streaming command (run() returns the handle once
# the start event arrives; a wedged control plane must not hang the caller —
# semgrep sandbox-commands-run-without-wait-for). Matches node_runner's
# background-command start bound (min(sandbox_timeout, 120)).
_STREAM_START_TIMEOUT = 120
# FAR-1050 R1/R6 log-tail primitive: the api.e2b.app HTTP log-tail call moved
# here from node_runner._fetch_sandbox_log_tail, which R6 deleted when the
# direct path was retired — this provider method is now the sole log probe.
# Entry window + fetch timeout + raw-payload fallback are pinned by the
# content-parity unit test.
_LOG_TAIL_ENTRY_LIMIT = 60
_LOG_TAIL_FETCH_TIMEOUT_S = 8
_LOG_TAIL_RAW_FALLBACK = 4000
# FAR-1050 R2a file-I/O primitives: bound on the native ``sandbox.files``
# calls (SDK ``request_timeout`` + outer ``asyncio.wait_for``), mirroring
# the house rule that every E2B SDK call sits under a wait_for.
_FILE_IO_TIMEOUT = 30
# FAR-1050 R6: bound on the ``sandbox.get_metrics`` poll (same house rule —
# every E2B SDK call sits under a wait_for). The watchdog applies its own
# outer bound on top; this inner bound keeps the provider primitive honest
# for callers that do not.
_METRICS_TIMEOUT = 10
# FAR-1050 R5: egress vocabulary accepted on ``WorkspaceSpec.egress_policy``.
# Two dialects feed this field (both reach the provider in production):
#   - the CANONICAL dialect stamped by the flag-ON dispatch
#     (``pipeline_engine.egress.resolve_egress`` output: ``None`` for the
#     provider default, ``"deny_all"``, ``"selected"``), and
#   - the WorkspaceSpec dialect stamped by the profile/bundled mappers
#     (``environment_profiles._build_workspace_spec`` /
#     ``runner_dispatch._workspace_spec_for_dispatch``): ``"none"`` for
#     deny_all, ``"outbound"`` for unrestricted.
# ``"default"`` is the node vocabulary's unrestricted value (resolve_egress
# maps it to ``None`` upstream; carried raw by direct callers).
_EGRESS_PERMISSIVE_POLICIES: frozenset[str] = frozenset({"", "default", "outbound"})
_EGRESS_RESTRICTIVE_POLICIES: frozenset[str] = frozenset({"deny_all", "selected", "none"})

# FAR-1275: identity-scoped enumeration. Reserved workspace-metadata keys
# stamped onto EVERY sandbox this provider creates — the E2B counterpart of
# Docker's ``_DEPLOYMENT_IDENTITY_LABEL`` / ``modulo.created_at`` container
# labels and Kubernetes' ``modulo.machine.id`` annotation. The identity key
# scopes :meth:`E2BRuntimeProvider.list_workspaces_by_identity` (two
# deployments sharing one E2B account never enumerate each other's
# sandboxes); the creation marker drives the orphan sweep's grace window and
# 24h max-lifetime backstop; the run-id key is what the sweep
# cross-references against ACTIVE runs.
_DEPLOYMENT_IDENTITY_ENV = "MODULO_RUNNER_MACHINE_ID"
_DEPLOYMENT_IDENTITY_METADATA_KEY = "modulo.machine.id"
_CREATED_AT_METADATA_KEY = "modulo.created_at"
_RUN_ID_METADATA_KEY = "modulo.run.id"
# Every E2B SDK call sits under an asyncio.wait_for (house rule): one bound
# per page fetch, plus a page cap so a pagination loop the control plane
# never terminates cannot run the sweep forever.
_LIST_PAGE_TIMEOUT = 30
_LIST_MAX_PAGES = 100


def _egress_allows_internet(policy: str | None) -> bool:
    """Map ``WorkspaceSpec.egress_policy`` onto the SDK's create boolean.

    Reference semantics — the legacy create path in ``node_runner``:

    .. code-block:: python

        allow_internet_access=(_egress_resolved.policy is None)

    i.e. a resolved restrictive policy denies internet, the provider default
    allows it (the E2B default). This mapping reproduces that over the two
    dialects the spec carries:

    - ``None`` / ``""`` / ``"default"`` / ``"outbound"`` -> ``True``
      (internet allowed — the E2B default, passed explicitly);
    - ``"deny_all"`` / ``"selected"`` / ``"none"`` -> ``False``;
    - anything unrecognised -> ``False`` (fail CLOSED: an unknown policy
      must never silently grant unrestricted egress — ADR 040's recorded
      defect class).

    ``"selected"`` denies ALL egress at this boolean level exactly like the
    legacy create (the host:port allowlist rides as sandbox metadata and is
    enforced in-sandbox, not by the SDK).
    """
    if policy is None:
        return True
    value = policy.strip().lower()
    if value in _EGRESS_PERMISSIVE_POLICIES:
        return True
    if value in _EGRESS_RESTRICTIVE_POLICIES:
        return False
    _log.warning(
        "E2B create_workspace: unrecognised egress_policy %r — failing closed (allow_internet_access=False)",
        policy,
    )
    return False


@dataclass(frozen=True)
class _StreamEnd:
    """Terminal queue marker for the E2B stream: healthy exit XOR stream error.

    Exactly one field is set (by construction in the waiter): a healthy
    stream end carries the real ``exit_code`` with ``error=None``; an
    engine/proxy drop carries the ``error`` description with
    ``exit_code=None`` — never a fabricated zero (ADR 040 "Streaming
    parity").
    """

    exit_code: int | None
    error: str | None


def _stream_error_message(exc: Exception) -> str:
    """Format an engine/proxy stream failure for ``ExecProcess.error`` (ADR 040)."""
    return f"{type(exc).__name__}: {str(exc)[:200]}"


def _metric_number(value: Any) -> float | None:
    """Coerce an SDK metric field to float, or ``None`` when unusable.

    bool is excluded (``True`` is not a measurement) and non-numeric /
    absent fields become ``None`` — the ABC carrier reports "not
    observable" rather than a number the killer would compare against.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _workspace_metrics(sample: Any) -> WorkspaceMetrics:
    """Map an SDK ``SandboxMetrics`` sample onto the ABC carrier (FAR-1050 R6).

    Sample semantics are preserved verbatim: ``cpu_used_pct`` stays a
    percentage, byte counters stay bytes, and ``cpu_count`` stays a
    core count (informational, never enforced as a threshold).
    """
    cpu_count = getattr(sample, "cpu_count", None)
    return WorkspaceMetrics(
        cpu_used_pct=_metric_number(getattr(sample, "cpu_used_pct", None)),
        cpu_count=cpu_count if isinstance(cpu_count, int) and not isinstance(cpu_count, bool) else None,
        mem_used=_metric_number(getattr(sample, "mem_used", None)),
        mem_total=_metric_number(getattr(sample, "mem_total", None)),
        disk_used=_metric_number(getattr(sample, "disk_used", None)),
        disk_total=_metric_number(getattr(sample, "disk_total", None)),
    )


def _workspace_list_entry(info: Any, wanted: str, now: float) -> WorkspaceListEntry | None:
    """Map one listed SDK sandbox onto the ABC carrier (FAR-1275), or skip it.

    Returns ``None`` for anything that must never reach the sweep's destroy
    path: a deployment-identity mismatch (the fail-closed client-side
    re-check — the server-side query narrows, this one guarantees) or a
    sandbox with no id. A malformed/absent creation marker yields
    ``created_age_s = 0.0`` ("cannot establish a grace age"), never a
    crash — the sweep then never treats it as a destroy candidate.
    """
    metadata = dict(getattr(info, "metadata", None) or {})
    if metadata.get(_DEPLOYMENT_IDENTITY_METADATA_KEY) != wanted:
        _log.warning(
            "list_workspaces_by_identity: skipping sandbox %s — deployment identity %r != %r",
            getattr(info, "sandbox_id", "<unknown>"),
            metadata.get(_DEPLOYMENT_IDENTITY_METADATA_KEY),
            wanted,
        )
        return None
    ref = str(getattr(info, "sandbox_id", "") or "")
    if not ref:
        _log.warning("list_workspaces_by_identity: skipping a sandbox with no sandbox_id")
        return None
    try:
        created = float(metadata.get(_CREATED_AT_METADATA_KEY, "0") or 0)
    except (TypeError, ValueError):
        created = 0.0
    return WorkspaceListEntry(
        provider_ref=ref,
        run_id=str(metadata.get(_RUN_ID_METADATA_KEY, "") or ""),
        created_age_s=max(0.0, now - created) if created else 0.0,
    )


class E2BRuntimeProvider(RuntimeProvider):
    """RuntimeProvider backed by E2B sandboxes.

    Each workspace is an E2B sandbox created from an EnvironmentProfile's
    ``image_ref`` (used as the E2B template ID).

    The E2B API key is resolved in this order:
    1. ``api_key`` argument passed to the constructor
    2. runtime override / ``MODULO_E2B_API_KEY`` env var
       (``key_bridge.get_e2b_api_key`` — the same bridge the provider-
       registration gate and node-runner enforcement check use, FAR-1159)

    The resolved key is passed to every ``AsyncSandbox.create`` call as
    ``api_key=`` (FAR-1171): without it the E2B SDK would resolve its own
    credential from the legacy ``E2B_API_KEY`` env var, and a runtime-key
    rotation via ``PUT /api/v1/admin/runtime-config`` would change the
    registration gate and the node-runner enforcement check but not the
    credential actually used to provision sandboxes.

    To store per-organisation keys securely, use ``FernetSecretsBackend`` at
    the service layer and pass the resolved key to the constructor.
    """

    provider_id = "e2b"

    def __init__(self, api_key: str | None = None) -> None:
        self._api_key = api_key or get_e2b_api_key()
        if not self._api_key:
            raise ValueError("E2B API key is required. Pass api_key= or set MODULO_E2B_API_KEY.")
        self._sandboxes: dict[str, Any] = {}
        # Strong references to in-flight stream waiter tasks (asyncio holds
        # only weak refs to tasks; RUF006). Discarded as each resolves.
        self._stream_waiters: set[asyncio.Task[None]] = set()

    # ------------------------------------------------------------------
    # Hub integration
    # ------------------------------------------------------------------

    def supports(self, profile: Any) -> bool:
        """Best-effort auto-detection — no longer consulted by the hub (FAR-587)."""
        hint = getattr(profile, "provider_hint", None) or ""
        if hint.lower() == "e2b":
            return True
        image_ref = getattr(profile, "image_ref", None) or ""
        return "e2b" in image_ref.lower()

    # ------------------------------------------------------------------
    # RuntimeProvider interface
    # ------------------------------------------------------------------

    async def create_workspace(self, spec: WorkspaceSpec) -> str:
        """Provision an E2B sandbox and optionally clone a repo.

        The template ID is taken from ``spec.image_ref``. If the first-class
        ``spec.repo_url`` field is set (FAR-595 — previously smuggled through
        the ``labels`` dict) the repository is cloned into ``/home/user/repo``
        and optionally checked out to ``spec.repo_ref``. ``spec.labels`` is
        env-var injection on Docker and is ignored by this provider.

        The spec's provider-neutral ``workspace_metadata`` maps to the E2B
        sandbox ``metadata`` (the SDK's tag-like carrier). On top of it the
        provider stamps its two RESERVED identity keys —
        ``modulo.machine.id`` (deployment identity) and ``modulo.created_at``
        — AFTER the operator metadata and always authoritative (the
        FAR-1493 ordering mirror of Docker's ``_build_workspace_labels`` /
        Kubernetes' ``_build_metadata``), because the identity-scoped
        enumeration (:meth:`list_workspaces_by_identity`, FAR-1275) and the
        orphan sweep's grace/max-lifetime ages key on exactly those two; an
        operator-supplied value must never hide a sandbox from this
        deployment's sweep or backdate it out of the grace window. The
        selected-mode egress allowlist rides in that carrier under the
        key the legacy create path uses (``egress_allowlist``, JSON-encoded)
        — the SDK has no native allowlist control, so sandbox metadata is
        the same tag the flag-OFF create stamps (FAR-1050 R5 parity).

        ``spec.egress_policy`` maps onto the SDK's ``allow_internet_access``
        boolean (FAR-1050 R5): a restrictive policy must never silently
        grant unrestricted internet (ADR 040). See
        :func:`_egress_allows_internet` for the exact mapping.

        ``spec.timeout_seconds`` is the SANDBOX LIFETIME and is forwarded to
        the SDK as ``timeout=`` (FAR-487/FAR-489 — see the inline note on
        ``lifetime_s`` below). It is a DIFFERENT concern from the
        provisioning wait bound on the ``asyncio.wait_for`` around
        ``AsyncSandbox.create``.
        """
        from e2b import AsyncSandbox

        template_id = spec.image_ref.strip() if spec.image_ref else _DEFAULT_TEMPLATE_ID

        # ------------------------------------------------------------------
        # 1. SANDBOX LIFETIME -> forwarded to the SDK as ``timeout=``.
        # ------------------------------------------------------------------
        # FAR-487/FAR-489: the dispatch (node_runner) stamps
        # ``spec.timeout_seconds`` as ``sandbox_timeout +
        # _SANDBOX_LIFETIME_GRACE_S`` so the platform endAt kill can never
        # preempt the runner's own timeout path. Before this fix the value
        # was read ONLY as the provisioning wait bound below and never
        # reached ``AsyncSandbox.create`` — so the SDK silently fell back to
        # its own ``SandboxBase.default_sandbox_timeout = 300``
        # (``e2b/sandbox/main.py:31``) and a node configured for a 1200s
        # lifetime got a sandbox that died at 300s.
        #
        # ``int()`` is MANDATORY (FAR-489): the SDK's attrs model does NOT
        # coerce a float, and E2B's Go server rejects ``"1320.0"`` with a 400
        # (int32 unmarshal), instantly failing every sandbox create.
        #
        # Range: the SDK documents a maximum keep-alive of 86_400s (Pro) /
        # 3_600s (Hobby). Both GraphValidator (``SANDBOX_TIMEOUT_EXCEEDS_E2B_
        # CAP`` at 3300, FAR-511) and the dispatch-time cap in
        # ``bundled_runner.runner_dispatch`` reject a sandbox lifetime above
        # 3300 BEFORE this code runs, so the forwarded value is always in
        # range. Deliberately NOT clamped here: clamping would silently
        # shorten the lifetime below the command timeout and resurrect the
        # FAR-487 defect this fix exists to prevent — an out-of-range value
        # must fail loudly at create time instead.
        lifetime_s = int(spec.timeout_seconds) if spec.timeout_seconds and spec.timeout_seconds > 0 else None

        # ------------------------------------------------------------------
        # 2. PROVISIONING WAIT BOUND -> bounds the create CALL, not the sandbox.
        # ------------------------------------------------------------------
        # Deliberately kept at the pre-forwarding expression
        # (``spec.timeout_seconds or _MAX_PROVISION_TIMEOUT``) rather than
        # capped at ``_MAX_PROVISION_TIMEOUT``: the dispatch wraps this very
        # call in its own authoritative bound
        # ``min(sandbox_timeout, SANDBOX_PROVISIONING_TIMEOUT_SECONDS)``
        # (node_runner's ``asyncio.wait_for(...create_workspace...)``), and
        # the lifetime is STRICTLY GREATER than ``sandbox_timeout`` — so this
        # inner bound can never preempt the dispatch's settings-driven
        # watchdog, which raises ``SandboxQueueTimeoutError`` and logs
        # ``sandbox_agent.provisioning_watchdog_fired``. Capping the inner
        # bound at ``_MAX_PROVISION_TIMEOUT`` (120) would preempt that
        # watchdog whenever an operator raises
        # ``SANDBOX_PROVISIONING_TIMEOUT_SECONDS`` above 120 (the field allows
        # up to 600), silently swapping the error class for a plain
        # ``RuntimeError``. For the direct callers that have no outer bound
        # (the environment-profile SSE test path, ``runner_dispatch``) the
        # bound is unchanged from before this fix: still finite, still
        # ``_MAX_PROVISION_TIMEOUT`` when no lifetime is supplied.
        provision_wait_s = spec.timeout_seconds or _MAX_PROVISION_TIMEOUT

        # FAR-1171: pass the resolved key explicitly — the SDK's ConnectionConfig
        # only falls back to the E2B_API_KEY env var when api_key is falsy, and
        # ``__init__`` guarantees ``self._api_key`` is non-empty (fail-closed).
        create_kwargs: dict[str, Any] = {"template": template_id, "api_key": self._api_key}
        if lifetime_s is None:
            # Defensive only: every production caller stamps a positive
            # lifetime (node_runner, runner_dispatch, environment_profiles).
            # Falling back to the SDK's 300s default is the exact defect this
            # fix removes, so make it loud instead of silent.
            _log.warning(
                "E2B create_workspace: spec.timeout_seconds=%r is not a positive lifetime — "
                "not forwarding it; the SDK's 300s default_sandbox_timeout applies "
                "(no FAR-487 strictly-greater-than-the-command guarantee)",
                spec.timeout_seconds,
            )
        else:
            create_kwargs["timeout"] = lifetime_s

        # FAR-1050 R5: carry spec.egress_policy into the create call. The
        # permissive side is passed explicitly too (never left to the SDK
        # default) so the posture of every create is visible at the call site.
        create_kwargs["allow_internet_access"] = _egress_allows_internet(spec.egress_policy)
        # Legacy-key parity: ``egress_allowlist`` / ``resource_limits``
        # are exactly the keys the flag-OFF create stamps as metadata.
        workspace_metadata = dict(spec.workspace_metadata or {})
        # Reserved identity keys — assigned AFTER the operator metadata and
        # always authoritative (FAR-1493 ordering, mirrored from Docker's
        # ``_build_workspace_labels`` / Kubernetes' ``_build_metadata``): an
        # operator-supplied ``modulo.machine.id`` would hide this sandbox
        # from the identity-scoped enumeration (FAR-1275), and a backdated
        # or future ``modulo.created_at`` would defeat the sweep's 5-minute
        # grace window and 24-hour max-lifetime backstop. Non-reserved keys
        # flow through unchanged. Stamped on EVERY create — not only when
        # the spec carries metadata — so the enumeration filter always has
        # something to match.
        workspace_metadata[_DEPLOYMENT_IDENTITY_METADATA_KEY] = self._deployment_identity()
        workspace_metadata[_CREATED_AT_METADATA_KEY] = str(int(time.time()))
        create_kwargs["metadata"] = workspace_metadata

        try:
            sandbox = await asyncio.wait_for(
                AsyncSandbox.create(**create_kwargs),
                timeout=provision_wait_s,
            )
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            raise RuntimeError(
                f"Timed out after {provision_wait_s}s provisioning E2B sandbox with template {template_id!r}"
            ) from None
        except Exception as exc:
            _log.exception("Failed to create E2B sandbox with template %s", template_id)
            raise RuntimeError(f"Failed to create E2B sandbox with template {template_id!r}: {exc}") from exc

        repo_url = spec.repo_url
        if repo_url:
            try:
                await self._clone_repo(sandbox, repo_url, spec.repo_ref)
            except asyncio.CancelledError:
                await self._kill_sandbox_best_effort(sandbox, "cancellation cleanup")
                raise
            except Exception:
                await self._kill_sandbox_best_effort(sandbox, "clone-failure cleanup")
                raise

        self._sandboxes[sandbox.sandbox_id] = sandbox

        return str(sandbox.sandbox_id)

    async def exec_command(
        self,
        provider_ref: str,
        command: list[str],
        *,
        cmd_timeout: int | None = None,
    ) -> ExecResult:
        """Execute a shell command inside the sandbox.

        The command is run via the E2B commands API. *timeout* is in seconds.
        """
        sandbox = self._get_sandbox(provider_ref)
        cmd_str = " ".join(shlex.quote(c) for c in command)
        effective_timeout = cmd_timeout if cmd_timeout is not None else _DEFAULT_CMD_TIMEOUT

        start = time.monotonic()
        try:
            proc = await asyncio.wait_for(
                sandbox.commands.run(cmd_str, timeout=effective_timeout),
                timeout=effective_timeout,
            )
            duration = int((time.monotonic() - start) * 1000)
            return ExecResult(
                exit_code=getattr(proc, "exit_code", -1),
                stdout=getattr(proc, "stdout", "") or "",
                stderr=getattr(proc, "stderr", "") or "",
                duration_ms=duration,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            duration = int((time.monotonic() - start) * 1000)
            _log.exception("exec_command failed in sandbox %s", provider_ref)
            return ExecResult(
                exit_code=-1,
                stdout="",
                stderr=f"Command execution failed: {exc}",
                duration_ms=duration,
            )

    async def destroy_workspace(self, provider_ref: str) -> None:
        """Kill the sandbox and release all resources.

        Best-effort: if the kill request fails (e.g. sandbox already
        terminated) the error is logged and swallowed.
        """
        sandbox = self._sandboxes.pop(provider_ref, None)
        if sandbox is not None:
            await self._kill_sandbox_best_effort(sandbox, "destroy cleanup")

    async def destroy_workspace_by_ref(self, provider_ref: str) -> bool:
        """Kill an E2B sandbox given only its sandbox id (ADR 040 primitive).

        Reconnects through ``AsyncSandbox.connect(sandbox_id)`` — the SDK's
        connect-by-id classmethod — rather than consulting
        ``self._sandboxes``, so reclamation works after a process restart
        or from another process (never-tracked-but-live refs included).

        Contract (see :meth:`RuntimeProvider.destroy_workspace_by_ref`):
          - already-gone / foreign ref → ``NotFoundException`` from the
            SDK → idempotent success (``True``), no error;
          - a tracked local handle is dropped only on confirmed-gone —
            on an unconfirmed kill it is kept so ``close()`` can retry;
          - connect/kill failures are logged and swallowed, returning
            ``False`` (the destroy could not be confirmed) — the two-phase
            ``destroy_intent`` / ``confirmed`` marker stays the CALLER's
            concern (delivered in a later slice).
        """
        from e2b import AsyncSandbox
        from e2b.exceptions import NotFoundException

        try:
            sandbox = await AsyncSandbox.connect(provider_ref, api_key=self._api_key)
        except asyncio.CancelledError:
            raise
        except NotFoundException:
            # Already destroyed (or a foreign ref): idempotent no-op success.
            self._sandboxes.pop(provider_ref, None)
            _log.debug("destroy_workspace_by_ref: sandbox %s already gone", provider_ref)
            return True
        except Exception:
            _log.exception(
                "destroy_workspace_by_ref: failed to reconnect to sandbox %s",
                provider_ref,
            )
            return False

        killed = await self._kill_sandbox_best_effort(sandbox, "destroy_workspace_by_ref")
        if killed:
            self._sandboxes.pop(provider_ref, None)
        return killed

    async def read_log_tail(self, provider_ref: str, *, max_bytes: int) -> bytes:
        """Read the E2B logs-endpoint tail for *provider_ref* (ADR 040 primitive).

        FAR-1050 R1/R6: this is the ``api.e2b.app`` HTTP log-tail call moved
        out of ``node_runner._fetch_sandbox_log_tail`` — R6 deleted that
        legacy helper when it retired the direct path, so this method is now
        the sole log probe. Fetch and parse retain its exact semantics
        (preferred-level reordering, the 60-entry window, the ``[-max_bytes:]``
        final bound, the ``min(4000, max_bytes)`` raw-payload fallback); the
        content-parity unit test pins the output over the same payloads.

        Contract (T6): never raises — invalid ref, missing key, network
        failure and parse failure all yield ``b""`` (or the raw fallback for
        parse-level failures), so callers keep the legacy "never raises /
        empty on no key" behaviour. Key resolution keeps the legacy fallback
        chain: runtime override / ``MODULO_E2B_API_KEY`` bridge, then the
        legacy ``E2B_API_KEY`` env var, then the constructor-held key.
        """
        if not isinstance(provider_ref, str) or not provider_ref:
            return b""
        api_key = get_e2b_api_key() or os.environ.get("E2B_API_KEY") or self._api_key
        if not api_key:
            return b""

        def _fetch_bytes() -> bytes:
            _req = urllib.request.Request(
                f"https://api.e2b.app/sandboxes/{provider_ref}/logs?limit={_LOG_TAIL_ENTRY_LIMIT}",
                headers={"X-API-KEY": api_key, "Accept": "application/json"},
            )
            # URL is a hard-coded https endpoint, not caller-controlled.
            with urllib.request.urlopen(_req, timeout=_LOG_TAIL_FETCH_TIMEOUT_S) as _resp:  # noqa: S310  # nosec B310
                return bytes(_resp.read())

        try:
            raw = (await asyncio.to_thread(_fetch_bytes)).decode("utf-8", errors="replace")
        except Exception:
            return b""
        try:
            payload = json.loads(raw)
            entries = payload.get("logEntries") if isinstance(payload, dict) else payload
            if not isinstance(entries, list):
                return raw[: min(_LOG_TAIL_RAW_FALLBACK, max_bytes)].encode("utf-8", errors="replace")
            combined = combine_log_entries(entries, _LOG_TAIL_ENTRY_LIMIT)
            return "\n".join(combined)[-max_bytes:].encode("utf-8", errors="replace")
        except Exception:
            return raw[: min(_LOG_TAIL_RAW_FALLBACK, max_bytes)].encode("utf-8", errors="replace")

    async def apply_isolation(
        self,
        provider_ref: str,
        spec: WorkspaceSpec,
        policy: IsolationPolicy,
    ) -> str | None:
        """Run the sandbox-policy enforcement scripts in the sandbox (FAR-1050 R3).

        ADR 040 ``apply_isolation``: wraps the existing
        ``sandbox_policy.apply_sandbox_policy`` — the same script builders,
        the same step order (git-credential scope -> egress allowlist ->
        FAR-1264 gh guard -> read-only seal), the same ``user=root``, and the
        same enforcement-critical-raise vs best-effort split — so the
        flag-ON primitive and the flag-OFF engine invocation emit identical
        enforcement for a fixed policy (pinned by the R3 parity unit test).

        FAR-1315: returns the one-PR guard's INSTALL STATUS
        (``installed``/``pre_planted``/``absent``/``failed``, or ``None`` when
        the flag did not arm the guard). The dispatch ``finally`` threads it
        into ``settle_run_pr_guard`` so a receipt probe that ran against a
        path no shim ever wrote (no ``gh`` / failed install) is treated as
        UNKNOWN rather than "confirmed no create".

        The sandbox is taken from this instance's tracked handles when the
        provider created the workspace (the R4 dispatch shape); otherwise it
        is reconnected by ref via ``AsyncSandbox.connect`` — the same by-ref
        pattern as :meth:`destroy_workspace_by_ref` — so the R3 flag-ON
        engine path can enforce a workspace the legacy direct path
        provisioned. ``spec`` carries workspace attribution for callers; the
        enforcement scripts target the fixed ``/home/user`` workspace and do
        not read it. The FAR-1264 one-PR-per-run ``gh`` guard is gated by
        ``policy.single_pr_per_run`` (FAR-1273: the typed ``IsolationPolicy``
        is the SINGLE carrier — no ``workspace_metadata`` key), and
        ``spec.run_id`` scopes the guard's claim marker so a marker can never
        leak across runs. A policy built without the flag installs no guard,
        so every existing caller and the R3 parity test are unaffected.
        ``policy.guard_owner`` (FAR-1315) threads the claiming node's identity
        into the run ledger so a SECOND flagged node of the same run gets a
        pre-planted refusal instead of a fresh claimable marker.
        """
        # Lazy import (house convention): sandbox_policy is dependency-free,
        # but importing it pulls the pipeline_engine package __init__ — the
        # engine process already has it loaded when this runs.
        from modulo.core.pipeline_engine.sandbox_policy import apply_sandbox_policy

        sandbox = await self._resolve_sandbox(provider_ref, "apply isolation")
        _run_scope = str(spec.run_id) if spec.run_id is not None else None
        return await apply_sandbox_policy(
            sandbox,
            read_only=policy.read_only,
            git_credentials=policy.git_credentials,
            egress_policy=policy.egress_policy,
            egress_allowlist=policy.egress_allowlist,
            allowed_hosts=policy.allowed_hosts,
            command_timeout=policy.command_timeout,
            single_pr_per_run=policy.single_pr_per_run,
            run_scope=_run_scope,
            guard_owner=policy.guard_owner,
        )

    # ------------------------------------------------------------------
    # File-I/O primitives (FAR-1050 R2a) — native SDK overrides
    # ------------------------------------------------------------------
    #
    # These override the ABC's exec-based defaults with E2B's native
    # ``sandbox.files`` API: no shell round-trip, no base64, binary-safe by
    # construction. Handle resolution mirrors :meth:`apply_isolation`
    # (tracked handle first, otherwise reconnect by ref).

    async def read_file(self, provider_ref: str, path: str) -> bytes:
        """Read a file from the sandbox via the native files API (FAR-1050 R2a)."""
        sandbox = await self._resolve_sandbox(provider_ref, "read_file")
        data = await asyncio.wait_for(
            sandbox.files.read(path, format="bytes", request_timeout=_FILE_IO_TIMEOUT),
            timeout=_FILE_IO_TIMEOUT,
        )
        return bytes(data)

    async def write_file(self, provider_ref: str, path: str, data: bytes) -> None:
        """Write *data* to a sandbox file via the native files API (FAR-1050 R2a).

        The SDK creates missing parent directories, matching the ABC
        default's ``mkdir -p`` behaviour.
        """
        sandbox = await self._resolve_sandbox(provider_ref, "write_file")
        await asyncio.wait_for(
            sandbox.files.write(path, data, request_timeout=_FILE_IO_TIMEOUT),
            timeout=_FILE_IO_TIMEOUT,
        )

    async def list_files(self, provider_ref: str, path: str) -> list[str]:
        """List a sandbox directory via the native files API (FAR-1050 R2a).

        Returns full child paths sorted by path — the same contract as the
        ABC's exec-based default (``entry.path`` is what the SDK reports).
        """
        sandbox = await self._resolve_sandbox(provider_ref, "list_files")
        entries = await asyncio.wait_for(
            sandbox.files.list(path, request_timeout=_FILE_IO_TIMEOUT),
            timeout=_FILE_IO_TIMEOUT,
        )
        return sorted(str(entry.path) for entry in entries if str(getattr(entry, "name", "")) not in {"", ".", ".."})

    async def get_info(self, provider_ref: str, path: str) -> WorkspaceFileInfo:
        """Stat a sandbox path via the native files API (FAR-1050 R2a)."""
        sandbox = await self._resolve_sandbox(provider_ref, "get_info")
        info = await asyncio.wait_for(
            sandbox.files.get_info(path, request_timeout=_FILE_IO_TIMEOUT),
            timeout=_FILE_IO_TIMEOUT,
        )
        file_type = getattr(info.type, "value", info.type)
        return WorkspaceFileInfo(
            path=path,
            size=int(getattr(info, "size", 0) or 0),
            is_dir=str(file_type) == "dir",
        )

    # ------------------------------------------------------------------
    # Resource-metrics primitive (FAR-1050 R6 — ADR 040 metrics gap)
    # ------------------------------------------------------------------

    async def get_metrics(self, provider_ref: str) -> list[WorkspaceMetrics]:
        """Poll the sandbox metrics endpoint (FAR-1050 R6 ABC primitive).

        E2B's ``sandbox.get_metrics()`` returns a list of SDK
        ``SandboxMetrics`` samples (oldest-first); each is mapped onto the
        provider-neutral :class:`WorkspaceMetrics` carrier, preserving the
        sample order so the caller can take the newest instantaneous
        reading. Handle resolution mirrors :meth:`apply_isolation` /
        the file-I/O primitives: the tracked handle when this provider
        created the workspace, otherwise reconnect by ref.

        Failures propagate (sandbox unreachable, poll timeout) — the
        resource-cap killer owns the fail-open, never this primitive.
        """
        sandbox = await self._resolve_sandbox(provider_ref, "get_metrics")
        samples = await asyncio.wait_for(sandbox.get_metrics(), timeout=_METRICS_TIMEOUT)
        return [_workspace_metrics(sample) for sample in samples]

    # ------------------------------------------------------------------
    # Identity-scoped enumeration primitive (FAR-1275)
    # ------------------------------------------------------------------

    async def list_workspaces_by_identity(self, identity: str) -> list[WorkspaceListEntry]:
        """List this deployment's running sandboxes (FAR-1275 ABC primitive).

        Uses the SDK's sandbox listing (``AsyncSandbox.list``) narrowed by a
        server-side metadata query on ``modulo.machine.id``, then RE-CHECKS
        the identity client-side on every entry: the server-side query
        narrows the listing, but only the exact client-side match can
        guarantee a foreign deployment's sandbox is never handed to the
        sweep's destroy path (two Modulo deployments can share one E2B
        account).

        Per the ABC contract: an empty/whitespace identity returns no
        entries without touching the API (fail closed — an unfiltered
        listing could only ever surface unattributed, foreign or
        pre-stamp sandboxes); listing failures PROPAGATE (the caller
        surfaces them as a reported sweep failure, never a silent empty
        list); each page fetch is bounded by ``_LIST_PAGE_TIMEOUT`` and the
        pagination loop by ``_LIST_MAX_PAGES`` (both logged, never
        swallowed).

        ``run_id`` and the creation age are read from the same reserved
        metadata keys :meth:`create_workspace` stamps, so an entry created
        before FAR-1275 (no identity key) never matches — it stays
        unreclaimable exactly as before, never wrongly reclaimable.
        """
        from e2b import AsyncSandbox, SandboxQuery

        wanted = identity.strip()
        if not wanted:
            # Fail closed: every sandbox this provider stamps carries a
            # non-empty identity, so an empty filter can only match
            # sandboxes we did NOT stamp (foreign or pre-stamp) — enumerate
            # nothing rather than those.
            _log.warning("list_workspaces_by_identity: empty deployment identity — returning no workspaces")
            return []

        paginator = AsyncSandbox.list(
            query=SandboxQuery(metadata={_DEPLOYMENT_IDENTITY_METADATA_KEY: wanted}),
            api_key=self._api_key,
            request_timeout=_LIST_PAGE_TIMEOUT,
        )
        now = time.time()
        entries: list[WorkspaceListEntry] = []
        pages = 0
        while paginator.has_next:
            pages += 1
            if pages > _LIST_MAX_PAGES:
                _log.warning(
                    "list_workspaces_by_identity: page cap %d reached — stopping enumeration",
                    _LIST_MAX_PAGES,
                )
                break
            page = await asyncio.wait_for(paginator.next_items(), timeout=_LIST_PAGE_TIMEOUT)
            for info in page:
                entry = _workspace_list_entry(info, wanted, now)
                if entry is not None:
                    entries.append(entry)
        return entries

    @staticmethod
    def _deployment_identity() -> str:
        """Machine deployment identity stamped into sandbox metadata (FAR-1275).

        ``MODULO_RUNNER_MACHINE_ID`` first, hostname fallback — the exact
        chain ``DockerRuntimeProvider._deployment_identity`` /
        ``KubernetesRuntimeProvider._deployment_identity`` and the
        reconciler's own ``deployment_identity()`` use, so the value this
        provider stamps at create time is the value the sweep filters on.
        """
        return os.environ.get(_DEPLOYMENT_IDENTITY_ENV) or socket.gethostname()

    async def _resolve_sandbox(self, provider_ref: str, operation: str) -> Any:
        """Return the sandbox handle for *provider_ref* (FAR-1050 R2a).

        The same handle-resolution shape as :meth:`apply_isolation`: use the
        tracked handle when this provider created the workspace, otherwise
        reconnect by ref via ``AsyncSandbox.connect`` — so the file-I/O
        primitives also work on a workspace the legacy direct path
        provisioned. A failed reconnect raises ``RuntimeError`` naming
        *operation*, the same failure class ``apply_isolation`` raises.
        """
        sandbox = self._sandboxes.get(provider_ref)
        if sandbox is not None:
            return sandbox
        from e2b import AsyncSandbox

        try:
            return await AsyncSandbox.connect(provider_ref, api_key=self._api_key)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise RuntimeError(f"Failed to reconnect to sandbox {provider_ref} to {operation}: {exc}") from exc

    async def exec_command_stream(
        self,
        provider_ref: str,
        command: list[str],
        *,
        environment: dict[str, str] | None = None,
    ) -> ExecProcess:
        """Stream a command's output as async chunks with a kill handle (ADR 040).

        E2B implementation of the streaming-parity primitive: the command is
        started as a background command (``commands.run(..., background=True,
        on_stdout=..., on_stderr=...)`` — the same SDK API the legacy
        node-runner path uses) and its output callbacks feed an internal
        queue that ``process.chunks`` drains.

        Lifecycle contract (mirrors the Docker implementation):
          - decoded :class:`ExecStreamChunk` values on ``chunks``, in order;
          - ``done`` fires when the stream ends (healthy or error) — also on
            an early consumer close (same as Docker);
          - ``exit_code`` stays ``None`` until the END of a HEALTHY stream
            (including a non-zero command exit — a command failure is a
            result, not a stream error);
          - an engine/proxy drop mid-stream (or an end-eventless
            termination) sets ``error`` and leaves ``exit_code`` ``None`` —
            never a fabricated ``exit_code == 0``;
          - ``kill()`` SIGKILLs the background command best-effort
            (failures logged and swallowed); the resulting end event then
            terminates ``chunks``.

        The command connection is unbounded (``timeout=0`` — the SDK's
        "do not limit the command connection time" value): the dispatch
        layer owns stall/deadline kills through :meth:`ExecProcess.kill`,
        exactly as it does for Docker's unbounded exec stream.
        """
        from e2b.sandbox.commands.command_handle import CommandExitException

        sandbox = self._get_sandbox(provider_ref)
        cmd_str = " ".join(shlex.quote(c) for c in command)
        queue: asyncio.Queue[ExecStreamChunk | _StreamEnd] = asyncio.Queue()

        async def _on_stdout(chunk: str) -> None:
            await queue.put(ExecStreamChunk(stream="stdout", data=chunk))

        async def _on_stderr(chunk: str) -> None:
            await queue.put(ExecStreamChunk(stream="stderr", data=chunk))

        handle = await asyncio.wait_for(
            sandbox.commands.run(
                cmd_str,
                background=True,
                envs=environment,
                on_stdout=_on_stdout,
                on_stderr=_on_stderr,
                timeout=_STREAM_CMD_TIMEOUT,
            ),
            timeout=_STREAM_START_TIMEOUT,
        )

        process = ExecProcess(chunks=None, kill=None)  # type: ignore[arg-type]

        async def _chunks() -> AsyncIterator[ExecStreamChunk]:
            exit_code: int | None = None
            error: str | None = None
            try:
                while True:
                    item = await queue.get()
                    if isinstance(item, _StreamEnd):
                        exit_code = item.exit_code
                        error = item.error
                        break
                    yield item
            finally:
                if error is not None:
                    process.error = error
                process.exit_code = exit_code
                process.done.set()

        async def _await_terminal() -> None:
            """Resolve the command once and enqueue exactly one terminal marker.

            The SDK resolves ``wait()`` only after every ``on_stdout``/
            ``on_stderr`` callback has run (they are awaited inside the same
            event-handler task), and the queue is FIFO — so the terminal
            marker always lands after all output chunks.
            """
            try:
                result = await handle.wait()
            except asyncio.CancelledError:
                raise
            except CommandExitException as exc:
                # Non-zero command exit: a HEALTHY stream end carrying the
                # real exit code (specific exception before its bases).
                await queue.put(_StreamEnd(exit_code=exc.exit_code, error=None))
            except Exception as exc:
                # Engine/proxy drop or an end-eventless termination: stream
                # ERROR — exit_code must stay None (no zero-exit fabrication).
                await queue.put(_StreamEnd(exit_code=None, error=_stream_error_message(exc)))
            else:
                await queue.put(_StreamEnd(exit_code=result.exit_code, error=None))

        process.chunks = _chunks()
        # Strong ref while in flight (asyncio holds only weak task refs);
        # discarded automatically when the terminal marker has been queued.
        waiter = asyncio.ensure_future(_await_terminal())
        self._stream_waiters.add(waiter)
        waiter.add_done_callback(lambda _t: self._stream_waiters.discard(waiter))

        async def _kill() -> None:
            try:
                await asyncio.wait_for(handle.kill(), timeout=_KILL_TIMEOUT)
            except asyncio.CancelledError:
                raise
            except Exception:
                _log.exception(
                    "exec_command_stream: failed to kill streamed command in sandbox %s",
                    provider_ref,
                )

        process._kill = _kill
        return process

    async def get_workspace_status(self, provider_ref: str) -> str:
        """Return the current status of the sandbox."""
        sandbox = self._sandboxes.get(provider_ref)
        if sandbox is None:
            return "terminated"
        try:
            running = await sandbox.is_running()
            return "running" if running else "stopped"
        except asyncio.CancelledError:
            raise
        except Exception:
            _log.exception("Failed to get status for sandbox %s", provider_ref)
            return "unknown"

    async def close(self) -> None:
        """Destroy every provider-tracked sandbox best-effort (hub-aclose disposal).

        Mirrors ``DockerRuntimeProvider.close()``: on abnormal teardown (e.g. an
        SSE stream dropped mid-test) the hub still disposes this provider, and
        without destroying tracked sandboxes they would keep billing after the
        hub is gone. Best-effort per sandbox — a kill failure is logged and
        never masks the remaining teardown.
        """
        for provider_ref in tuple(self._sandboxes.keys()):
            try:
                await self.destroy_workspace(provider_ref)
            except asyncio.CancelledError:
                raise
            except Exception:
                _log.exception(
                    "Failed to destroy E2B sandbox %s during close()",
                    provider_ref,
                )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get_sandbox(self, provider_ref: str) -> Any:
        sandbox = self._sandboxes.get(provider_ref)
        if sandbox is None:
            raise ValueError(f"Unknown sandbox: {provider_ref}")
        return sandbox

    async def _kill_sandbox_best_effort(self, sandbox: Any, context: str) -> bool:
        """Kill a sandbox during error cleanup without masking the cause.

        Logs and swallows kill failures (including a ``TimeoutError`` from the
        ``wait_for`` wrapper) so the caller can always re-raise the original
        exception that triggered cleanup.

        Returns an outcome for the ADR 040 reclamation path: ``True`` when
        the kill request completed (the sandbox was killed, or the API
        reported it already gone — neither is an error), ``False`` when the
        kill attempt failed (logged + swallowed). Cleanup callers ignore the
        return value, as before.
        """
        try:
            await asyncio.wait_for(sandbox.kill(), timeout=_KILL_TIMEOUT)
        except asyncio.CancelledError:
            raise
        except Exception:
            _log.exception(
                "Failed to kill E2B sandbox %s during %s",
                getattr(sandbox, "sandbox_id", "<unknown>"),
                context,
            )
            return False
        return True

    async def _clone_repo(self, sandbox: Any, repo_url: str, repo_ref: str) -> None:
        """Clone a git repository inside the sandbox.

        Raises RuntimeError if the clone or checkout fails.
        """
        cmds = [f"git clone {shlex.quote(repo_url)} /home/user/repo"]
        if repo_ref:
            cmds.append(f"cd /home/user/repo && git checkout {shlex.quote(repo_ref)}")
        combined = " && ".join(cmds)
        try:
            result = await asyncio.wait_for(
                sandbox.commands.run(combined, timeout=_REPO_CLONE_TIMEOUT),
                timeout=_REPO_CLONE_TIMEOUT,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise RuntimeError(f"Repo clone failed for {repo_url}: {exc}") from exc
        exit_code = getattr(result, "exit_code", 1)
        if exit_code != 0:
            stderr = getattr(result, "stderr", "") or ""
            raise RuntimeError(f"Repo clone failed (exit {exit_code}) for {repo_url}: {stderr}")
