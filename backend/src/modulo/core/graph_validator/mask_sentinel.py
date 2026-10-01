"""Mask-sentinel detection for credential-bearing graph node fields (FAR-1374).

Composite template sub-graphs and top-level pipeline graphs store configured
environment values in plaintext at rest and mask them on every READ surface.
The read mask writes :data:`modulo.core.secret_patterns.SENSITIVE_VALUE_MASK`
into serialised copies only - the sentinel must NEVER be what a write path
persists, and it must NEVER reach a run snapshot (the node would execute with
``GITHUB_TOKEN=......`` and clobber a working host-injected credential with a
misleading upstream 401/403).

This module is the ONE detection rule shared by every gate that refuses the
sentinel:

- the four composite-template write entries (create / PATCH / editor PUT /
  save-as-composite) reject a submitted sentinel with 422
  (``api.middleware.sensitive_mask.resolve_and_reject_mask_sentinels``);
- composite expansion refuses a pre-fix or direct-DB-written sentinel
  (``core.composite_engine.expander``);
- graph-save validation surfaces the same condition with the same issue code
  (``core.graph_validator``);
- the detection-only housekeeping sweep reports degraded rows
  (``core.housekeeping``).

Detection is the SUBSTRING test (``SENSITIVE_VALUE_MASK in value``) - the same
primitive as ``_is_masked_echo`` - because the masker embeds the sentinel
inside wider strings (e.g. ``Bearer ......``); an equality-only detector would
miss those.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from typing import Any

from modulo.core.secret_patterns import SENSITIVE_VALUE_MASK

__all__ = [
    "GRAPH_NODE_SECRET_FIELDS",
    "MASK_SENTINEL_ISSUE_CODE",
    "MaskSentinelFinding",
    "find_mask_sentinel_values",
    "find_unresolved_mask_sentinels",
    "format_mask_sentinel_detail",
    "format_mask_sentinel_path",
]

#: Issue/error code for every gate that refuses a stored or submitted mask
#: sentinel on a graph node's credential-bearing fields. One spelling, shared
#: by the write-entry 422 detail, the expansion failure, and the graph
#: validator issue so operators can grep a single code across surfaces.
MASK_SENTINEL_ISSUE_CODE = "COMPOSITE_SUBGRAPH_MASKED_CREDENTIAL"

#: The credential-bearing graph node fields (kept in sync with the read-side
#: masker ``mask_pipeline_graph_node`` - the field set it masks is the field
#: set this detector must scan; both import THIS tuple so they cannot drift).
GRAPH_NODE_SECRET_FIELDS: tuple[str, ...] = (
    "env_vars",
    "context_files",
    "composite_parameter_values",
    "parameter_overrides",
)


@dataclass(frozen=True)
class MaskSentinelFinding:
    """One sentinel-bearing value: which node, which field, which key path."""

    node_id: str
    field: str
    path: tuple[str | int, ...]

    @property
    def key(self) -> str:
        return format_mask_sentinel_path(self.path)

    def describe(self) -> str:
        if self.path:
            return f"sub-node '{self.node_id}' field '{self.field}' key '{self.key}'"
        return f"sub-node '{self.node_id}' field '{self.field}' value"


def format_mask_sentinel_path(path: tuple[str | int, ...]) -> str:
    """Render a walk path as ``outer.inner[0]`` style dotted/index notation."""
    rendered = ""
    for segment in path:
        if isinstance(segment, int):
            rendered += f"[{segment}]"
        elif rendered:
            rendered = f"{rendered}.{segment}"
        else:
            rendered = str(segment)
    return rendered


def _walk_sentinels(value: Any, path: tuple[str | int, ...]) -> Iterator[tuple[str | int, ...]]:
    """Yield every key path under *value* whose string contains the sentinel.

    Dict keys, list indices, and (for a non-container field value) the empty
    path are all reported, so a sentinel survives detection no matter how the
    payload is shaped.
    """
    if isinstance(value, str):
        if SENSITIVE_VALUE_MASK in value:
            yield path
    elif isinstance(value, dict):
        for key, item in value.items():
            yield from _walk_sentinels(item, (*path, str(key)))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from _walk_sentinels(item, (*path, index))


def find_mask_sentinel_values(nodes: Iterable[Any]) -> list[MaskSentinelFinding]:
    """Return every credential-field value in *nodes* containing the sentinel.

    Scans exactly :data:`GRAPH_NODE_SECRET_FIELDS` on every dict node. A
    non-container field value carrying the sentinel (a malformed but
    pass-through shape) is reported with an empty path rather than skipped -
    detection must never fail open on an odd payload.
    """
    findings: list[MaskSentinelFinding] = []
    for node in nodes:
        if not isinstance(node, dict):
            continue
        node_id = str(node.get("id") or "?")
        for field in GRAPH_NODE_SECRET_FIELDS:
            field_value = node.get(field)
            if field_value is None:
                continue
            findings.extend(
                MaskSentinelFinding(node_id=node_id, field=field, path=path)
                for path in _walk_sentinels(field_value, ())
            )
    return findings


def _value_at_path(container: Any, path: tuple[str | int, ...]) -> Any:
    """Return the stored value at *path*, or ``None`` when any hop is missing."""
    if not path:
        return container
    current = container
    for segment in path:
        if isinstance(current, dict):
            current = current.get(segment)
        elif isinstance(current, list) and isinstance(segment, int) and 0 <= segment < len(current):
            current = current[segment]
        else:
            return None
    return current


def find_unresolved_mask_sentinels(
    incoming_nodes: Iterable[Any],
    stored_nodes: Iterable[Any] | None,
) -> list[MaskSentinelFinding]:
    """Sentinels in *incoming_nodes* with NO stored counterpart to resolve to.

    The write-path merge (``merge_masked_graph_nodes``) restores a masked
    echo from the stored node and drops an echo with no stored counterpart.
    A dropped echo means the caller submitted a FRESH sentinel (not a
    round-trip of a masked read) - fail closed with a 422 instead of silently
    dropping the key. Findings whose sentinel SURVIVES the merge (non-conforming
    shapes, degraded stored rows) are caught by :func:`find_mask_sentinel_values`
    over the resolved graph instead, so callers scan both and de-duplicate.
    """
    stored_by_id: dict[str, Any] = {}
    for stored_node in stored_nodes or []:
        if isinstance(stored_node, dict) and stored_node.get("id") is not None:
            stored_by_id[str(stored_node["id"])] = stored_node

    unresolved: list[MaskSentinelFinding] = []
    for finding in find_mask_sentinel_values(incoming_nodes):
        stored_node = stored_by_id.get(finding.node_id)
        stored_field = stored_node.get(finding.field) if stored_node is not None else None
        if _value_at_path(stored_field, finding.path) is None:
            unresolved.append(finding)
    return unresolved


def format_mask_sentinel_detail(findings: list[MaskSentinelFinding], *, limit: int = 5) -> str:
    """Render findings for an error detail (bounded - never a wall of text)."""
    described = [finding.describe() for finding in findings[:limit]]
    remaining = len(findings) - limit
    if remaining > 0:
        described.append(f"(+{remaining} more)")
    return "; ".join(described)
