"""Policy packs — compliance bundles as guardrail-as-code (FAR-216 PR A).

A policy pack is a compliance bundle (SOC 2, GDPR, ...) expressed as code:
a versioned ``PolicyPack`` whose ``controls`` are the individual compliance
controls (e.g. SOC2 ``CC6.1``, GDPR ``art.32``). Each control MAY carry a
concrete :class:`~modulo.core.guardrails.config.GuardrailConfigItem` — the
control→guardrail mapping. The framework guarantees that every MAPPED control
instantiates to a schema-valid guardrail config and that a pack with
unmapped or uninstantiable controls FAILS CI (a pack author cannot merge a
pack with gaps).

The layers provided here:

* **Pack schema** — :class:`PolicyPack` / :class:`PolicyControl` with a
  ``mapped`` flag (a supplied ``guardrail`` implies the mapping).
* **Instantiation** — :func:`instantiate_pack` builds a
  :class:`~modulo.core.guardrails.config.GuardrailConfigSet` from every
  MAPPED control's guardrail, validating each against the shipped
  ``validate_config_set``. An uninstantiable control (one whose guardrail
  fails validation, or whose guardrail id duplicates another control's)
  RAISES fail-closed — a partial config set that silently drops a security
  control is never produced.
* **Mapping validator + gap report** — :func:`validate_pack` produces a
  structured :class:`PackValidationReport` with ``mapped`` / ``unmapped`` /
  ``uninstantiable`` counts, the unmapped control ids, the uninstantiable
  control ids WITH their validation error, and a flat ``errors`` list.
* **CI gate** — :func:`assert_pack_ci_ready` raises
  :class:`~modulo.core.guardrails.GuardrailConfigError` when a pack has any
  unmapped or uninstantiable control. A CLI entry
  (``python -m modulo.core.guardrails.policy_pack <pack.yaml>``) wires the
  gate into CI: exit 0 when ready, 1 when gapped.
* **Warn-mode-first rollout** — :func:`pack_rollout_config` returns the
  instantiated set with every guardrail's action forced to the rollout mode.
  A pack is rolled out in ``observe``/``warn`` (shadow) mode first, then
  PROMOTED to ``block`` once the org is confident it does not false-positive
  on legitimate traffic.

The SOC2/GDPR pack CONTENT (the actual controls and their guardrail
mappings) is a separate delivery (FAR-216 PR B) — this module ships the
framework only.
"""

from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import pydantic
import yaml
from pydantic import BaseModel, Field, model_validator

from modulo.core.guardrails import GuardrailAction, GuardrailConfigError
from modulo.core.guardrails.config import (
    GuardrailConfigItem,
    GuardrailConfigSet,
    validate_config_set,
)

PackRolloutMode = Literal["observe", "warn", "block"]

# Rollout modes valid for warn-mode-first rollout + block promotion. ``redact``
# is not a rollout mode — a pack control that needs redaction declares it on
# its own guardrail; a rollout never silences a redact into another action.
_ROLLOUT_MODES: frozenset[GuardrailAction] = frozenset(
    {GuardrailAction.OBSERVE, GuardrailAction.WARN, GuardrailAction.BLOCK}
)


class PolicyControl(BaseModel):
    """One compliance control in a pack.

    ``id`` is the control reference (SOC2 ``CC6.1``, GDPR ``art.32``).
    ``guardrail`` is the concrete
    :class:`~modulo.core.guardrails.config.GuardrailConfigItem` this control
    instantiates to — the mapping. ``mapped`` records whether the control has
    a concrete mapping: a supplied ``guardrail`` implies ``mapped=True``; a
    control explicitly marked ``mapped=True`` without a guardrail is
    rejected (a lying mapping must never pass validation).
    """

    id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9._-]*$", min_length=1, max_length=100)
    title: str = Field(min_length=1, max_length=255)
    description: str = ""
    guardrail: GuardrailConfigItem | None = None
    mapped: bool = False

    @model_validator(mode="after")
    def _sync_mapped(self) -> PolicyControl:
        if self.mapped and self.guardrail is None:
            raise ValueError(f"Control {self.id!r} is marked mapped but has no guardrail mapping.")
        if not self.mapped and self.guardrail is not None:
            # A supplied guardrail IS a mapping — sync the flag rather than
            # silently ignoring the guardrail a pack author configured.
            self.mapped = True
        return self


