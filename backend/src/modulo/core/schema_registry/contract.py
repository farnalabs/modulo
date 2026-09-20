"""Advisory schema contract writer for sandboxed agent dispatch (FAR-901).

Writes a node's input and output schemas into the sandbox filesystem so the
dispatched runtime and the operator's own tooling can use them structurally.
**Modulo validates independently — these files are ADVISORY inputs, never the
gate.**  No in-sandbox code path may perform schema validation.

The writer is PURE and E2B-DECOUPLED: all filesystem operations go through
``pathlib.Path`` and ``os.replace``, making the function fully unit-testable
in a temp dir with no E2B import.

Sanitisation
------------
Free-text keywords (``description``, ``title``, ``examples``) are stripped
from all written schema files.  ``default`` is a **functional** keyword and
is **never** stripped.  ``const``/``enum`` string values are capped at 256
chars; ``enum`` arrays are capped at 50 entries.  Exceeding a cap triggers
reject/fallback — never silent truncation.

Atomicity / failure
-------------------
Each file is written to a ``.tmp`` sibling, ``fsync``'d, then atomically
replaced via ``os.replace``.  On failure, orphan ``.tmp`` files are cleaned
up.  If the canonical file succeeds but the active file fails, the canonical
file is **rolled back** — a half-updated state is never left on disk.

A write failure **never fails the node**, for any profile.  On error the
function logs a warning and emits a sentinel ``{"_schema_available": false}``
file so consumers can detect absence structurally.

Version contract
----------------
Every written file carries ``_schema_contract_version`` — a top-level integer
key that the host-side validator (FAR-899 path in ``node_runner``) reads and
compares.  A version mismatch triggers a warn (lenient) or re-render (strict).
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from modulo.core.schema_registry.rendering import (
    SchemaProfile,
    render_for_profile,
)

_log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Contract version — single source of truth
# ---------------------------------------------------------------------------

# Bump this when the file layout or content semantics change.  The host-side
# validator in ``node_runner._validate_against_schema`` reads and compares it.
SCHEMA_CONTRACT_VERSION = 1

# Sentinel written when schema files cannot be written.
_SENTINEL_UNAVAILABLE: dict[str, Any] = {"_schema_available": False}

# Maximum string length for const/enum values (chars).
_MAX_CONST_STRING_LENGTH = 256
# Maximum number of entries in an enum array.
_MAX_ENUM_CARDINALITY = 50

# Free-text keywords to strip from schemas (advisory, not structural).
_STRIP_KEYWORDS = frozenset({"description", "title", "examples"})

# Schema directory name inside the output dir.
_SCHEMA_DIR = "schemas"

# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ContractWriteResult:
    """Outcome of :func:`write_schema_contract`."""

    schema_files_written: bool
    """True when at least one schema file was written to disk."""

    warnings: list[str] = field(default_factory=list)
    """Human-readable warnings emitted during the write (sanitisation caps, etc.)."""


# ---------------------------------------------------------------------------
# Sanitisation helpers
# ---------------------------------------------------------------------------


def _sanitise_schema(schema: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Return a deep copy of *schema* with free-text keywords stripped.

    The following keywords are removed at every level of the schema:
    ``description``, ``title``, ``examples``.

    ``default`` is a functional keyword and is **never** stripped.

    ``const`` string values exceeding 256 chars and ``enum`` arrays exceeding
    50 entries trigger a fallback (the original schema is returned unchanged)
    — never silent truncation.

    Returns ``(sanitised_schema, warnings)``.
    """
    warnings: list[str] = []

    # Deep copy to avoid mutating the caller's schema.
    try:
        sanitised = json.loads(json.dumps(schema, default=str))
    except (TypeError, ValueError):
        # Non-serialisable schema — return as-is with a warning.
        warnings.append("schema_not_serialisable")
        return schema, warnings

    # Check const/enum bounds BEFORE stripping — a violation means we
    # reject the sanitised copy and fall back to the original.
    reject, reject_reasons = _check_bounds(sanitised)
    if reject:
        warnings.extend(reject_reasons)
        return schema, warnings

    # Strip free-text keywords.
    _strip_free_text(sanitised)

    return sanitised, warnings


