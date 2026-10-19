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

FAR-1598 team-scope backstop: the three REST writers FAR-1558 closed
(bind time, pipeline scope change, profile scope change) can all be
bypassed by a path that writes the binding without going through a route
(a direct DB write, a race). Dispatch therefore re-validates the loaded
profile with the SAME shared predicate
(``core.team_visibility.environment_profile_team_mismatch``): a
team-private profile not owned by the pipeline's effective owner team
raises the typed :class:`SandboxDispatchUnboundError` (detail prefixed
with the named ``environment_profile_team_mismatch`` code) BEFORE any
provider is selected — never a silent fallback to another provider or to
the default route. The profile read is team-BLIND but org-scoped for the
same reason (see :func:`load_environment_profile`); org-visible profiles
and NULL bindings resolve exactly as before.

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

from modulo.core.bundled_runner.profile import (
    TEMPLATE_CONFIG_JSON,
    is_placeholder_bundled_runner_image_ref,
)
from modulo.core.runtime_provider import (
    ExecProcess,
    ProviderNotConfiguredError,
    WorkspaceSpec,
    build_hub,
    egress_tier_for_provider_type,
)
from modulo.core.runtime_provider.hub import RuntimeProviderHub
from modulo.util import validate_workspace_network

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable


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

    provider_type: str  # "runner_docker" | "e2b" | "kubernetes" | "none"
    profile: Any = None
    provider: Any = None
    hub: RuntimeProviderHub | None = None
    # FAR-1051 review: on a kubernetes route the bound profile's declared
    # image_ref is the authoritative workspace/pod image. Carried on the route
    # so node_runner's sandbox spec uses the same source as the bundled-runner
    # mapper (``_workspace_spec_for_dispatch``); None on every other route.
    image_ref_override: str | None = None


async def load_environment_profile(
    session_factory: Callable[..., Any] | None,
    org_id: uuid.UUID | None,
    environment_profile_id: uuid.UUID | None,
) -> Any:
    """Load the pipeline-level bound EnvironmentProfile row (same-org enforced).

    FAR-1598: the read is team-BLIND but org-scoped — dispatch is background
    machinery, so it runs with ``set_rls_execution_context`` (the documented
    ``db.rls`` seam for executor/cron/dispatch reads). Without that widened
    context ``rls_team_isolation`` hides a team-owned profile row from this
    org-only session, the lookup would return ``None`` for a binding that
    demonstrably exists, and the dispatch would silently resolve the DEFAULT
    route — precisely the silent fallback the FAR-1598 backstop exists to
    prevent. A genuinely absent / cross-organisation id still resolves to
    ``None`` (org scope unchanged), i.e. the historical default route.

    The read runs in its OWN transaction (``session.begin()``): the
    ``set_rls_*`` helpers require an active transaction, and a bare
    ``async with session_factory() as session`` leaves none — with every
    real session factory (the executor's is ``autobegin=False``) the missing
    ``begin()`` raised ``RuntimeError: set_rls_* requires an active
    transaction`` on the first dispatch that carried a bound profile id
    (latent since FAR-1558 made the snapshot carry one; every unit double
    hid it by reporting ``in_transaction() == True``). This is the same
    ``async with session_factory() as session, session.begin():`` shape the
    13 node_runner readers use.
    """
    from sqlalchemy import select

    from modulo.db.models.environment_profile import EnvironmentProfile
    from modulo.db.rls import set_rls_execution_context, set_rls_org

    if session_factory is None or org_id is None or environment_profile_id is None:
        return None
    async with session_factory() as session, session.begin():
        await set_rls_org(session, org_id)
        await set_rls_execution_context(session)
        result = await session.execute(
            select(EnvironmentProfile).where(
                EnvironmentProfile.id == environment_profile_id,
                EnvironmentProfile.organisation_id == org_id,
                EnvironmentProfile.deleted_at.is_(None),
            )
        )
        return result.scalar_one_or_none()


async def _pipeline_owner_team_for_dispatch(
    session_factory: Callable[..., Any] | None,
    org_id: uuid.UUID | None,
    pipeline_id: Any,
) -> uuid.UUID | None:
    """Resolve the pipeline's EFFECTIVE owner team for the FAR-1598 backstop.

    Source of truth — documented per FAR-1598 ("documenting your source is
    part of the task"):

    * ``PipelineSnapshot`` carries NO owner team of its own (verified: the
      model has ``pipeline_id`` and ``environment_profile_id`` only), so the
      frozen snapshot cannot answer the question;
    * ``runs.owner_team_id`` is stamped FROM this same pipeline row at run
      create (``db.crud.run._resolve_owner_team_id``), and the audited REST
      route path blocks a team transfer while a non-terminal run exists
      (``PipelineHasActiveRunsError``) — so on THAT path, for an in-flight
      run, the live pipeline row and the run's frozen team agree. The block
      is path-scoped, not universal: ``db.crud.update_pipeline`` only applies
      it when audit context is supplied, so callers that pass none (internal
      tooling, MCP) transfer without the check and the two CAN diverge. This
      read therefore re-validates against the live row at dispatch instead of
      assuming agreement; where they have drifted the shared predicate
      refuses — fail-closed over-refusal, never a silent allow;
    * the three FAR-1558 writers all validate against
      ``pipelines.owner_team_id`` — this read re-validates a possibly
      DRIFTED binding against exactly the quantity they enforce, live at
      dispatch time.

    The row is read team-BLIND (``set_rls_execution_context``) because
    ``rls_team_isolation`` would otherwise hide a team-private pipeline from
    this org-only session and turn a legal same-team binding into a refusal;
    ``include_soft_deleted`` matches the executor's in-flight-run posture
    (a soft-deleted pipeline's runs still dispatch).

    Returns ``None`` when the id is absent/unparseable, no session factory
    is available, or no row resolves — a value the shared predicate treats
    as "no owner team", so a team-private profile then mismatches every
    pipeline (fail closed, never fail open).
    """
    from sqlalchemy import select

    from modulo.db.models.pipeline import Pipeline
    from modulo.db.rls import set_rls_execution_context, set_rls_org
    from modulo.db.soft_delete import include_soft_deleted

    pipeline_uuid = _parse_uuid(pipeline_id)
    if session_factory is None or org_id is None or pipeline_uuid is None:
        return None
    # Own transaction — the ``set_rls_*`` contract (see load_environment_profile).
    async with session_factory() as session, session.begin():
        await set_rls_org(session, org_id)
        await set_rls_execution_context(session)
        result = await session.execute(
            include_soft_deleted(
                select(Pipeline.owner_team_id).where(
                    Pipeline.id == pipeline_uuid,
                    Pipeline.organisation_id == org_id,
                )
            )
        )
        owner_team: Any = result.scalar_one_or_none()
        # Anything that is not a UUID (a hidden row -> None, or a malformed
        # value from a stand-in session) is treated as "no owner team", which
        # the shared predicate then refuses for a team-private profile —
        # fail closed, never fail open.
        return owner_team if isinstance(owner_team, uuid.UUID) else None