class PolicyPack(BaseModel):
    """A versioned compliance bundle (e.g. SOC 2, GDPR)."""

    id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9._-]*$", min_length=1, max_length=100)
    name: str = Field(min_length=1, max_length=255)
    version: str = Field(default="1.0.0", min_length=1, max_length=50)
    controls: list[PolicyControl] = Field(default_factory=list)

    @model_validator(mode="after")
    def _unique_control_ids(self) -> PolicyPack:
        seen: set[str] = set()
        for control in self.controls:
            if control.id in seen:
                raise ValueError(f"Duplicate control id {control.id!r} in policy pack {self.id!r}.")
            seen.add(control.id)
        return self


@dataclass
class PackValidationReport:
    """Structured gap report for a policy pack.

    ``mapped`` is the number of controls with a concrete guardrail (regardless
    of whether that guardrail is instantiable); ``unmapped`` the number without
    one; ``uninstantiable`` the subset of mapped controls whose guardrail fails
    validation (or duplicates another control's guardrail id). The invariant
    ``mapped + unmapped == total`` holds by construction; ``uninstantiable``
    is a strict subset of ``mapped``.
    """

    pack_id: str
    mapped: int
    unmapped: int
    uninstantiable: int
    total: int
    unmapped_controls: list[str] = field(default_factory=list)
    uninstantiable_controls: list[tuple[str, str]] = field(default_factory=list)

    @property
    def errors(self) -> list[str]:
        """Flat, human-readable list of every gap (unmapped + uninstantiable)."""
        errors = [
            f"control {control_id!r} is unmapped (no concrete guardrail)" for control_id in self.unmapped_controls
        ]
        errors.extend(
            f"control {control_id!r} is uninstantiable: {error}" for control_id, error in self.uninstantiable_controls
        )
        return errors

    @property
    def ci_ready(self) -> bool:
        """True when the pack has no unmapped or uninstantiable controls."""
        return not self.unmapped_controls and not self.uninstantiable_controls

    def to_dict(self) -> dict[str, Any]:
        """JSON-friendly report (for tooling / CI output)."""
        return {
            "pack_id": self.pack_id,
            "mapped": self.mapped,
            "unmapped": self.unmapped,
            "uninstantiable": self.uninstantiable,
            "total": self.total,
            "unmapped_controls": list(self.unmapped_controls),
            "uninstantiable_controls": [
                {"control_id": control_id, "error": error} for control_id, error in self.uninstantiable_controls
            ],
            "errors": self.errors,
            "ci_ready": self.ci_ready,
        }


def _validate_mapped_guardrail(control: PolicyControl) -> str | None:
    """Return a validation error string for a mapped control's guardrail, or None.

    Validates the guardrail as a single-item :class:`GuardrailConfigSet`
    against the shipped ``validate_config_set`` (regex needs pattern+field,
    json_schema needs a schema dict, non-empty redaction paths). The
    ``control.mapped`` invariant (mapped ⇒ guardrail present) is enforced by
    the model validator, so the guardrail is never None here.
    """
    assert control.guardrail is not None  # nosec B101 - enforced by _sync_mapped (mapped ⇒ guardrail) + validate_pack guard
    try:
        validate_config_set(GuardrailConfigSet(guardrails=[control.guardrail]))
    except GuardrailConfigError as exc:
        return str(exc)
    return None


