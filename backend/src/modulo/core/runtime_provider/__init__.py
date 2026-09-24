"""Runtime provider abstraction for agent execution environments.

Supports creating ephemeral or persistent workspaces (containers, VMs,
sandboxed processes) and executing commands within them.
"""

from __future__ import annotations

import asyncio
import logging
import os
import uuid
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from modulo.util import WorkspaceNetworkValidationError, validate_workspace_network

_log = logging.getLogger(__name__)

# Re-export for callers that import from this module.
__all__ = [
    "ArtifactTooLargeError",
    "BackendUnreachableError",
    "ExecProcess",
    "ExecResult",
    "ExecStreamChunk",
    "ProviderCapabilityUnsupportedError",
    "ProviderNotConfiguredError",
    "ProvisionTimeoutError",
    "RateLimitedError",
    "RuntimeProvider",
    "RuntimeProviderError",
    "SdkMissingError",
    "StreamingUnsupportedError",
    "UnknownProviderTypeError",
    "UnknownRefError",
    "WorkspaceGoneError",
    "WorkspaceNetworkValidationError",
    "WorkspaceSpec",
    "build_hub",
    "create_default_hub",
    "env_var_for_provider_type",
    "validate_workspace_network",
]

if TYPE_CHECKING:
    from modulo.core.runtime_provider.hub import RuntimeProviderHub


class ProviderNotConfiguredError(RuntimeError):
    """A profile's ``provider_type`` is known but has no registered runtime provider.

    Raised by :meth:`RuntimeProviderHub.resolve` when the profile's explicit
    ``provider_type`` is a valid vocabulary member but the matching provider
    is not registered because its enabling environment variable is unset.
    Carries the env var that would register the provider, so callers can
    surface remediation copy instead of a silent fallback.
    """

    def __init__(self, provider_type: str, env_var: str | None = None) -> None:
        self.provider_type = provider_type
        self.env_var = env_var
        if env_var and env_var in _DOCKER_ENV_VARS:
            message = (
                f"No runtime provider registered for provider_type '{provider_type}'. "
                "Set MODULO_DOCKER_HOST (or DOCKER_HOST) and restart to enable it, "
                "or choose a different provider type for the profile."
            )
        elif env_var:
            message = (
                f"No runtime provider registered for provider_type '{provider_type}'. "
                f"Set the {env_var} environment variable (and restart) to enable it, "
                f"or choose a different provider type for the profile."
            )
        else:
            message = f"No runtime provider registered for provider_type '{provider_type}'."
        super().__init__(message)


class UnknownProviderTypeError(ProviderNotConfiguredError):
    """A profile's ``provider_type`` is not in the known provider vocabulary.

    Subclasses :class:`ProviderNotConfiguredError` so that every existing
    ``except ProviderNotConfiguredError`` handler automatically catches
    unknown-type errors too — the two failure modes (unknown type vs. known
    but unregistered) share the same remediation surface (update the profile).

    Raised by :meth:`RuntimeProviderHub.resolve` when the profile's explicit
    ``provider_type`` does not match any value in the ``PROVIDER_TYPES``
    vocabulary.  This is distinct from a plain
    :class:`ProviderNotConfiguredError` (a known type whose registration env
    var is unset) in carrying the ``valid_types`` vocabulary for actionable
    remediation copy.
    """

    def __init__(self, provider_type: str, valid_types: frozenset[str]) -> None:
        self.valid_types = valid_types
        sorted_types = ", ".join(sorted(valid_types))
        message = (
            f"Unknown provider_type '{provider_type}'. "
            f"Valid types are: {sorted_types}. "
            "Update the environment profile to use a valid provider type."
        )
        # Initialise parent to set self.provider_type and self.env_var,
        # then override the message with the more specific unknown-type copy.
        super().__init__(provider_type, env_var=None)
        self.args = (message,)