def _check_bounds(schema: dict[str, Any]) -> tuple[bool, list[str]]:
    """Return ``(should_reject, reasons)`` when const/enum bounds are exceeded.

    Checks recursively.  A single violation is sufficient to reject.
    """
    reasons: list[str] = []

    def _walk(node: Any) -> None:
        if isinstance(node, dict):
            # const bounds
            const_val = node.get("const")
            if isinstance(const_val, str) and len(const_val) > _MAX_CONST_STRING_LENGTH:
                reasons.append(f"const_string_exceeds_{_MAX_CONST_STRING_LENGTH}_chars")
            # enum bounds
            enum_val = node.get("enum")
            if isinstance(enum_val, list):
                if len(enum_val) > _MAX_ENUM_CARDINALITY:
                    reasons.append(f"enum_cardinality_exceeds_{_MAX_ENUM_CARDINALITY}")
                else:
                    for entry in enum_val:
                        if isinstance(entry, str) and len(entry) > _MAX_CONST_STRING_LENGTH:
                            reasons.append(f"enum_entry_exceeds_{_MAX_CONST_STRING_LENGTH}_chars")
                            break
            # Recurse into children.
            for value in node.values():
                if isinstance(value, (dict, list)):
                    _walk(value)
        elif isinstance(node, list):
            for item in node:
                if isinstance(item, (dict, list)):
                    _walk(item)

    _walk(schema)
    return bool(reasons), reasons


def _strip_free_text(node: Any) -> None:
    """Remove free-text keywords in-place.  Never strips ``default``."""
    if isinstance(node, dict):
        keys_to_remove = [k for k in node if k in _STRIP_KEYWORDS]
        for key in keys_to_remove:
            del node[key]
        for value in node.values():
            if isinstance(value, (dict, list)):
                _strip_free_text(value)
    elif isinstance(node, list):
        for item in node:
            if isinstance(item, (dict, list)):
                _strip_free_text(item)


# ---------------------------------------------------------------------------
# Atomic file writing
# ---------------------------------------------------------------------------


def _atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    """Write *data* as JSON to *path* atomically.

    Writes to a temporary sibling (``.tmp``), ``fsync``'s the file, then
    uses ``os.replace`` for an atomic rename.  The ``.tmp`` file is cleaned
    up on failure.

    Raises ``OSError`` / ``ValueError`` on write failure — the caller must
    handle rollback.
    """
    tmp_fd: int | None = None
    tmp_path: Path | None = None
    try:
        tmp_fd, tmp_path_str = tempfile.mkstemp(
            dir=str(path.parent),
            suffix=".tmp",
            prefix=path.stem + ".",
        )
        tmp_path = Path(tmp_path_str)
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
            tmp_fd = None  # fdopen owns it now
            json.dump(data, f, indent=2, ensure_ascii=False, default=str)
            f.flush()
            os.fsync(f.fileno())
        tmp_path.replace(path)
    except Exception:
        # Clean up orphan .tmp on failure.
        if tmp_fd is not None:
            with contextlib.suppress(OSError):
                os.close(tmp_fd)
        if tmp_path is not None:
            tmp_path.unlink(missing_ok=True)
        raise


def _cleanup_orphan_tmps(schema_dir: Path) -> None:
    """Remove orphan ``.tmp`` files left behind by failed writes."""
    if not schema_dir.is_dir():
        return
    for tmp_file in schema_dir.glob("*.tmp"):
        try:
            tmp_file.unlink(missing_ok=True)
        except OSError:
            _log.debug("contract.orphan_tmp_cleanup_failed", extra={"path": str(tmp_file)})


# ---------------------------------------------------------------------------
# Core writer
# ---------------------------------------------------------------------------