def _assert_profile_team_scope_for_dispatch(
    profile: Any,
    *,
    pipeline_owner_team_id: uuid.UUID | None,
) -> None:
    """FAR-1598: refuse a team-private profile the pipeline's team does not own.

    Verdict comes from the SHARED ``environment_profile_team_mismatch``
    predicate and the named ``environment_profile_team_mismatch`` code in
    ``core.team_visibility`` — the same rule the three FAR-1558 REST writers
    apply — so write-time and dispatch-time can never drift. The refusal is
    the existing typed :class:`SandboxDispatchUnboundError` family (no new
    exception type): the caller invokes this BEFORE any provider/hub
    resolution, so a refused binding selects no provider and never falls
    back to the default route.

    ``pipeline_owner_team_id`` is the effective owner team resolved by
    :func:`_pipeline_owner_team_for_dispatch`; ``None`` (unknown/unresolvable)
    is a fail-closed input — a team-private profile then mismatches every
    pipeline.

    Detail is generic on purpose (FAR-1558 F3 parity): the run's error may be
    readable by a caller who is NOT a member of the profile's owner team, so
    it must not leak the profile's name, id or owning team — clients branch
    on the named code, and the identifying detail is logged server-side only.
    """
    from modulo.core.team_visibility import (
        ENVIRONMENT_PROFILE_TEAM_MISMATCH,
        environment_profile_team_mismatch,
    )

    visibility = getattr(profile, "visibility", None)
    owner_team_id = getattr(profile, "owner_team_id", None)
    if not environment_profile_team_mismatch(visibility, owner_team_id, pipeline_owner_team_id):
        return
    _log.warning(
        "runner_dispatch.team_scope_refused",
        extra={
            "profile_id": str(getattr(profile, "id", "")),
            "profile_owner_team_id": str(owner_team_id),
            "pipeline_owner_team_id": str(pipeline_owner_team_id),
        },
    )
    raise SandboxDispatchUnboundError(
        f"{ENVIRONMENT_PROFILE_TEAM_MISMATCH}: the bound environment profile is team-private "
        "and is not owned by this pipeline's owner team. Dispatch refuses the binding and "
        "selects no provider — re-bind the pipeline to a profile its owner team owns, or "
        "re-scope the profile."
    )


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


async def _assert_team_scope_backstop(
    session_factory: Callable[..., Any] | None,
    org_uuid: Any,
    pipeline_id: Any,
    profile: Any,
) -> None:
    """FAR-1598 team-scope backstop: refuse a cross-team private profile.

    The owner-team read is skipped unless the profile is team-private — the
    shared predicate can only return False for an org-visible one, so the
    org/NULL path issues no extra query.
    """
    if (getattr(profile, "visibility", None) or "org") != "team":
        return
    _assert_profile_team_scope_for_dispatch(
        profile,
        pipeline_owner_team_id=await _pipeline_owner_team_for_dispatch(session_factory, org_uuid, pipeline_id),
    )


def _assert_bundled_runner_image_provisionable(profile: Any) -> None:
    """Fail loud when a runner_docker profile still carries the placeholder digest."""
    if not is_placeholder_bundled_runner_image_ref(getattr(profile, "image_ref", None)):
        return
    raise SandboxDispatchUnboundError(
        f"Environment profile '{getattr(profile, 'name', profile)}' is bound to the Bundled "
        "Runner (runner_docker) but its image_ref still carries the release-advanced placeholder "
        "digest (sha256:0000…0000) and cannot provision a workspace. This is a known GA follow-up: "
        "the GHCR publish job bumps the pinned digest. See "
        "docs/security/bundled-runner-operator-guide.md. Re-bind the pipeline to a provisionable "
        "profile, or wait for the digest to land."
    )


def _assert_kubernetes_image_ref(profile: Any) -> None:
    """Fail loud when a kubernetes profile declares no workspace pod image."""
    if (getattr(profile, "image_ref", None) or "").strip():
        return
    raise SandboxDispatchUnboundError(
        f"Environment profile '{getattr(profile, 'name', profile)}' is bound to the "
        "Kubernetes provider but declares no image_ref; the Kubernetes tier requires the "
        "workspace pod image (an E2B template_id is not a container image), so set the "
        "profile's image_ref to a runner container image before dispatching."
    )


def _build_dispatch_hub_provider(profile: Any) -> tuple[Any, Any]:
    """Build a fresh hub (concurrency-wired) and resolve *profile* to a provider.

    A provider/resolve failure surfaces as the typed
    :class:`SandboxDispatchUnboundError` (never a bare provider error).
    """
    try:
        from modulo.core.runtime_config.key_bridge import override_int_or
        from modulo.settings import get_settings

        settings = get_settings()
        # FAR-1135: MODULO_MAX_LOCAL_CONCURRENCY is hot-reloadable.
        concurrency = override_int_or(
            "MODULO_MAX_LOCAL_CONCURRENCY",
            int(getattr(settings, "modulo_max_local_concurrency", 2) or 2),
        )
        hub = build_hub(max_local_concurrency=concurrency)
        provider = hub.resolve(profile)
    except ProviderNotConfiguredError as exc:
        raise SandboxDispatchUnboundError(str(exc)) from exc
    return hub, provider