# ---------------------------------------------------------------------------
# RuntimeProviderError family (ADR 040 "Error family") — NEW members only
# ---------------------------------------------------------------------------
#
# DELIBERATE DUAL HIERARCHY: the pre-existing ProviderNotConfiguredError /
# UnknownProviderTypeError tree above is intentionally NOT re-parented under
# RuntimeProviderError. Unifying them would change the control flow of every
# existing ``except ProviderNotConfiguredError`` handler (they would start
# catching runtime-failure members they were never meant to catch), so ADR 040
# records the unification as a separate, GA-gated ticket. Until that gate
# closes, new-family members are admitted ONLY under RuntimeProviderError —
# they are siblings of the configuration-error tree, not subclasses. Handlers
# catching ProviderNotConfiguredError will NOT catch these, and vice versa;
# each call site was reconciled explicitly when this family landed
# (FAR-1050 slice 1).


class RuntimeProviderError(RuntimeError):
    """Base class for NEW runtime-provider failure members (ADR 040).

    Raised for mechanism/runtime failures during provider operations
    (capability refusals, workspace-gone, streaming-unsupported, artefact
    limits, rate limits, missing SDK, provision timeouts, unknown refs,
    unreachable backends). Configuration-resolution failures keep their own
    separate hierarchy (:class:`ProviderNotConfiguredError` and its subclass
    :class:`UnknownProviderTypeError`) — see the dual-hierarchy note above.
    """


class ProviderCapabilityUnsupportedError(RuntimeProviderError):
    """The provider cannot enforce a requested control (ADR 040 isolation).

    Terminal, named-code refusal — e.g. a node-level egress-allowlist /
    read-only-seal / git-credential-scoping request on a tier that cannot
    express it. Never a silent downgrade.
    """


class WorkspaceGoneError(RuntimeProviderError):
    """The workspace/ref no longer exists on the substrate.

    Distinct from an unknown-ref at lookup time: the workspace was tracked
    and is now gone (destroyed, reclaimed, or expired out from under the
    caller).
    """


class StreamingUnsupportedError(RuntimeProviderError):
    """The provider does not implement ``exec_command_stream`` (ADR 040).

    Raised by the ABC's default implementation and by any dispatch that
    *requires* streaming against a non-overriding provider. The buffered
    collect-then-return route (:meth:`RuntimeProvider.exec_command`) remains
    available to callers that can use it.
    """


class ArtifactTooLargeError(RuntimeProviderError):
    """An artefact exceeds the provider or retention size limit."""


class RateLimitedError(RuntimeProviderError):
    """The substrate rate-limited or quota-capped the request."""


class SdkMissingError(RuntimeProviderError):
    """The provider's backing SDK is not installed in this environment."""


class ProvisionTimeoutError(RuntimeProviderError):
    """Workspace provisioning exceeded its timeout without becoming ready."""


class UnknownRefError(RuntimeProviderError):
    """The supplied workspace ref is not recognised by the provider."""


class BackendUnreachableError(RuntimeProviderError):
    """The provider's backend/endpoint could not be reached."""


# ---------------------------------------------------------------------------
# Provider-registration environment signals (single source of truth, FAR-587)
# ---------------------------------------------------------------------------
#
# Every documented signal lives here once; ``build_hub``'s registration gates
# and :func:`env_var_for_provider_type` (the remediation-copy mapping) both
# derive from these constants so the documented behaviour and the implemented
# behaviour can never drift apart.

_DOCKER_ENV_VARS: tuple[str, ...] = ("MODULO_DOCKER_HOST", "DOCKER_HOST")
_E2B_ENV_VAR = "MODULO_E2B_API_KEY"

# Documented unconfigured behaviour (ADR 029 / FAR-587): every
# ``ck_env_profiles_provider_type`` CHECK value maps either to a provider that
# is always registered ("local") or to the env var whose presence registers
# the provider (docker-family types: _DOCKER_ENV_VARS; e2b: _E2B_ENV_VAR).
# Assertion tests pin this mapping against the model's CHECK.
_PROVIDER_ENV_VARS: dict[str, str] = {
    "e2b": _E2B_ENV_VAR,
    "runner_docker": _DOCKER_ENV_VARS[0],
    "docker": _DOCKER_ENV_VARS[0],
    "local_docker": _DOCKER_ENV_VARS[0],
}


def env_var_for_provider_type(provider_type: str) -> str | None:
    """Return the env var that registers ``provider_type``, if one is documented."""
    return _PROVIDER_ENV_VARS.get(provider_type.strip().lower())