def validate_pack(pack: PolicyPack) -> PackValidationReport:
    """Validate every control mapping and produce the pack's gap report.

    Non-raising — this is the reporting surface. Each mapped control's
    guardrail is validated individually (so errors are attributed to the
    owning control) AND the pack's mapped guardrail ids are checked for
    duplicates (two controls mapping to the same guardrail id would collide
    on instantiation). Unmapped controls are allowed at schema level but are
    reported here.
    """
    unmapped = [control.id for control in pack.controls if not control.mapped]
    mapped_controls = [control for control in pack.controls if control.mapped]
    uninstantiable: list[tuple[str, str]] = []
    seen_guardrail_ids: set[str] = set()
    for control in mapped_controls:
        if control.guardrail is None:
            unmapped.append(control.id)
            continue
        if control.guardrail.id in seen_guardrail_ids:
            uninstantiable.append((control.id, f"duplicate guardrail id {control.guardrail.id!r} across pack"))
            continue
        seen_guardrail_ids.add(control.guardrail.id)
        error = _validate_mapped_guardrail(control)
        if error is not None:
            uninstantiable.append((control.id, error))
    return PackValidationReport(
        pack_id=pack.id,
        mapped=len(mapped_controls),
        unmapped=len(unmapped),
        uninstantiable=len(uninstantiable),
        total=len(pack.controls),
        unmapped_controls=unmapped,
        uninstantiable_controls=uninstantiable,
    )


def instantiate_pack(pack: PolicyPack) -> GuardrailConfigSet:
    """Build a :class:`GuardrailConfigSet` from every MAPPED control.

    Raises :class:`GuardrailConfigError` fail-closed when any mapped control
    is uninstantiable (its guardrail fails validation, or its guardrail id
    duplicates another control's) — a partial set that silently drops a
    security control must never be produced. Unmapped controls are EXCLUDED
    by design (allowed, but reported via :func:`validate_pack` /
    :func:`assert_pack_ci_ready`).
    """
    report = validate_pack(pack)
    if report.uninstantiable:
        first_id, first_error = report.uninstantiable_controls[0]
        raise GuardrailConfigError(
            f"Policy pack {pack.id!r} cannot be instantiated: control {first_id!r} is uninstantiable: {first_error}"
        )
    return GuardrailConfigSet(
        guardrails=[control.guardrail for control in pack.controls if control.mapped and control.guardrail is not None]
    )


def assert_pack_ci_ready(pack: PolicyPack) -> None:
    """CI gate — raise when *pack* has any unmapped or uninstantiable control.

    A pack with gaps must fail CI so a pack author cannot merge a pack whose
    controls are not all concretely mapped to schema-valid guardrails. Raises
    :class:`GuardrailConfigError` listing every gap; returns None when ready.
    """
    report = validate_pack(pack)
    if report.unmapped or report.uninstantiable:
        details = "\n".join(report.errors)
        raise GuardrailConfigError(
            f"Policy pack {pack.id!r} is not CI-ready: {report.unmapped} unmapped control(s) and "
            f"{report.uninstantiable} uninstantiable control(s):\n{details}"
        )


def pack_rollout_config(
    pack: PolicyPack,
    mode: PackRolloutMode | GuardrailAction | str = "warn",
) -> GuardrailConfigSet:
    """Return the instantiated pack with every guardrail action forced to *mode*.

    Warn-mode-first rollout: a pack is rolled out in ``observe``/``warn``
    (shadow — log-and-continue, never block) first, then PROMOTED to ``block``
    once the org is confident the pack does not false-positive on legitimate
    traffic. *mode* must be ``observe``, ``warn``, or ``block`` (``redact`` is
    not a rollout mode — a control that needs redaction declares it on its own
    guardrail). Unmapped controls remain excluded (they are not part of the
    rollout set).
    """
    if isinstance(mode, GuardrailAction):
        action = mode
    else:
        try:
            action = GuardrailAction(str(mode))
        except ValueError:
            raise GuardrailConfigError(
                f"Invalid rollout mode {mode!r}; valid modes are observe, warn, block."
            ) from None
    if action not in _ROLLOUT_MODES:
        raise GuardrailConfigError(
            f"Invalid rollout mode {mode!r}; observe, warn, and block only (redact is not a rollout mode)."
        )
    config_set = instantiate_pack(pack)
    # A rollout never silences a redact into another action (module invariant
    # _ROLLOUT_MODES): a guardrail that declares REDACT on its own keeps it.
    rolled_guardrails = [
        item if item.action == GuardrailAction.REDACT else item.model_copy(update={"action": action})
        for item in config_set.guardrails
    ]
    return config_set.model_copy(update={"guardrails": rolled_guardrails})


