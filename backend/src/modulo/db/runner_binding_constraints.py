"""Pure save-time constraints for AgentRunnerBinding (FAR-592 / D6).

DB-half module (importable by db.crud without crossing into modulo.core):
name validation, reserved-var exclusion, and the credential-field surface.
The provision-time resolution lives in ``modulo.core.runner_bindings``,
which re-exports these.
"""

from __future__ import annotations

import re

# Protected env vars: exact names (denylist limitation documented in
# docs/architecture.md — a denylist cannot cover every future reserved name).
# Families:
#  * Modulo's own contract vars (the FAR-296 per-run minted path) — MODULO_* /
#    APP_MODULO_* are ALSO denied by the reserved prefixes; the exact names
#    stay listed so the constant is self-documenting and individually testable.
#  * Loader/interpreter injectives — a bound var overriding any of these
#    hijacks the sandbox process itself.
#  * Docker host redirection — DOCKER_HOST lets the agent pivot the runner's
#    Docker endpoint.
RESERVED_ENV_VARS: frozenset[str] = frozenset(
    {
        "MODULO_API_KEY",
        "MODULO_RUN_ID",
        "MODULO_PIPELINE_ID",
        "MODULO_ORG_ID",
        "MODULO_INPUT_PAYLOAD",
        "MODULO_ALLOWED_TOOLS",
        "MODULO_BRIDGE_ENDPOINT",
        "MODULO_BRIDGE_CONFIG",
        "APP_MODULO_OPENCODE_API_KEY",
        "GITHUB_TOKEN",
        "LD_PRELOAD",
        "LD_LIBRARY_PATH",
        "PATH",
        "NODE_OPTIONS",
        "PYTHONPATH",
        "PYTHONSTARTUP",
        "BASH_ENV",
        "ENV",
        "DOCKER_HOST",
    }
)

# Prefix families (matched case-insensitively after uppercasing):
#  - MODULO_* / APP_MODULO_* — Modulo-owned contract vars; the FAR-296
#    per-run minted key stays set AFTER the bindings merge, and the
#    reserved-prefix validator keeps node env vars from overriding them.
#  - GIT_ — git's proxy/config env family (GIT_PROXY_COMMAND, GIT_CONFIG_*,
#    GIT_SSL_*, GIT_TERMINAL_PROMPT, GIT_SSH, ...) is effective pipeline
#    configuration, not an application credential.
RESERVED_ENV_VAR_PREFIXES: tuple[str, ...] = ("MODULO_", "APP_MODULO_", "GIT_")

_ENV_NAME_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_MAX_TARGET_ENV_VAR_LEN = 128
_MAX_SOURCE_FIELD_LEN = 64

# Credential fields a ModelBackend's decrypted secret can carry. The backend
# write path stores exactly one secret per backend — ``{"api_key": ...}`` via
# ``secrets_backend.set_secret`` (api/routes/model_backends.py) — so
# ``api_key`` is the ONLY field a binding can actually resolve today. Anything
# wider here would be save-time-accepted but provision-time-unresolvable (the
# trap this validation exists to prevent); extend the surface only together
# with a write path that stores the richer credential JSON.
KNOWN_SOURCE_FIELDS_ALL: frozenset[str] = frozenset({"api_key"})


def known_source_fields_for(provider: str) -> frozenset[str]:
    """Return the credential fields a backend of ``provider`` can expose.

    Uniform surface today (``api_key``); per-provider extension point for the
    day a write path stores richer credential JSON.
    """
    return KNOWN_SOURCE_FIELDS_ALL


class BindingValidationError(ValueError):
    """A binding's ``target_env_var`` or ``source_field`` is not acceptable."""


def is_reserved_env_var(name: str) -> bool:
    """True when ``name`` targets a Modulo-reserved or interpreter/loader env var."""
    normalized = (name or "").strip().upper()
    if normalized in RESERVED_ENV_VARS:
        return True
    return any(normalized.startswith(prefix) for prefix in RESERVED_ENV_VAR_PREFIXES)


def validate_target_env_var(name: str) -> str:
    """Validate and return the canonical ``target_env_var`` for a binding.

    The canonical form is UPPERCASE (decision, FAR-592 qa fixes): the SERVER
    normalises on save — the returned canonical value is what the UNIQUE
    (org, agent, target_env_var) constraint and every dedupe check see, so
    case variants cannot create duplicate bindings. Clients must not
    pre-transform; the server is the single canonicaliser.
    """
    candidate = (name or "").strip()
    if not candidate:
        raise BindingValidationError("target_env_var must not be empty")
    if len(candidate) > _MAX_TARGET_ENV_VAR_LEN:
        raise BindingValidationError(f"target_env_var exceeds {_MAX_TARGET_ENV_VAR_LEN} characters")
    if not _ENV_NAME_PATTERN.fullmatch(candidate):
        raise BindingValidationError(
            "target_env_var must match [A-Za-z_][A-Za-z0-9_]* (start with a letter or underscore; no dashes or dots)"
        )
    candidate = candidate.upper()
    if is_reserved_env_var(candidate):
        raise BindingValidationError(f"target_env_var '{candidate}' is reserved and cannot be bound")
    return candidate


def _validate_source_field(source_field: str, provider: str) -> str:
    """Validate a binding's ``source_field`` against the provider's known fields."""
    candidate = (source_field or "").strip()
    if not candidate:
        raise BindingValidationError("source_field must not be empty")
    if len(candidate) > _MAX_SOURCE_FIELD_LEN:
        raise BindingValidationError("source_field exceeds 64 characters")
    if not _ENV_NAME_PATTERN.fullmatch(candidate):
        raise BindingValidationError("source_field must match [A-Za-z_][A-Za-z0-9_]*")
    known = known_source_fields_for(provider)
    if candidate not in known:
        raise BindingValidationError(
            f"source_field '{candidate}' is not a known credential field of a "
            f"'{provider}' model backend (known: {', '.join(sorted(known))})"
        )
    return candidate


def validate_binding_pair(*, target_env_var: str, source_field: str, provider: str) -> tuple[str, str]:
    """Validate both halves of a binding at save time. Returns the canonical pair."""
    target = validate_target_env_var(target_env_var)
    source = _validate_source_field(source_field, provider)
    return target, source
