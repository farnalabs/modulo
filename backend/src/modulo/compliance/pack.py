"""Versioned compliance control-mapping pack (FAR-2036).

The pack is shipped *in the product*, so it moves with the release it
describes. It is structured data (YAML under ``modulo/compliance/mappings/``),
never a Python literal, and it is versioned independently of the product.

Honesty is enforced structurally at load time (see :func:`validate_mapping_rows`):

* every row names a known artefact type, a coverage attribution and a
  provenance tag from the controlled vocabularies;
* ``satisfied`` rows can never be evidenced by customer-supplied material;
* ``customer-responsibility`` rows must be tagged ``customer-supplied``;
* every row states what remains the customer's responsibility;
* every framework states its ``source`` and ``scope_note``, and the pack states
  its ``title``, ``published_at`` and ``honesty_note`` - none may be omitted or
  YAML ``null``, because those fields carry the pack's limitations;
* every framework contains at least one row Modulo does not itself satisfy;
* the SOC 2 minimum clause set (CC6, CC7, CC8) is present.

The pack carries a version (``pack_version``) and a deterministic content hash
(``content_hash``). The evidence index records both, so a later release cannot
silently change what was asserted for a past audit period.
"""

from __future__ import annotations

import hashlib
import json
import re
from functools import lru_cache
from importlib import resources
from typing import Any, Final

import yaml
from pydantic import BaseModel, ConfigDict

from modulo.compliance.artifacts import (
    ARTIFACT_TYPES,
    COVERAGE_CUSTOMER_RESPONSIBILITY,
    COVERAGE_SATISFIED,
    COVERAGE_SUPPORTED,
    COVERAGE_VALUES,
    INDEXED_ARTIFACT_TYPES,
    NON_SELF_COVERAGE_VALUES,
    PROVENANCE_CUSTOMER_SUPPLIED,
    PROVENANCE_VALUES,
)

PACK_VERSION_PATTERN: Final[re.Pattern[str]] = re.compile(r"^\d+\.\d+\.\d+$")

#: Minimum SOC 2 clause coverage the pack must keep. Enforced so a later edit
#: cannot quietly drop a required clause (the ticket names CC6, CC7, CC8 plus
#: the evidence layer as the floor).
SOC2_REQUIRED_CLAUSES: Final[frozenset[str]] = frozenset(
    {
        "CC6.1",
        "CC6.2",
        "CC6.3",
        "CC6.6",
        "CC6.7",
        "CC7.1",
        "CC7.2",
        "CC7.3",
        "CC7.4",
        "CC7.5",
        "CC8.1",
        "EV-1",
        "EV-2",
    }
)

FRAMEWORK_SOC2: Final[str] = "soc2"
FRAMEWORK_ISO_42001: Final[str] = "iso_42001"
FRAMEWORK_EU_AI_ACT: Final[str] = "eu_ai_act"

FRAMEWORK_IDS: Final[tuple[str, ...]] = (
    FRAMEWORK_SOC2,
    FRAMEWORK_ISO_42001,
    FRAMEWORK_EU_AI_ACT,
)

_MAPPINGS_PACKAGE: Final[str] = "modulo.compliance.mappings"
_PACK_METADATA_FILE: Final[str] = "pack.yaml"

#: Shortest acceptable responsibility statement. A one-word placeholder ("N/A")
#: must not satisfy the honesty requirement.
_MIN_RESPONSIBILITY_LENGTH: Final[int] = 20


class MappingPackError(ValueError):
    """The shipped mapping pack is structurally invalid or dishonest."""


class MappingRow(BaseModel):
    """One framework clause mapped to a Modulo control and its evidence."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    framework: str
    clause: str
    clause_intent: str
    modulo_control: str
    artifact_type: str
    customer_location: str
    coverage: str
    provenance: str
    responsibility: str
    notes: str | None = None


class Framework(BaseModel):
    """One framework's metadata plus its mapping rows."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    name: str
    source: str
    scope_note: str
    rows: tuple[MappingRow, ...]


