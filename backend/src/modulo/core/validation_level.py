"""Integration validation level — single source of truth (FAR-935).

Every connector type and model backend provider resolves to a validation level
from a STATIC baseline map in this module. The baseline declares each
integration's *ceiling* — the highest level it is *capable* of being validated
to based on what tests exist today.

The *current* level is written to the DB by the health/canary sweep
(``health_sweep.py``), which mirrors ``last_health_check_at``: a successful
canary writes the baseline level (which is the ceiling), while a failed canary
degrades the level to the highest level below ``canary-green`` (since a red
canary can never leave a stale "green").

Levels (ascending):

* ``unit-only`` — hand-written mocks / SDK patched; no real server contacted
* ``contract-recorded`` — replaying a recorded real interaction
* ``self-hosted-e2e`` — exercised against a real container-hosted server in CI
* ``canary-green`` — live call against the real vendor API succeeded recently

Design rules:

* The baseline is a STATIC map: it changes only when new tests are added. It
  does NOT drift with canary state — canary state is the dynamic part.
* Degradation is AUTOMATIC: if the most recent live canary for an integration
  failed, the resolved level drops. A red canary can never leave a stale "green".
* Failure-safe: if the level computation fails for any reason, degrade to the
  baseline — never raise, never break ``get_integration_status``.
"""

from __future__ import annotations

from enum import StrEnum

from modulo.connectors.base import ConnectorType
from modulo.db.enums import ModelBackendProvider


class ValidationLevel(StrEnum):
    """How thoroughly an integration has been validated (ascending)."""

    UNIT_ONLY = "unit-only"
    CONTRACT_RECORDED = "contract-recorded"
    SELF_HOSTED_E2E = "self-hosted-e2e"
    CANARY_GREEN = "canary-green"


_LEVEL_ORDER: dict[str, int] = {v: i for i, v in enumerate(ValidationLevel)}


def _level_below(level: str) -> str:
    """Return the level immediately below *level*, or UNIT_ONLY if already at floor."""
    idx = _LEVEL_ORDER.get(level, 0)
    if idx <= 0:
        return ValidationLevel.UNIT_ONLY
    for k, v in _LEVEL_ORDER.items():
        if v == idx - 1:
            return k
    return ValidationLevel.UNIT_ONLY


# ---------------------------------------------------------------------------
# Static baseline maps — the ceiling for each integration type.
#
# Connector types use ConnectorType (StrEnum). Model backend providers use
# ModelBackendProvider (StrEnum). Both are mapped to a ValidationLevel.
#
# Default is UNIT_ONLY. Types with real integration test coverage are
# elevated. This map is the SINGLE source of truth for the baseline.
# ---------------------------------------------------------------------------