@dataclass
class WorkspaceSpec:
    """Parameters for creating a new workspace from an EnvironmentProfile.

    Field semantics are provider-specific (FAR-595 contract, pinned on the
    :class:`RuntimeProvider` ABC):

    - ``labels``: environment-variable injection — Docker maps it to the
      container Env. E2B and Local ignore it (they have no env-injection
      carrier at provision time).
    - ``workspace_metadata``: provider-neutral metadata carrier — Docker
      maps it to container Labels, E2B to sandbox metadata, Local ignores it.
    - ``repo_url`` / ``repo_ref``: first-class clone inputs (FAR-595) —
      E2B clones into ``/home/user/repo`` and optionally checks out
      ``repo_ref``; Local clones into the workspace directory (``repo_ref``
      is not honoured on this tier); Docker ignores both (the bundled
      runner image handles code sync). Deliberately NOT carried in
      ``labels`` — a consumer setting labels for env-injection on an
      E2B/Local profile must never silently trigger a clone.
    """

    environment_profile_id: uuid.UUID
    organisation_id: uuid.UUID
    run_id: uuid.UUID | None = None
    image_ref: str = ""
    capabilities: list[str] = field(default_factory=list)
    timeout_seconds: int = 3600
    resource_limits: dict[str, Any] = field(default_factory=dict)
    egress_policy: str | None = None
    persistence_policy: str = "ephemeral"
    labels: dict[str, str] = field(default_factory=dict)
    # Provider-neutral metadata attached to the workspace itself (Docker maps
    # it to container Labels, E2B to sandbox metadata, Local ignores it).
    # Deliberately separate from ``labels`` (Docker env-var injection) and
    # from ``repo_url``/``repo_ref`` (clone semantics).
    workspace_metadata: dict[str, str] = field(default_factory=dict)
    # Dedicated workspace network name (D4): the Docker provider attaches the
    # container to THIS bridge network (never the compose/backend network).
    # None -> provider default. The `none` opt-in is expressed via
    # ``egress_policy == "none"``.
    workspace_network: str | None = None
    # First-class repo-clone inputs (FAR-595). Previously smuggled through
    # ``labels["repo_url"]``/``labels["repo_ref"]``, which collided with
    # Docker's labels-as-Env semantics. See the class docstring for the
    # per-provider semantics.
    repo_url: str = ""
    repo_ref: str = ""
    # FAR-1036: root-user opt-in.  When True the Docker provider skips the
    # non-root user stamp (uid 1001) — for images that genuinely cannot run
    # as an arbitrary uid.  The default is False (non-root), so every
    # workspace runs as a non-root uid unless the profile explicitly opts
    # in to root.  Used only by the Docker provider; E2B/Local ignore it.
    allow_root_user: bool = False


@dataclass
class ExecResult:
    """Result of executing a command in a workspace."""

    exit_code: int
    stdout: str
    stderr: str
    duration_ms: int | None = None


@dataclass(frozen=True)
class ExecStreamChunk:
    """One decoded output chunk from a streaming exec (D4 primitive)."""

    stream: str  # "stdout" | "stderr"
    data: str


class ExecProcess:
    """Streaming-exec handle (FAR-590 D4): async output chunks + a kill handle.

    The provider yields decoded chunks as :class:`ExecStreamChunk` values via
    ``chunks`` — the caller (script-mode dispatch) consumes them for live-log
    streaming and stall detection exactly as E2B's background command stream.
    An engine/proxy drop mid-stream is delivered as a stream ERROR (via
    ``error``, never a fabricated ``exit_code == 0``).

    Lifecycle contract:
      - ``done`` events fire when the stream ended (normally or by error);
      - ``exit_code`` stays ``None`` until the END of a healthy stream — a
        consumer that finishes on a stream ERROR must treat the process as
        failed, never as a success (no zero-exit fabrication);
      - ``error`` carries the stream failure description (engine/proxy drop),
        or stays None.
    """

    def __init__(self, chunks: AsyncIterator[ExecStreamChunk], kill: Callable[[], Awaitable[None]]) -> None:
        self.chunks = chunks
        self._kill = kill
        self.done = asyncio.Event()
        self.exit_code: int | None = None
        self.error: str | None = None

    async def kill(self) -> None:
        """Terminate the exec stream + underlying command best-effort."""
        await self._kill()


