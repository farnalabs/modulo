"""Pydantic config models for ``modulo apply`` (FAR-681, slices 1+2).

Mirrors the API-layer Create/Update shapes for the entity set covered by
slice 1 (schemas + versions, model_backends) and slice 2 (pipelines with
agent name-refs in graphs, triggers with (pipeline, name) identity).
"""

from __future__ import annotations

import re
import uuid
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

API_VERSION_PREFIX = "modulo.dev/v"
API_VERSION_SUPPORTED_MAJOR = 1

# The (pipeline, name) composite key separator: '/' is forbidden in pipeline
# and trigger names so the display key is always unambiguous.
COMPOSITE_KEY_SEPARATOR = "/"

# daily_spend_limit is a Numeric(12, 4) column; the desired value is quantized
# to the same scale before hashing so a higher-precision declaration cannot
# produce permanent false drift against the stored 4dp value.
_SPEND_QUANTUM = Decimal("0.0001")


def quantize_daily_spend_limit(value: float | None) -> float | None:
    """Quantize a daily_spend_limit to the column's 4dp scale (None passthrough)."""
    if value is None:
        return None
    return float(Decimal(str(value)).quantize(_SPEND_QUANTUM, rounding=ROUND_HALF_UP))


# \Z (not $) so a trailing-newline variant ("${env:VAR}\n") fails to match.
ENV_REF_PATTERN = re.compile(r"\$\{env:([A-Za-z_][A-Za-z0-9_]*)\}\Z")
SECRET_REF_PATTERN = re.compile(r"secretref://\S+\Z")


def parse_api_version_major(raw: str) -> int:
    """Return the declared major version of an ``modulo.dev/v<major>`` string.

    Raises ApplyConfigError on an unparseable value (the same rule
    ``check_api_version`` applies).
    """
    if not isinstance(raw, str) or not raw.startswith(API_VERSION_PREFIX):
        msg = f"api_version must start with {API_VERSION_PREFIX!r}, got {raw!r}"
        raise ApplyConfigError(msg)
    major_part = raw[len(API_VERSION_PREFIX) :].split(".", maxsplit=1)[0]
    try:
        return int(major_part)
    except ValueError:
        msg = f"api_version {raw!r} is not parseable"
        raise ApplyConfigError(msg) from None


class ApplyConfigError(ValueError):
    """Raised when an apply config is structurally/semantically invalid."""


def check_api_version(raw: str) -> None:
    """Gate the declared api_version.

    Major must match the supported major exactly (hard error). Any minor
    suffix is accepted leniently.
    """
    declared_major = parse_api_version_major(raw)
    if declared_major != API_VERSION_SUPPORTED_MAJOR:
        msg = (
            "api_version major mismatch: config declares modulo.dev/v"
            f"{declared_major}, this client supports modulo.dev/v"
            f"{API_VERSION_SUPPORTED_MAJOR}"
        )
        raise ApplyConfigError(msg)


class ApplyVersionSpec(BaseModel):
    """A schema version requested inside a SchemaEntity."""

    model_config = ConfigDict(extra="forbid")

    version: str = Field(min_length=1, max_length=64)
    version_number: int = Field(ge=0)
    definition_json: dict[str, Any] = Field(min_length=1)
    published: bool = False

    def managed_view(self) -> dict[str, Any]:
        """Canonical managed-field view used for hashing (version + number included)."""
        return {
            "version": self.version,
            "version_number": self.version_number,
            "definition_json": self.definition_json,
            "published": self.published,
        }