_CONNECTOR_BASELINE: dict[ConnectorType, ValidationLevel] = {
    # --- CI runners (have Docker-based contract tests) ---
    ConnectorType.GITHUB: ValidationLevel.CONTRACT_RECORDED,
    ConnectorType.GITLAB: ValidationLevel.CONTRACT_RECORDED,
    ConnectorType.BITBUCKET: ValidationLevel.CONTRACT_RECORDED,
    ConnectorType.CIRCLECI: ValidationLevel.CONTRACT_RECORDED,
    ConnectorType.BUILDKITE: ValidationLevel.CONTRACT_RECORDED,
    ConnectorType.JENKINS: ValidationLevel.CONTRACT_RECORDED,
    ConnectorType.TEAMCITY: ValidationLevel.CONTRACT_RECORDED,
    ConnectorType.AZURE_PIPELINES: ValidationLevel.CONTRACT_RECORDED,
    # --- Ticket trackers (have recorded contract tests) ---
    ConnectorType.JIRA: ValidationLevel.CONTRACT_RECORDED,
    ConnectorType.LINEAR: ValidationLevel.CONTRACT_RECORDED,
    ConnectorType.TRELLO: ValidationLevel.CONTRACT_RECORDED,
    ConnectorType.ASANA: ValidationLevel.CONTRACT_RECORDED,
    ConnectorType.SHORTCUT: ValidationLevel.CONTRACT_RECORDED,
    ConnectorType.YOUTRACK: ValidationLevel.CONTRACT_RECORDED,
    ConnectorType.NOTION: ValidationLevel.CONTRACT_RECORDED,
    # --- Observability / monitoring (unit-level only) ---
    ConnectorType.DATADOG: ValidationLevel.UNIT_ONLY,
    ConnectorType.SENTRY: ValidationLevel.UNIT_ONLY,
    ConnectorType.PAGERDUTY: ValidationLevel.UNIT_ONLY,
    ConnectorType.GRAFANA: ValidationLevel.UNIT_ONLY,
    ConnectorType.SONARQUBE: ValidationLevel.UNIT_ONLY,
    ConnectorType.CODECLIMATE: ValidationLevel.UNIT_ONLY,
    ConnectorType.SNYK: ValidationLevel.UNIT_ONLY,
    ConnectorType.TRIVY: ValidationLevel.UNIT_ONLY,
    # --- Communication ---
    ConnectorType.SLACK: ValidationLevel.UNIT_ONLY,
    ConnectorType.DISCORD: ValidationLevel.UNIT_ONLY,
    ConnectorType.MICROSOFT_TEAMS: ValidationLevel.UNIT_ONLY,
    ConnectorType.OPSGENIE: ValidationLevel.UNIT_ONLY,
    # --- Secrets / package management ---
    ConnectorType.ONEPASSWORD: ValidationLevel.UNIT_ONLY,
    ConnectorType.AZURE_KEY_VAULT: ValidationLevel.UNIT_ONLY,
    ConnectorType.NPM: ValidationLevel.UNIT_ONLY,
    ConnectorType.PYPI: ValidationLevel.UNIT_ONLY,
    # --- Misc ---
    ConnectorType.FILESYSTEM: ValidationLevel.UNIT_ONLY,
    ConnectorType.SHELL: ValidationLevel.UNIT_ONLY,
    ConnectorType.REST: ValidationLevel.UNIT_ONLY,
    ConnectorType.N8N: ValidationLevel.UNIT_ONLY,
    ConnectorType.GITEA: ValidationLevel.UNIT_ONLY,
    ConnectorType.AZURE_REPOS: ValidationLevel.UNIT_ONLY,
    ConnectorType.TICKET_TRACKER: ValidationLevel.UNIT_ONLY,
    ConnectorType.CUSTOM: ValidationLevel.UNIT_ONLY,
    ConnectorType.SHAREPOINT: ValidationLevel.UNIT_ONLY,
    ConnectorType.MONDAY: ValidationLevel.UNIT_ONLY,
    ConnectorType.CONFLUENCE: ValidationLevel.UNIT_ONLY,
    ConnectorType.DROPBOX_PAPER: ValidationLevel.UNIT_ONLY,
    ConnectorType.CI_RUNNER: ValidationLevel.UNIT_ONLY,
}