async def resolve_sandbox_dispatch_route(
    session_factory: Callable[..., Any] | None,
    org_id_raw: Any,
    environment_profile_id_raw: Any,
    pipeline_id: Any = None,
) -> RunnerDispatchRoute:
    """Resolve the dispatch route for a sandbox_agent node (D4 dispatch adapter).

    - No bound (or missing) profile -> ``provider_type="none"`` (the
      historical E2B default route).
    - ``e2b`` -> e2b route (the caller applies the LOUD timeout validation
      before provisioning).
    - ``runner_docker`` -> hub-resolved Docker provider (the registration
      matrix per ADR 029: ``MODULO_DOCKER_HOST`` or ``DOCKER_HOST``
      registers the provider), plus the locked-ephemeral check.
    - ``kubernetes`` (FAR-1051) -> hub-resolved Kubernetes provider — the
      registration matrix's ``MODULO_KUBERNETES_ENABLED`` gate — so an
      unregistered provider fails LOUD here with the typed config error
      (carrying its remediation) instead of deep inside provisioning. The
      route carries provider + hub so node_runner reuses that ONE instance
      as the dispatch provider and disposes the hub in its finally.
    - ``local`` / ``local_docker`` -> dispatch-unbound: typed config error
      (the D4 upgrade rule — never silently activated).

    FAR-1598 team-scope backstop: right after the profile loads — and BEFORE
    any provider branch — a team-private profile is validated against
    ``pipeline_id``'s effective owner team with the shared FAR-1558
    predicate; a mismatch raises the typed
    :class:`SandboxDispatchUnboundError` (named
    ``environment_profile_team_mismatch`` detail) so no provider is selected
    and there is no fallback to another provider or to the default route.
    ``pipeline_id`` is the run's pipeline (the caller threads it from the
    conformance ctx the executor froze); absent it, the owner team resolves
    to ``None`` and a team-private profile is refused (fail closed).
    Org-visible profiles and NULL bindings are unaffected.
    """
    org_uuid = _parse_uuid(org_id_raw)
    profile_uuid = _parse_uuid(environment_profile_id_raw)
    profile = await load_environment_profile(session_factory, org_uuid, profile_uuid)
    if profile is None:
        return RunnerDispatchRoute(provider_type="none", profile=None)
    # FAR-1598: the backstop runs FIRST, before any provider branch, so a
    # drifted/forged cross-team binding can never select a provider or reach
    # the default route. The owner-team read is skipped unless the profile is
    # team-private — the shared predicate can only return False for an
    # org-visible one, so the org/NULL path issues no extra query and behaves
    # exactly as before.
    await _assert_team_scope_backstop(session_factory, org_uuid, pipeline_id, profile)
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
        _assert_bundled_runner_image_provisionable(profile)
        hub, provider = _build_dispatch_hub_provider(profile)
        return RunnerDispatchRoute(
            provider_type="runner_docker",
            profile=profile,
            provider=provider,
            hub=hub,
        )
    if provider_type == "kubernetes":
        # FAR-1051: mirrors the runner_docker branch's hub resolution EXACTLY
        # (fresh hub -> hub.resolve(profile) -> typed config error on failure),
        # because route resolution is where an unregistered provider must fail
        # LOUD — with the remediation env var — rather than deep inside
        # node_runner's provisioning path. Deliberately NOT mirrored from
        # docker: no placeholder-digest check (that pins the Bundled Runner's
        # own image ref), and no E2B timeout validation (Kubernetes
        # provisioning is provider-managed, ADR 029).
        #
        # Error family: ``UnknownProviderTypeError`` subclasses
        # ``ProviderNotConfiguredError``, so this one handler maps BOTH to the
        # typed dispatch-unbound error; the ADR 040 new-family errors
        # (``RateLimitedError`` and friends, ``RuntimeProviderError``) are not
        # caught and propagate unwrapped (error-family site map A).
        #
        # FAR-1051 review: the Kubernetes tier requires an explicit pod image.
        # ``node_runner`` falls back to the node's E2B ``template_id`` when the
        # route carries no image_ref, and a template_id is not a container
        # image — the pod then ImagePullBackOffs mid-run. Fail LOUD here,
        # BEFORE building the hub (mirroring the runner_docker
        # placeholder-digest check above), so the operator is told to set the
        # profile's image_ref rather than discovering it from a stuck pod.
        _assert_kubernetes_image_ref(profile)
        # The returned provider + hub are consumed by ``node_runner``: the
        # provider becomes the dispatch's provider (one client per dispatch)
        # and the hub is aclosed in the dispatch's finally.
        hub, provider = _build_dispatch_hub_provider(profile)
        return RunnerDispatchRoute(
            provider_type="kubernetes",
            profile=profile,
            provider=provider,
            hub=hub,
            # FAR-1051 review: the operator's declared image_ref governs the pod
            # image, matching the bundled-runner mapper — so the sandbox route
            # never silently substitutes the node's E2B template_id.
            image_ref_override=getattr(profile, "image_ref", None),
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
    node_egress_policy: str | None = None,
    node_egress_allowlist: list[dict[str, str | int]] | None = None,
) -> WorkspaceSpec:
    """Build the hardened WorkspaceSpec for a Bundled Runner dispatch.

    Hardening (ADR 029 / plan D4): non-root user, read-only rootfs + tmpfs
    workdir (adequate $HOME sizing), dropped caps + no-new-privileges, 1.0
    CPU / 1 GiB (fixed committed value), structured labels mapped to
    container Labels (modulo.run/org/node.id), the dedicated workspace
    bridge network (never the compose/backend network), and the egress
    default permitted (the tier's purpose) with the per-profile ``none``
    opt-in.

    Egress resolution (FAR-1085): uses :func:`resolve_egress` with the TIER
    the profile's provider resolves to (FAR-1051 — previously hardcoded to
    ``docker``), so both node-level and profile-level policies are
    considered, and ``selected`` without an allowlist or on a
    non-enforcing tier returns a refusal.
    """
    from modulo.core.pipeline_engine.egress import (
        WORKSPACE_METADATA_EGRESS_ALLOWLIST_KEY,
        resolve_egress,
        spec_egress_for_canonical,
    )

    cfg = getattr(profile, "config_json", None) or {}
    metadata: dict[str, str] = {
        "modulo.run.id": run_id,
        "modulo.org.id": str(org_id) if org_id else "",
        "modulo.node.id": node_id,
    }

    # FAR-1085: canonical egress resolution — node -> profile -> provider default,
    # with tier capability check (e.g. Docker cannot enforce "selected").
    #
    # FAR-1051: the TIER follows the profile's RESOLVED provider instead of the
    # historical hardcoded "docker", so a kubernetes-bound profile resolves —
    # and, where the tier cannot enforce the policy, REFUSES by name — on the
    # tier its dispatch actually runs (a hardcoded docker tier let such a
    # profile certify an enforcement the provider never performs). An
    # unrecognised provider_type fails CLOSED: the raw value is passed through
    # so ``resolve_egress`` refuses it as an unknown tier rather than silently
    # defaulting to an enforceable one. A profile with no declared type keeps
    # this arm's historical Docker tier (the Bundled Runner is the only route
    # that reached this mapper before FAR-1051).
    _provider_type = getattr(profile, "provider_type", None)
    _provider_type = _provider_type.strip().lower() if isinstance(_provider_type, str) else ""
    _tier = egress_tier_for_provider_type(_provider_type) or (_provider_type or "docker")
    egress_resolved = resolve_egress(
        node_egress_policy=node_egress_policy,
        node_egress_allowlist=node_egress_allowlist,
        profile_network_policy=getattr(profile, "network_policy", None),
        tier=_tier,
    )
    if egress_resolved.refusal is not None:
        from modulo.core.pipeline_engine.node_runner import SandboxTierRefusedError

        raise SandboxTierRefusedError(
            f"Node '{node_id}' egress refused on {_tier.capitalize()} tier: {egress_resolved.refusal}"
        )

    # FAR-1690: record the TYPED egress posture on the workspace, never a
    # boolean. On this route the reachable tiers (docker, kubernetes) yield
    # ``enforced_by_substrate`` for a restrictive docker policy and ``none``
    # for the unrestricted default; the value names HOW the posture is
    # established so a permissive tier is never read as an enforcement it
    # does not perform.
    metadata["modulo.egress.posture"] = egress_resolved.posture.value

    # Map canonical policy to WorkspaceSpec egress_policy vocabulary
    # LOSSLESSLY (FAR-1050): the shared helper is the single owner of this
    # mapping — identical to the profile-path mapper in
    # api/routes/environment_profiles.py, so the two cannot drift (ADR 040).
    # The tiers this mapper runs on today (docker, kubernetes) both refuse
    # "selected" above (tier capability), so only None/"deny_all" are
    # reachable — the lossless mapping is defence-in-depth.
    spec_egress = spec_egress_for_canonical(egress_resolved.policy)
    # FAR-1050 R5 parity: carry the selected-mode allowlist in the same
    # metadata key the legacy E2B create path stamps and the E2B provider
    # reads. Unreachable here today (every reachable tier refuses "selected"),
    # stamped as defence-in-depth against a future allowlist source silently
    # behaving as deny_all.
    if egress_resolved.policy == "selected" and egress_resolved.allowlist:
        metadata[WORKSPACE_METADATA_EGRESS_ALLOWLIST_KEY] = json.dumps(egress_resolved.allowlist)

    # Defense-in-depth (FAR-1020): validate workspace_network at dispatch
    # even if the CRUD boundary already validated it — a value written
    # directly to the DB could bypass the route validation.
    raw_network = cfg.get("workspace_network")
    validated_network = validate_workspace_network(raw_network)
    # FAR-1051 (qa-iterate): the FALLBACK timeout is read from the shipped
    # profile default block (``TEMPLATE_CONFIG_JSON["timeout_seconds"]``) —
    # the same constant the seeded profile's own config starts from — so the
    # dispatch default and the profile default can never drift apart. This is
    # the previous expression with ONLY the magic literal replaced: an
    # explicitly-set ``config_json.timeout_seconds`` keeps its exact
    # historical coercion (``int`` of the raw value, TypeError on a
    # non-numeric one, AttributeError on a malformed non-dict config — never
    # a silent substitute default).
    _timeout_seconds = int(cfg.get("timeout_seconds", TEMPLATE_CONFIG_JSON["timeout_seconds"]))
    return WorkspaceSpec(
        environment_profile_id=profile.id,
        organisation_id=org_id,  # type: ignore[arg-type]
        run_id=run_uuid,
        image_ref=getattr(profile, "image_ref", None) or "",
        capabilities=getattr(profile, "capabilities_json", None) or [],
        timeout_seconds=_timeout_seconds,
        resource_limits={"memory_mb": int(cfg.get("memory_mb", 1024))},
        egress_policy=spec_egress,
        persistence_policy=getattr(profile, "persistence_policy", "ephemeral"),
        labels={},
        workspace_metadata={key: value for key, value in metadata.items() if value},
        workspace_network=validated_network,
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

    pump_task = asyncio.create_task(
        _pump_exec_to_queue(
            exec_process,
            queue,
            stream_broker=stream_broker,
            node_id=node_id,
            throttle_state=throttle_state,
        )
    )
    deadline = time.monotonic() + sandbox_timeout
    last_progress = time.monotonic()
    timed_out = False
    stalled = False
    try:
        while True:
            kind, item = await _next_stream_item(
                queue, deadline=deadline, last_progress=last_progress, stall_timeout=stall_timeout
            )
            if kind == "timeout":
                timed_out = True
                break
            if kind == "stall":
                stalled = True
                break
            if item is None:
                break
            collected.append(item)
            last_progress = time.monotonic()
            if stall_detector is not None:
                stall_detector.touch("output")
                if touch_heartbeat:
                    stall_detector.touch("heartbeat")
    finally:
        await _finalize_stream_consume(exec_process, pump_task, node_id=node_id, kill=timed_out or stalled)
    if exec_process.error:
        _log.info(
            "runner_dispatch.exec_stream_error",
            extra={"node_id": node_id, "error": exec_process.error[:200]},
        )
    return collected, timed_out, stalled


async def _pump_exec_to_queue(
    exec_process: ExecProcess,
    queue: asyncio.Queue[tuple[str, str] | None],
    *,
    stream_broker: Any | None,
    node_id: str,
    throttle_state: dict[str, Any],
) -> None:
    """Pump exec chunks into the queue (publishing to the broker), then None."""
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


async def _next_stream_item(
    queue: asyncio.Queue[tuple[str, str] | None],
    *,
    deadline: float,
    last_progress: float,
    stall_timeout: float,
) -> tuple[str, tuple[str, str] | None]:
    """Wait for the next queued chunk within the deadline/stall window.

    Returns ``("item", chunk)`` (chunk ``None`` is the stream-end sentinel),
    ``("timeout", None)`` when the total deadline passed, or
    ``("stall", None)`` when no output arrived within ``stall_timeout``.
    """
    while True:
        now = time.monotonic()
        if now >= deadline:
            return "timeout", None
        wait = min(deadline, last_progress + stall_timeout) - now
        if wait <= 0:
            return "stall", None
        try:
            item = await asyncio.wait_for(queue.get(), timeout=max(wait, 0.05))
        except TimeoutError:
            continue
        return "item", item


async def _finalize_stream_consume(
    exec_process: ExecProcess,
    pump_task: asyncio.Task[None],
    *,
    node_id: str,
    kill: bool,
) -> None:
    """Kill the exec (when timed out/stalled), cancel the pump and join it."""
    if kill:
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

    # FAR-811: node > pipeline > org resolution.  Node-explicit settings always
    # win — including a node that sets ONLY stdout_max_bytes without
    # stdout_retention_mode: its explicit max_bytes must win, while the mode is
    # inherited from the pipeline default (or the legacy "tail" default).
    _node_raw_mode = node_def.get("stdout_retention_mode")
    _node_raw_max_bytes = node_def.get("stdout_max_bytes")
    if _node_raw_mode is not None:
        mode = _coerce_stdout_retention_mode(_node_raw_mode)
        max_bytes = _coerce_stdout_max_bytes(_node_raw_max_bytes)
    elif _node_raw_max_bytes is not None:
        mode = _coerce_stdout_retention_mode(pipeline_default.get("mode")) if pipeline_default is not None else "tail"
        max_bytes = _coerce_stdout_max_bytes(_node_raw_max_bytes)
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


def _render_agent_template(
    *,
    sandbox_mode: str,
    agent_command: str,
    agent_prompt_template: str,
    state: dict[str, Any],
    scoped_run_context: dict[str, Any],
    raw_input: Any,
    run_id: str,
    node_id: str,
    node_def: dict[str, Any] | None = None,
) -> tuple[str, str, str] | dict[str, Any]:
    """Render agent prompt and command templates for the sandbox.

    Returns ``(rendered_prompt, rendered_agent_command, input_json)`` on
    success, or a skip-status dict when the template references missing input
    fields (the caller returns this dict directly as the node result).
    """
    if sandbox_mode == "script":
        return ("", agent_command, json.dumps(raw_input))
    # FAR-226: route through the shared sandbox Jinja helper (single source of
    # truth with the save-time validator + the E2B node-runner path). Imported
    # lazily so this dependency-light module does not drag in pipeline_engine.
    from modulo.core.pipeline_engine.sandbox_mode import sandbox_jinja_environment

    env = sandbox_jinja_environment()
    scoped_state = dict(state)
    scoped_state["run_context"] = scoped_run_context
    template_vars: dict[str, Any] = {
        "state": scoped_state,
        "run_context": scoped_run_context,
        "input": raw_input,
    }
    # FAR-1743 / ADR 037 rev 2 (EXPERIMENTAL): the top-level `{{ model }}`
    # template variable from the SNAPSHOT's baked `_resolved_model`, with the
    # run's frozen `_run_overrides.model` folded in pre-dispatch (run override
    # wins) — the same interpolation the E2B node-runner path performs; never a
    # registry read and never a write to `input.model`. Reads the FULL
    # run_context from state (not the context_scope-filtered view). Fail closed
    # before any workspace provisioning when an opted-in node has no value.
    # Lazy import: this module stays dependency-light (mirrors the jinja helper).
    from modulo.core.model_class_registry import effective_model_template_value

    _node_def = node_def or {}
    _model_value = effective_model_template_value(_node_def, state.get("run_context") or {})
    if _model_value is not None:
        template_vars["model"] = _model_value
    elif _node_def.get("model_class"):
        raise ValueError(
            f"sandbox_agent node '{node_id}' declares model_class "
            f"{_node_def.get('model_class')!r} but no model was resolved at snapshot mint "
            "(no baked _resolved_model and no run-override model) — refusing to dispatch "
            "pre-sandbox (ADR 037 D3)"
        )
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


@dataclass(frozen=True)
class _ProvisionRequest:
    """Inputs to ``_provision_workspace`` (grouped to keep the call short)."""

    state: dict[str, Any]
    org_id: str
    run_id: str
    node_id: str
    route: RunnerDispatchRoute
    org_uuid: uuid.UUID | None
    run_uuid: uuid.UUID | None
    sandbox_mode: str
    context_files: dict[str, str]
    input_json: str
    raw_input: Any
    rendered_prompt: str
    node_egress_policy: str | None = None
    node_egress_allowlist: list[dict[str, str | int]] | None = None


async def _provision_workspace(
    session_factory: Callable[..., Any] | None,
    request: _ProvisionRequest,
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
        claim_lease=request.state.get("_claim_lease"),
        org_id=request.org_id,
        run_id=request.run_id,
        node_id=request.node_id,
        provider=request.route.provider_type,
    )
    if attempt_key is None:
        raise SupersededNodeError(
            "Bundled Runner dispatch marker denied — run superseded or not running; workspace not created"
        )

    profile = request.route.profile
    provider = request.route.provider
    if profile is None or provider is None:
        raise RuntimeError("Bundled Runner route resolved without a profile/provider")
    spec = _workspace_spec_for_dispatch(
        profile,
        org_id=request.org_uuid,
        run_id=request.run_id,
        node_id=request.node_id,
        run_uuid=request.run_uuid,
        node_egress_policy=request.node_egress_policy,
        node_egress_allowlist=request.node_egress_allowlist,
    )
    provider_ref = await provider.create_workspace(spec)
    _emit_script_span_event(
        "script.provisioned",
        {"sandbox_id": provider_ref, "provider": "runner_docker", "mode": request.sandbox_mode},
    )
    await _sandbox_store_dispatch_marker_sandbox(
        provider_ref,
        session_factory=session_factory,
        claim_lease=request.state.get("_claim_lease"),
        org_id=request.org_id,
        run_id=request.run_id,
        attempt_key=attempt_key,
        provider=request.route.provider_type,
    )

    for raw_path, raw_content in request.context_files.items():
        write_path = raw_path.removesuffix(".b64") if raw_path.endswith(".b64") else raw_path
        write_content = base64.b64decode(raw_content).decode() if raw_path.endswith(".b64") else raw_content
        await _write_file_via_exec(provider, provider_ref, write_path, write_content)
    if request.sandbox_mode == "script":
        await _write_file_via_exec(provider, provider_ref, _INPUT_JSON_PATH, request.input_json)
    else:
        safe_input = request.input_json
        if len(request.input_json) > 10240:
            safe_input = json.dumps(
                {
                    "_truncated": True,
                    "_key_count": len(request.raw_input) if isinstance(request.raw_input, dict) else 0,
                }
            )
        await _write_file_via_exec(provider, provider_ref, _INPUT_JSON_PATH, safe_input)
        await _write_file_via_exec(provider, provider_ref, _PROMPT_PATH, request.rendered_prompt)

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
    provider: str,
) -> tuple[dict[str, str], bool]:
    """Resolve sandbox env vars and perform pre-run setup.

    Returns ``(sandbox_envs, script_lease_claimed)``. In script mode this also
    claims the script lease, mints a run API key, and checks the wallclock
    budget (raising :class:`ScriptBudgetKilledError` on overrun). EVERY mode
    (script AND llm) issues a run-scoped credential through the
    ``CredentialIssuer`` seam (FAR-1690 A1) and stamps its issuance provenance.
    """
    from modulo.core.credential_issuer import RUN_CREDENTIAL_PROVENANCE_ENV
    from modulo.core.pipeline_engine.node_runner import (
        ScriptBudgetKilledError,
        _build_sandbox_envs,
        _emit_script_span_event,
        _sandbox_issue_run_credential_for_sandbox,
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
            provider=provider,
        )
        script_lease_claimed = True
        _emit_script_span_event(
            "script.lease_claimed",
            {"run_id": run_id, "node_id": node_def.get("node_id", "")},
        )

    # FAR-1690 slice A1: EVERY dispatch mode (script AND llm) issues a
    # short-TTL, run-scoped credential through the CredentialIssuer seam and
    # stamps its issuance provenance (``modulo_minted`` — attested, NEVER
    # ``observed``). Fail-open: an issuance failure leaves the key absent (the
    # workspace still runs).
    issued = await _sandbox_issue_run_credential_for_sandbox(
        session_factory=session_factory,
        org_id=org_id,
        run_id=run_id,
        node_id=node_def.get("node_id", ""),
        sandbox_timeout=sandbox_timeout,
    )
    if issued is not None:
        sandbox_envs["MODULO_API_KEY"] = issued.secret
        sandbox_envs[RUN_CREDENTIAL_PROVENANCE_ENV] = issued.provenance

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


