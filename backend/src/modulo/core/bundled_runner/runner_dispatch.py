"""Bundled Runner dispatch adapter (FAR-590, D4).

Branch point of the sandbox_agent dispatch path: when the pipeline-level
bound profile (``PipelineSnapshot.environment_profile_id``, consumed and
same-org validated at dispatch) declares ``provider_type == runner_docker``,
the dispatch runs through the hub-resolved :class:`DockerRuntimeProvider`
(the bundled ``modulo-runner:opencode`` workspace behind the filtered
socket proxy) instead of the legacy direct ``AsyncSandbox.create`` E2B
path. Both ``sandbox_mode`` values are supported (script + llm); the E2B
path in ``node_runner._sandbox_agent_impl`` is untouched.

Upgrade rule (committed by this delivery): pipeline-level profile refs to
providers that were NEVER dispatch-relevant — ``local`` always, and
``local_docker`` under its old inert semantics — are treated as
**dispatch-unbound**: a typed :class:`SandboxDispatchUnboundError` is
raised only at dispatch (never silently activated as dispatch reality);
the operator re-binds the profile deliberately.

E2B keeps its existing path; the adapter only LOUDLY validates legacy
timeout values for it (:func:`validate_e2b_dispatch_timeout`, GraphValidator
parity at <=3300, no silent clamp).
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import shlex
import time
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import jinja2
from jinja2.sandbox import SandboxedEnvironment

from modulo.core.bundled_runner.profile import (
    is_placeholder_bundled_runner_image_ref,
)
from modulo.core.runtime_provider import (
    ExecProcess,
    ProviderNotConfiguredError,
    WorkspaceSpec,
    build_hub,
)
from modulo.core.runtime_provider.hub import RuntimeProviderHub

if TYPE_CHECKING:
    from collections.abc import Callable


_log = logging.getLogger(__name__)

_E2B_MAX_TIMEOUT_SECONDS = 3300  # GraphValidator parity bound (ADR 029)
_OUTPUT_JSON_PATH = "/home/user/output.json"
_INPUT_JSON_PATH = "/home/user/input.json"
_PROMPT_PATH = "/home/user/prompt.md"
_STREAM_THROTTLE_INTERVAL = 1.0
_PROVIDER_ACLOSE_TIMEOUT = 60.0
_DESTROY_TIMEOUT = 30.0
_LOOP_BRIDGE_CLOSE_TIMEOUT = 30.0


class SandboxDispatchUnboundError(ValueError):
    """Typed config error for a dispatch-unbound pipeline-level provider ref."""


class SandboxDispatchTimeoutValidationError(ValueError):
    """Loud validation error for an out-of-range legacy timeout at dispatch."""


@dataclass(frozen=True)
class RunnerDispatchRoute:
    """The resolved dispatch route for a sandbox_agent node."""

    provider_type: str  # "runner_docker" | "e2b" | "none"
    profile: Any = None
    provider: Any = None
    hub: RuntimeProviderHub | None = None


async def load_environment_profile(
    session_factory: Callable[..., Any] | None,
    org_id: uuid.UUID | None,
    environment_profile_id: uuid.UUID | None,
) -> Any:
    """Load the pipeline-level bound EnvironmentProfile row (same-org enforced)."""
    from sqlalchemy import select

    from modulo.db.models.environment_profile import EnvironmentProfile
    from modulo.db.rls import set_rls_org

    if session_factory is None or org_id is None or environment_profile_id is None:
        return None
    async with session_factory() as session:
        await set_rls_org(session, org_id)
        result = await session.execute(
            select(EnvironmentProfile).where(
                EnvironmentProfile.id == environment_profile_id,
                EnvironmentProfile.organisation_id == org_id,
                EnvironmentProfile.deleted_at.is_(None),
            )
        )
        return result.scalar_one_or_none()


def validate_persistence_for_provider(profile: Any) -> None:
    """``runner_docker`` persistence is LOCKED to ephemeral (model-validator parity)."""
    provider_type = (getattr(profile, "provider_type", "") or "").strip().lower()
    persistence = getattr(profile, "persistence_policy", "ephemeral")
    if provider_type == "runner_docker" and persistence != "ephemeral":
        raise SandboxDispatchUnboundError(
            f"Environment profile '{getattr(profile, 'name', profile)}': the Bundled Runner "
            f"(runner_docker) locks persistence_policy to 'ephemeral' (got '{persistence}'); "
            "retained/cache workspaces are not supported on this provider."
        )


async def resolve_sandbox_dispatch_route(
    session_factory: Callable[..., Any] | None,
    org_id_raw: Any,
    environment_profile_id_raw: Any,
) -> RunnerDispatchRoute:
    """Resolve the dispatch route for a sandbox_agent node (D4 dispatch adapter).

    - No bound (or missing) profile -> ``provider_type="none"`` (the
      historical E2B default route).
    - ``e2b`` -> e2b route (the caller applies the LOUD timeout validation
      before provisioning).
    - ``runner_docker`` -> hub-resolved Docker provider (the registration
      matrix per ADR 029: a ``MODULO_RUNNER_*`` var or a Docker endpoint
      env registers the provider), plus the locked-ephemeral check.
    - ``local`` / ``local_docker`` -> dispatch-unbound: typed config error
      (the D4 upgrade rule — never silently activated).
    """
    org_uuid = _parse_uuid(org_id_raw)
    profile_uuid = _parse_uuid(environment_profile_id_raw)
    profile = await load_environment_profile(session_factory, org_uuid, profile_uuid)
    if profile is None:
        return RunnerDispatchRoute(provider_type="none", profile=None)
    provider_type = (getattr(profile, "provider_type", "") or "").strip().lower()
    if provider_type in ("local", "local_docker"):
        raise SandboxDispatchUnboundError(
            f"Pipeline-level environment profile '{getattr(profile, 'name', profile)}' is bound to "
            f"provider '{provider_type}' which is not dispatch-relevant — re-bind the pipeline "
            "to a Runner provider (runner_docker or e2b), or unbind it."
        )
    validate_persistence_for_provider(profile)
    if provider_type == "e2b":
        return RunnerDispatchRoute(provider_type="e2b", profile=profile)
    if provider_type == "runner_docker":
        # Fail-loud (and documented) when the profile still carries the release-
        # advanced placeholder digest: such a profile cannot provision a
        # workspace (the pinned image does not exist in any registry), so a
        # pipeline re-pointed onto the Bundled Runner — e.g. the legacy
        # modulo-dev row — would otherwise fail at container-create with an
        # opaque pull error. This is a known GA follow-up (the GHCR publish job
        # bumps the real digest); surface it explicitly rather than silently
        # breaking the dispatch. See docs/security/bundled-runner-operator-guide.md.
        _image_ref = getattr(profile, "image_ref", None)
        if is_placeholder_bundled_runner_image_ref(_image_ref):
            raise SandboxDispatchUnboundError(
                f"Environment profile '{getattr(profile, 'name', profile)}' is bound to the Bundled "
                "Runner (runner_docker) but its image_ref still carries the release-advanced placeholder "
                "digest (sha256:0000…0000) and cannot provision a workspace. This is a known GA follow-up: "
                "the GHCR publish job bumps the pinned digest. See "
                "docs/security/bundled-runner-operator-guide.md. Re-bind the pipeline to a provisionable "
                "profile, or wait for the digest to land."
            )
        try:
            from modulo.settings import get_settings

            settings = get_settings()
            hub = build_hub(max_local_concurrency=int(getattr(settings, "modulo_max_local_concurrency", 2) or 2))
            provider = hub.resolve(profile)
        except ProviderNotConfiguredError as exc:
            raise SandboxDispatchUnboundError(str(exc)) from exc
        return RunnerDispatchRoute(
            provider_type="runner_docker",
            profile=profile,
            provider=provider,
            hub=hub,
        )
    raise SandboxDispatchUnboundError(f"Environment profile provider_type '{provider_type}' is not dispatchable.")


def validate_e2b_dispatch_timeout(sandbox_timeout: Any) -> None:
    """Loud validation for out-of-range legacy E2B timeout values (D4, no clamp)."""
    if sandbox_timeout is None:
        return
    try:
        timeout_value = int(sandbox_timeout)
    except (TypeError, ValueError):
        return
    if timeout_value > _E2B_MAX_TIMEOUT_SECONDS:
        raise SandboxDispatchTimeoutValidationError(
            f"sandbox_agent timeout_seconds={timeout_value} exceeds the E2B provisioning cap "
            f"(loud validation, GraphValidator parity at <={_E2B_MAX_TIMEOUT_SECONDS}) — adjust "
            "the node configuration instead of relying on a silent clamp."
        )


def _parse_uuid(value: Any) -> uuid.UUID | None:
    if value is None:
        return None
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError):
        return None


def _workspace_spec_for_dispatch(
    profile: Any,
    *,
    org_id: uuid.UUID | None,
    run_id: str,
    node_id: str,
    run_uuid: uuid.UUID | None,
) -> WorkspaceSpec:
    """Build the hardened WorkspaceSpec for a Bundled Runner dispatch.

    Hardening (ADR 029 / plan D4): non-root user, read-only rootfs + tmpfs
    workdir (adequate $HOME sizing), dropped caps + no-new-privileges, 1.0
    CPU / 1 GiB (fixed committed value), structured labels mapped to
    container Labels (modulo.run/org/node.id), the dedicated workspace
    bridge network (never the compose/backend network), and the egress
    default permitted (the tier's purpose) with the per-profile ``none``
    opt-in.
    """
    cfg = getattr(profile, "config_json", None) or {}
    metadata: dict[str, str] = {
        "modulo.run.id": run_id,
        "modulo.org.id": str(org_id) if org_id else "",
        "modulo.node.id": node_id,
    }
    egress = (getattr(profile, "network_policy", None) or "outbound").strip().lower()
    return WorkspaceSpec(
        environment_profile_id=profile.id,
        organisation_id=org_id,  # type: ignore[arg-type]
        run_id=run_uuid,
        image_ref=getattr(profile, "image_ref", None) or "",
        capabilities=getattr(profile, "capabilities_json", None) or [],
        timeout_seconds=int(cfg.get("timeout_seconds", 3600)),
        resource_limits={"memory_mb": int(cfg.get("memory_mb", 1024))},
        egress_policy="none" if egress == "none" else "outbound",
        persistence_policy=getattr(profile, "persistence_policy", "ephemeral"),
        labels={},
        workspace_metadata={key: value for key, value in metadata.items() if value},
        workspace_network=cfg.get("workspace_network"),
    )


async def _write_file_via_exec(provider: Any, provider_ref: str, path: str, content: str) -> None:
    """Write a file into the workspace via a base64 exec (container archive
    endpoints stay outside the socket-proxy allowlist on purpose)."""
    payload = base64.b64encode(content.encode("utf-8")).decode("ascii")
    parent = path.rsplit("/", 1)[0] or "/"
    cmd = [
        "sh",
        "-c",
        f"mkdir -p {shlex.quote(parent)} && printf '%s' {shlex.quote(payload)} | base64 -d > {shlex.quote(path)}",
    ]
    result = await provider.exec_command(provider_ref, cmd, cmd_timeout=30)
    if result.exit_code != 0:
        raise RuntimeError(
            f"Bundled Runner workspace file write failed for {path} "
            f"(exit {result.exit_code}): {(result.stderr or '')[:200]}"
        )


async def _read_file_via_exec(provider: Any, provider_ref: str, path: str) -> str:
    """Read a file from the workspace via ``cat`` ("" on absence/error)."""
    try:
        result = await provider.exec_command(
            provider_ref,
            ["sh", "-c", f"if [ -f {shlex.quote(path)} ]; then cat {shlex.quote(path)}; fi"],
            cmd_timeout=30,
        )
    except asyncio.CancelledError:
        raise
    except Exception:
        _log.info("runner_dispatch.file_read_failed", extra={"path": path}, exc_info=True)
        return ""
    if result.exit_code != 0:
        _log.info(
            "runner_dispatch.file_read_failed",
            extra={"path": path, "exit": result.exit_code},
        )
        return ""
    return str(result.stdout)


def _publish_stream_chunk(
    broker: Any,
    *,
    node_id: str,
    chunk: str,
    stream: str,
    throttle_state: dict[str, Any],
) -> None:
    """Throttled live node.stdout_chunk / node.stderr_chunk publish (FAR-98 shape).

    Redaction rides the shared node_runner redactor; a broker failure is
    best-effort and never fatal (same contract as the E2B publication).
    """
    if broker is None or not chunk:
        return
    now = time.monotonic()
    throttle_state.setdefault("buf", []).append(chunk)
    if now - throttle_state.get("last_ts", 0.0) < _STREAM_THROTTLE_INTERVAL:
        return
    from modulo.core.pipeline_engine.node_runner import _redact_raw_output

    payload = {
        "node_id": node_id,
        "chunk": _redact_raw_output("".join(throttle_state.pop("buf", []))),
        "ts": int(now * 1000),
    }
    throttle_state["last_ts"] = now
    try:
        broker.publish(f"node.{stream}_chunk", payload)
    except RuntimeError:
        return  # Broker closed (run finalised) — stop publishing.
    except Exception:
        _log.info("runner_dispatch.stream_publish_failed", extra={"node_id": node_id}, exc_info=True)


async def _consume_stream(
    exec_process: ExecProcess,
    *,
    node_id: str,
    sandbox_timeout: float,
    stall_timeout: float,
    stream_broker: Any | None = None,
    stall_detector: Any | None = None,
    touch_heartbeat: bool = True,
) -> tuple[list[tuple[str, str]], bool, bool]:
    """Consume a streaming exec to deadline with stall detection (D4).

    Returns ``(collected_chunks, timed_out, stalled)`` — each chunk a
    ``(stream, data)`` tuple. On a stall (no output within ``stall_timeout``)
    OR a total timeout the kill handle fires — the exec is terminated, never
    left running past its budget. On a stream error (engine/proxy drop) the
    consume exits with ``stalled=timed_out=False``, ``exit_code`` stays None,
    and the caller classifies the RETRYABLE failure (a zero exit code is
    NEVER fabricated).
    """

    queue: asyncio.Queue[tuple[str, str] | None] = asyncio.Queue()
    collected: list[tuple[str, str]] = []
    throttle_state: dict[str, Any] = {"buf": [], "last_ts": 0.0}

    async def _pump() -> None:
        try:
            async for chunk in exec_process.chunks:
                data = chunk.data or ""
                if data:
                    _publish_stream_chunk(
                        stream_broker,
                        node_id=node_id,
                        chunk=data,
                        stream=chunk.stream,
                        throttle_state=throttle_state,
                    )
                await queue.put((chunk.stream, data))
        finally:
            await queue.put(None)

    pump_task = asyncio.create_task(_pump())
    deadline = time.monotonic() + sandbox_timeout
    last_progress = time.monotonic()
    timed_out = False
    stalled = False
    try:
        while True:
            now = time.monotonic()
            if now >= deadline:
                timed_out = True
                break
            stall_target = last_progress + stall_timeout
            wait = min(deadline, stall_target) - now
            if wait <= 0:
                stalled = True
                break
            try:
                item = await asyncio.wait_for(queue.get(), timeout=max(wait, 0.05))
            except TimeoutError:
                continue
            if item is None:
                break
            collected.append(item)
            last_progress = time.monotonic()
            if stall_detector is not None:
                stall_detector.touch("output")
                if touch_heartbeat:
                    stall_detector.touch("heartbeat")
    finally:
        if timed_out or stalled:
            try:
                await asyncio.wait_for(exec_process.kill(), timeout=30)
            except asyncio.CancelledError:
                raise
            except Exception:
                _log.exception("runner_dispatch.exec_kill_failed", extra={"node_id": node_id})
        pump_task.cancel()
        try:
            await asyncio.wait_for(asyncio.wait([pump_task], timeout=5.0), timeout=5.5)
        except asyncio.CancelledError:
            raise
        except Exception:
            _log.info("runner_dispatch.pump_join_failed", extra={"node_id": node_id}, exc_info=True)
    if exec_process.error:
        _log.info(
            "runner_dispatch.exec_stream_error",
            extra={"node_id": node_id, "error": exec_process.error[:200]},
        )
    return collected, timed_out, stalled


def _run_broker_for(run_id: str) -> Any:
    """The process-local run event broker for live output (never in state)."""
    if not run_id:
        return None
    try:
        from modulo.core.pipeline_engine.event_broker import get_registry

        return get_registry().get(uuid.UUID(run_id))
    except (TypeError, ValueError, ImportError):
        return None


def _no_output_message(node_id: str) -> str:
    return (
        f"Bundled Runner agent produced no parseable output.json for node '{node_id}' "
        "(a sandbox agent with zero usable work must not complete silently)"
    )


def _schema_failure_message(node_id: str, schema_exc: str) -> str:
    return (
        f"Bundled Runner output for node '{node_id}' failed its declared output "
        f"schema validation — retry in a fresh sandbox: {schema_exc}"
    )


def _budget_killed_message(node_id: str) -> str:
    return (
        f"Bundled Runner sandbox exceeded its wall-clock budget for node '{node_id}' "
        "— killed by the platform-side runtime killer"
    )


def _resolve_stall_timeout(stall_timeout_override: Any) -> float:
    from modulo.core.pipeline_engine.node_runner import _SANDBOX_IDLE_TIMEOUT

    if stall_timeout_override is None:
        return float(_SANDBOX_IDLE_TIMEOUT)
    try:
        return float(stall_timeout_override)
    except (TypeError, ValueError):
        _log.warning(
            "runner_dispatch.stall_timeout_invalid_fallback value=%s",
            stall_timeout_override,
        )
        return float(_SANDBOX_IDLE_TIMEOUT)


def _resolve_stdout_cap(
    node_def: dict[str, Any],
    *,
    org_ceiling: int | None = None,
    pipeline_default: dict[str, Any] | None = None,
) -> int:
    """Resolve the effective stdout/stderr retention cap for a node (FAR-792).

    Node-level free-form config (mirroring the E2B path): ``"tail"`` keeps the
    legacy bounded 512KB artifact cap; ``"full"`` retains up to
    ``stdout_max_bytes`` when set (defaulting to the 5MB fallback when absent).
    Coercion + resolution delegate to the shared node_runner helpers so this
    path can never drift from the E2B retention semantics.

    ``org_ceiling`` (FAR-811) is the org-level hard ceiling from
    ``system_config.sandbox_stdout_retention_max_bytes``; when set the resolved
    cap is hard-clamped to it, so the Bundled Runner path enforces the same
    "no node can exceed the org ceiling" invariant as the E2B path. When
    ``None`` (the default, used by the pure-coercion tests) the node's own cap
    applies unchanged.

    ``pipeline_default`` (FAR-811) is the pipeline-level default for stdout
    retention.  When the node did NOT explicitly set stdout_retention_mode
    (raw key absent from node_def), the pipeline default's mode/max_bytes are
    inherited before the org ceiling clamp.  Node-explicit settings always win.
    """
    from modulo.core.pipeline_engine.node_runner import (
        _coerce_stdout_max_bytes,
        _coerce_stdout_retention_mode,
    )
    from modulo.core.pipeline_engine.node_runner import (
        _resolve_stdout_cap as _resolve_shared_cap,
    )

    # FAR-811: node > pipeline > org resolution.
    _node_raw_mode = node_def.get("stdout_retention_mode")
    if _node_raw_mode is not None:
        mode = _coerce_stdout_retention_mode(_node_raw_mode)
        max_bytes = _coerce_stdout_max_bytes(node_def.get("stdout_max_bytes"))
    elif pipeline_default is not None:
        mode = _coerce_stdout_retention_mode(pipeline_default.get("mode"))
        max_bytes = _coerce_stdout_max_bytes(pipeline_default.get("max_bytes"))
    else:
        mode = "tail"
        max_bytes = None

    return _resolve_shared_cap(
        mode,
        max_bytes,
        org_ceiling=org_ceiling,
    )


def _combine_raw_outputs(raw_file: str, stdout_raw: str) -> str:
    from modulo.core.pipeline_engine.node_runner import _normalize_marker_text

    return "\n".join(p for p in (_normalize_marker_text(raw_file), stdout_raw) if p)


def _source_contains_sentinel(text: Any, sentinel: Any) -> bool:
    from modulo.core.pipeline_engine.node_runner import _source_contains_delivery_sentinel

    return _source_contains_delivery_sentinel(text, sentinel)


async def _check_idempotency_gate(
    *,
    session_factory: Callable[..., Any] | None,
    state: dict[str, Any],
    run_id: str,
    org_id: str,
    node_id: str,
    delivery_sentinel: str | None,
    single_sandbox_node: bool,
) -> dict[str, Any] | None:
    """Guard A: delivery-sentinel idempotency gate (shared with E2B path).

    Returns the skip envelope when the node was already delivered, or ``None``
    to continue normal dispatch.
    """
    if not (delivery_sentinel and single_sandbox_node):
        return None
    try:
        from modulo.settings import get_settings

        gate_enabled = bool(getattr(get_settings(), "modulo_idempotency_gate_enabled", True))
    except Exception:
        gate_enabled = True
    if not gate_enabled:
        return None
    from modulo.core.pipeline_engine.node_runner import (
        _idempotency_gate_skipped_envelope,
        _marker_delivery_done_for_node,
        _read_run_raw_output_markers_for_gate,
    )

    markers = await _read_run_raw_output_markers_for_gate(
        session_factory,
        run_id=run_id,
        org_id_raw=org_id,
        claim_lease=state.get("_claim_lease"),
        node_id=node_id,
    )
    if _marker_delivery_done_for_node(markers, run_id, node_id):
        _log.info(
            "sandbox_agent.runner.idempotency_gate.skipped",
            extra={"node_id": node_id, "run_id": run_id},
        )
        return _idempotency_gate_skipped_envelope(node_id)
    return None


async def _render_agent_template(
    *,
    sandbox_mode: str,
    agent_command: str,
    agent_prompt_template: str,
    state: dict[str, Any],
    scoped_run_context: dict[str, Any],
    raw_input: Any,
    run_id: str,
    node_id: str,
) -> tuple[str, str, str] | dict[str, Any]:
    """Render agent prompt and command templates for the sandbox.

    Returns ``(rendered_prompt, rendered_agent_command, input_json)`` on
    success, or a skip-status dict when the template references missing input
    fields (the caller returns this dict directly as the node result).
    """
    if sandbox_mode == "script":
        return ("", agent_command, json.dumps(raw_input))
    env = SandboxedEnvironment()
    scoped_state = dict(state)
    scoped_state["run_context"] = scoped_run_context
    template_vars: dict[str, Any] = {
        "state": scoped_state,
        "run_context": scoped_run_context,
        "input": raw_input,
    }
    try:
        rendered_prompt = env.from_string(agent_prompt_template).render(**template_vars)
        rendered_agent_command = env.from_string(agent_command).render(**template_vars)
    except jinja2.UndefinedError as _exc:
        _log.warning("runner_dispatch.template_missing_input run=%s: %s", run_id, _exc)
        return {
            "status": "skipped",
            "summary": f"Skipped: prompt template references missing input fields ({_exc})",
            "agent_stdout": "",
            "agent_stderr": "",
            "exit_code": 0,
        }
    except (TypeError, jinja2.TemplateSyntaxError) as _exc:
        _log.warning(
            "runner_dispatch.agent_command_not_template run=%s node=%s; verbatim: %s",
            run_id,
            node_id,
            _exc,
        )
        rendered_agent_command = agent_command
    if rendered_agent_command and not rendered_agent_command.strip():
        raise ValueError(
            f"sandbox_agent node '{node_id}' rendered agent_command is empty after "
            "template resolution — a sandbox agent cannot run an empty command"
        ) from None
    return (rendered_prompt, rendered_agent_command, json.dumps(raw_input))


@dataclass(frozen=True)
class _ProvisionResult:
    """Result of workspace provisioning for a Bundled Runner dispatch."""

    attempt_key: str
    provider_ref: str


async def _provision_workspace(
    *,
    session_factory: Callable[..., Any] | None,
    state: dict[str, Any],
    org_id: str,
    run_id: str,
    node_id: str,
    route: RunnerDispatchRoute,
    org_uuid: uuid.UUID | None,
    run_uuid: uuid.UUID | None,
    sandbox_mode: str,
    context_files: dict[str, str],
    input_json: str,
    raw_input: Any,
    rendered_prompt: str,
) -> _ProvisionResult:
    """Acquire the dispatch marker, provision a workspace, and write context/input files.

    Raises :class:`SupersededNodeError` when the dispatch marker is denied.
    Returns the attempt key and provider reference on success.
    """
    from modulo.core.pipeline_engine.node_runner import (
        SupersededNodeError,
        _emit_script_span_event,
        _sandbox_acquire_dispatch_marker,
        _sandbox_store_dispatch_marker_sandbox,
    )

    attempt_key = await _sandbox_acquire_dispatch_marker(
        session_factory=session_factory,
        claim_lease=state.get("_claim_lease"),
        org_id=org_id,
        run_id=run_id,
        node_id=node_id,
    )
    if attempt_key is None:
        raise SupersededNodeError(
            "Bundled Runner dispatch marker denied — run superseded or not running; workspace not created"
        )

    profile = route.profile
    provider = route.provider
    if profile is None or provider is None:
        raise RuntimeError("Bundled Runner route resolved without a profile/provider")
    spec = _workspace_spec_for_dispatch(
        profile,
        org_id=org_uuid,
        run_id=run_id,
        node_id=node_id,
        run_uuid=run_uuid,
    )
    provider_ref = await provider.create_workspace(spec)
    _emit_script_span_event(
        "script.provisioned",
        {"sandbox_id": provider_ref, "provider": "runner_docker", "mode": sandbox_mode},
    )
    await _sandbox_store_dispatch_marker_sandbox(
        provider_ref,
        session_factory=session_factory,
        claim_lease=state.get("_claim_lease"),
        org_id=org_id,
        run_id=run_id,
        attempt_key=attempt_key,
    )

    for raw_path, raw_content in context_files.items():
        write_path = raw_path.removesuffix(".b64") if raw_path.endswith(".b64") else raw_path
        write_content = base64.b64decode(raw_content).decode() if raw_path.endswith(".b64") else raw_content
        await _write_file_via_exec(provider, provider_ref, write_path, write_content)
    if sandbox_mode == "script":
        await _write_file_via_exec(provider, provider_ref, _INPUT_JSON_PATH, input_json)
    else:
        safe_input = input_json
        if len(input_json) > 10240:
            safe_input = json.dumps(
                {"_truncated": True, "_key_count": len(raw_input) if isinstance(raw_input, dict) else 0}
            )
        await _write_file_via_exec(provider, provider_ref, _INPUT_JSON_PATH, safe_input)
        await _write_file_via_exec(provider, provider_ref, _PROMPT_PATH, rendered_prompt)

    return _ProvisionResult(attempt_key=attempt_key, provider_ref=provider_ref)


async def _resolve_sandbox_envs_with_script_setup(
    *,
    node_def: dict[str, Any],
    run_id: str,
    pipeline_id: str,
    org_id: str,
    sandbox_mode: str,
    state: dict[str, Any],
    session_factory: Callable[..., Any] | None,
    wallclock_budget_seconds: int | None,
    start_time: float,
    attempt_key: str,
    sandbox_timeout: int,
    input_json: str,
) -> tuple[dict[str, str], bool]:
    """Resolve sandbox env vars and perform script-mode pre-run setup.

    Returns ``(sandbox_envs, script_lease_claimed)``.  In script mode this
    also claims the script lease, mints a run API key, and checks the
    wallclock budget (raising :class:`ScriptBudgetKilledError` on overrun).
    """
    from modulo.core.pipeline_engine.node_runner import (
        ScriptBudgetKilledError,
        _build_sandbox_envs,
        _emit_script_span_event,
        _sandbox_mint_run_api_key_for_sandbox,
        _sandbox_resolve_secret_ref,
        _sandbox_store_script_lease,
        _sandbox_wallclock_budget_exceeded,
        resolve_env_var_refs,
    )

    env_vars_extra: dict[str, str] = await resolve_env_var_refs(
        node_def.get("env_vars") or {},
        lambda key: _sandbox_resolve_secret_ref(
            key,
            session_factory=session_factory,
            org_id=org_id,
        ),
    )
    node_scope = node_def.get("capability_scope") or {}
    allowed_tools = node_scope.get("allowed_tools")
    if allowed_tools:
        env_vars_extra["MODULO_ALLOWED_TOOLS"] = ",".join(str(t) for t in allowed_tools)
    sandbox_envs = _build_sandbox_envs(
        run_id=run_id,
        pipeline_id=pipeline_id,
        org_id=org_id,
        input_json=input_json,
        sandbox_mode=sandbox_mode,
        env_vars_extra=env_vars_extra,
    )

    script_lease_claimed = False
    if sandbox_mode == "script":
        if _sandbox_wallclock_budget_exceeded(
            sandbox_mode=sandbox_mode,
            wallclock_budget_seconds=wallclock_budget_seconds,
            start_time=start_time,
        ):
            _log.warning(
                "sandbox_agent.runner.wallclock_budget_overrun_pre_run",
                extra={"run_id": run_id, "node_id": node_def.get("node_id", "")},
            )
            raise ScriptBudgetKilledError(_budget_killed_message(node_def.get("node_id", ""))) from None
        await _sandbox_store_script_lease(
            session_factory=session_factory,
            claim_lease=state.get("_claim_lease"),
            org_id=org_id,
            run_id=run_id,
            attempt_key=attempt_key,
        )
        script_lease_claimed = True
        _emit_script_span_event(
            "script.lease_claimed",
            {"run_id": run_id, "node_id": node_def.get("node_id", "")},
        )
        run_api_key = await _sandbox_mint_run_api_key_for_sandbox(
            session_factory=session_factory,
            org_id=org_id,
            run_id=run_id,
            node_id=node_def.get("node_id", ""),
            sandbox_timeout=sandbox_timeout,
        )
        if run_api_key:
            sandbox_envs["MODULO_API_KEY"] = run_api_key

    return sandbox_envs, script_lease_claimed


def _classify_exec_result(
    exec_process: Any,
    *,
    timed_out: bool,
    stalled: bool,
    sandbox_mode: str,
    script_lease_claimed: bool,
    stall_timeout: float,
    sandbox_timeout: int,
    node_id: str,
) -> int:
    """Classify the exec result: raise on error, return exit_code on success.

    Raises :class:`SandboxNodeFailedError` on stream error, stall, timeout,
    or missing exit code.  Raises :class:`ScriptSideEffectUnknownError` when
    a script-mode run is killed mid-execution.
    """
    from modulo.core.pipeline_engine.node_runner import (
        SandboxNodeFailedError,
        ScriptSideEffectUnknownError,
    )

    if exec_process.error:
        raise SandboxNodeFailedError(
            f"Bundled Runner exec stream error (retryable, engine/proxy drop): {exec_process.error[:500]}",
            node_id=node_id,
        )
    if timed_out or stalled:
        if sandbox_mode == "script" and script_lease_claimed:
            raise ScriptSideEffectUnknownError(
                "Bundled Runner script-mode terminated mid-execution (side effect unknown): "
                + (
                    f"stalled — no output for {stall_timeout:.0f}s"
                    if stalled
                    else f"no output within {sandbox_timeout}s"
                )
            )
        raise SandboxNodeFailedError(
            (
                f"agent produced no output for {stall_timeout:.0f}s"
                if stalled
                else f"Bundled Runner command produced no output within {sandbox_timeout}s"
            ),
            node_id=node_id,
        )
    if exec_process.exit_code is None:
        raise SandboxNodeFailedError(
            "Bundled Runner exec stream ended without an inspectable exit code (retryable)",
            node_id=node_id,
        )
    exit_code: int = exec_process.exit_code
    return exit_code


async def _read_and_validate_output(
    *,
    provider: Any,
    provider_ref: str,
    session_factory: Callable[..., Any] | None,
    run_id: str,
    org_id: str,
    node_id: str,
    attempt_key: str,
    agent_stdout_raw: str,
    agent_stderr_raw: str,
    delivery_sentinel: str | None,
    stdout_cap: int,
    exit_code: int,
    sandbox_mode: str,
    script_lease_claimed: bool,
    output_schema_json: Any,
) -> Any:
    """Read output.json from the workspace, parse it, and validate against schema.

    Returns the parsed output dict (or a non-dict parseable value).  Raises
    on truly-empty output, script-mode non-zero exit, or schema validation
    failure.  Retains raw-output markers on every error path.
    """
    from modulo.core.pipeline_engine.node_runner import (
        SandboxNodeFailedError,
        ScriptFailedError,
        ScriptInvalidOutputError,
        _retain_raw_output_marker,
        _validate_against_schema,
    )

    raw_output_str = await _read_file_via_exec(provider, provider_ref, _OUTPUT_JSON_PATH)
    output_json: Any = None
    if raw_output_str:
        try:
            output_json = json.loads(raw_output_str)
        except json.JSONDecodeError:
            output_json = None

    if output_json is None or not isinstance(output_json, dict):
        if raw_output_str and not isinstance(output_json, dict) and output_json is not None:
            parse_error = f"output.json parsed to non-dict type {type(output_json).__name__}"
        elif raw_output_str:
            parse_error = "output.json is not valid JSON"
        else:
            parse_error = "output.json is empty or JSON null"
        await _retain_raw_output_marker(
            session_factory,
            run_id=run_id,
            org_id_raw=org_id,
            node_id=node_id,
            attempt_key=attempt_key,
            summary="Bundled Runner agent produced no parseable output.json — raw output retained",
            source=_combine_raw_outputs(raw_output_str, agent_stdout_raw),
            parse_error=parse_error,
            exit_code=exit_code,
            stdout_length=len(agent_stdout_raw),
            stderr_length=len(agent_stderr_raw),
            delivery_sentinel=delivery_sentinel,
            max_artifact_bytes=stdout_cap,
        )
        if output_json is None:
            if sandbox_mode == "script" and script_lease_claimed:
                raise ScriptInvalidOutputError(_no_output_message(node_id))
            raise SandboxNodeFailedError(_no_output_message(node_id), node_id=node_id)

    if sandbox_mode == "script" and exit_code != 0:
        raise ScriptFailedError(f"Script-mode Bundled Runner exited with code {exit_code} (post-claim, terminal)")

    if isinstance(output_schema_json, dict) and isinstance(output_json, dict):
        try:
            _validate_against_schema(output_json, output_schema_json)
        except ValueError as schema_exc:
            _log.exception(
                "sandbox_agent.runner.schema_validation_failed",
                extra={"node_id": node_id},
            )
            if sandbox_mode == "script" and script_lease_claimed:
                raise ScriptInvalidOutputError(
                    f"Script-mode output failed schema validation for node {node_id!r}: {schema_exc}"
                ) from None
            await _retain_raw_output_marker(
                session_factory,
                run_id=run_id,
                org_id_raw=org_id,
                node_id=node_id,
                attempt_key=attempt_key,
                summary=("Bundled Runner agent output failed declared output schema validation — raw output retained"),
                source=_combine_raw_outputs(raw_output_str, agent_stdout_raw),
                parse_error=str(schema_exc),
                exit_code=exit_code,
                stdout_length=len(agent_stdout_raw),
                stderr_length=len(agent_stderr_raw),
                delivery_sentinel=delivery_sentinel,
                max_artifact_bytes=stdout_cap,
            )
            raise SandboxNodeFailedError(
                _schema_failure_message(node_id=node_id, schema_exc=str(schema_exc)),
                node_id=node_id,
            ) from None

    return output_json


@dataclass(frozen=True)
class _ResultShape:
    """Shaped result envelope fields for a Bundled Runner node."""

    status: str
    result_summary: str
    agent_status: str | None
    agent_outcome: str | None
    changed_files: list[str]
    pr_url: str
    cost: float
    sandbox_session_lost: bool


def _shape_result(
    *,
    exit_code: int,
    output_json: Any,
    sandbox_mode: str,
    elapsed: float,
) -> _ResultShape:
    """Determine the final status, summary, and agent-failure fields from the exec result.

    Surfaces the agent's own ``status`` / ``outcome`` verdicts verbatim
    (A1 elevation input for agent-failure UX) — never derived from
    ``exit_code``.
    """
    from modulo.core.pipeline_engine.node_runner import (
        _compute_sandbox_cost,
        _is_sandbox_session_lost_echo,
    )

    cost = _compute_sandbox_cost(elapsed, output_json)
    status = "completed" if exit_code == 0 else "failed"
    result_summary = ""
    agent_status: str | None = None
    agent_outcome: str | None = None
    changed_files: list[str] = []
    pr_url: str = ""
    sandbox_session_lost = False
    if sandbox_mode == "script":
        result_summary = f"script mode: exit_code={exit_code}"
    elif isinstance(output_json, dict):
        result_summary = output_json.get("summary", "")
        changed_files = output_json.get("changed_files", [])
        pr_url = output_json.get("pr_url", "")
        sandbox_session_lost = _is_sandbox_session_lost_echo(output_json)
        _raw_status = output_json.get("status")
        _raw_outcome = output_json.get("outcome")
        if isinstance(_raw_status, str) and not sandbox_session_lost:
            agent_status = _raw_status
        if isinstance(_raw_outcome, str) and not sandbox_session_lost:
            agent_outcome = _raw_outcome
        if sandbox_session_lost:
            status = "failed"
    if status == "failed" and not result_summary:
        result_summary = "Bundled Runner command failed"

    return _ResultShape(
        status=status,
        result_summary=result_summary,
        agent_status=agent_status,
        agent_outcome=agent_outcome,
        changed_files=changed_files,
        pr_url=pr_url,
        cost=cost,
        sandbox_session_lost=sandbox_session_lost,
    )


async def _maybe_retain_delivery_sentinel(
    *,
    session_factory: Callable[..., Any] | None,
    run_id: str,
    org_id: str,
    node_id: str,
    attempt_key: str,
    agent_stdout_raw: str,
    exit_code: int,
    stdout_len: int,
    stderr_len: int,
    delivery_sentinel: str | None,
    status: str,
    stdout_cap: int,
) -> None:
    """Best-effort retention of the delivery-sentinel idempotency marker."""
    from modulo.core.pipeline_engine.node_runner import _retain_raw_output_marker

    if not (delivery_sentinel and _source_contains_sentinel(agent_stdout_raw, delivery_sentinel)):
        return
    try:
        await _retain_raw_output_marker(
            session_factory,
            run_id=run_id,
            org_id_raw=org_id,
            node_id=node_id,
            attempt_key=attempt_key,
            summary="Bundled Runner completed with delivery sentinel observed (idempotency gate)",
            source=agent_stdout_raw,
            parse_error="",
            exit_code=exit_code,
            stdout_length=stdout_len,
            stderr_length=stderr_len,
            delivery_sentinel=delivery_sentinel,
            status=status,
            max_artifact_bytes=stdout_cap,
        )
    except Exception:
        _log.exception(
            "sandbox_agent.runner.delivery_marker_persist_failed",
            extra={"node_id": node_id},
        )


async def run_bundled_runner_node(
    state: dict[str, Any],
    config: Any,
    route: RunnerDispatchRoute,
) -> dict[str, Any]:
    """Execute a sandbox_agent node on the Bundled Runner (Docker) packaging.

    Mirrors the E2B dispatch's structured-output contract (same envelope
    shape, same DB-atomic dispatch-marker machinery, same error classes)
    while provisioning a hardened workspace through the resolved provider
    and consuming the D4 streaming-exec primitive for live output +
    stall/no-output detection. Both ``sandbox_mode`` values are supported.

    Reduced-parity note (for D8 cohesion): the E2B path's dispatch-time
    check-then-act capacity pre-gate is not duplicated here — the DB-atomic
    dispatch marker is the correctness fence, and D8's uniform advisory
    gate replaces all pre-gates for every tier.
    """
    from modulo.core.pipeline_engine.node_runner import (
        _UNSET,
        SandboxNodeFailedError,
        _build_sandbox_node_envelope,
        _compute_sandbox_cost,
        _configure_stall_detector,
        _emit_script_span_event,
        _persist_full_stdout_artifact,
        _read_org_stdout_retention_ceiling,
        _redact_raw_output,
        _run_identity_strs,
        _SandboxNodeOutput,
    )

    node_id: str = config.node_id
    node_def: dict[str, Any] = config.node_def
    sandbox_mode: str = config.sandbox_mode
    agent_command: str = config.agent_command
    agent_prompt_template: str = config.agent_prompt_template
    wallclock_budget_seconds: int | None = config.wallclock_budget_seconds
    output_schema_json = config.output_schema_json
    sandbox_timeout: int = config.sandbox_timeout
    stall_timeout_override = config.stall_timeout_override
    context_files: dict[str, str] = config.context_files
    delivery_sentinel: str | None = config.delivery_sentinel
    session_factory = config.session_factory
    single_sandbox_node = config.single_sandbox_node
    loop_intercept = config.loop_intercept_config
    workspace_inputs: list[dict[str, Any]] = getattr(config, "workspace_inputs", None) or []

    # FAR-800 follow-up: the Bundled Runner path does NOT yet support managed
    # workspace inputs (no host-side ref resolution, no in-workspace clone
    # provisioning).  Fail CLOSED with a clear error when inputs are configured
    # rather than silently ignoring them.
    if workspace_inputs:
        raise SandboxNodeFailedError(
            f"Bundled Runner (runner_docker) does not support managed workspace inputs "
            f"yet — node '{node_id}' has {len(workspace_inputs)} input(s) configured. "
            "Use the E2B path (provider_type=e2b) or remove workspace_inputs from "
            "the node configuration.",
            node_id=node_id,
        )

    run_id, pipeline_id, org_id = _run_identity_strs(state)
    org_uuid = _parse_uuid(org_id)
    run_uuid = _parse_uuid(run_id)

    attempt_key: str | None = None
    dispatch_marker_set = False
    script_lease_claimed = False
    output_json: Any = None
    start_time = time.monotonic()

    try:
        # Guard A (delivery-sentinel skip) — shared with the E2B path.
        gate_result = await _check_idempotency_gate(
            session_factory=session_factory,
            state=state,
            run_id=run_id,
            org_id=org_id,
            node_id=node_id,
            delivery_sentinel=delivery_sentinel,
            single_sandbox_node=single_sandbox_node,
        )
        if gate_result is not None:
            return gate_result

        run_context: dict[str, Any] = state.get("run_context") or {}
        raw_input: Any = run_context.get("input", {})
        from modulo.core.capability_scope import filter_run_context_scope

        scoped_run_context = filter_run_context_scope(
            run_context, (node_def.get("capability_scope") or {}).get("context_scope")
        )
        template_result = await _render_agent_template(
            sandbox_mode=sandbox_mode,
            agent_command=agent_command,
            agent_prompt_template=agent_prompt_template,
            state=state,
            scoped_run_context=scoped_run_context,
            raw_input=raw_input,
            run_id=run_id,
            node_id=node_id,
        )
        if isinstance(template_result, dict):
            return template_result
        rendered_prompt, rendered_agent_command, input_json = template_result

        # DB-atomic dispatch fence (same fenced WHERE as the E2B path).
        provision = await _provision_workspace(
            session_factory=session_factory,
            state=state,
            org_id=org_id,
            run_id=run_id,
            node_id=node_id,
            route=route,
            org_uuid=org_uuid,
            run_uuid=run_uuid,
            sandbox_mode=sandbox_mode,
            context_files=context_files,
            input_json=input_json,
            raw_input=raw_input,
            rendered_prompt=rendered_prompt,
        )
        attempt_key = provision.attempt_key
        dispatch_marker_set = True
        provider_ref = provision.provider_ref
        provider = route.provider

        sandbox_envs, script_lease_claimed = await _resolve_sandbox_envs_with_script_setup(
            node_def=node_def,
            run_id=run_id,
            pipeline_id=pipeline_id,
            org_id=org_id,
            sandbox_mode=sandbox_mode,
            state=state,
            session_factory=session_factory,
            wallclock_budget_seconds=wallclock_budget_seconds,
            start_time=start_time,
            attempt_key=attempt_key,
            sandbox_timeout=sandbox_timeout,
            input_json=input_json,
        )

        effective_command = rendered_agent_command
        if loop_intercept is not None and loop_intercept.enabled:
            effective_command = (
                await _maybe_start_loop_bridge(
                    state,
                    provider=provider,
                    provider_ref=provider_ref,
                    sandbox_envs=sandbox_envs,
                    loop_intercept=loop_intercept,
                    rendered_command=rendered_agent_command,
                    session_factory=session_factory,
                    org_id=state.get("_org_id"),
                    pipeline_id=state.get("_pipeline_id"),
                    run_id=run_id,
                    node_id=node_id,
                )
                or rendered_agent_command
            )

        wrapped_command = f"( {effective_command} ) 2>&1"
        stall_timeout = _resolve_stall_timeout(stall_timeout_override)
        stall = _configure_stall_detector(
            enable_heartbeat=config.enable_heartbeat,
            watch_log_path=None,
            stdout_percentage_delta=config.stdout_percentage_delta,
            watch_globs=[],
        )
        stream_broker = _run_broker_for(run_id)

        start_time = time.monotonic()
        exec_process = await provider.exec_command_stream(
            provider_ref,
            ["bash", "-lc", wrapped_command],
            environment=sandbox_envs,
        )
        _emit_script_span_event(
            "script.command_started",
            {
                "run_id": run_id,
                "node_id": node_id,
                "command": (rendered_agent_command[:200] if sandbox_mode == "script" else "llm"),
            },
        )
        collected, timed_out, stalled = await _consume_stream(
            exec_process,
            node_id=node_id,
            sandbox_timeout=sandbox_timeout,
            stall_timeout=stall_timeout,
            stream_broker=stream_broker,
            stall_detector=stall,
            touch_heartbeat=config.enable_heartbeat,
        )
        agent_stdout_raw = "".join(data for (stream_name, data) in collected if stream_name == "stdout")
        agent_stderr_raw = "".join(data for (stream_name, data) in collected if stream_name == "stderr")
        elapsed = time.monotonic() - start_time
        # FAR-792 + FAR-811: resolve the node's effective stdout/stderr retention
        # cap early so both the envelope artifacts AND the raw-output retention
        # markers honour it (E2B parity). The org-level hard ceiling
        # (system_config.sandbox_stdout_retention_max_bytes) is read and applied
        # here too, so the "no node can exceed the org ceiling" invariant holds
        # on the Bundled Runner path exactly as on the E2B path.
        org_stdout_ceiling = await _read_org_stdout_retention_ceiling(config.session_factory)
        _pipeline_ret_cfg = getattr(config, "pipeline_stdout_retention_config", None)
        stdout_cap_unclamped = _resolve_stdout_cap(node_def, pipeline_default=_pipeline_ret_cfg)
        stdout_cap = _resolve_stdout_cap(node_def, org_ceiling=org_stdout_ceiling, pipeline_default=_pipeline_ret_cfg)
        if org_stdout_ceiling is not None and stdout_cap < stdout_cap_unclamped:
            _log.warning(
                "sandbox_agent.runner.stdout_cap_clamped_by_org_ceiling",
                extra={
                    "node_id": node_id,
                    "cap_without_ceiling": stdout_cap_unclamped,
                    "org_ceiling": org_stdout_ceiling,
                    "effective_cap": stdout_cap,
                },
            )

        # Stream error / engine-proxy drop / no exit code: RETRYABLE — never
        # a fabricated zero-exit completion (D4 acceptance criteria).
        exit_code = _classify_exec_result(
            exec_process,
            timed_out=timed_out,
            stalled=stalled,
            sandbox_mode=sandbox_mode,
            script_lease_claimed=script_lease_claimed,
            stall_timeout=stall_timeout,
            sandbox_timeout=sandbox_timeout,
            node_id=node_id,
        )

        output_json = await _read_and_validate_output(
            provider=provider,
            provider_ref=provider_ref,
            session_factory=session_factory,
            run_id=run_id,
            org_id=org_id,
            node_id=node_id,
            attempt_key=attempt_key,
            agent_stdout_raw=agent_stdout_raw,
            agent_stderr_raw=agent_stderr_raw,
            delivery_sentinel=delivery_sentinel,
            stdout_cap=stdout_cap,
            exit_code=exit_code,
            sandbox_mode=sandbox_mode,
            script_lease_claimed=script_lease_claimed,
            output_schema_json=output_schema_json,
        )

        # FAR-792: redact BEFORE truncation so credential-scrubbing sees the
        # full stream, then slice to the node's effective retention cap.
        agent_stdout = _redact_raw_output(agent_stdout_raw)[:stdout_cap]
        agent_stderr = _redact_raw_output(agent_stderr_raw)[:stdout_cap]
        stdout_len = len(agent_stdout_raw)
        stderr_len = len(agent_stderr_raw)
        stdout_truncated = stdout_len > stdout_cap
        if stdout_truncated or stderr_len > stdout_cap:
            _log.warning(
                "sandbox_agent.stdout_stderr_truncated",
                extra={
                    "node_id": node_id,
                    "run_id": run_id,
                    "retention_cap_bytes": stdout_cap,
                    "stderr_truncated": stderr_len > stdout_cap,
                    "stdout_length": stdout_len,
                    "stderr_length": stderr_len,
                },
            )

        # FAR-811 parity with the E2B path: over-cap redacted stdout is retained
        # IN FULL in the artifact store with an envelope pointer (stdout_artifact)
        # instead of only the truncated head.  Best-effort: a store failure keeps
        # today's inline (truncated) behaviour with no pointer key.  Redaction
        # happened ABOVE (order unchanged) — the stored bytes are the redacted text.
        _stdout_artifact: dict[str, Any] | None = None
        if stdout_truncated:
            _stdout_artifact = _persist_full_stdout_artifact(
                org_id=org_id,
                run_id=run_id,
                node_id=node_id,
                attempt_key=attempt_key,
                node_cap=stdout_cap,
                redacted_stdout=_redact_raw_output(agent_stdout_raw),
            )

        cost = _compute_sandbox_cost(elapsed, output_json)
        result = _shape_result(
            exit_code=exit_code,
            output_json=output_json,
            sandbox_mode=sandbox_mode,
            elapsed=elapsed,
        )
        status = result.status
        result_summary = result.result_summary
        agent_status = result.agent_status
        agent_outcome = result.agent_outcome
        changed_files = result.changed_files
        pr_url = result.pr_url
        sandbox_session_lost = result.sandbox_session_lost

        await _maybe_retain_delivery_sentinel(
            session_factory=session_factory,
            run_id=run_id,
            org_id=org_id,
            node_id=node_id,
            attempt_key=attempt_key,
            agent_stdout_raw=agent_stdout_raw,
            exit_code=exit_code,
            stdout_len=stdout_len,
            stderr_len=stderr_len,
            delivery_sentinel=delivery_sentinel,
            status=status,
            stdout_cap=stdout_cap,
        )

        return _build_sandbox_node_envelope(
            node_id=node_id,
            output=_SandboxNodeOutput(
                status=status,
                summary=result_summary,
                exit_code=exit_code,
                wall_clock_time_ms=int(elapsed * 1000),
                cost_estimate_usd=cost,
                cost_source=output_json,
                output_json=output_json,
                agent_stdout=agent_stdout,
                agent_stderr=agent_stderr,
                stdout_length=stdout_len,
                stderr_length=stderr_len,
                stdout_truncated=stdout_truncated,
                stdout_artifact=_stdout_artifact if _stdout_artifact is not None else _UNSET,
                attempt_key=attempt_key,
                agent_status=agent_status,
                agent_outcome=agent_outcome,
                changed_files=changed_files,
                pr_url=pr_url,
                sandbox_session_lost=sandbox_session_lost,
            ),
            exclude_from_output=frozenset({"changed_files", "pr_url"}),
        )
    finally:
        await _teardown_and_clear(
            route=route,
            session_factory=session_factory,
            state=state,
            run_id=run_id,
            org_id=org_id,
            _attempt_key=attempt_key,
            dispatch_marker_set=dispatch_marker_set,
        )


async def _maybe_start_loop_bridge(
    state: dict[str, Any],
    *,
    provider: Any,
    provider_ref: str,
    sandbox_envs: dict[str, str],
    loop_intercept: Any,
    rendered_command: str,
    session_factory: Callable[..., Any] | None,
    org_id: Any,
    pipeline_id: Any,
    run_id: str,
    node_id: str,
) -> str | None:
    """Best-effort loop-intercept bridge (Docker packaging) — fail-open.

    The bridge server binds on the SAQ host; workspaces reach it via
    ``host.docker.internal`` on Docker (ADR 003 amendment: a setup failure
    disables the bridge for this node and NEVER blocks the dispatch).
    Returns the bridge-wrapped command, or None (no bridge).
    """
    try:
        from functools import partial

        from modulo.core.eval_engine import EvalEngine
        from modulo.core.guardrails.loop_intercept import (
            LoopInterceptCallbackServer,
            bridge_client_source,
            load_loop_intercept_guardrails,
            persist_loop_interception_audit,
        )

        bridge_defs = await load_loop_intercept_guardrails(
            session_factory,
            org_id=org_id,
            pipeline_id=pipeline_id,
        )
        if not bridge_defs:
            return None
        bridge_server = LoopInterceptCallbackServer(
            engine=EvalEngine(),
            definitions=bridge_defs,
            config=loop_intercept,
            audit_sink=partial(
                persist_loop_interception_audit,
                session_factory=session_factory,
                org_id=str(org_id) if org_id else "",
                run_id=run_id,
                node_id=node_id,
            ),
        )
        bridge_port = await bridge_server.start()
        state["_loop_intercept_bridge_server"] = bridge_server
        await _write_file_via_exec(provider, provider_ref, "/home/user/modulo_bridge.py", bridge_client_source())
        await _write_file_via_exec(
            provider,
            provider_ref,
            "/home/user/modulo_bridge_config.json",
            json.dumps(loop_intercept.model_dump(mode="json")),
        )
        sandbox_envs["MODULO_BRIDGE_ENDPOINT"] = f"http://host.docker.internal:{bridge_port}"
        sandbox_envs["MODULO_BRIDGE_CONFIG"] = "/home/user/modulo_bridge_config.json"
        return f"python3 /home/user/modulo_bridge.py --wrap -- {rendered_command}"
    except asyncio.CancelledError:
        raise
    except Exception:
        _log.exception(
            "sandbox_agent.runner.loop_intercept_setup_failed",
            extra={"node_id": node_id, "run_id": run_id},
        )
        return None


async def _teardown_and_clear(
    *,
    route: RunnerDispatchRoute,
    session_factory: Callable[..., Any] | None,
    state: dict[str, Any],
    run_id: str,
    org_id: str,
    _attempt_key: str | None,
    dispatch_marker_set: bool,
) -> None:
    """Best-effort workspace destroy + client close + fenced marker clear."""
    provider = route.provider
    hub = route.hub
    if provider is not None:
        refs = list(getattr(provider, "_workspaces", {}).keys())
        for ref in refs:
            try:
                await asyncio.wait_for(provider.destroy_workspace(ref), timeout=_DESTROY_TIMEOUT)
            except asyncio.CancelledError:
                raise
            except Exception:
                _log.exception("runner_dispatch.workspace_destroy_failed", extra={"ref": ref})
        if refs:
            try:
                await asyncio.wait_for(provider.close(), timeout=_PROVIDER_ACLOSE_TIMEOUT)
            except asyncio.CancelledError:
                raise
            except Exception:
                _log.exception("runner_dispatch.provider_close_failed")
    if hub is not None:
        try:
            await asyncio.wait_for(hub.aclose(), timeout=_PROVIDER_ACLOSE_TIMEOUT)
        except asyncio.CancelledError:
            raise
        except Exception:
            _log.exception("runner_dispatch.hub_aclose_failed")
    bridge_server = state.get("_loop_intercept_bridge_server")
    if bridge_server is not None:
        try:
            await asyncio.wait_for(
                asyncio.shield(bridge_server.close()),
                timeout=_LOOP_BRIDGE_CLOSE_TIMEOUT,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            _log.exception("runner_dispatch.loop_intercept_teardown_failed")
    if dispatch_marker_set:
        from modulo.core.pipeline_engine.node_runner import _sandbox_clear_dispatch_marker

        try:
            await _sandbox_clear_dispatch_marker(
                session_factory=session_factory,
                claim_lease=state.get("_claim_lease"),
                org_id=org_id,
                run_id=run_id,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            _log.exception("runner_dispatch.dispatch_marker_clear_failed")