class MappingPack(BaseModel):
    """The loaded, validated mapping pack."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    pack_version: str
    published_at: str
    title: str
    honesty_note: str
    frameworks: dict[str, Framework]

    @property
    def rows(self) -> tuple[MappingRow, ...]:
        """Every row across every framework, in pack order."""
        return tuple(row for framework in self.frameworks.values() for row in framework.rows)

    def framework_ids(self) -> tuple[str, ...]:
        """Framework ids in pack order."""
        return tuple(self.frameworks)

    def rows_for_artifact(self, artifact_type: str) -> tuple[MappingRow, ...]:
        """Rows whose evidence is *artifact_type*."""
        return tuple(row for row in self.rows if row.artifact_type == artifact_type)

    def mapping_row_ids_for_artifact(self, artifact_type: str) -> tuple[str, ...]:
        """Stable ids of the mapping rows evidenced by *artifact_type*."""
        return tuple(row.id for row in self.rows_for_artifact(artifact_type))

    def coverage_counts(self) -> dict[str, dict[str, int]]:
        """Per-framework count of each coverage attribution (all values present)."""
        counts: dict[str, dict[str, int]] = {}
        for framework_id, framework in self.frameworks.items():
            per_framework = dict.fromkeys(COVERAGE_VALUES, 0)
            for row in framework.rows:
                per_framework[row.coverage] += 1
            counts[framework_id] = per_framework
        return counts

    def content_hash(self) -> str:
        """Deterministic SHA-256 over the pack's asserted content.

        Covers the pack version and every row's fields, so an edit to any
        assertion changes the hash even if ``pack_version`` was not bumped.
        """
        payload: dict[str, Any] = {
            "pack_version": self.pack_version,
            "frameworks": [
                {
                    "id": framework.id,
                    "name": framework.name,
                    "source": framework.source,
                    "scope_note": framework.scope_note,
                    "rows": [row.model_dump(mode="json", exclude={"notes"}) for row in framework.rows],
                }
                for framework in self.frameworks.values()
            ],
        }
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _require_non_empty(value: str, field: str, row_id: str) -> None:
    if not value.strip():
        raise MappingPackError(f"mapping row {row_id!r}: {field} must be a non-empty string")


#: Row fields that must carry a non-empty statement. ``responsibility`` is
#: checked again for a minimum length in :func:`_require_responsibility_statement`
#: - non-empty is not the same as honest.
_REQUIRED_ROW_TEXT_FIELDS: Final[tuple[str, ...]] = (
    "clause",
    "clause_intent",
    "modulo_control",
    "customer_location",
    "responsibility",
)


def _require_unique_row_id(row: MappingRow, seen_ids: set[str]) -> None:
    """Reject an empty or repeated row id, registering the id when it is new."""
    if not row.id.strip():
        raise MappingPackError("mapping row with an empty id")
    if row.id in seen_ids:
        raise MappingPackError(f"duplicate mapping row id {row.id!r}")
    seen_ids.add(row.id)


def _require_row_text_fields(row: MappingRow) -> None:
    """Every statement field must be non-empty."""
    for field in _REQUIRED_ROW_TEXT_FIELDS:
        _require_non_empty(getattr(row, field), field, row.id)


def _require_row_vocabulary(row: MappingRow) -> None:
    """Framework, artefact type, coverage and provenance must come from the vocabularies."""
    if row.framework not in FRAMEWORK_IDS:
        raise MappingPackError(f"mapping row {row.id!r}: unknown framework {row.framework!r}")
    if row.artifact_type not in ARTIFACT_TYPES:
        raise MappingPackError(f"mapping row {row.id!r}: unknown artefact type {row.artifact_type!r}")
    if row.coverage not in COVERAGE_VALUES:
        raise MappingPackError(f"mapping row {row.id!r}: unknown coverage attribution {row.coverage!r}")
    if row.provenance not in PROVENANCE_VALUES:
        raise MappingPackError(f"mapping row {row.id!r}: unknown provenance tag {row.provenance!r}")


def _require_responsibility_statement(row: MappingRow) -> None:
    """A one-word placeholder must not satisfy the honesty requirement."""
    if len(row.responsibility.strip()) < _MIN_RESPONSIBILITY_LENGTH:
        raise MappingPackError(
            f"mapping row {row.id!r}: responsibility must state, in a sentence, what "
            "remains the customer's responsibility"
        )


def _require_row_honesty(row: MappingRow) -> None:
    """Reject the attributions that would let the pack overclaim."""
    # A row Modulo claims to SATISFY can never rest on material only the
    # customer holds - that would present a customer control as ours.
    if row.coverage == COVERAGE_SATISFIED and row.provenance == PROVENANCE_CUSTOMER_SUPPLIED:
        raise MappingPackError(
            f"mapping row {row.id!r}: a 'satisfied' row cannot be evidenced by customer-supplied material"
        )
    # A customer-responsibility row's evidence is, by definition, the
    # customer's own record - never an observed Modulo artefact.
    if row.coverage == COVERAGE_CUSTOMER_RESPONSIBILITY and row.provenance != PROVENANCE_CUSTOMER_SUPPLIED:
        raise MappingPackError(
            f"mapping row {row.id!r}: a 'customer-responsibility' row must be tagged 'customer-supplied'"
        )
    # Material the instance does not produce cannot evidence something
    # Modulo claims to satisfy or support.
    if not ARTIFACT_TYPES[row.artifact_type].indexed and row.coverage in {COVERAGE_SATISFIED, COVERAGE_SUPPORTED}:
        raise MappingPackError(
            f"mapping row {row.id!r}: artefact type {row.artifact_type!r} is not produced "
            "by the instance, so it cannot evidence a satisfied/supported claim"
        )


def validate_mapping_rows(rows: tuple[MappingRow, ...]) -> None:
    """Validate the pack's cross-row invariants, raising :class:`MappingPackError`.

    These are the honesty rules - not formatting. Each one exists because
    violating it would let the pack overclaim. The rules live in the helpers
    above so each one stays independently readable and testable; the order of
    the checks is the order a reader needs (identity, completeness, vocabulary,
    then the honesty rules that depend on all three).
    """
    seen_ids: set[str] = set()
    for row in rows:
        _require_unique_row_id(row, seen_ids)
        _require_row_text_fields(row)
        _require_row_vocabulary(row)
        _require_responsibility_statement(row)
        _require_row_honesty(row)


def validate_frameworks(frameworks: dict[str, Framework]) -> None:
    expected = set(FRAMEWORK_IDS)
    present = set(frameworks)
    if present != expected:
        missing = sorted(expected - present)
        unexpected = sorted(present - expected)
        raise MappingPackError(
            f"pack frameworks must be exactly {sorted(expected)}; missing={missing} unexpected={unexpected}"
        )

    for framework_id, framework in frameworks.items():
        if framework.id != framework_id:
            raise MappingPackError(f"framework key {framework_id!r} does not match its declared id {framework.id!r}")
        if not framework.rows:
            raise MappingPackError(f"framework {framework_id!r} has no mapping rows")
        for row in framework.rows:
            if row.framework != framework_id:
                raise MappingPackError(
                    f"mapping row {row.id!r} is filed under {framework_id!r} but declares framework {row.framework!r}"
                )
        # "A pack that claims everything is worse than no pack": every
        # framework must be honest about at least one thing it does not satisfy.
        if not any(row.coverage in NON_SELF_COVERAGE_VALUES for row in framework.rows):
            raise MappingPackError(
                f"framework {framework_id!r} claims every clause as met; the pack must "
                "name at least one gap or customer responsibility"
            )

    soc2_clauses = {row.clause for row in frameworks[FRAMEWORK_SOC2].rows}
    missing_soc2 = sorted(SOC2_REQUIRED_CLAUSES - soc2_clauses)
    if missing_soc2:
        raise MappingPackError("SOC 2 pack is missing required clauses: " + ", ".join(missing_soc2))

    # Every indexed artefact type must be mapped to at least one clause. An
    # index entry with no mapping rows would be evidence nobody asked for; a
    # mapped type with no entry would be a silent omission - both are caught
    # here rather than at read time.
    referenced = {row.artifact_type for framework in frameworks.values() for row in framework.rows}
    unmapped = [artifact_type for artifact_type in INDEXED_ARTIFACT_TYPES if artifact_type not in referenced]
    if unmapped:
        raise MappingPackError("pack does not map these indexed artefact types to any clause: " + ", ".join(unmapped))


def _read_yaml(filename: str) -> Any:
    """Read and parse one packaged YAML file with ``safe_load``."""
    text = resources.files(_MAPPINGS_PACKAGE).joinpath(filename).read_text(encoding="utf-8")
    try:
        return yaml.safe_load(text)
    except yaml.YAMLError as exc:  # pragma: no cover - a shipped-file corruption
        raise MappingPackError(f"{filename}: invalid YAML: {exc}") from exc


def _as_mapping(value: Any, context: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise MappingPackError(f"{context}: expected a mapping, got {type(value).__name__}")
    return value


def _required_text(document: dict[str, Any], key: str, context: str) -> str:
    """Read a required non-empty string field, rejecting ``null`` and omission.

    A YAML ``null`` used to be coerced by ``str()`` into the literal ``"None"``,
    and an omitted key into ``""`` - both of which silently drop a documented
    limitation from an artefact an auditor relies on. Neither is acceptable
    here, so both fail the load.
    """
    value = document.get(key)
    if not isinstance(value, str) or not value.strip():
        raise MappingPackError(
            f"{context}: {key!r} must be a non-empty string; a YAML null or an omitted "
            "key drops a limitation the pack must state"
        )
    return value.strip()


def _build_pack() -> MappingPack:
    metadata = _as_mapping(_read_yaml(_PACK_METADATA_FILE), f"{_MAPPINGS_PACKAGE}/{_PACK_METADATA_FILE}")

    pack_version = metadata.get("pack_version")
    if not isinstance(pack_version, str) or not PACK_VERSION_PATTERN.match(pack_version):
        raise MappingPackError(f"pack_version must be a MAJOR.MINOR.PATCH string, got {pack_version!r}")

    framework_entries = metadata.get("frameworks")
    if not isinstance(framework_entries, list) or not framework_entries:
        raise MappingPackError("pack metadata must list at least one framework")

    frameworks: dict[str, Framework] = {}
    all_rows: list[MappingRow] = []
    for entry_value in framework_entries:
        entry = _as_mapping(entry_value, "pack framework entry")
        framework_id = entry.get("id")
        filename = entry.get("file")
        if not isinstance(framework_id, str) or not isinstance(filename, str):
            raise MappingPackError("each pack framework entry needs string 'id' and 'file'")
        if framework_id in frameworks:
            raise MappingPackError(f"duplicate framework {framework_id!r} in pack metadata")

        document = _as_mapping(_read_yaml(filename), f"{_MAPPINGS_PACKAGE}/{filename}")
        if document.get("framework") != framework_id:
            raise MappingPackError(f"{filename}: 'framework' must equal {framework_id!r}")

        raw_rows = document.get("rows")
        if not isinstance(raw_rows, list) or not raw_rows:
            raise MappingPackError(f"{filename}: 'rows' must be a non-empty list")

        rows = tuple(
            MappingRow.model_validate({**_as_mapping(raw_row, filename), "framework": framework_id})
            for raw_row in raw_rows
        )
        all_rows.extend(rows)

        framework_context = f"{_MAPPINGS_PACKAGE}/{filename}"
        frameworks[framework_id] = Framework(
            id=framework_id,
            name=_required_text(document, "name", framework_context),
            # source and scope_note carry the framework's stated boundary and
            # provenance; an omitted or null one must not degrade into
            # "None"/"".
            source=_required_text(document, "source", framework_context),
            scope_note=_required_text(document, "scope_note", framework_context),
            rows=rows,
        )

    validate_mapping_rows(tuple(all_rows))
    validate_frameworks(frameworks)

    metadata_context = f"{_MAPPINGS_PACKAGE}/{_PACK_METADATA_FILE}"
    return MappingPack(
        pack_version=pack_version,
        published_at=_required_text(metadata, "published_at", metadata_context),
        title=_required_text(metadata, "title", metadata_context),
        # The honesty note is the pack's own statement of what it does NOT
        # claim. An empty one is the single most dangerous omission here.
        honesty_note=_required_text(metadata, "honesty_note", metadata_context),
        frameworks=frameworks,
    )


@lru_cache(maxsize=1)
def load_mapping_pack() -> MappingPack:
    """Load and validate the shipped mapping pack (cached for the process).

    A failure here is fatal by design: an invalid or dishonest pack must break
    the surface that serves it rather than degrade to an empty one.
    """
    return _build_pack()