class SchemaEntity(BaseModel):
    """A declarative schema (mirrors SchemaCreate + SchemaCreateVersionCreate)."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=255)
    description: str | None = Field(default=None, max_length=2000)
    abstract_name: str | None = None
    versions: list[ApplyVersionSpec] = Field(default_factory=list)

    def managed_view(self) -> dict[str, Any]:
        """Canonical managed-field view used for hashing (name excluded)."""
        return {
            "description": self.description,
            "abstract_name": self.abstract_name,
            "versions": [v.managed_view() for v in sorted(self.versions, key=lambda s: (s.version, s.version_number))],
        }


class ModelBackendEntity(BaseModel):
    """A declarative model backend (mirrors ModelBackendCreate).

    ``api_key`` is write-only and MUST be a reference:
    ``${env:VAR}`` (resolved client-side at apply time). ``secretref://<key>``
    values parse here but are BLOCKED at plan time by this slice — server-side
    resolution does not exist yet, so passing one through would store a
    non-functional literal. Inline secret values are forbidden.
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=255)
    display_name: str = Field(min_length=1, max_length=255)
    provider: str = Field(min_length=1, max_length=128)
    model_id: str = Field(min_length=1, max_length=128)
    api_key: str = Field(min_length=1)
    default_params: dict[str, Any] = Field(default_factory=dict)
    visibility: str = Field(default="org")
    tier: Literal["native", "preview", "in_dev"] = "native"

    @field_validator("api_key")
    @classmethod
    def _api_key_must_be_ref(cls, value: str) -> str:
        if not ENV_REF_PATTERN.fullmatch(value) and not SECRET_REF_PATTERN.fullmatch(value):
            msg = (
                "api_key must be a reference (${env:VAR_NAME} — letters, digits, "
                "underscores — or secretref://<key>); "
                "inline secret values are forbidden"
            )
            raise ValueError(msg)
        return value

    def env_ref_var(self) -> str | None:
        """Return the env var name when api_key is an ${env:...} ref, else None."""
        match = ENV_REF_PATTERN.match(self.api_key)
        return match.group(1) if match else None

    def managed_view(self, *, include_api_key: bool = False) -> dict[str, Any]:
        """Canonical managed-field view used for hashing.

        ``api_key`` is write-only and is never included by default.
        """
        payload = {
            "display_name": self.display_name,
            "provider": self.provider,
            "model_id": self.model_id,
            "default_params": self.default_params,
            "visibility": self.visibility,
            "tier": self.tier,
        }
        if include_api_key:
            payload["api_key"] = self.api_key
        return payload


TRIGGER_TYPE_PATTERN = re.compile(r"^(manual|webhook|cron|polling|agent_signal|ongoing|slack_app_mention)$")


class ApplyEntityResolutionError(ValueError):
    """Raised when a config entity references something apply cannot resolve.

    Unmanaged references (an agent, a pipeline) are never auto-created by
    apply — the entity is blocked at plan time instead.
    """


class ApplyGraphPosition(BaseModel):
    """Mirror of the API GraphPosition (node canvas coordinates)."""

    model_config = ConfigDict(extra="forbid")

    x: float = Field(allow_inf_nan=False)
    y: float = Field(allow_inf_nan=False)


class ApplyGraphConnectorBinding(BaseModel):
    """Mirror of the API ConnectorBinding."""

    model_config = ConfigDict(extra="forbid")

    type: str = Field(min_length=1, max_length=100)
    instance_id: uuid.UUID


class ApplyGraphSchemaPin(BaseModel):
    """Mirror of the API SchemaPin (concrete version pins only)."""

    model_config = ConfigDict(extra="forbid")

    schema_id: uuid.UUID
    schema_version: str

    @field_validator("schema_version")
    @classmethod
    def _version_must_be_concrete(cls, v: str) -> str:
        if v in ("latest", "*", "") or len(v) > 50:
            raise ValueError(f"schema_version must be a concrete version, got '{v}'")
        return v


class ApplyGraphCapabilityScope(BaseModel):
    """Mirror of the API CapabilityScope (node-level least-privilege)."""

    model_config = ConfigDict(extra="forbid")

    allowed_connectors: list[str] | None = None
    allowed_tools: list[str] | None = None
    context_scope: list[str] | None = None