@dataclass(frozen=True)
class _OutputReadRequest:
    """Inputs to ``_read_and_validate_output`` (grouped to keep the call short)."""

    provider: Any
    provider_ref: str
    session_factory: Callable[..., Any] | None
    run_id: str
    org_id: str
    node_id: str
    attempt_key: str
    agent_stdout_raw: str
    agent_stderr_raw: str
    delivery_sentinel: str | None
    stdout_cap: int
    exit_code: int
    sandbox_mode: str
    script_lease_claimed: bool
    output_schema_json: Any
    schema_validator_mode: str = "lenient"


def _output_parse_error(raw_output_str: str, output_json: Any) -> str:
    """Human-readable reason the output.json payload could not be used."""
    if raw_output_str and not isinstance(output_json, dict) and output_json is not None:
        return f"output.json parsed to non-dict type {type(output_json).__name__}"
    if raw_output_str:
        return "output.json is not valid JSON"
    return "output.json is empty or JSON null"


def _no_output_error(node_id: str, sandbox_mode: str, script_lease_claimed: bool) -> Exception:
    """The typed error for a genuinely-empty output.json (script vs runner)."""
    from modulo.core.pipeline_engine.node_runner import (
        SandboxNodeFailedError,
        ScriptInvalidOutputError,
    )

    if sandbox_mode == "script" and script_lease_claimed:
        return ScriptInvalidOutputError(_no_output_message(node_id))
    return SandboxNodeFailedError(_no_output_message(node_id), node_id=node_id)


