"""Per-agent Model Backend env-var bindings (FAR-592 / D6).

A binding injects one decrypted Model Backend credential field into the
agent's runner env under a named env var at provision time:
``{backend X, target_env_var="OPENCODE_API_KEY", source_field="api_key"}``.

Standing-credential posture (ADR 029 amendment): the injected value is a
user-configured credential readable by the agent's own code — that is the
feature's purpose. Mitigations: reserved-var exclusion, audit-logged
resolutions (never values), and a real FK so a bound backend cannot be deleted.

The hub driven here is a FRESH, short-lived :class:`ModelBackendHub` per
provisioning — the documented one-instance-per-run contract — resolving ONLY
the referenced backends, decrypting via the secrets backend, and explicitly
disposed via the async context manager (never left to GC).

Pure save-time constraints (name validation, reserved-var exclusion, and the
credential-field surface) live in ``modulo.db.runner_binding_constraints``
and are re-exported here for the core users (node_runner dispatch, routes).
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select

from modulo.db.models.environment_profile import EnvironmentProfile
from modulo.db.models.model_backend import ModelBackend
from modulo.db.runner_binding_constraints import (
    RESERVED_ENV_VAR_PREFIXES,
    RESERVED_ENV_VARS,
    BindingValidationError,
    is_reserved_env_var,
    known_source_fields_for,
    validate_binding_pair,
    validate_target_env_var,
)

# Re-exports for core users (node_runner dispatch, routes); ``__all__`` makes
# them explicit under mypy's no-implicit-reexport (strict).
__all__ = [
    "RESERVED_ENV_VARS",
    "RESERVED_ENV_VAR_PREFIXES",
    "AgentBindingResolutionError",
    "BindingValidationError",
    "LocalProviderBindingsRefusedError",
    "ResolvedBinding",
    "is_reserved_env_var",
    "known_source_fields_for",
    "resolve_agent_bindings",
    "validate_binding_pair",
    "validate_target_env_var",
]

_log = logging.getLogger(__name__)

_LOCAL_OPT_IN_KEY = "allow_runner_env_bindings"


class AgentBindingResolutionError(RuntimeError):
    """Provision-time binding resolution failed (retryable config error).

    The D6 rollback trigger reads the rate of the ``sandbox.binding_resolution``
    error code this feeds.
    """

    def __init__(self, message: str, *, backend_id: uuid.UUID | None = None) -> None:
        super().__init__(message)
        self.backend_id = backend_id


class LocalProviderBindingsRefusedError(AgentBindingResolutionError):
    """The Local (host-subprocess) provider refused a bindings-carrying agent.

    Mirrors the D7 hard-refusal posture: bindings inject standing host-env
    credentials, and the Local tier executes on the HOST process (no container
    isolation), so a Local profile without an explicit
    ``config_json.allow_runner_env_bindings`` opt-in MUST refuse at provision
    time — never silently host the standing key.
    """

    def __init__(self, agent_label: str) -> None:
        super().__init__(
            "Runner bindings are refused for the Local (host-subprocess) provider tier. "
            f"Agent '{agent_label}' carries bindings and its environment profile is "
            "provider_type 'local' without an explicit opt-in. Set "
            "allow_runner_env_bindings: true in the profile's config_json to opt in, "
            "or choose a container/workspace tier (runner_docker / e2b)."
        )


@dataclass(frozen=True)
class ResolvedBinding:
    """One injected env var, resolved at provision time."""

    target_env_var: str
    value: str
    backend_id: uuid.UUID
    source_field: str


async def resolve_agent_bindings(
    *,
    session_factory: Any,
    org_id: uuid.UUID | str | None,
    agent_id: uuid.UUID | str | None,
    environment_profile_id: uuid.UUID | None = None,
    run_id: str = "",
    node_id: str = "",
) -> dict[str, str]:
    """Resolve an agent's runner bindings to an env dict at provision time.

    Precedence with the caller (DELIBERATE, node_runner.py): profile secrets <
    runner bindings < node ``env_vars_extra`` — the caller merges this dict
    BETWEEN its profile/host creds and the node's extra envs, so the NODE WINS
    (the reviewbot ``GITHUB_TOKEN`` override keeps working).

    FRESH short-lived hub per provisioning (documented one-instance-per-run),
    resolving ONLY the referenced backends, explicitly disposed via the async
    context manager. Every injected var is audit-logged (backend id, env var
    name, run id — NEVER values).

    Raises:
        LocalProviderBindingsRefusedError: the Local provider tier refuses
            bindings without an explicit profile opt-in.
        AgentBindingResolutionError: any other provision-time failure.
    """
    org_uuid = _uuid_or_none(org_id)
    agent_uuid = _uuid_or_none(agent_id)
    if session_factory is None or org_uuid is None or agent_uuid is None:
        return {}

    from modulo.core.model_backend_hub import ModelBackendHub
    from modulo.core.secrets_backend import create_secrets_backend
    from modulo.db.models.agent import Agent
    from modulo.db.models.agent_runner_binding import AgentRunnerBinding
    from modulo.db.rls import set_rls_execution_context, set_rls_org
    from modulo.settings import get_settings

    resolved: dict[str, str] = {}
    async with session_factory() as session, session.begin():
        await set_rls_org(session, org_uuid)
        await set_rls_execution_context(session)

        bindings_rows = await session.execute(
            select(AgentRunnerBinding).where(AgentRunnerBinding.agent_id == agent_uuid)
        )
        bindings = list(bindings_rows.scalars())
        if not bindings:
            return {}

        backend_ids = {binding.model_backend_id for binding in bindings}
        backends_rows = await session.execute(
            select(ModelBackend).where(
                ModelBackend.organisation_id == org_uuid,
                ModelBackend.id.in_(backend_ids),
            )
        )
        backends_by_id = {row.id: row for row in backends_rows.scalars()}

        agent_rows = await session.execute(select(Agent).where(Agent.id == agent_uuid))
        agent_row = agent_rows.scalar_one_or_none()
        agent_label = agent_row.name if agent_row is not None else str(agent_uuid)

        opted_in = True
        env_profile_uuid = _uuid_or_none(environment_profile_id)
        if env_profile_uuid is not None:
            profile_rows = await session.execute(
                select(EnvironmentProfile).where(
                    EnvironmentProfile.organisation_id == org_uuid,
                    EnvironmentProfile.id == env_profile_uuid,
                )
            )
            profile = profile_rows.scalar_one_or_none()
            if profile is not None and profile.provider_type.strip().lower() == "local":
                config = profile.config_json if isinstance(profile.config_json, dict) else {}
                opted_in = bool(config.get(_LOCAL_OPT_IN_KEY))

        if not opted_in:
            raise LocalProviderBindingsRefusedError(agent_label)

        settings = get_settings()
        secrets_backend = create_secrets_backend(fernet_key=settings.fernet_key, session=session)
        # Resolve the referenced backends up front so a binding whose backend is
        # no longer visible to the org raises the typed, retryable error (rather
        # than an uncaught KeyError inside the initialise/list-comprehension).
        resolved_backend_rows: list[ModelBackend] = []
        for binding in bindings:
            backend_row = backends_by_id.get(binding.model_backend_id)
            if backend_row is None:
                raise AgentBindingResolutionError(
                    "bound model backend is no longer visible to the organisation",
                    backend_id=binding.model_backend_id,
                )
            resolved_backend_rows.append(backend_row)
        async with ModelBackendHub() as hub:
            await hub.initialise(resolved_backend_rows, secrets_backend=secrets_backend)
            for binding in bindings:
                backend_row = backends_by_id[binding.model_backend_id]
                creds = hub.creds_for(binding.model_backend_id)
                if not creds or binding.source_field not in creds:
                    raise AgentBindingResolutionError(
                        f"source field '{binding.source_field}' unavailable for model backend '{backend_row.name}'",
                        backend_id=binding.model_backend_id,
                    )
                resolved[str(binding.target_env_var)] = str(creds[binding.source_field])
        # Hub explicitly disposed above via the async context manager — never GC.

    for target_env_var in resolved:
        _log.info(
            "sandbox_agent.binding_injected",
            extra={
                "run_id": run_id,
                "node_id": node_id,
                "target_env_var": target_env_var,
            },
        )
    return resolved


def _uuid_or_none(value: Any) -> uuid.UUID | None:
    if value is None:
        return None
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError):
        return None