class ApplyGraphNode(BaseModel):
    """A pipeline graph node in the apply config.

    Same shape as the API ``PipelineGraphNode`` EXCEPT the agent is referenced
    by NAME (``agent``) instead of ``agent_id`` — the executor resolves names
    to ids via GET /agents and BLOCKS the entity when a referenced agent is
    missing (unmanaged reference: apply never auto-creates agents).
    ``extra="forbid"`` also makes unrecognised fields a loud load-time error —
    the API node model silently DROPS unknown keys, and a silently-dropped
    declarative field would create permanent plan drift.

    Node-type rules (manual/composite/sandbox validators) are NOT duplicated
    here: the executor normalises the resolved payload through the REAL API
    node model (same validators the server runs on save) cheap client-side.
    """

    model_config = ConfigDict(extra="forbid")

    id: uuid.UUID
    node_type: Literal["agent", "manual", "composite", "sandbox_agent", "router", "hitl", "join"] = "agent"
    agent: str | None = None
    position: ApplyGraphPosition
    connector_binding: ApplyGraphConnectorBinding | None = None
    output_schema_id: uuid.UUID | None = None
    input_schema_pin: ApplyGraphSchemaPin | None = None
    output_schema_pin: ApplyGraphSchemaPin | None = None
    capability_scope: ApplyGraphCapabilityScope | None = None
    label: str | None = Field(default=None, max_length=255)
    role: str | None = None
    autonomy_recommendation: str | None = None
    idempotent: bool = True
    composite_ref: uuid.UUID | None = None
    composite_parameter_values: dict[str, Any] | None = None
    composite_input_mapping: dict[str, Any] | None = None
    composite_output_mapping: dict[str, Any] | None = None
    parameter_set_id: uuid.UUID | None = None
    parameter_overrides: dict[str, Any] | None = None
    template_id: str | None = None
    mode: Literal["llm", "script"] = "llm"
    agent_command: str | None = None
    agent_commands: list[str] | None = None
    commands_concatenation_string: str = " && "
    agent_prompt: str | None = None
    script_command: str | None = None
    egress_policy: Literal["default", "deny_all", "selected"] | None = None
    egress_allowlist: list[dict[str, Any]] | None = None
    resource_limits: dict[str, Any] | None = None
    read_only: bool = False
    git_credentials: Literal["scoped", "unscoped", "none"] | None = None
    wallclock_budget_seconds: int | None = None
    delivery_sentinel: str | None = None
    env_vars: dict[str, str] | None = None
    context_files: dict[str, str] | None = None
    timeout_seconds: int | None = Field(default=None, ge=60, le=604800)
    output_schema_json: dict[str, Any] | None = None
    description: str | None = Field(default=None, max_length=2000)
    stall_timeout_seconds: int | None = Field(default=None, ge=60, le=604800)
    enable_heartbeat: bool = True
    watch_log_path: str | None = None
    stdout_percentage_delta: float | None = Field(default=None, ge=0.0, le=1.0)
    watch_globs: list[str] = Field(default_factory=list)
    router_config: dict[str, Any] | None = None
    hitl_config: dict[str, Any] | None = None
    fan_out: dict[str, Any] | None = None
    collect: list[dict[str, Any]] | None = None
    aggregate: dict[str, Any] | None = None
    join_partial_policy: Literal["collect_and_proceed", "fail"] = "collect_and_proceed"
    inputs: list[dict[str, Any]] | None = None
    outputs: list[dict[str, Any]] | None = None
    # FAR-792: per-node sandbox stdout/stderr retention (API PipelineGraphNode twin).
    # Declared here so the CLI does NOT reject it loudly as an unknown field on a
    # real saved graph; value rules are enforced by the REAL API node model when the
    # executor normalises the resolved payload through it. ``stdout_max_bytes`` uses
    # the same positive-integer gate as the API model.
    stdout_retention_mode: Literal["tail", "full"] | None = None
    stdout_max_bytes: int | None = None

    @field_validator("stdout_max_bytes", mode="before")
    @classmethod
    def _validate_stdout_max_bytes(cls, v: Any) -> Any:
        """Mirror of the API PipelineGraphNode positive-integer gate (FAR-792)."""
        if v is None:
            return v
        if isinstance(v, bool):
            raise ValueError("stdout_max_bytes must be a positive integer")
        try:
            if isinstance(v, int):
                value = v
            else:
                f = float(v)
                if not f.is_integer():
                    raise ValueError
                value = int(f)
        except (TypeError, ValueError, OverflowError):
            raise ValueError("stdout_max_bytes must be a positive integer") from None
        if value <= 0:
            raise ValueError("stdout_max_bytes must be a positive integer")
        return value

    @field_validator("commands_concatenation_string", mode="before")
    @classmethod
    def _default_commands_concatenation_string(cls, v: Any) -> Any:
        """Normalise absent/empty/null joiner to the runtime default (API twin)."""
        return v if isinstance(v, str) and v else " && "

    def api_node_payload(self) -> dict[str, Any]:
        """Config node -> API node dict (without agent resolution)."""
        payload = self.model_dump(mode="json")
        payload.pop("agent", None)
        return payload