async def _handle_schema_validation_failure(
    request: _OutputReadRequest, raw_output_str: str, schema_exc: ValueError
) -> None:
    """Retain the marker and raise the typed failure for a schema violation."""
    from modulo.core.pipeline_engine.node_runner import (
        SandboxNodeFailedError,
        ScriptInvalidOutputError,
        _RawOutputMarkerRequest,
        _retain_raw_output_marker,
    )

    _log.exception(
        "sandbox_agent.runner.schema_validation_failed",
        extra={"node_id": request.node_id},
    )
    if request.sandbox_mode == "script" and request.script_lease_claimed:
        raise ScriptInvalidOutputError(
            f"Script-mode output failed schema validation for node {request.node_id!r}: {schema_exc}"
        ) from None
    await _retain_raw_output_marker(
        request.session_factory,
        _RawOutputMarkerRequest(
            run_id=request.run_id,
            org_id_raw=request.org_id,
            node_id=request.node_id,
            attempt_key=request.attempt_key,
            summary=("Bundled Runner agent output failed declared output schema validation — raw output retained"),
            source=_combine_raw_outputs(raw_output_str, request.agent_stdout_raw),
            parse_error=str(schema_exc),
            exit_code=request.exit_code,
            stdout_length=len(request.agent_stdout_raw),
            stderr_length=len(request.agent_stderr_raw),
            delivery_sentinel=request.delivery_sentinel,
            max_artifact_bytes=request.stdout_cap,
        ),
    )
    raise SandboxNodeFailedError(
        _schema_failure_message(node_id=request.node_id, schema_exc=str(schema_exc)),
        node_id=request.node_id,
    ) from None