# ---------------------------------------------------------------------------
# YAML load / dump (safe only — semgrep-enforced repo rule)
# ---------------------------------------------------------------------------


def load_pack(yaml_text: str) -> PolicyPack:
    """Parse and validate *yaml_text* into a :class:`PolicyPack`.

    Raises :class:`GuardrailConfigError` for empty input, malformed YAML, a
    non-mapping document, or a Pydantic shape violation (bad control id,
    duplicate control id, a control marked mapped without a guardrail).
    """
    if not yaml_text or not yaml_text.strip():
        raise GuardrailConfigError("Policy pack is empty.")
    try:
        data = yaml.safe_load(yaml_text)
    except yaml.YAMLError as exc:
        raise GuardrailConfigError(f"Invalid YAML in policy pack: {exc}") from exc
    if not isinstance(data, dict):
        raise GuardrailConfigError("Policy pack must be a mapping with 'id', 'name', and 'controls'.")
    try:
        return PolicyPack.model_validate(data)
    except pydantic.ValidationError as exc:
        raise GuardrailConfigError(f"Invalid policy pack: {exc}") from exc


def dump_pack(pack: PolicyPack) -> str:
    """Serialize *pack* to stable YAML (block style, unicode preserved)."""
    return yaml.safe_dump(
        pack.model_dump(mode="json", by_alias=True),
        sort_keys=False,
        default_flow_style=False,
        allow_unicode=True,
    )


def _safe_input_path(path: str) -> str:
    """Resolve *path* and require it to stay within the working directory."""
    resolved = os.path.realpath(path)
    base = os.path.realpath(Path.cwd())
    if resolved != base and not resolved.startswith(base + os.sep):
        raise GuardrailConfigError(f"policy pack path {path!r} is outside the working directory")
    return resolved


def load_pack_file(path: str) -> PolicyPack:
    """Load and validate a policy pack YAML file from disk."""
    try:
        with open(path, encoding="utf-8") as fh:
            return load_pack(fh.read())
    except OSError as exc:
        raise GuardrailConfigError(f"Cannot read policy pack file {path!r}: {exc}") from exc


def main(argv: list[str] | None = None) -> int:
    """CLI entry point for the pack CI gate.

    ``python -m modulo.core.guardrails.policy_pack <pack.yaml>`` loads the
    pack, runs :func:`assert_pack_ci_ready`, prints the gap report when not
    ready, and exits 0 (ready) or 1 (gapped). Wire this into CI so a pack
    with unmapped or uninstantiable controls fails the build.
    """
    parser = argparse.ArgumentParser(prog="policy-pack", description="Validate a policy pack for CI readiness.")
    parser.add_argument("pack_file", help="Path to a policy pack YAML file")
    args = parser.parse_args(argv)
    try:
        pack = load_pack_file(_safe_input_path(args.pack_file))
        assert_pack_ci_ready(pack)
    except GuardrailConfigError as exc:
        print(f"policy-pack: {exc}", file=sys.stderr)  # noqa: T201 — CLI entry point
        return 1
    print(f"policy-pack: {pack.id} v{pack.version} is CI-ready ({len(pack.controls)} controls)")  # noqa: T201
    return 0


if __name__ == "__main__":
    sys.exit(main())


__all__ = [
    "PackRolloutMode",
    "PackValidationReport",
    "PolicyControl",
    "PolicyPack",
    "assert_pack_ci_ready",
    "dump_pack",
    "instantiate_pack",
    "load_pack",
    "load_pack_file",
    "main",
    "pack_rollout_config",
    "validate_pack",
]