def write_schema_contract(
    output_dir: Path,
    *,
    node_id: str,
    input_schema: dict[str, Any] | None,
    output_schema: dict[str, Any] | None,
    profile: SchemaProfile,
    provider_id: str | None,
) -> ContractWriteResult:
    """Write advisory schema contract files for a single node.

    Writes four JSON files under ``<output_dir>/schemas/<node_id>/``:

    * ``input.canonical.json``  — the source-of-truth input schema
    * ``input.active.json``     — the rendered (or verbatim) input schema
    * ``output.canonical.json`` — the source-of-truth output schema
    * ``output.active.json``    — the rendered (or verbatim) output schema

    ``canonical`` files are always the raw, unsanitised schema.
    ``active`` files are sanitised and equal to ``canonical`` when the
    profile is ``verbatim``; otherwise they are the rendered form.

    This function is **advisory only** — it never blocks or fails a node.
    On any error it logs a warning and emits a sentinel file.

    Args:
        output_dir: Base directory (typically the sandbox filesystem root).
        node_id: The executing node's identifier.
        input_schema: The node's input JSON Schema, or ``None``.
        output_schema: The node's output JSON Schema, or ``None``.
        profile: The effective schema profile (``verbatim``, ``provider-strict``,
            or ``runtime-sdk``).
        provider_id: The provider identifier for ``provider-strict`` rendering,
            or ``None``.

    Returns:
        A :class:`ContractWriteResult` with ``schema_files_written`` and any
        warnings.
    """
    if not node_id:
        return ContractWriteResult(schema_files_written=False, warnings=["empty_node_id"])

    schema_dir = output_dir / _SCHEMA_DIR / node_id

    all_warnings: list[str] = []
    files_written = False

    # Resolve the pairs to write: (direction, schema_or_none).
    pairs: list[tuple[str, dict[str, Any] | None]] = [
        ("input", input_schema),
        ("output", output_schema),
    ]

    # --- sanitise both schemas up front ---
    sanitised_schemas: dict[str, tuple[dict[str, Any], list[str]]] = {}
    for direction, raw_schema in pairs:
        if raw_schema is None:
            sanitised_schemas[direction] = ({}, [])
            continue
        sanitised, warns = _sanitise_schema(raw_schema)
        sanitised_schemas[direction] = (sanitised, warns)
        all_warnings.extend(f"{direction}:{w}" for w in warns)

    # --- render for profile ---
    rendered_schemas: dict[str, dict[str, Any]] = {}
    for direction, raw_schema in pairs:
        if raw_schema is None:
            rendered_schemas[direction] = {}
            continue
        if profile == "verbatim":
            # Verbatim: active == sanitised canonical (no render_for_profile
            # needed — avoid the extra deep-copy in render_for_profile).
            rendered_schemas[direction] = sanitised_schemas[direction][0]
        else:
            try:
                result = render_for_profile(raw_schema, profile, provider_id)
                if result.skipped:
                    # Rendering was skipped (abstract schema, translation error,
                    # etc.) — fall back to sanitised canonical.
                    rendered_schemas[direction] = sanitised_schemas[direction][0]
                else:
                    # Sanitise the rendered output too — the rendered form may
                    # have different free-text content.
                    rendered_sanitised, render_warns = _sanitise_schema(result.schema)
                    rendered_schemas[direction] = rendered_sanitised
                    all_warnings.extend(f"{direction}:render:{w}" for w in render_warns)
            except Exception:
                _log.exception(
                    "contract.render_for_profile_failed",
                    extra={"node_id": node_id, "direction": direction, "profile": profile},
                )
                # Fall back to sanitised canonical.
                rendered_schemas[direction] = sanitised_schemas[direction][0]
                all_warnings.append(f"{direction}:render_error_fallback")

    # --- stamp contract version (only on non-empty schemas) ---
    for direction in ("input", "output"):
        if sanitised_schemas[direction][0]:
            sanitised_schemas[direction][0]["_schema_contract_version"] = SCHEMA_CONTRACT_VERSION
        if rendered_schemas[direction]:
            rendered_schemas[direction]["_schema_contract_version"] = SCHEMA_CONTRACT_VERSION

    # --- write files ---
    # Order: canonical first, then active.  If canonical succeeds but active
    # fails, rollback canonical.
    try:
        schema_dir.mkdir(parents=True, exist_ok=True)

        for direction in ("input", "output"):
            canonical_data, _sanitise_warns = sanitised_schemas[direction]
            active_data = rendered_schemas[direction]

            canonical_path = schema_dir / f"{direction}.canonical.json"
            active_path = schema_dir / f"{direction}.active.json"

            # --- Write canonical ---
            try:
                if canonical_data:
                    _atomic_write_json(canonical_path, canonical_data)
                else:
                    # Empty schema — write sentinel with contract version.
                    _atomic_write_json(
                        canonical_path,
                        {**_SENTINEL_UNAVAILABLE, "_schema_contract_version": SCHEMA_CONTRACT_VERSION},
                    )
            except Exception:
                _log.exception(
                    "contract.canonical_write_failed",
                    extra={"node_id": node_id, "direction": direction},
                )
                all_warnings.append(f"{direction}:canonical_write_failed")
                continue

            # --- Write active ---
            try:
                if active_data:
                    _atomic_write_json(active_path, active_data)
                else:
                    _atomic_write_json(
                        active_path,
                        {**_SENTINEL_UNAVAILABLE, "_schema_contract_version": SCHEMA_CONTRACT_VERSION},
                    )
            except Exception:
                _log.exception(
                    "contract.active_write_failed",
                    extra={"node_id": node_id, "direction": direction},
                )
                # Rollback canonical — never leave a half-updated state.
                try:
                    canonical_path.unlink(missing_ok=True)
                except OSError:
                    _log.exception(
                        "contract.canonical_rollback_failed",
                        extra={"node_id": node_id, "direction": direction},
                    )
                all_warnings.append(f"{direction}:active_write_failed_rolled_back")
                continue

            files_written = True

    except Exception:
        _log.exception(
            "contract.write_failed",
            extra={"node_id": node_id},
        )
        all_warnings.append("write_failed")
    finally:
        # Best-effort orphan .tmp cleanup.
        try:
            _cleanup_orphan_tmps(schema_dir)
        except Exception:
            _log.debug("contract.orphan_cleanup_failed", extra={"node_id": node_id})

    return ContractWriteResult(
        schema_files_written=files_written,
        warnings=all_warnings,
    )