async def _read_and_validate_output(request: _OutputReadRequest) -> Any:
    """Read output.json from the workspace, parse it, and validate against schema.

    Returns the parsed output dict (or a non-dict parseable value).  Raises
    on truly-empty output, script-mode non-zero exit, or schema validation
    failure.  Retains raw-output markers on every error path.
    """
    from modulo.core.pipeline_engine.node_runner import (
        ScriptFailedError,
        _RawOutputMarkerRequest,
        _retain_raw_output_marker,
        _validate_against_schema,
    )

    raw_output_str = await _read_file_via_exec(request.provider, request.provider_ref, _OUTPUT_JSON_PATH)
    output_json: Any = None
    if raw_output_str:
        try:
            output_json = json.loads(raw_output_str)
        except json.JSONDecodeError:
            output_json = None

    if output_json is None or not isinstance(output_json, dict):
        parse_error = _output_parse_error(raw_output_str, output_json)
        await _retain_raw_output_marker(
            request.session_factory,
            _RawOutputMarkerRequest(
                run_id=request.run_id,
                org_id_raw=request.org_id,
                node_id=request.node_id,
                attempt_key=request.attempt_key,
                summary="Bundled Runner agent produced no parseable output.json — raw output retained",
                source=_combine_raw_outputs(raw_output_str, request.agent_stdout_raw),
                parse_error=parse_error,
                exit_code=request.exit_code,
                stdout_length=len(request.agent_stdout_raw),
                stderr_length=len(request.agent_stderr_raw),
                delivery_sentinel=request.delivery_sentinel,
                max_artifact_bytes=request.stdout_cap,
            ),
        )
        if output_json is None:
            raise _no_output_error(request.node_id, request.sandbox_mode, request.script_lease_claimed)

    if request.sandbox_mode == "script" and request.exit_code != 0:
        raise ScriptFailedError(
            f"Script-mode Bundled Runner exited with code {request.exit_code} (post-claim, terminal)"
        )

    if isinstance(request.output_schema_json, dict) and isinstance(output_json, dict):
        try:
            # FAR-899: honour the operator-toggled schema validator mode
            # (resolved by _sandbox_agent_impl and threaded through the config).
            _validate_against_schema(output_json, request.output_schema_json, mode=request.schema_validator_mode)
        except ValueError as schema_exc:
            await _handle_schema_validation_failure(request, raw_output_str, schema_exc)

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
    from modulo.core.pipeline_engine.node_runner import _RawOutputMarkerRequest, _retain_raw_output_marker

    if not (delivery_sentinel and _source_contains_sentinel(agent_stdout_raw, delivery_sentinel)):
        return
    try:
        await _retain_raw_output_marker(
            session_factory,
            _RawOutputMarkerRequest(
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
            ),
        )
    except Exception:
        _log.exception(
            "sandbox_agent.runner.delivery_marker_persist_failed",
            extra={"node_id": node_id},
        )


async def _resolve_effective_stdout_cap(config: Any, node_def: dict[str, Any], *, node_id: str) -> int:
    """Resolve the node's effective stdout/stderr retention cap (FAR-792 / FAR-811).

    The org-level hard ceiling (``system_config.sandbox_stdout_retention_max_bytes``)
    is read and applied here too, so the "no node can exceed the org ceiling"
    invariant holds on the Bundled Runner path exactly as on the E2B path; a
    clamp is logged.
    """
    from modulo.core.pipeline_engine.node_runner import _read_org_stdout_retention_ceiling

    org_stdout_ceiling = await _read_org_stdout_retention_ceiling(config.session_factory)
    pipeline_ret_cfg = getattr(config, "pipeline_stdout_retention_config", None)
    stdout_cap_unclamped = _resolve_stdout_cap(node_def, pipeline_default=pipeline_ret_cfg)
    stdout_cap = _resolve_stdout_cap(node_def, org_ceiling=org_stdout_ceiling, pipeline_default=pipeline_ret_cfg)
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
    return stdout_cap


def _runner_retention_artifacts(
    *,
    node_id: str,
    run_id: str,
    org_id: str,
    attempt_key: str,
    agent_stdout_raw: str,
    agent_stderr_raw: str,
    stdout_cap: int,
) -> tuple[str, str, int, int, bool, dict[str, Any] | None, dict[str, Any] | None]:
    """Redact+truncate the streams and persist the over-cap side-car artifacts.

    Returns ``(agent_stdout, agent_stderr, stdout_len, stderr_len,
    stdout_truncated, stdout_artifact, stderr_artifact)``. FAR-792/811/879:
    redaction happens BEFORE truncation (credential-scrubbing sees the full
    stream); the full redacted transcripts are persisted best-effort when they
    exceed the inline cap.
    """
    from modulo.core.pipeline_engine.node_runner import (
        _persist_full_stderr_artifact,
        _persist_full_stdout_artifact,
        _redact_raw_output,
    )

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
    stdout_artifact: dict[str, Any] | None = None
    if stdout_truncated:
        stdout_artifact = _persist_full_stdout_artifact(
            org_id=org_id,
            run_id=run_id,
            node_id=node_id,
            attempt_key=attempt_key,
            node_cap=stdout_cap,
            redacted_stdout=_redact_raw_output(agent_stdout_raw),
        )
    stderr_artifact: dict[str, Any] | None = None
    if stderr_len > stdout_cap:
        stderr_artifact = _persist_full_stderr_artifact(
            org_id=org_id,
            run_id=run_id,
            node_id=node_id,
            attempt_key=attempt_key,
            node_cap=stdout_cap,
            redacted_stderr=_redact_raw_output(agent_stderr_raw),
        )
    return agent_stdout, agent_stderr, stdout_len, stderr_len, stdout_truncated, stdout_artifact, stderr_artifact