class ApplyGraphEdge(BaseModel):
    """A pipeline graph edge in the apply config (API PipelineGraphEdge twin)."""

    model_config = ConfigDict(extra="forbid")

    id: uuid.UUID
    source_node_id: uuid.UUID
    target_node_id: uuid.UUID
    edge_type: str = Field(pattern=r"^(normal|reject|conditional|loop)$")
    hitl_gate_config: dict[str, Any] | None = None
    condition_expression: str | None = Field(default=None, max_length=500)
    source_port: str = "out"
    target_port: str = "in"

    def api_edge_payload(self) -> dict[str, Any]:
        """Config edge -> API edge dict."""
        return self.model_dump(mode="json")


class ApplyGraph(BaseModel):
    """A pipeline graph: nodes + edges (API PipelineGraphUpdate shape)."""

    model_config = ConfigDict(extra="forbid")

    # DoS guards mirrored from the API graph-update limits apply side: the
    # server rejects oversized graphs at save time either way; failing at
    # load keeps the plan honest before any fetch.
    nodes: list[ApplyGraphNode] = Field(default_factory=list)
    edges: list[ApplyGraphEdge] = Field(default_factory=list)

    @model_validator(mode="after")
    def _unique_ids(self) -> ApplyGraph:
        max_nodes = 500
        max_edges = 1000
        if len(self.nodes) > max_nodes:
            msg = f"Graph exceeds maximum of {max_nodes} nodes"
            raise ValueError(msg)
        if len(self.edges) > max_edges:
            msg = f"Graph exceeds maximum of {max_edges} edges"
            raise ValueError(msg)
        node_ids = [node.id for node in self.nodes]
        if len(node_ids) != len(set(node_ids)):
            msg = "Graph node IDs must be unique"
            raise ValueError(msg)
        edge_ids = [edge.id for edge in self.edges]
        if len(edge_ids) != len(set(edge_ids)):
            msg = "Graph edge IDs must be unique"
            raise ValueError(msg)
        paths = [(edge.source_node_id, edge.target_node_id, edge.edge_type) for edge in self.edges]
        if len(paths) != len(set(paths)):
            msg = "Graph edge paths must be unique"
            raise ValueError(msg)
        return self