class RuntimeProvider(ABC):
    """Abstract base for a runtime backend (Docker, K8s, sandbox, etc.)."""

    provider_id = ""
    provider_aliases: frozenset[str] = frozenset()

    @abstractmethod
    async def create_workspace(self, spec: WorkspaceSpec) -> str:
        """Provision a new workspace and return its provider-specific reference.

        WorkspaceSpec semantics every implementation must honour (FAR-595):

        - ``spec.labels``: env-var injection only. Docker maps it to the
          container Env; E2B and Local ignore it. Never read clone inputs
          out of it — those are the first-class ``spec.repo_url`` /
          ``spec.repo_ref`` fields.
        - ``spec.workspace_metadata``: provider-neutral metadata. Docker ->
          container Labels, E2B -> sandbox metadata, Local -> ignored.
        - ``spec.repo_url`` / ``spec.repo_ref``: clone semantics. E2B clones
          into ``/home/user/repo`` (+ optional checkout); Local clones into
          the workspace directory; Docker ignores both.
        """
        ...

    @abstractmethod
    async def exec_command(
        self,
        provider_ref: str,
        command: list[str],
        *,
        cmd_timeout: int | None = None,
    ) -> ExecResult:
        """Run a command inside an existing workspace (collect-then-return)."""
        ...

    async def exec_command_stream(
        self,
        provider_ref: str,
        command: list[str],
        *,
        environment: dict[str, str] | None = None,
    ) -> ExecProcess:
        """Stream a command's output as async chunks with a kill handle (D4).

        The streaming primitive alongside collect-then-return
        :meth:`exec_command` so script-mode's live-log drain + stall/no-output
        detection work on every provider exactly as on E2B. Optional
        base-class method (ADR 040 "Streaming parity"): providers that do not
        override it keep the collect-then-return contract, and the default
        raises the typed :class:`StreamingUnsupportedError` — not a raw
        ``NotImplementedError`` (ADR 040 "Error honesty": an explicit
        carve-out from the contract freeze). A dispatch that *requires*
        streaming fails terminally with this error.
        """
        raise StreamingUnsupportedError(
            f"Runtime provider '{self.__class__.__name__}' does not implement exec_command_stream"
        )

    @abstractmethod
    async def destroy_workspace(self, provider_ref: str) -> None:
        """Tear down a workspace and release all associated resources."""
        ...

    async def destroy_workspace_by_ref(self, provider_ref: str) -> bool:
        """Destroy a workspace using only its provider reference (ADR 040).

        Substrate-level destroy for reclamation paths: the caller supplies a
        ref persisted elsewhere (e.g. ``runs.sandbox_id``), so this must work
        after a process restart or from another process — it may never depend
        on in-process tracking of live workspace handles.

        Contract:
          - **Idempotent**: destroying an already-destroyed or foreign ref is
            a no-op success, not an error;
          - returns ``True`` when the ref is confirmed gone (killed by this
            call, or already gone) and ``False`` when the destroy could not
            be confirmed — the failure is logged and swallowed
            (best-effort), never raised;
          - two-phase destroy-marker semantics (``destroy_intent`` /
            ``confirmed``) are the CALLER's concern, not this primitive's —
            they are delivered in a later slice (ADR 040 workspace-lifetime
            clause);
          - optional base-class method: providers that do not override it
            raise the typed :class:`ProviderCapabilityUnsupportedError` —
            never a raw ``NotImplementedError`` (ADR 040 "Error honesty",
            the same carve-out as the :meth:`exec_command_stream` default).

        ``destroy_workspace``'s existing contract is unchanged — the
        never-tracked-but-live case is served by this primitive.
        """
        raise ProviderCapabilityUnsupportedError(
            f"Runtime provider '{self.__class__.__name__}' does not implement destroy_workspace_by_ref"
        )

    async def read_log_tail(self, provider_ref: str, *, max_bytes: int) -> bytes:
        """Read the workspace log tail as raw bytes (ADR 040).

        Valid from provisioning until substrate reclamation; where the
        substrate retains post-kill logs, valid post-destroy for a bounded
        retention window. ``max_bytes`` caps how much of the NEWEST end of
        the log is returned; the bound is applied as a character slice on the
        decoded tail text before re-encoding (matching the legacy probe), not
        on the encoded byte length.

        Optional base-class method: providers that do not override it
        raise the typed :class:`ProviderCapabilityUnsupportedError` —
        never a raw ``NotImplementedError`` (ADR 040 "Error honesty",
        the same carve-out as the :meth:`exec_command_stream` and
        :meth:`destroy_workspace_by_ref` defaults). Callers that need a
        log tail on a non-overriding provider surface the refusal as a
        terminal named failure; they must never fall back to a
        provider-specific direct call.
        """
        raise ProviderCapabilityUnsupportedError(
            f"Runtime provider '{self.__class__.__name__}' does not implement read_log_tail"
        )

    @abstractmethod
    async def get_workspace_status(self, provider_ref: str) -> str:
        """Return the current status string for the workspace."""
        ...

    def matches_provider_type(self, provider_type: str) -> bool:
        """Return whether this provider implements an explicit profile type."""
        normalized = provider_type.strip().lower()
        return bool(normalized) and normalized in {self.provider_id, *self.provider_aliases}

    async def close(self) -> None:
        """Destroy provider-tracked live workspaces best-effort, then release owned clients.

        Invoked by :meth:`RuntimeProviderHub.aclose` (hub-aclose disposal,
        ADR 029). Implementations must not raise for individual workspace
        failures; the hub is itself best-effort per provider and owns no
        live state after this returns.
        """
        return


