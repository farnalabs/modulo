"""Guardrail config-as-code — versioned, hashed, PR-gated org configuration.

T3 of the phase-guardrails epic (FAR-219) layers a git-style authoring and
review workflow on top of the shipped T1 engine (:mod:`modulo.core.guardrails`).

An org's guardrail configuration is expressed as a
:class:`GuardrailConfigSet` — a versioned YAML document whose entries map
1:1 to the ``eval_type='guardrail'`` rows the engine consumes. The layer is
an authoring/source-of-truth seam: it validates against the engine's own
guardrail rules, computes stable content hashes, produces add/update/remove
diffs, and pins applied snapshots (``guardrail_pins_json``) so drift can be
detected when the live rows diverge from the applied config.

The PR-style workflow is: **propose** (validate + hash + diff) → **apply**
(the approve/merge step — reconciles the live ``EvalDefinition`` rows) →
**reject** (discard the proposal). Every step is audited.

Constraints honoured here (mirroring the engine):

* Detection is deterministic pure evals ONLY — ``regex`` | ``json_schema``.
* ``failure_behaviour='retry'`` is never expressible on a config-set guardrail
  — guardrail rows are always written with ``failure_behaviour='warn'``
  because block semantics are guardrail-owned (``config.action == 'block'``),
  never the eval ``failure_behaviour``.
* Redaction field paths are STATIC author config (never payload-derived).
* Hashes are computed over a canonical serialization (sorted keys, sorted
  guardrail list by stable id) — NOT the raw YAML text — so equivalent YAML
  layouts hash identically and drift recomputation from DB rows is stable.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

import pydantic
import yaml
from pydantic import BaseModel, Field, model_validator

from modulo.core.eval_engine import EvalDefinition, EvalType
from modulo.core.guardrails import (
    DEFAULT_GUARDRAIL_TIMEOUT_SECONDS,
    DEFAULT_MAX_GUARDRAILS_PER_NODE,
    GUARDRAIL_DETECTION_TYPES,
    FieldRedactionMode,
    GuardrailAction,
    GuardrailConfigError,
    _resolve_detection,
    resolve_guardrail_cap,
    resolve_guardrail_timeout,
)

GuardrailPinStatus = Literal["clean", "proposed", "drift"]
ConfigChangeAction = Literal["add", "update", "remove"]


class GuardrailDetection(BaseModel):
    """Deterministic detection declaration — regex or json_schema only."""

    model_config = {"populate_by_name": True}

    type: Literal["regex", "json_schema"]
    pattern: str | None = None
    field: str | None = None
    # ``schema`` is the wire key (YAML/JSON). The Python attribute is
    # ``schema_data`` because ``schema`` shadows a deprecated ``BaseModel``
    # method (pydantic UserWarning).
    schema_data: dict[str, Any] | None = Field(
        default=None,
        validation_alias="schema",
        serialization_alias="schema",
    )

    @model_validator(mode="after")
    def _coerce_detection_shape(self) -> GuardrailDetection:
        if self.type == "regex" and isinstance(self.schema_data, dict):
            raise ValueError("regex detection cannot carry a 'schema'; use pattern + field")
        if self.type == "json_schema" and (self.pattern is not None or self.field is not None):
            raise ValueError("json_schema detection cannot carry 'pattern'/'field'; use 'schema'")
        return self


class RedactionRule(BaseModel):
    """Static field-path redaction policy (author config, NEVER payload-derived)."""

    path: str = Field(min_length=1)
    mode: FieldRedactionMode = FieldRedactionMode.TRANSFORM


class GuardrailConfigItem(BaseModel):
    """One guardrail as code — the full DTO the engine's rows are built from.

    ``id`` is the STABLE per-guardrail slug used for idempotent re-imports and
    diffs; it maps to the applied ``EvalDefinition.name``. ``name`` is the
    human-readable label.
    """

    id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9._-]*$", min_length=1, max_length=100)
    name: str = Field(min_length=1, max_length=255)
    action: GuardrailAction = GuardrailAction.OBSERVE
    detection: GuardrailDetection
    redaction: list[RedactionRule] = Field(default_factory=list)
    required_capabilities: list[str] = Field(default_factory=list)


class GuardrailConfigSet(BaseModel):
    """An org's full guardrail configuration as code.

    ``max_guardrails_per_node`` (item 7) caps how many guardrail eval
    definitions may be bound to a single pipeline node; ``0`` disables the
    cap. ``guardrail_timeout_seconds`` (item 7) is the per-guardrail hard
    detection timeout applied at the ingestion edge. Both are ORG-LEVEL knobs
    mirrored onto every applied row's ``config_json`` so the engine's seam
    reads them from the rows it already loads.
    """

    version: int = Field(default=1, ge=1)
    guardrails: list[GuardrailConfigItem] = Field(default_factory=list)
    max_guardrails_per_node: int = Field(default=DEFAULT_MAX_GUARDRAILS_PER_NODE, ge=0)
    guardrail_timeout_seconds: float = Field(default=DEFAULT_GUARDRAIL_TIMEOUT_SECONDS, gt=0)


@dataclass
class GuardrailPin:
    """The org's snapshot pin (persisted as ``guardrail_pins_json``).

    ``applied_*`` fields record the last APPLIED config; ``proposed_*`` the
    in-flight proposal waiting for apply/reject. ``status`` is
    ``"clean" | "proposed" | "drift"`` — ``drift`` means the live DB rows
    no longer match ``applied_hash``.
    """

    org_id: uuid.UUID
    applied_hash: str | None = None
    applied_at: str | None = None
    serialized_snapshot: str | None = None
    proposed_hash: str | None = None
    proposed_at: str | None = None
    serialized_proposal: str | None = None
    status: GuardrailPinStatus = "clean"

    def to_json(self) -> dict[str, Any]:
        return {
            "applied_hash": self.applied_hash,
            "applied_at": self.applied_at,
            "serialized_snapshot": self.serialized_snapshot,
            "proposed_hash": self.proposed_hash,
            "proposed_at": self.proposed_at,
            "serialized_proposal": self.serialized_proposal,
            "status": self.status,
        }

    @classmethod
    def from_json(cls, org_id: uuid.UUID, data: dict[str, Any] | None) -> GuardrailPin | None:
        if not data:
            return None
        status = data.get("status", "clean")
        if status not in ("clean", "proposed", "drift"):
            status = "clean"
        return cls(
            org_id=org_id,
            applied_hash=data.get("applied_hash"),
            applied_at=data.get("applied_at"),
            serialized_snapshot=data.get("serialized_snapshot"),
            proposed_hash=data.get("proposed_hash"),
            proposed_at=data.get("proposed_at"),
            serialized_proposal=data.get("serialized_proposal"),
            status=status,
        )


@dataclass
class ConfigChange:
    """One per-guardrail change in a config-set diff (add / update / remove)."""

    action: ConfigChangeAction
    id: str
    name: str
    old_hash: str | None = None
    new_hash: str | None = None
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "id": self.id,
            "name": self.name,
            "old_hash": self.old_hash,
            "new_hash": self.new_hash,
            "detail": self.detail,
        }


# ---------------------------------------------------------------------------
# YAML load / dump (safe only — semgrep-enforced repo rule)
# ---------------------------------------------------------------------------


def load_config_set(yaml_text: str) -> GuardrailConfigSet:
    """Parse and validate *yaml_text* into a :class:`GuardrailConfigSet`.

    Raises :class:`GuardrailConfigError` for malformed YAML, a non-mapping
    document, a Pydantic shape violation, or a guardrail-rule violation.
    """
    if not yaml_text or not yaml_text.strip():
        raise GuardrailConfigError("Guardrail config is empty.")
    try:
        data = yaml.safe_load(yaml_text)
    except yaml.YAMLError as exc:
        raise GuardrailConfigError(f"Invalid YAML in guardrail config: {exc}") from exc
    if not isinstance(data, dict):
        raise GuardrailConfigError("Guardrail config must be a mapping with 'version' and 'guardrails'.")
    try:
        config_set = GuardrailConfigSet.model_validate(data)
    except pydantic.ValidationError as exc:
        raise GuardrailConfigError(f"Invalid guardrail config: {exc}") from exc
    validate_config_set(config_set)
    return config_set


def dump_config_set(config_set: GuardrailConfigSet) -> str:
    """Serialize *config_set* to stable YAML (sorted keys, block style)."""
    return yaml.safe_dump(
        config_set.model_dump(mode="json", by_alias=True),
        sort_keys=False,
        default_flow_style=False,
        allow_unicode=True,
    )


# Mask used for the non-admin guardrail config read (FAR-309 PR A): the actual
# regex patterns / JSON schemas / redaction field paths are safety-control
# internals a viewer must not see. The elevated (admin) read returns them
# unmasked.
REDACTED_MASK = "********"


def mask_config_set(config_set: GuardrailConfigSet) -> GuardrailConfigSet:
    """Return a structural copy of *config_set* with sensitive values masked.

    Preserves the full topology (ids, names, actions, detection types, knobs)
    so a non-admin viewer can still see which guardrails exist and what they
    do, but replaces the deny-rule internals — the regex ``pattern``, the
    JSON ``schema``, and every ``redaction[].path`` — with the redaction mask.
    Used by the standard (non-admin) ``GET /guardrails/config`` read; the
    elevated admin read returns the unmasked set.
    """
    masked_items: list[GuardrailConfigItem] = []
    for item in config_set.guardrails:
        detection = item.detection.model_copy(deep=True)
        if detection.type == "regex":
            detection.pattern = REDACTED_MASK
        else:
            detection.schema_data = {"redacted": True} if detection.schema_data else None
        redaction = [rule.model_copy(update={"path": REDACTED_MASK}) for rule in item.redaction]
        masked_items.append(item.model_copy(update={"detection": detection, "redaction": redaction}))
    return config_set.model_copy(update={"guardrails": masked_items})


# ---------------------------------------------------------------------------
# Validation (reuses the engine's guardrail rules)
# ---------------------------------------------------------------------------


def validate_config_set(config_set: GuardrailConfigSet) -> None:
    """Apply the engine's guardrail validation rules to the whole set.

    Raises :class:`GuardrailConfigError` on the first violation. Detection
    type is restricted to ``regex``/``json_schema`` at the DTO level; here we
    enforce the cross-field rules the engine enforces at evaluation time
    (regex needs pattern+field, json_schema needs a schema dict) so an invalid
    config fails at propose time — never silently pass through at the edge.
    """
    seen: set[str] = set()
    for item in config_set.guardrails:
        if item.id in seen:
            raise GuardrailConfigError(f"Duplicate guardrail id {item.id!r} in config set.")
        seen.add(item.id)
        if item.detection.type == "regex":
            if not item.detection.pattern or not item.detection.field:
                raise GuardrailConfigError(
                    f"Guardrail {item.id!r} regex detection requires non-empty 'pattern' and 'field' "
                    f"(got pattern={item.detection.pattern!r}, field={item.detection.field!r})."
                )
        elif not isinstance(item.detection.schema_data, dict):
            raise GuardrailConfigError(
                f"Guardrail {item.id!r} json_schema detection requires a 'schema' dict "
                f"(got {item.detection.schema_data!r})."
            )
        for rule in item.redaction:
            if not rule.path.strip():
                raise GuardrailConfigError(f"Guardrail {item.id!r} has an empty redaction path.")


# ---------------------------------------------------------------------------
# Content hashing (canonical serialization — NOT the raw YAML text)
# ---------------------------------------------------------------------------


def _canonical_dict(config_set: GuardrailConfigSet) -> dict[str, Any]:
    """Canonical serialization of *config_set*.

    Guardrails are sorted by stable ``id`` so semantically-equivalent sets
    (different authoring order or YAML layout) hash identically, and so the
    hash is reproducible when the set is rebuilt from DB rows keyed by id.
    The human-readable ``name`` is deliberately excluded from the digest —
    it is display metadata that is never persisted on the applied rows, so
    including it would make the rebuilt (drift) hash depend on data the rows
    cannot carry.
    """
    guardrails = sorted(
        (
            item.model_dump(mode="json", exclude_none=True, by_alias=True, exclude={"name"})
            for item in config_set.guardrails
        ),
        key=lambda g: g["id"],
    )
    return {
        "version": config_set.version,
        "guardrails": guardrails,
        "max_guardrails_per_node": config_set.max_guardrails_per_node,
        "guardrail_timeout_seconds": config_set.guardrail_timeout_seconds,
    }


def hash_config_set(config_set: GuardrailConfigSet) -> str:
    """Return a stable SHA-256 hex digest for *config_set*.

    The digest is over a canonical JSON serialization (sorted keys, guardrails
    sorted by id) — not the raw YAML text — so equivalent YAML layouts hash
    identically and drift recomputation from DB rows is deterministic.
    """
    payload = json.dumps(_canonical_dict(config_set), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def hash_guardrail_item(item: GuardrailConfigItem) -> str:
    """Return a stable SHA-256 hex digest for a single guardrail item.

    ``name`` is excluded (display metadata, never persisted on applied rows).
    """
    payload = json.dumps(
        item.model_dump(mode="json", exclude_none=True, by_alias=True, exclude={"name"}),
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Diff (per-guardrail, keyed by stable id)
# ---------------------------------------------------------------------------


def diff_config_sets(current: GuardrailConfigSet, proposed: GuardrailConfigSet) -> list[ConfigChange]:
    """Compute the per-guardrail diff from *current* to *proposed*.

    Actions: ``add`` (only in proposed), ``remove`` (only in current),
    ``update`` (in both, hash differs). Stable ids make re-imports idempotent.
    """
    current_by_id = {item.id: item for item in current.guardrails}
    proposed_by_id = {item.id: item for item in proposed.guardrails}
    changes: list[ConfigChange] = []
    for gid, item in sorted(proposed_by_id.items()):
        if gid not in current_by_id:
            changes.append(ConfigChange(action="add", id=gid, name=item.name, new_hash=hash_guardrail_item(item)))
    for gid, item in sorted(current_by_id.items()):
        if gid not in proposed_by_id:
            changes.append(ConfigChange(action="remove", id=gid, name=item.name, old_hash=hash_guardrail_item(item)))
    for gid in sorted(current_by_id.keys() & proposed_by_id.keys()):
        old_item = current_by_id[gid]
        new_item = proposed_by_id[gid]
        old_hash = hash_guardrail_item(old_item)
        new_hash = hash_guardrail_item(new_item)
        if old_hash != new_hash:
            changes.append(
                ConfigChange(
                    action="update",
                    id=gid,
                    name=new_item.name,
                    old_hash=old_hash,
                    new_hash=new_hash,
                    detail=_describe_item_delta(old_item, new_item),
                )
            )
    return changes


def _describe_item_delta(old_item: GuardrailConfigItem, new_item: GuardrailConfigItem) -> str:
    """Short human-readable summary of a per-guardrail update (no payloads)."""
    parts: list[str] = []
    if old_item.action != new_item.action:
        parts.append(f"action {old_item.action.value}->{new_item.action.value}")
    old_type = old_item.detection.type
    new_type = new_item.detection.type
    if old_type != new_type:
        parts.append(f"detection {old_type}->{new_type}")
    elif old_type == "regex":
        if old_item.detection.pattern != new_item.detection.pattern:
            parts.append("regex pattern changed")
        if old_item.detection.field != new_item.detection.field:
            parts.append("regex field changed")
    elif old_item.detection.schema_data != new_item.detection.schema_data:
        parts.append("schema changed")
    if old_item.redaction != new_item.redaction:
        parts.append("redaction changed")
    if old_item.required_capabilities != new_item.required_capabilities:
        parts.append("required_capabilities changed")
    return ", ".join(parts) or "configuration changed"


# ---------------------------------------------------------------------------
# DB row <-> config DTO (round-trip bijection via the engine's resolver)
# ---------------------------------------------------------------------------


def to_eval_config(
    item: GuardrailConfigItem,
    *,
    max_guardrails_per_node: int = DEFAULT_MAX_GUARDRAILS_PER_NODE,
    guardrail_timeout_seconds: float = DEFAULT_GUARDRAIL_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """Build the engine's ``config_json`` from a config-set guardrail.

    The engine reads detection from either a flattened ``type``/``pattern``/
    ``field``/``schema`` layout or a ``detection`` envelope. Config-as-code
    writes the FLATTENED form; ``config_item_from_engine_definition`` reads it
    back (via the engine's own resolver) giving a deterministic round-trip for
    drift hashing. ``failure_behaviour`` is deliberately never written here —
    guardrail rows always use ``'warn'`` (block semantics are guardrail-owned
    via ``action``).

    The ORG-LEVEL knobs (``max_guardrails_per_node`` / ``guardrail_timeout_seconds``,
    item 7) are mirrored onto EVERY row's ``config_json`` so the interception
    seam — which loads rows by pipeline, not by config-set — reads the same
    effective cap/timeout the config-set declares, and so a set rebuilt from
    rows hashes identically (drift stays ``clean``).
    """
    config: dict[str, Any] = {
        "interception_point": "input",
        "action": item.action.value,
        "redaction": [{"path": rule.path, "mode": rule.mode.value} for rule in item.redaction],
        "required_capabilities": list(item.required_capabilities),
        "max_guardrails_per_node": max_guardrails_per_node,
        "guardrail_timeout_seconds": guardrail_timeout_seconds,
    }
    if item.detection.type == "json_schema":
        config["type"] = "json_schema"
        config["schema"] = item.detection.schema_data
    else:
        config["type"] = "regex"
        config["pattern"] = item.detection.pattern
        config["field"] = item.detection.field
    return config


def _resolve_db_action(config: dict[str, Any]) -> GuardrailAction:
    raw = config.get("action")
    if raw is None:
        # A row with no action key is the engine's default — preserve it rather
        # than treating "absent" as an unknown value.
        return GuardrailAction.OBSERVE
    try:
        return GuardrailAction(raw)
    except ValueError:
        # Fail closed like the detection-type path: an unknown/mutated action
        # must never be silently rebuilt as OBSERVE, which would mask a drifted
        # action in the rebuild hash and hide the drift from the operator.
        raise GuardrailConfigError(
            f"Guardrail has unknown action {raw!r}; valid actions are {[a.value for a in GuardrailAction]}"
        ) from None


def config_item_from_engine_definition(engine_def: EvalDefinition) -> GuardrailConfigItem:
    """Rebuild a config-set guardrail from an engine ``EvalDefinition`` DTO.

    Detection resolution is delegated to the engine's :func:`_resolve_detection`
    (envelope authoritative, merged effective config) rather than re-implemented
    here — a row authored with the documented ``detection`` envelope (PRD §8.17)
    rebuilds with its schema/pattern/field intact, and an unknown declared type
    surfaces loudly (fail-closed) instead of being silently downgraded. The
    stable ``id`` is the row ``name`` (the config-as-code key), so a set
    rebuilt from DB rows hashes identically to the set that produced them.
    """
    config = engine_def.config or {}
    detection_type, effective_config = _resolve_detection(engine_def)
    if detection_type not in GUARDRAIL_DETECTION_TYPES:
        # Fail closed, mirroring the engine's own validation error — an unknown
        # declared detection type must never be silently rebuilt as regex.
        raise GuardrailConfigError(
            f"Guardrail {engine_def.name!r} must use regex or json_schema detection (got config {config!r})"
        )
    if detection_type == str(EvalType.JSON_SCHEMA):
        detection = GuardrailDetection(type="json_schema", schema=effective_config.get("schema"))
    else:
        detection = GuardrailDetection(
            type="regex",
            pattern=effective_config.get("pattern"),
            field=effective_config.get("field"),
        )
    redaction = [
        RedactionRule(path=str(raw.get("path") or ""), mode=raw.get("mode", "transform"))
        for raw in config.get("redaction") or []
        if isinstance(raw, dict)
    ]
    try:
        return GuardrailConfigItem(
            id=engine_def.name,
            name=engine_def.name,
            action=_resolve_db_action(config),
            detection=detection,
            redaction=redaction,
            required_capabilities=[str(c) for c in config.get("required_capabilities") or []],
        )
    except pydantic.ValidationError as exc:
        # The shipped T1 engine allows org-level guardrail names the config id
        # pattern rejects (spaces, >100 chars), so a legacy row can already
        # exist. The read surface must fail closed with a clear message — never
        # a bare pydantic.ValidationError surfacing as a generic 422, and never
        # a silent skip that would corrupt the rebuild hash.
        raise GuardrailConfigError(
            f"Guardrail {engine_def.name!r} cannot be represented as config-as-code: {exc}"
        ) from exc


def build_config_set_from_definitions(definitions: list[EvalDefinition]) -> GuardrailConfigSet:
    """Build the org's applied config set from its guardrail engine definitions.

    Guardrail rows are bound per-pipeline; the same org-level guardrail id is
    replicated across pipelines. Rows are deduplicated by the stable ``name``
    (the config id) so the rebuilt set matches the applied set regardless of
    how many pipelines carry it. Guardrail rows are keyed in deterministic
    (sorted-by-id) order for hash stability.
    """
    by_id: dict[str, EvalDefinition] = {}
    for definition in definitions:
        if definition.eval_type != EvalType.GUARDRAIL:
            continue
        by_id.setdefault(definition.name, definition)
    items = [config_item_from_engine_definition(by_id[key]) for key in sorted(by_id)]
    # Recover the org-level knobs (item 7) from the applied rows so the rebuilt
    # set hashes identically to the applied set. ``to_eval_config`` mirrors
    # these onto every row; the effective value is resolved through the engine's
    # own helpers (``resolve_guardrail_cap`` / ``resolve_guardrail_timeout``) so
    # the rebuilt knobs can never drift from the engine's semantics (MAX
    # declared, 0 = feature off for the cap).
    defs = list(by_id.values())
    cap = resolve_guardrail_cap(defs)
    timeout = resolve_guardrail_timeout(defs)
    try:
        return GuardrailConfigSet(
            guardrails=items,
            max_guardrails_per_node=cap,
            guardrail_timeout_seconds=timeout,
        )
    except pydantic.ValidationError as exc:
        # Same fail-closed guarantee as the per-item conversion: a set that the
        # DTOs cannot represent must raise a GuardrailConfigError, never leak a
        # bare pydantic.ValidationError to the read surface.
        raise GuardrailConfigError(f"Guardrail definitions cannot be represented as config-as-code: {exc}") from exc


# ---------------------------------------------------------------------------
# Drift detection
# ---------------------------------------------------------------------------


def check_guardrail_drift(definitions: list[EvalDefinition], pin: GuardrailPin | None) -> bool:
    """Return True when the live DB guardrail definitions drift from *pin*.

    Recomputes the applied hash from ``definitions`` (via
    :func:`build_config_set_from_definitions`) and compares it to
    ``pin.applied_hash``. A ``None``/never-applied pin is compared against the
    empty set — so the first apply to a fresh org is never flagged, but a
    config that is pinned yet not present in the rows IS flagged.
    """
    current_hash = hash_config_set(build_config_set_from_definitions(definitions))
    expected = hash_config_set(GuardrailConfigSet()) if pin is None or not pin.applied_hash else pin.applied_hash
    return current_hash != expected


def utc_now_iso() -> str:
    """Current UTC time as an ISO-8601 string (pin timestamp)."""
    return datetime.now(UTC).isoformat()


__all__ = [
    "REDACTED_MASK",
    "ConfigChange",
    "GuardrailConfigError",
    "GuardrailConfigItem",
    "GuardrailConfigSet",
    "GuardrailDetection",
    "GuardrailPin",
    "GuardrailPinStatus",
    "RedactionRule",
    "build_config_set_from_definitions",
    "check_guardrail_drift",
    "config_item_from_engine_definition",
    "diff_config_sets",
    "dump_config_set",
    "hash_config_set",
    "hash_guardrail_item",
    "load_config_set",
    "mask_config_set",
    "to_eval_config",
    "utc_now_iso",
    "validate_config_set",
]