# ---------------------------------------------------------------------------
# Schema file readers (for host-side validation)
# ---------------------------------------------------------------------------

SCHEMA_DIR_ENV_VAR = "MODULO_SCHEMA_DIR"
"""Environment variable pointing to the ``schemas/`` directory inside the sandbox."""


def read_schema_contract_version(schema_dir: Path, node_id: str) -> int | None:
    """Read the ``_schema_contract_version`` from a node's canonical output schema.

    Returns the version integer, or ``None`` when the file is absent,
    unreadable, or does not carry a version key.  Never raises.
    """
    canonical_path = schema_dir / _SCHEMA_DIR / node_id / "output.canonical.json"
    try:
        data = json.loads(canonical_path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            version = data.get("_schema_contract_version")
            if isinstance(version, int):
                return version
    except (OSError, json.JSONDecodeError, ValueError):
        pass
    return None


def list_schema_nodes(schema_dir: Path) -> list[str]:
    """List node IDs that have schema contract files under *schema_dir*.

    Returns an empty list when the directory does not exist.  Never raises.
    """
    schemas_root = schema_dir / _SCHEMA_DIR
    if not schemas_root.is_dir():
        return []
    return sorted(entry.name for entry in schemas_root.iterdir() if entry.is_dir())


def cleanup_schema_contract(schema_dir: Path, node_id: str | None = None) -> bool:
    """Remove schema contract files for a node (or all nodes).

    When *node_id* is provided, removes ``schemas/<node_id>/``.
    When *node_id* is ``None``, removes the entire ``schemas/`` directory.

    Called at run terminalization to tie schema lifecycle to run retention.
    Tolerant of absence — returns ``True`` when the directory was removed or
    did not exist, ``False`` on an unexpected error.  Never raises.
    """
    import shutil

    try:
        target = schema_dir / _SCHEMA_DIR / node_id if node_id is not None else schema_dir / _SCHEMA_DIR
        if target.is_dir():
            shutil.rmtree(target)
        return True
    except Exception:
        _log.debug(
            "contract.cleanup_failed",
            extra={"schema_dir": str(schema_dir), "node_id": node_id},
            exc_info=True,
        )
        return False