_MODEL_BACKEND_BASELINE: dict[ModelBackendProvider, ValidationLevel] = {
    # --- Cloud providers with recorded contract tests ---
    ModelBackendProvider.OPENAI: ValidationLevel.CONTRACT_RECORDED,
    ModelBackendProvider.ANTHROPIC: ValidationLevel.CONTRACT_RECORDED,
    ModelBackendProvider.AZURE_OPENAI: ValidationLevel.CONTRACT_RECORDED,
    ModelBackendProvider.BEDROCK: ValidationLevel.CONTRACT_RECORDED,
    ModelBackendProvider.GEMINI: ValidationLevel.CONTRACT_RECORDED,
    ModelBackendProvider.VERTEXAI: ValidationLevel.CONTRACT_RECORDED,
    ModelBackendProvider.GROQ: ValidationLevel.CONTRACT_RECORDED,
    ModelBackendProvider.DEEPSEEK: ValidationLevel.CONTRACT_RECORDED,
    ModelBackendProvider.MISTRAL: ValidationLevel.CONTRACT_RECORDED,
    ModelBackendProvider.COHERE: ValidationLevel.CONTRACT_RECORDED,
    ModelBackendProvider.OPENROUTER: ValidationLevel.CONTRACT_RECORDED,
    ModelBackendProvider.TOGETHERAI: ValidationLevel.CONTRACT_RECORDED,
    ModelBackendProvider.PERPLEXITY: ValidationLevel.CONTRACT_RECORDED,
    ModelBackendProvider.FIREWORKS: ValidationLevel.CONTRACT_RECORDED,
    ModelBackendProvider.GROK: ValidationLevel.CONTRACT_RECORDED,
    ModelBackendProvider.QWEN: ValidationLevel.CONTRACT_RECORDED,
    ModelBackendProvider.AI21: ValidationLevel.CONTRACT_RECORDED,
    ModelBackendProvider.REPLICATE: ValidationLevel.CONTRACT_RECORDED,
    ModelBackendProvider.WATSONX: ValidationLevel.CONTRACT_RECORDED,
    ModelBackendProvider.OPENCODE: ValidationLevel.CONTRACT_RECORDED,
    # --- Local / self-hosted (unit-level only) ---
    ModelBackendProvider.OLLAMA: ValidationLevel.UNIT_ONLY,
    ModelBackendProvider.VLLM: ValidationLevel.UNIT_ONLY,
    ModelBackendProvider.TGI: ValidationLevel.UNIT_ONLY,
    ModelBackendProvider.LM_STUDIO: ValidationLevel.UNIT_ONLY,
    ModelBackendProvider.JAN: ValidationLevel.UNIT_ONLY,
    ModelBackendProvider.LOCALAI: ValidationLevel.UNIT_ONLY,
    ModelBackendProvider.LLAMACPP: ValidationLevel.UNIT_ONLY,
    ModelBackendProvider.CUSTOM: ValidationLevel.UNIT_ONLY,
}


def connector_baseline_level(connector_type: str) -> str:
    """Return the static baseline validation level for a connector type.

    Falls back to ``unit-only`` for unknown types — never raise.
    """
    try:
        ct = ConnectorType(connector_type)
    except ValueError:
        return ValidationLevel.UNIT_ONLY
    return _CONNECTOR_BASELINE.get(ct, ValidationLevel.UNIT_ONLY)


def model_backend_baseline_level(provider: str) -> str:
    """Return the static baseline validation level for a model backend provider.

    Falls back to ``unit-only`` for unknown providers — never raise.
    """
    try:
        p = ModelBackendProvider(provider)
    except ValueError:
        return ValidationLevel.UNIT_ONLY
    return _MODEL_BACKEND_BASELINE.get(p, ValidationLevel.UNIT_ONLY)


def resolve_validation_level(
    baseline: str,
    *,
    last_health_check_at: object | None,
    last_health_check_error: str | None,
) -> str:
    """Resolve the *current* validation level from the baseline and canary state.

    Rules:

    * If no canary has ever run (``last_health_check_at is None``), return the
      baseline (the ceiling).
    * If the most recent canary succeeded (``last_health_check_error`` is
      ``None`` or empty), return the baseline (the ceiling was achieved).
    * If the most recent canary failed (``last_health_check_error`` is
      non-empty), degrade one level below the baseline — a red canary can
      never leave a stale "green".
    * On any computation failure, degrade to the baseline (fail-safe, never
      break ``get_integration_status``).
    """
    try:
        if last_health_check_at is None:
            # No canary yet — return the ceiling.
            return baseline
        if not last_health_check_error:
            # Canary passed — return the ceiling.
            return baseline
        # Canary failed — degrade one level below baseline.
        return _level_below(baseline)
    except Exception:
        return baseline