def build_hub(max_local_concurrency: int = 2) -> RuntimeProviderHub:
    """Build a fresh RuntimeProviderHub from the process environment.

    A new factory instance is returned on every call — the hub holds no
    process-global state and provider-owned clients are released explicitly
    via :meth:`RuntimeProviderHub.aclose` (per-provision disposal, ADR 029).

    Registration matrix (env-gated, operator opt-in = consent):

    - ``local`` — always registered (host-process fallback tier).
    - ``e2b`` — registered when ``MODULO_E2B_API_KEY`` is set (env var or
      runtime override — both resolved via ``key_bridge.get_e2b_api_key``,
      the same bridge the node-runner enforcement check uses, FAR-1159).
    - ``runner_docker`` (aliases ``docker`` / ``local_docker``) — registered
      when ``MODULO_DOCKER_HOST`` or ``DOCKER_HOST`` is set.  An unrelated
      ``MODULO_RUNNER_*`` variable does NOT register Docker (FAR-996).
    """
    if max_local_concurrency < 1:
        _log.warning(
            "max_local_concurrency=%d is invalid, falling back to 2",
            max_local_concurrency,
        )
        max_local_concurrency = 2

    from modulo.core.runtime_config.key_bridge import get_e2b_api_key
    from modulo.core.runtime_provider.hub import RuntimeProviderHub
    from modulo.core.runtime_provider.local import LocalRuntimeProvider

    hub = RuntimeProviderHub()

    local = LocalRuntimeProvider(max_concurrency=max_local_concurrency)
    hub.register("local", local)

    if get_e2b_api_key():
        try:
            from modulo.core.runtime_provider.e2b import E2BRuntimeProvider

            e2b = E2BRuntimeProvider()
            hub.register("e2b", e2b)
        except ImportError:
            _log.warning("E2B dependency not installed; skipping E2B provider")

    if any(os.environ.get(var) for var in _DOCKER_ENV_VARS):
        try:
            from modulo.core.runtime_provider.docker import DockerRuntimeProvider

            docker = DockerRuntimeProvider()
            hub.register("runner_docker", docker)
        except ImportError:
            _log.warning("Docker dependency not installed; skipping Docker provider")
        except ValueError as exc:
            # FAR-1038: TLS validation failure during provider registration
            # is a configuration error — surface it as a warning (the provider
            # stays unregistered; a dispatch attempt later raises
            # ProviderNotConfiguredError with remediation).
            _log.warning("Docker provider not registered (TLS required): %s", exc)

    return hub


# Backwards-compatible alias — the factory concept is unchanged; new callers
# should prefer :func:`build_hub`.
create_default_hub = build_hub