async def _harvest_gh_claim_receipt(
    provider: Any, provider_ref: str, run_id: str
) -> tuple[bool | None, asyncio.CancelledError | None]:
    """Bounded, cancellation-safe harvest of the one-PR guard receipt (FAR-1315).

    Returns ``(receipt, cancellation)``. The probe task is always
    awaited-or-cancelled (never left running against a container the teardown
    destroys); a cancellation is RECORDED, not re-raised here, so teardown still
    runs and the dispatch re-raises it afterwards.
    """
    if provider is None or not provider_ref:
        return None, None
    from modulo.core.pipeline_engine.sandbox_policy import harvest_gh_pr_claim_bounded

    async def _claim_receipt_exec(command: list[str]) -> Any:
        return await provider.exec_command(provider_ref, command, cmd_timeout=15)

    try:
        receipt = await harvest_gh_pr_claim_bounded(_claim_receipt_exec, run_scope=run_id)
        return receipt, None
    except asyncio.CancelledError as cancel_exc:  # NOSONAR S7497
        return None, cancel_exc
    except Exception:
        return None, None


async def _settle_single_pr_guard(
    *,
    armed: bool,
    provider: Any,
    provider_ref: str,
    run_id: str,
    node_id: str,
    agent_stdout_raw: str,
    agent_stderr_raw: str,
    pr_url: str,
    output_json: Any,
    install_status: str | None,
) -> asyncio.CancelledError | None:
    """Settle this node's run-scoped one-PR guard slot (FAR-1315), exception-safe.

    Returns a cancellation to re-raise AFTER teardown (or ``None``). Runs BEFORE
    ``_teardown_and_clear`` so the shim's success receipt can still be harvested.
    """
    if not armed:
        return None
    from modulo.core.pipeline_engine.sandbox_policy import settle_run_pr_guard

    cancelled: asyncio.CancelledError | None = None
    try:
        claim_receipt, cancelled = await _harvest_gh_claim_receipt(provider, provider_ref, run_id)
        settle_run_pr_guard(
            run_id,
            node_id,
            agent_stdout_raw,
            agent_stderr_raw,
            pr_url=pr_url or (output_json.get("pr_url") if isinstance(output_json, dict) else None),
            claim_receipt=claim_receipt,
            # FAR-1315 (MAJOR 2b): only a LIVE install makes a definitive
            # receipt=False mean "confirmed no create".
            guard_install_status=install_status,
        )
    except asyncio.CancelledError as cancel_exc:  # NOSONAR S7497
        # Defence in depth: settle itself is synchronous, but never let a
        # cancellation skip ``_teardown_and_clear``.
        if cancelled is None:
            cancelled = cancel_exc
    except Exception:
        _log.debug("runner_dispatch.gh_guard_settle_failed", exc_info=True)
    return cancelled


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
    # FAR-899: schema validator mode resolved by the async caller
    # (_sandbox_agent_impl) and threaded through the config. Defaults to
    # "lenient" when unset (e.g. direct callers / tests).
    schema_validator_mode: str = getattr(config, "schema_validator_mode", "lenient")

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
    # FAR-1315: pre-bound at function scope so the finally-block settle and
    # the harvest reference them DIRECTLY (no ``locals()`` string lookups —
    # see tests/architecture/test_no_locals_lookup.py). An unbound value is
    # simply "" / None, which contributes no spend evidence.
    provider: Any = None
    provider_ref: str = ""
    agent_stdout_raw: str = ""
    agent_stderr_raw: str = ""
    pr_url: str = ""
    # FAR-1315: set once this node's run-scoped one-PR guard install ran (so
    # the finally settles the ledger slot only for nodes that actually armed).
    _single_pr_guard_armed = False
    # FAR-1315 (MAJOR 2b): the guard install's status (installed /
    # pre_planted / absent / failed), threaded into the finally's settle so a
    # receipt probe against a path no shim ever wrote is UNKNOWN rather than
    # "confirmed no create".
    _gh_guard_install_status: str | None = None

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
        template_result = _render_agent_template(
            sandbox_mode=sandbox_mode,
            agent_command=agent_command,
            agent_prompt_template=agent_prompt_template,
            state=state,
            scoped_run_context=scoped_run_context,
            raw_input=raw_input,
            run_id=run_id,
            node_id=node_id,
            # FAR-1743 / ADR 037: the snapshot-baked model_class metadata the
            # `{{ model }}` interpolation (and its fail-closed backstop) reads.
            node_def=node_def,
        )
        if isinstance(template_result, dict):
            return template_result
        rendered_prompt, rendered_agent_command, input_json = template_result

        # DB-atomic dispatch fence (same fenced WHERE as the E2B path).
        provision = await _provision_workspace(
            session_factory,
            _ProvisionRequest(
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
                node_egress_policy=node_def.get("egress_policy"),
                node_egress_allowlist=node_def.get("egress_allowlist"),
            ),
        )
        attempt_key = provision.attempt_key
        dispatch_marker_set = True
        provider_ref = provision.provider_ref
        provider = route.provider

        # FAR-1315: the one-PR-per-run ``gh`` guard on the Bundled Runner
        # tier. The engine's sandbox-policy step (E2B-only) never runs on this
        # dispatch branch — it returns to ``run_bundled_runner_node`` before
        # the policy block — so a flagged node installs the SAME run-scoped
        # shim here through the provider's exec primitive. The shared run
        # LEDGER decides: the first flagged node of the run gets the live
        # guard, a later or concurrent one gets a pre-planted refusal. Any
        # absence or failure is surfaced LOUDLY at dispatch naming the tier
        # (silent absence on a non-E2B tier is exactly the gap this closes).
        if node_def.get("single_pr_per_run") is True:
            _single_pr_guard_armed = True
            _gh_guard_install_status = await _install_single_pr_guard(
                provider,
                provider_ref,
                run_id=run_id,
                node_id=node_id,
                provider_type=route.provider_type,
            )

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
            provider=route.provider_type,
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
            sandbox_mode=sandbox_mode,
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
        # markers honour it (E2B parity). The org-level hard ceiling is read and
        # applied inside the helper, so the "no node can exceed the org ceiling"
        # invariant holds here exactly as on the E2B path.
        stdout_cap = await _resolve_effective_stdout_cap(config, node_def, node_id=node_id)

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
            _OutputReadRequest(
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
                schema_validator_mode=schema_validator_mode,
            )
        )

        # FAR-792 / FAR-811 / FAR-879: redact BEFORE truncation, slice to the
        # effective cap, and persist the full over-cap transcripts to the
        # artifact store (side-car pointer) — extracted verbatim.
        (
            agent_stdout,
            agent_stderr,
            stdout_len,
            stderr_len,
            stdout_truncated,
            _stdout_artifact,
            _stderr_artifact,
        ) = _runner_retention_artifacts(
            node_id=node_id,
            run_id=run_id,
            org_id=org_id,
            attempt_key=attempt_key,
            agent_stdout_raw=agent_stdout_raw,
            agent_stderr_raw=agent_stderr_raw,
            stdout_cap=stdout_cap,
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
                stderr_artifact=_stderr_artifact if _stderr_artifact is not None else _UNSET,
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
        # FAR-1315: settle this node's run-scoped one-PR guard slot (only if
        # the install ran above). Exception-safe and best-effort; runs BEFORE
        # ``_teardown_and_clear`` destroys the workspace, so the shim's success
        # receipt can still be HARVESTED here. SPEND EVIDENCE: harvested
        # receipt -> a CORROBORATED delivered ``pr_url`` -> the captured
        # streams (sentinel) only as a fallback when the receipt is UNKNOWN.
        # A definitive receipt=False (LIVE install + probe ran) ignores pr_url
        # and the sentinel and RELEASES this node's hold so a later flagged
        # node of the run may claim; an ABSENT/FAILED install (the shipped
        # runner image: no ``gh``) leaves the receipt meaningless rather than
        # "confirmed no create". The streams / ``pr_url`` / install status are
        # pre-bound at function scope and referenced DIRECTLY (no ``locals()``
        # string lookups); an unbound value simply contributes nothing.
        #
        # CANCELLATION (re-gate MAJOR 1): the harvest is the first await in
        # this finally and ``asyncio.CancelledError`` is a BaseException the
        # ``except Exception`` below would not catch — a cancel landing here
        # used to unwind the finally BEFORE ``_teardown_and_clear`` (leaked
        # workspace, stale dispatch marker, stranded hold). The bounded
        # harvest records the cancellation instead, teardown runs
        # UNCONDITIONALLY, and the cancellation is re-raised at the end.
        _gh_settle_cancelled = await _settle_single_pr_guard(
            armed=_single_pr_guard_armed,
            provider=provider,
            provider_ref=provider_ref,
            run_id=run_id,
            node_id=node_id,
            agent_stdout_raw=agent_stdout_raw,
            agent_stderr_raw=agent_stderr_raw,
            pr_url=pr_url,
            output_json=output_json,
            install_status=_gh_guard_install_status,
        )
        await _teardown_and_clear(
            route=route,
            session_factory=session_factory,
            state=state,
            run_id=run_id,
            org_id=org_id,
            _attempt_key=attempt_key,
            dispatch_marker_set=dispatch_marker_set,
        )
        # FAR-1315 (re-gate MAJOR 1): teardown has now run — deliver the
        # cancellation that landed during the receipt harvest.
        if _gh_settle_cancelled is not None:
            raise _gh_settle_cancelled


async def _install_single_pr_guard(
    provider: Any, provider_ref: str, *, run_id: str, node_id: str, provider_type: str
) -> str | None:
    """Install the run-scoped one-PR ``gh`` guard on the Bundled Runner tier.

    FAR-1315: the engine's sandbox-policy step is E2B-only, so a flagged bundled
    node installs the SAME run-scoped shim here through the provider's exec
    primitive. Returns the install status (``installed`` / ``pre_planted`` /
    ``absent`` / ``failed``) for the finally's settle, and logs the outcome
    LOUDLY naming the tier.
    """
    from modulo.core.pipeline_engine.sandbox_policy import install_gh_pr_guard_via_exec

    async def _guard_exec(command: list[str]) -> Any:
        return await provider.exec_command(provider_ref, command, cmd_timeout=30)

    guard_install = await install_gh_pr_guard_via_exec(
        _guard_exec,
        run_scope=run_id,
        guard_owner=node_id,
    )
    status = guard_install.status
    if status in ("absent", "failed"):
        _log.warning(
            "runner_dispatch.gh_pr_guard_unavailable: tier=%s status=%s node=%s run=%s detail=%s",
            provider_type,
            status,
            node_id,
            run_id,
            (guard_install.detail or "no detail")[:300],
        )
    elif status == "pre_planted":
        _log.warning(
            "runner_dispatch.gh_guard_run_claim_denied: tier=%s node=%s run=%s - "
            "pre-planted refusal, this node cannot create a PR (FAR-1315)",
            provider_type,
            node_id,
            run_id,
        )
    else:
        _log.info(
            "runner_dispatch.gh_pr_guard_installed: tier=%s node=%s run=%s",
            provider_type,
            node_id,
            run_id,
        )
    return status


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
    ``host.docker.internal`` on Docker (ADR 044 amendment: a setup failure
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


async def _teardown_one_resource(
    res: Awaitable[Any],
    *,
    log_event: str,
    timeout_seconds: float | None = None,
    extra: dict[str, Any] | None = None,
) -> None:
    """Run one best-effort teardown step: log-and-continue on failure.

    ``asyncio.CancelledError`` is re-raised — a teardown ladder must never
    swallow a cancellation. Any other failure is logged under ``log_event``
    (with ``extra`` merged into the record) and swallowed, so the remaining
    steps still run. ``timeout_seconds`` bounds the step via
    ``asyncio.wait_for``; ``None`` awaits unbounded (matching a plain
    ``await``).
    """
    try:
        await asyncio.wait_for(res, timeout=timeout_seconds)
    except asyncio.CancelledError:
        raise
    except Exception:
        _log.exception(log_event, extra=extra)


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
            await _teardown_one_resource(
                provider.destroy_workspace(ref),
                log_event="runner_dispatch.workspace_destroy_failed",
                timeout_seconds=_DESTROY_TIMEOUT,
                extra={"ref": ref},
            )
        if refs:
            await _teardown_one_resource(
                provider.close(),
                log_event="runner_dispatch.provider_close_failed",
                timeout_seconds=_PROVIDER_ACLOSE_TIMEOUT,
            )
    if hub is not None:
        await _teardown_one_resource(
            hub.aclose(),
            log_event="runner_dispatch.hub_aclose_failed",
            timeout_seconds=_PROVIDER_ACLOSE_TIMEOUT,
        )
    bridge_server = state.get("_loop_intercept_bridge_server")
    if bridge_server is not None:
        await _teardown_one_resource(
            asyncio.shield(bridge_server.close()),
            log_event="runner_dispatch.loop_intercept_teardown_failed",
            timeout_seconds=_LOOP_BRIDGE_CLOSE_TIMEOUT,
        )
    if dispatch_marker_set:
        from modulo.core.pipeline_engine.node_runner import _sandbox_clear_dispatch_marker

        await _teardown_one_resource(
            _sandbox_clear_dispatch_marker(
                session_factory=session_factory,
                claim_lease=state.get("_claim_lease"),
                org_id=org_id,
                run_id=run_id,
            ),
            log_event="runner_dispatch.dispatch_marker_clear_failed",
        )
