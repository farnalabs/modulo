"""Cross-team resource binding enforcement (PRD §9.3, FAR-1515, FAR-1618).

THE MODEL (teams are a VISIBILITY GROUPING, not a credential trust boundary):

* ``visibility: org`` — an org-wide resource (connector, model backend,
  environment profile) is SHARED ACROSS THE ORGANISATION: it binds to ANY
  pipeline, including a pipeline owned by a team. No mismatch is ever
  produced for an org-visibility row. This matches the product map's
  "org-wide resources stay shared" rule and the BDD
  ``cross_team_isolation`` scenario "Org-wide resources are accessible across
  teams"; model backends always behaved this way (ModelBackendHub has no
  invocation-time visibility gate).
* ``visibility: team`` — a team-PRIVATE resource is usable ONLY by a
  pipeline owned by the team that owns it. A pipeline owned by a different
  team (or an org pipeline with no owner team) is a mismatch.

FAR-1618 removed the reverse direction that FAR-1515 introduced (a TEAM
pipeline pinning an ``org``-visibility connector) and the FAR-516 run-gate it
mirrored: the run-gate was a dead guard from the initial commit, revived by a
hardening pass with no ADR, and it contradicted the shared rule above.
Neither a save nor a run rejects an org-visibility connector on a team
pipeline any more.

Three rules are enforced at the write layers that can create a cross-team
binding, each with its own named error:

``connector_team_mismatch``
    A connector instance with ``visibility: team`` is only usable within
    pipelines owned by the same team. Binding one to a pipeline owned by a
    different team (or to an org pipeline) is blocked. An org-visibility
    connector NEVER mismatches (see THE MODEL above).

``model_backend_team_mismatch``
    A model backend with ``visibility: team`` is only usable by a pipeline
    owned by the same team; an org-visibility backend never mismatches. The
    two rules now say the same thing — see the parity note on
    :func:`model_backend_team_mismatch`.

The candidate rows behind both pipeline-save rules are read team-blind but
org-scoped (FAR-1515 CRITICAL 1): the ``rls_team_isolation`` policy hides another team's
``visibility='team'`` rows from the request session, so a read in the caller's
own context used to return NOTHING for the very connector the predicate had to
judge — the binding looked absent and was skipped, and the save was accepted.
:func:`team_blind_org_scope` (``db.crud.team_scope``) temporarily sets the
policy's ``app.execution_context`` escape hatch (still AND-gated by
``app.organisation_id``), reads, and restores the caller's GUCs. A connector
binding the organisation genuinely cannot resolve — after that team-blind
read an absent row is definitive — is the named ``connector_team_mismatch``
refusal :class:`ConnectorBindingMissingError`, never a silent skip.

``environment_profile_team_mismatch`` (FAR-1558)
    An environment profile with ``visibility: team`` is only usable by a
    pipeline owned by the same team. The predicate is SHARED by all three
    writers of the rule — bind time (``api/routes/pipelines.py``), a pipeline
    scope change (same module, stored-binding re-validation), and a profile
    scope change (``api/routes/environment_profiles.py``) — and by the
    dispatch-time backstop (FAR-1598,
    ``core.bundled_runner.runner_dispatch``), so the four cannot drift. The
    profile-side writer emits its own wire code
    (``ENVIRONMENT_PROFILE_BINDING_TEAM_MISMATCH``) because it refuses a
    different operation (stranding an existing binding vs. creating a
    mismatched one), and its bound-pipelines read is team-blind through
    ``team_blind_org_scope`` for exactly the FAR-1515 CRITICAL 1 reason above:
    a non-admin caller cannot see another team's team-private pipeline, so a
    caller-context scan would return nothing and the strand would pass
    vacuously.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.core.graph_validator._types import try_parse_uuid, try_parse_uuids
from modulo.db.crud.team_scope import team_blind_org_scope
from modulo.db.models.connector_instance import ConnectorInstance
from modulo.db.models.model_backend import ModelBackend

CONNECTOR_TEAM_MISMATCH = "connector_team_mismatch"
MODEL_BACKEND_TEAM_MISMATCH = "model_backend_team_mismatch"
# FAR-1558: TWO wire codes for the one environment-profile rule, because the
# two writers refuse DIFFERENT operations — binding/scoping a pipeline (the
# profile's metadata must not leak to that caller) vs. re-scoping the profile
# itself (the operation's own detail names the blocked count). Both are
# defined HERE so neither route can re-hardcode its own spelling.
ENVIRONMENT_PROFILE_TEAM_MISMATCH = "environment_profile_team_mismatch"
ENVIRONMENT_PROFILE_BINDING_TEAM_MISMATCH = "environment_profile_binding_team_mismatch"


@dataclass(frozen=True)
class ConnectorTeamMismatch:
    """A connector binding that the team-scope rule refuses.

    Only the team-PRIVATE direction ever produces one (FAR-1618): an
    org-visibility connector never mismatches, so the row needs nothing but
    the identities required to name the refusal.
    """

    connector_id: uuid.UUID
    connector_name: str
    connector_owner_team_id: uuid.UUID | None
    pipeline_owner_team_id: uuid.UUID | None
    node_id: str | None = None


def connector_team_mismatch(
    visibility: str | None,
    connector_owner_team_id: uuid.UUID | None,
    pipeline_owner_team_id: uuid.UUID | None,
) -> bool:
    """Return True when a connector binding crosses team boundaries.

    Team-private connector: only usable by a pipeline owned by the *same*
    team. A pipeline without an owning team (``pipeline_owner_team_id=None``)
    or owned by a different team is a mismatch.

    An org-visibility connector is NEVER a mismatch (FAR-1618): teams are a
    visibility grouping, not a credential trust boundary, so an org-wide
    connector is shared across the organisation and binds to any team's
    pipeline. This is the same rule
    :func:`model_backend_team_mismatch` applies to model backends.
    """
    if (visibility or "org") != "team":
        # Org-visibility connector: shared across the organisation, so no
        # pipeline can create a cross-team binding with it.
        return False
    if connector_owner_team_id is None:
        return True
    return connector_owner_team_id != pipeline_owner_team_id


def connector_team_mismatch_detail(mismatches: list[ConnectorTeamMismatch]) -> str:
    """Build the HTTP error detail for a set of mismatches.

    The message always starts with the machine-readable named error
    ``connector_team_mismatch`` so clients can branch on it, and always names
    the connector plus the fix, so the operator can act on it directly.

    Every mismatch reaching here is the team-PRIVATE direction (an
    org-visibility connector never mismatches, so it is never built into a
    :class:`ConnectorTeamMismatch`).
    """
    parts = [
        (
            f"connector '{m.connector_name}' (id={m.connector_id}) is team-private "
            f"(owner team {m.connector_owner_team_id}) but pipeline is owned by team "
            f"{m.pipeline_owner_team_id}"
        )
        for m in mismatches
    ]
    return f"{CONNECTOR_TEAM_MISMATCH}: {'; '.join(parts)}"


class ConnectorBindingMissingError(Exception):
    """A parsed connector binding the team-blind org-scoped read could not resolve.

    FAR-1515 CRITICAL 1: candidate rows are read team-blind but org-scoped, so
    an id absent from the result is DEFINITIVE — this organisation has no such
    connector (another team's row would have been visible to the widened read).
    A binding the gate cannot validate must fail closed, never ride through as
    "no mismatch found"; the message carries the same machine-readable
    ``connector_team_mismatch`` prefix as the mismatch detail so every surface
    maps it to the same named 409 / error envelope.

    Raised by :func:`find_connector_team_mismatches` when unresolvable
    bindings are the only refusal (a real cross-team mismatch takes precedence
    and is returned normally). Callers — the REST enforcement helper, the MCP
    graph-update tool, the import gate — catch it and translate.
    """

    def __init__(self, missing: list[tuple[uuid.UUID, str | None]]) -> None:
        self.missing = missing
        super().__init__(connector_binding_missing_detail(missing))


def connector_binding_missing_detail(missing: Sequence[tuple[uuid.UUID, str | None]]) -> str:
    """Build the named ``connector_team_mismatch`` detail for unresolvable ids."""
    parts = [
        f"connector id {connector_id} (node {node_id}) does not resolve to a connector in this "
        f"organisation - the binding cannot be team-validated, so it is refused; fix or remove the "
        f"binding (the connector may have been deleted, or belongs to another organisation)"
        for connector_id, node_id in missing
    ]
    return f"{CONNECTOR_TEAM_MISMATCH}: {'; '.join(parts)}"


def extract_connector_bindings(nodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Extract connector binding descriptors from graph node dicts.

    Returns the same shape used by snapshot ``connector_bindings_json``:
    ``[{"node_id": ..., "connector_instance_id": ...}, ...]``.
    """
    bindings: list[dict[str, Any]] = []
    for node in nodes:
        binding = node.get("connector_binding")
        if not isinstance(binding, dict):
            continue
        instance_id = binding.get("instance_id")
        if instance_id is None:
            continue
        bindings.append(
            {
                "node_id": str(node.get("id")),
                "connector_instance_id": str(instance_id),
            }
        )
    return bindings


def model_backend_team_mismatch(
    model_backend_visibility: str | None,
    model_backend_owner_team_id: uuid.UUID | None,
    pipeline_owner_team_id: uuid.UUID | None,
) -> bool:
    """Return True when a model-backend binding crosses team boundaries.

    PRD §9.3: a model backend with ``visibility: team`` is only usable by a
    pipeline owned by the *same* team, mirroring the connector rule. Org-wide
    model backends never mismatch.

    PARITY NOTE (FAR-1515, restated by FAR-1618): the connector rule and this
    one now say exactly the same thing — a team-PRIVATE row mismatches a
    pipeline outside its owner team (including an org pipeline), and an
    org-visibility row never mismatches, because org-wide resources stay
    shared across the organisation. ModelBackendHub has no invocation-time
    visibility gate (``_init_model_backend_hub`` loads every active backend in
    the org and ``node_runner`` resolves a pin with ``hub.get(backend_id)``
    without consulting ``visibility``); after FAR-1618 the connector hub has
    no such gate either, so neither rule has an invocation-time counterpart to
    mirror. This function is unchanged by FAR-1618.
    """
    if (model_backend_visibility or "org") != "team":
        return False
    if model_backend_owner_team_id is None:
        return True
    return model_backend_owner_team_id != pipeline_owner_team_id


def environment_profile_team_mismatch(
    profile_visibility: str | None,
    profile_owner_team_id: uuid.UUID | None,
    pipeline_owner_team_id: uuid.UUID | None,
) -> bool:
    """Return True when an environment-profile binding crosses team boundaries.

    FAR-1558: an environment profile with ``visibility: team`` is only usable
    by a pipeline owned by the *same* team — byte-identical to the
    connector/model-backend rule (an org-visible profile never mismatches; a
    team-private profile with NO owner team is owned by nobody, so it mismatches
    every pipeline; an org-wide pipeline can only ever hold an org-visible
    profile).

    SHARED by all three writers of the rule so they cannot drift:

    1. bind time — ``api/routes/pipelines.py::_assert_environment_profile_bindable``;
    2. pipeline scope change — the same helper re-validating a STORED binding;
    3. profile scope change — ``api/routes/environment_profiles.py``
       (refuses re-scoping a profile whose bound pipelines would be stranded).

    The visibility argument is accepted rather than read from a row so the
    profile-side writer can evaluate a PROPOSED scope (``updates`` merged over
    the stored row) with the same code path as the stored one.
    """
    if (profile_visibility or "org") != "team":
        return False
    if profile_owner_team_id is None:
        return True
    return profile_owner_team_id != pipeline_owner_team_id


@dataclass(frozen=True)
class ModelBackendTeamMismatch:
    """A team-private model backend pinned by a pipeline owned by a different team."""

    model_backend_id: uuid.UUID
    model_backend_name: str
    model_backend_owner_team_id: uuid.UUID | None
    pipeline_owner_team_id: uuid.UUID | None
    node_id: str | None = None


def model_backend_team_mismatch_detail(mismatches: list[ModelBackendTeamMismatch]) -> str:
    """Build the HTTP error detail for a set of model-backend mismatches.

    The message always starts with the machine-readable named error
    ``model_backend_team_mismatch`` so clients can branch on it.
    """
    parts = [
        (
            f"model backend '{m.model_backend_name}' (id={m.model_backend_id}) is team-private "
            f"(owner team {m.model_backend_owner_team_id}) but pipeline is owned by team "
            f"{m.pipeline_owner_team_id}"
        )
        for m in mismatches
    ]
    return f"{MODEL_BACKEND_TEAM_MISMATCH}: {'; '.join(parts)}"


async def _select_candidate_rows(
    session: AsyncSession,
    model: type[Any],
    org_id: uuid.UUID,
    ids: set[uuid.UUID],
) -> Sequence[Any]:
    """Read the candidate rows TEAM-BLIND but ORG-SCOPED (FAR-1515 CRITICAL 1).

    Under ``rls_team_isolation`` a request session cannot see another team's
    ``visibility='team'`` rows, so reading candidates in the caller's own
    context silently dropped the exact rows the predicate must judge (a
    Team-A member binding Team-B's connector looked "absent" and was skipped).
    The widened read lives in ``db.crud.team_scope.team_blind_org_scope`` (the
    db-layer seam both ``modulo.db`` and ``modulo.core`` may import), and an id
    the widened read still cannot resolve is a named refusal rather than a
    skip (see ``find_connector_team_mismatches``).
    """
    stmt = select(model).where(model.organisation_id == org_id, model.id.in_(ids))
    async with team_blind_org_scope(session, org_id):
        return (await session.execute(stmt)).scalars().all()


async def _find_team_scope_mismatches[MismatchT](
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    pipeline_owner_team_id: uuid.UUID | None,
    entries: list[dict[str, Any]],
    id_key: str,
    model: type[Any],
    check_mismatch: Callable[[str | None, uuid.UUID | None, uuid.UUID | None], bool],
    build_mismatch: Callable[[Any, uuid.UUID | None, str | None], MismatchT],
) -> tuple[list[MismatchT], list[tuple[uuid.UUID, str | None]]]:
    """Shared fetch-and-filter pattern for team-scoped binding enforcement.

    Looks up the org-scoped resource rows referenced by ``entries`` - read
    team-blind via :func:`_select_candidate_rows` - and returns
    ``(mismatches, missing)``: the entries the team-scope rule flags as
    cross-team, plus every parsed id the team-blind read could NOT resolve
    (``[(connector/backend id, node_id), ...]``). What each caller does with
    ``missing`` is its own contract: the connector half refuses (FAR-1515
    CRITICAL 1 - with team-blind visibility an absent row is definitive), the
    model-backend half keeps ignoring it (parity note: its rule is unchanged).
    """
    if not entries:
        return [], []

    raw_ids = [entry.get(id_key) for entry in entries]
    parsed_ids, _ = try_parse_uuids(raw_ids)
    if not parsed_ids:
        return [], []

    rows = await _select_candidate_rows(session, model, org_id, parsed_ids)
    found: dict[uuid.UUID, Any] = {r.id: r for r in rows}

    mismatches: list[MismatchT] = []
    missing: list[tuple[uuid.UUID, str | None]] = []
    for entry in entries:
        node_id = str(entry.get("node_id")) if entry.get("node_id") else None
        rid = try_parse_uuid(entry.get(id_key))
        if rid is None:
            continue
        resource = found.get(rid)
        if resource is None:
            missing.append((rid, node_id))
            continue
        if not check_mismatch(resource.visibility, resource.owner_team_id, pipeline_owner_team_id):
            continue
        mismatches.append(build_mismatch(resource, pipeline_owner_team_id, node_id))
    return mismatches, missing


async def find_model_backend_team_mismatches(
    session: AsyncSession,
    org_id: uuid.UUID,
    pipeline_owner_team_id: uuid.UUID | None,
    model_backend_pins: list[dict[str, Any]],
) -> list[ModelBackendTeamMismatch]:
    """Return cross-team model-backend pin violations for a graph save.

    ``model_backend_pins`` uses the snapshot pin shape:
    ``[{"node_id": ..., "model_backend_id": ...}, ...]``. The candidate rows
    are read team-blind but org-scoped (same read as the connector half, so a
    hidden cross-team backend is judged rather than skipped); pins that cannot
    be resolved (missing, or in another org) remain ignored - the graph
    validator reports them separately. That asymmetry with the connector half
    is deliberate (FAR-1515 parity note above): the model-backend RULE is
    unchanged here.
    """
    mismatches, _missing = await _find_team_scope_mismatches(
        session,
        org_id=org_id,
        pipeline_owner_team_id=pipeline_owner_team_id,
        entries=model_backend_pins,
        id_key="model_backend_id",
        model=ModelBackend,
        check_mismatch=model_backend_team_mismatch,
        build_mismatch=_build_model_backend_mismatch,
    )
    return mismatches


async def find_connector_team_mismatches(
    session: AsyncSession,
    org_id: uuid.UUID,
    pipeline_owner_team_id: uuid.UUID | None,
    connector_bindings: list[dict[str, Any]],
) -> list[ConnectorTeamMismatch]:
    """Return cross-team connector binding violations for a graph save.

    ``connector_bindings`` uses the same shape as snapshot
    ``connector_bindings_json`` entries: ``{"node_id": ..., "connector_instance_id": ...}``.

    The candidate rows are read team-blind but org-scoped
    (:func:`_select_candidate_rows`), so another team's team-private connector
    is judged rather than hidden (FAR-1515 CRITICAL 1). A binding id the
    organisation genuinely cannot resolve raises
    :class:`ConnectorBindingMissingError` (the named ``connector_team_mismatch``
    refusal), never a silent skip: a binding this gate cannot validate must
    not ride through it. A real cross-team mismatch takes precedence and is
    returned normally — the caller refuses either way.
    """
    mismatches, missing = await _find_team_scope_mismatches(
        session,
        org_id=org_id,
        pipeline_owner_team_id=pipeline_owner_team_id,
        entries=connector_bindings,
        id_key="connector_instance_id",
        model=ConnectorInstance,
        check_mismatch=connector_team_mismatch,
        build_mismatch=_build_connector_mismatch,
    )
    if mismatches:
        return mismatches
    if missing:
        raise ConnectorBindingMissingError(missing)
    return []


def _build_model_backend_mismatch(
    backend: Any, pipeline_owner_team_id: uuid.UUID | None, node_id: str | None
) -> ModelBackendTeamMismatch:
    return ModelBackendTeamMismatch(
        model_backend_id=backend.id,
        model_backend_name=backend.name,
        model_backend_owner_team_id=backend.owner_team_id,
        pipeline_owner_team_id=pipeline_owner_team_id,
        node_id=node_id,
    )


def _build_connector_mismatch(
    instance: Any, pipeline_owner_team_id: uuid.UUID | None, node_id: str | None
) -> ConnectorTeamMismatch:
    return ConnectorTeamMismatch(
        connector_id=instance.id,
        connector_name=instance.name,
        connector_owner_team_id=instance.owner_team_id,
        pipeline_owner_team_id=pipeline_owner_team_id,
        node_id=node_id,
    )