class PipelineEntity(BaseModel):
    """A declarative pipeline (mirrors PipelineCreate + PipelineGraphUpdate).

    ``graph`` is OPTIONAL: when omitted, apply does not manage the graph at
    all (the hash never includes it and no graph write is ever sent) so a
    UI-authored graph is not clobbered by a graph-less config.
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=255)
    description: str | None = Field(default=None, max_length=2000)
    max_concurrent_runs: int = Field(default=5, ge=1)
    graph: ApplyGraph | None = None

    @field_validator("name")
    @classmethod
    def _name_must_not_contain_separator(cls, value: str) -> str:
        return _reject_composite_separator("pipeline name", value)

    def managed_view(self, *, graph: dict[str, Any] | None = None) -> dict[str, Any]:
        """Canonical managed-field view used for hashing.

        The live graph view is passed in by the executor AFTER agent-name
        resolution + server-shape normalisation (see pipeline_apply); the
        static method returns the graph-free baseline.
        """
        view: dict[str, Any] = {
            "description": self.description,
            "max_concurrent_runs": self.max_concurrent_runs,
        }
        if self.graph is not None:
            view["graph"] = {"nodes": [], "edges": []} if graph is None else graph
        return view


class TriggerEntity(BaseModel):
    """A declarative trigger (mirrors TriggerCreate/TriggerUpdate).

    Identity is (pipeline, name): ``pipeline`` cross-references a pipeline by
    NAME (declared earlier in the same document or already present in the
    target org). ``name`` is the declarative identity handle created by the
    0201 migration (and enforced unique per (org, pipeline) among live rows
    by the same migration's partial unique index).

    config_json secret policy (same refs-only policy as backend api_key):
    non-empty string values under sensitive keys — at ANY nesting depth (the
    LEAF key of each walked path is what the sensitive-key predicate tests) —
    or values the server would mask as secret-shaped MUST be ``${env:VAR}``
    or ``secretref://<key>`` references — inline secret literals are forbidden.
    ``secretref://`` values parse but are BLOCKED at plan time (no
    server-side resolution yet). ``hmac_secret``/``signing_secret`` are
    Fernet-encrypted server-side on write and masked on read, so those keys
    are excluded from drift hashing entirely (apply never verifies an
    existing secret's value; secret-only rotation is invisible to the hash —
    use the CLI's ``--refresh-secrets`` to re-send such configs).
    """

    model_config = ConfigDict(extra="forbid")

    pipeline: str = Field(min_length=1, max_length=255)
    name: str = Field(min_length=1, max_length=255)
    trigger_type: str = Field(pattern=TRIGGER_TYPE_PATTERN)
    active: bool = True
    max_concurrent_runs: int = Field(default=1, ge=1)
    daily_spend_limit: float | None = Field(default=None, ge=0)
    cron_expression: str | None = Field(default=None, max_length=100)
    cron_timezone: str | None = Field(default=None, max_length=50)
    config_json: dict[str, Any] = Field(default_factory=dict)

    @field_validator("pipeline", "name")
    @classmethod
    def _identity_must_not_contain_separator(cls, value: str) -> str:
        return _reject_composite_separator("trigger identity", value)

    @model_validator(mode="after")
    def _config_secrets_must_be_refs(self) -> TriggerEntity:
        for path, value in _walk_config_strings(self.config_json):
            # The sensitive-KEY predicate tests the LEAF key of every walked
            # path (matching is_sensitive_key semantics): smtp.password is a
            # secret even though the top-level key is not.
            if not _is_secret_shaped(path[-1], value):
                continue
            if not ENV_REF_PATTERN.fullmatch(value) and not SECRET_REF_PATTERN.fullmatch(value):
                msg = (
                    f"config_json secret value under {'.'.join(path)!r} must be a reference "
                    "(${env:VAR_NAME} or secretref://<key>); inline secret values are forbidden "
                    "(hmac_secret/signing_secret are Fernet-encrypted server-side on write)"
                )
                raise ApplyConfigError(msg)
        return self

    def with_resolved_config(self, resolved_config_json: dict[str, Any]) -> TriggerEntity:
        """Return a copy whose config_json is the RESOLVED mapping.

        Hashing (``managed_view``) must run on resolved literals so a
        ``${env:VAR}`` desired view compares equal to the server's stored
        resolved value (raw refs vs stored literals would be permanent false
        drift). Call this BEFORE ``managed_view`` for every planned trigger.
        """
        return self.model_copy(update={"config_json": resolved_config_json})

    def managed_view(self) -> dict[str, Any]:
        """Canonical managed-field view used for hashing.

        config_json secret-shaped entries (sensitive keys, secret-pattern
        values) are excluded — the server masks them on read, so comparing
        them would be permanent false drift. Hash the RESOLVED config: pass
        the resolved mapping through :meth:`with_resolved_config` first.
        daily_spend_limit is quantized to the column's 4dp scale.
        """
        return {
            "trigger_type": self.trigger_type,
            "active": self.active,
            "max_concurrent_runs": self.max_concurrent_runs,
            "daily_spend_limit": quantize_daily_spend_limit(self.daily_spend_limit),
            "cron_expression": self.cron_expression,
            "cron_timezone": self.cron_timezone,
            "config_json": strip_secret_shaped_config(self.config_json),
        }

    def display_key(self) -> str:
        """The (pipeline, name) composite identity used in plan reports."""
        return f"{self.pipeline}/{self.name}"

    def create_payload(self, resolved_config_json: dict[str, Any]) -> dict[str, Any]:
        """POST /pipelines/{pipeline_id}/triggers body (TriggerCreate shape)."""
        return {
            "name": self.name,
            "trigger_type": self.trigger_type,
            "active": self.active,
            "max_concurrent_runs": self.max_concurrent_runs,
            "daily_spend_limit": self.daily_spend_limit,
            "cron_expression": self.cron_expression,
            "cron_timezone": self.cron_timezone,
            "config_json": resolved_config_json,
        }

    def update_payload(self, resolved_config_json: dict[str, Any]) -> dict[str, Any]:
        """PUT /triggers/{trigger_id} body (TriggerUpdate shape).

        ``daily_spend_limit`` is ALWAYS included (present-in-fields-set):
        a declared None clears the (non-ongoing) limit; ongoing triggers
        reject clearing server-side and the rejection surfaces as a failed
        entity with the server's reason.
        """
        return {
            "active": self.active,
            "max_concurrent_runs": self.max_concurrent_runs,
            "daily_spend_limit": self.daily_spend_limit,
            "cron_expression": self.cron_expression,
            "cron_timezone": self.cron_timezone,
            "config_json": resolved_config_json,
        }


# The API mask's sensitive-KEY pattern set (api.middleware.sensitive_mask),
# replicated LOCALLY so the CLI never imports the FastAPI/DB-heavy middleware
# module at validation time (the apply CLI runs without server settings). The
# pair (this set + the substring matching rule) is pinned byte-identical to
# the middleware by tests/unit/cli/test_apply_models.py — the drift alarm.
_SENSITIVE_KEY_PATTERNS: frozenset[str] = frozenset(
    {
        "token",
        "secret",
        "api_key",
        "password",
        "passwd",
        "key",
        "credential",
        "database_url",
        "encryption",
        "signing",
        "private",
    }
)


def is_sensitive_key(key: str) -> bool:
    """Local twin of api.middleware.sensitive_mask.is_sensitive_key."""
    key_lower = key.lower().replace("-", "_").replace(" ", "_")
    return any(pattern in key_lower for pattern in _SENSITIVE_KEY_PATTERNS)


def _is_secret_shaped(key: str, value: str) -> bool:
    """True when a config value is treated as a secret by the masking policy.

    Two triggers: a sensitive KEY name (same pattern the API mask uses), or a
    string value that the server's secret-VALUE patterns would redact. Empty
    strings are never secrets (the mask only replaces truthy values).
    """
    if not isinstance(value, str) or not value:
        return False
    if is_sensitive_key(key):
        return True
    return _value_is_secret_pattern(value)


def _walk_config_strings(
    value: Any,
    path: tuple[str, ...] = (),
) -> list[tuple[tuple[str, ...], str]]:
    """Depth-first (key, string-value) pairs of a config dict (mask-aware)."""

    found: list[tuple[tuple[str, ...], str]] = []

    def _walk(node: Any, prefix: tuple[str, ...]) -> None:
        if isinstance(node, dict):
            for key, item in node.items():
                if isinstance(item, str) and item:
                    found.append(((*prefix, str(key)), item))
                else:
                    _walk(item, (*prefix, str(key)))
        elif isinstance(node, list):
            for _index, item in enumerate(node):
                _walk(item, prefix)

    _walk(value, path)
    return found


def strip_secret_shaped_config(config: dict[str, Any]) -> dict[str, Any]:
    """Recursively REMOVE secret-shaped entries from config_json.

    Applied symmetrically to the desired view (before hashing) and to the
    current view (which arrives masked from the API): an entry is dropped
    when its key is sensitive-pattern OR the server masked its value. Apply
    therefore never drifts on server-stored secrets it cannot read back, and
    secret CHANGES still write through: the executor always re-sends the
    declared config on any create/update (the server Fernet-encrypts
    hmac_secret/signing_secret and merges non-masked values).
    """
    from modulo.core.secret_patterns import SENSITIVE_VALUE_MASK

    def _drop_secret_string(item: str, key_str: str) -> bool:
        if not item:
            return False
        return SENSITIVE_VALUE_MASK in item or is_sensitive_key(key_str) or _value_is_secret_pattern(item)

    def _strip_child(item: Any, key_str: str) -> Any:
        if isinstance(item, str):
            return None if _drop_secret_string(item, key_str) else item
        if isinstance(item, dict):
            return _strip(item)
        if isinstance(item, list):
            kept: list[Any] = []
            for element in item:
                if isinstance(element, str):
                    if not _drop_secret_string(element, key_str):
                        kept.append(element)
                else:
                    kept.append(_strip_child(element, key_str))
            return kept
        return item

    def _strip(node: dict[str, Any]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for key, item in node.items():
            key_str = str(key)
            child = _strip_child(item, key_str)
            if child is None:
                continue
            out[key_str] = child
        return out

    return _strip(config)


def _value_is_secret_pattern(value: str) -> bool:
    from modulo.core.secret_patterns import mask_secret_values_in_text

    return bool(value) and mask_secret_values_in_text(value) != value


def config_declares_secrets(config: dict[str, Any]) -> bool:
    """True when the (RESOLVED) config declares any secret-shaped entry.

    Called on the env-RESOLVED mapping: an entry the drift hash strips
    (sensitive LEAF key, or a resolved value the server would mask) cannot be
    compared against the stored state, so its rotation is invisible to the
    plan — the executor's ``--refresh-secrets`` flag re-sends such configs.
    """
    return any(_is_secret_shaped(path[-1], value) for path, value in _walk_config_strings(config))


def _reject_composite_separator(label: str, value: str) -> str:
    """Forbid the (pipeline, name) composite-key separator in identity names.

    A '/' inside a pipeline or trigger name would make the ``pipeline/name``
    display key ambiguous (plan reports and trigger identity maps key on it),
    so it is a load-time error.
    """
    if COMPOSITE_KEY_SEPARATOR in value:
        msg = (
            f"{label} {value!r} must not contain {COMPOSITE_KEY_SEPARATOR!r} — it is the "
            "(pipeline, name) composite-key separator"
        )
        raise ValueError(msg)
    return value


class EntitySet(BaseModel):
    """Entities declared for a single apply document."""

    model_config = ConfigDict(extra="forbid")

    schemas: list[SchemaEntity] = Field(default_factory=list)
    model_backends: list[ModelBackendEntity] = Field(default_factory=list)
    pipelines: list[PipelineEntity] = Field(default_factory=list)
    triggers: list[TriggerEntity] = Field(default_factory=list)


class ApplyConfig(BaseModel):
    """Top-level document shape. api_version gate enforced by a validator."""

    model_config = ConfigDict(extra="forbid")

    api_version: str
    entities: EntitySet = Field(default_factory=EntitySet)

    @model_validator(mode="after")
    def _gate_api_version(self) -> ApplyConfig:
        check_api_version(self.api_version)
        return self

    @model_validator(mode="after")
    def _unique_names_per_kind(self) -> ApplyConfig:
        schema_names = [s.name for s in self.entities.schemas]
        backend_names = [b.name for b in self.entities.model_backends]
        pipeline_names = [p.name for p in self.entities.pipelines]
        # Trigger identity is (pipeline, name) — the pipeline name is part of
        # the entity key, so duplicates are detected on the pair.
        trigger_keys = [f"{t.pipeline}/{t.name}" for t in self.entities.triggers]
        for label, names in (
            ("schemas", schema_names),
            ("model_backends", backend_names),
            ("pipelines", pipeline_names),
            ("triggers", trigger_keys),
        ):
            seen: set[str] = set()
            for name in names:
                if name in seen:
                    msg = f"duplicate {label} entity {name!r} in apply config"
                    raise ApplyConfigError(msg)
                seen.add(name)
        return self

    def merge_entities(self, other: ApplyConfig) -> ApplyConfig:
        """Combine entities from another document (for multi-doc YAML).

        Raises ApplyConfigError on an api_version MAJOR mismatch (minors are
        compared leniently — "modulo.dev/v1" and "modulo.dev/v1.3" merge) or
        duplicate entity names of the same kind.
        """
        self_major = parse_api_version_major(self.api_version)
        other_major = parse_api_version_major(other.api_version)
        if self_major != other_major:
            msg = f"api_version major differs across YAML documents: {self.api_version!r} vs {other.api_version!r}"
            raise ApplyConfigError(msg)
        # Forward-reference gate: a trigger's pipeline must be declared in the
        # SAME or an EARLIER document (apply's phase order runs all pipelines
        # before all triggers, but a cross-document pipeline that only exists
        # in a LATER doc is an ambiguous intent — reject with a reorder hint).
        own_pipelines = {p.name for p in self.entities.pipelines}
        other_pipelines = {p.name for p in other.entities.pipelines}
        for trigger in self.entities.triggers:
            if trigger.pipeline not in own_pipelines and trigger.pipeline in other_pipelines:
                msg = (
                    f"trigger {trigger.display_key()!r} forward-references pipeline {trigger.pipeline!r} "
                    "declared in a later YAML document - move the pipeline into the trigger's document "
                    "(or an earlier one)"
                )
                raise ApplyConfigError(msg)
        merged_entities = EntitySet(
            schemas=[*self.entities.schemas, *other.entities.schemas],
            model_backends=[
                *self.entities.model_backends,
                *other.entities.model_backends,
            ],
            pipelines=[*self.entities.pipelines, *other.entities.pipelines],
            triggers=[*self.entities.triggers, *other.entities.triggers],
        )
        # Collect duplicate names across the merged set up front (the model
        # validators only check single documents at construction time).
        for label, names in (
            ("schemas", [s.name for s in merged_entities.schemas]),
            ("model_backends", [b.name for b in merged_entities.model_backends]),
            ("pipelines", [p.name for p in merged_entities.pipelines]),
            ("triggers", [f"{t.pipeline}/{t.name}" for t in merged_entities.triggers]),
        ):
            if len(names) != len(set(names)):
                msg = f"duplicate {label} entity across YAML documents"
                raise ApplyConfigError(msg)
        return ApplyConfig(api_version=self.api_version, entities=merged_entities)
