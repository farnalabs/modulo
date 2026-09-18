"""Schema translation/render layer for target profiles.

FAR-900: An additive, node-level translation pass that renders a JSON Schema
for a target profile (provider strict-mode / runtime SDK). The pass is
BEST-EFFORT: it must NEVER block a node.

Profiles
--------
- ``verbatim`` — identity (returns the schema unchanged).
- ``provider-strict`` — adapt for the target provider's strict subset using a
  PER-PROVIDER unsupported-keyword set (NOT one global set).  ``pattern`` is
  retained where the provider supports it; ``format`` is advisory and stripped
  where unsupported.  Every stripped keyword emits a per-keyword warning.
- ``runtime-sdk`` — the target runtime's form.

$ref handling
-------------
Inline ``$ref``/``$defs`` OURSELVES (jsonschema must never be asked to
resolve them here).  Hard limits: max depth 32, max expanded nodes 10 000.
Exceeding either → REJECT and fall back to ``verbatim``.  Reject external
``$ref`` (non-local pointer).

Caching
-------
Bounded LRU memoisation keyed by (schema content SHA-256, profile, provider_id,
renderer_version).  Mutation-safe: hash/copy the input — a mutated cached dict
is a defect.
"""

from __future__ import annotations

import copy
import hashlib
import json
import logging
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Literal, get_args

_log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------

SchemaProfile = Literal["verbatim", "provider-strict", "runtime-sdk"]

# FIX 4: canonical value set derived from SchemaProfile — single source of truth.
SCHEMA_PROFILE_VALUES: tuple[str, ...] = get_args(SchemaProfile)

RENDERER_VERSION = "1"

_MAX_REF_DEPTH = 32
_MAX_EXPANDED_NODES = 10_000

# LRU cache size per (profile, provider) combination
_LRU_MAX_SIZE = 256


@dataclass
class RenderWarning:
    """A single per-keyword warning emitted during translation."""

    keyword: str
    path: str
    message: str


@dataclass
class RenderResult:
    """The output of ``render_for_profile``."""

    schema: dict[str, Any]
    profile: SchemaProfile
    warnings: list[RenderWarning] = field(default_factory=list)
    skipped: bool = False
    skip_reason: str | None = None


# ---------------------------------------------------------------------------
# Per-provider unsupported keyword sets
# ---------------------------------------------------------------------------

# Each provider defines its own set of JSON Schema keywords that it does NOT
# support in strict mode.  The sets are intentionally different per provider —
# a keyword unsupported by one may be supported by another.

_PROVIDER_UNSUPPORTED: dict[str, frozenset[str]] = {
    # OpenAI strict mode supports most of JSON Schema Draft 2020-12
    # but strips unsupported keywords for its structured-output contract.
    "openai": frozenset(
        {
            "$id",
            "$schema",
            "$defs",
            "definitions",
            "default",
            "examples",
            "minItems",
            "maxItems",
            "minLength",
            "maxLength",
            "minimum",
            "maximum",
            "exclusiveMinimum",
            "exclusiveMaximum",
            "multipleOf",
            "pattern",
            "minProperties",
            "maxProperties",
        }
    ),
    # Anthropic supports JSON Schema for tool use but strips several keywords.
    "anthropic": frozenset(
        {
            "$id",
            "$schema",
            "$defs",
            "definitions",
            "default",
            "examples",
            "minItems",
            "maxItems",
            "minLength",
            "maxLength",
            "minimum",
            "maximum",
            "exclusiveMinimum",
            "exclusiveMaximum",
            "multipleOf",
            "patternProperties",
            "additionalProperties",
            "minProperties",
            "maxProperties",
        }
    ),
    # Google Gemini strict mode
    "google": frozenset(
        {
            "$id",
            "$schema",
            "$defs",
            "definitions",
            "default",
            "examples",
            "minItems",
            "maxItems",
            "minLength",
            "maxLength",
            "minimum",
            "maximum",
            "exclusiveMinimum",
            "exclusiveMaximum",
            "multipleOf",
            "pattern",
            "patternProperties",
            "minProperties",
            "maxProperties",
        }
    ),
    # DeepSeek
    "deepseek": frozenset(
        {
            "$id",
            "$schema",
            "$defs",
            "definitions",
            "default",
            "examples",
            "pattern",
            "patternProperties",
            "minProperties",
            "maxProperties",
            "minItems",
            "maxItems",
        }
    ),
}

# Keywords that are always advisory (stripped in provider-strict regardless
# of provider if the provider's set does not already include them).
_ADVISORY_KEYWORDS = frozenset({"default", "examples", "title", "description", "format"})

# Keywords that are NEVER stripped in provider-strict mode (structural)
_ALWAYS_KEEP = frozenset(
    {
        "type",
        "properties",
        "required",
        "items",
        "anyOf",
        "oneOf",
        "allOf",
        "not",
        "if",
        "then",
        "else",
        "enum",
        "const",
        "$ref",
        "$defs",
    }
)


# ---------------------------------------------------------------------------
# Generic schema traversal helper (FIX 3)
# ---------------------------------------------------------------------------


def _walk_schema(
    node: Any,
    path: str,
    visit_fn: Any,
) -> None:
    """Walk a JSON Schema, calling visit_fn for each (key, value, path) pair.

    The visitor receives (key: str, value: Any, path: str) and returns None.
    Recursion continues into dicts and lists unconditionally — the visitor
    decides (by side-effect) whether to record warnings or strip keys.

    Used by both ``_render_provider_strict`` and ``preview_strip_warnings``.
    """
    if isinstance(node, dict):
        for key, value in node.items():
            visit_fn(key, value, path)
            if isinstance(value, dict):
                _walk_schema(value, f"{path}.{key}", visit_fn)
            elif isinstance(value, list):
                for i, item in enumerate(value):
                    if isinstance(item, dict):
                        _walk_schema(item, f"{path}.{key}[{i}]", visit_fn)
    elif isinstance(node, list):
        for i, item in enumerate(node):
            if isinstance(item, dict):
                _walk_schema(item, f"{path}[{i}]", visit_fn)


# ---------------------------------------------------------------------------
# $ref flattener
# ---------------------------------------------------------------------------


class _RefFlattenError(Exception):
    """Raised when $ref flattening hits a hard limit."""


def _is_local_ref(ref: str) -> bool:
    """Return True when a $ref points to a local definition (starts with #)."""
    return ref.startswith("#")


def _resolve_pointer(root: dict[str, Any], pointer: str) -> dict[str, Any]:
    """Resolve a JSON Pointer (e.g. '#/$defs/Foo') against the root schema."""
    if not pointer.startswith("#/"):
        raise _RefFlattenError(f"External $ref not allowed: {pointer}")
    parts = pointer[2:].split("/") if pointer != "#" else []
    current: Any = root
    for raw_part in parts:
        # JSON Pointer uses ~0 for ~ and ~1 for /
        part = raw_part.replace("~1", "/").replace("~0", "~")
        if isinstance(current, dict) and part in current:
            current = current[part]
        else:
            raise _RefFlattenError(f"Cannot resolve pointer part '{part}' in '{pointer}'")
    if not isinstance(current, dict):
        raise _RefFlattenError(f"Pointer '{pointer}' resolved to non-dict: {type(current).__name__}")
    return current


def _flatten_refs(
    schema: dict[str, Any],
    *,
    _root: dict[str, Any] | None = None,
    _depth: int = 0,
    _node_count: int = 0,
    _cache: dict[str, tuple[dict[str, Any], int]] | None = None,
) -> tuple[dict[str, Any], int]:
    """Inline $ref references within the schema.

    Returns (flattened_schema, total_node_count).

    Raises _RefFlattenError on hard limits or external refs.

    FIX 6: Diamond-shaped schemas are memoised by ``$ref`` pointer so each
    unique definition is flattened once and reused at every reference site.
    """
    if _root is None:
        _root = schema
    if _cache is None:
        _cache = {}
    if _depth > _MAX_REF_DEPTH:
        raise _RefFlattenError(f"Exceeded max $ref depth {_MAX_REF_DEPTH}")
    if _node_count > _MAX_EXPANDED_NODES:
        raise _RefFlattenError(f"Exceeded max expanded nodes {_MAX_EXPANDED_NODES}")

    result: dict[str, Any] = {}
    node_count = _node_count + 1

    for key, value in schema.items():
        if key == "$ref":
            if not isinstance(value, str):
                raise _RefFlattenError(f"$ref must be a string, got {type(value).__name__}")
            if not _is_local_ref(value):
                raise _RefFlattenError(f"External $ref not allowed: {value}")
            # FIX 6: memoise by pointer — count each unique definition once
            if value in _cache:
                cached_result, _cached_count_delta = _cache[value]
                result.update(cached_result)
                # Each unique definition counted once (cached)
            else:
                resolved = _resolve_pointer(_root, value)
                count_before = node_count
                flattened, node_count = _flatten_refs(
                    resolved,
                    _root=_root,
                    _depth=_depth + 1,
                    _node_count=node_count,
                    _cache=_cache,
                )
                # Cache the result and the node count delta for this pointer
                _cache[value] = (flattened, node_count - count_before)
                result.update(flattened)
        elif isinstance(value, dict):
            flattened_value, node_count = _flatten_refs(
                value,
                _root=_root,
                _depth=_depth + 1,
                _node_count=node_count,
                _cache=_cache,
            )
            result[key] = flattened_value
        elif isinstance(value, list):
            new_list: list[Any] = []
            for item in value:
                if isinstance(item, dict):
                    flattened_item, node_count = _flatten_refs(
                        item,
                        _root=_root,
                        _depth=_depth + 1,
                        _node_count=node_count,
                        _cache=_cache,
                    )
                    new_list.append(flattened_item)
                else:
                    new_list.append(item)
            result[key] = new_list
        else:
            result[key] = value

    return result, node_count


def _strip_local_defs(schema: dict[str, Any]) -> dict[str, Any]:
    """Remove $defs/definitions keys from the top level after flattening."""
    result = dict(schema)
    result.pop("$defs", None)
    result.pop("definitions", None)
    return result


# ---------------------------------------------------------------------------
# Profile renderers
# ---------------------------------------------------------------------------


def _render_verbatim(schema: dict[str, Any]) -> RenderResult:
    """Identity profile — return the schema unchanged."""
    return RenderResult(
        schema=copy.deepcopy(schema),
        profile="verbatim",
    )


def _strip_keywords(schema: dict[str, Any], strip_set: frozenset[str]) -> dict[str, Any]:
    """Return a deep copy of *schema* with keys in *strip_set` removed.

    Structural keywords in ``_ALWAYS_KEEP`` are never stripped regardless of
    their presence in *strip_set*.  This is a write-side helper; the read-side
    warning collection lives in ``preview_strip_warnings`` (which shares the
    same ``_walk_schema`` traversal via FIX 3).
    """

    def _strip(node: Any) -> Any:
        if isinstance(node, dict):
            return {k: _strip(v) for k, v in node.items() if k not in strip_set or k in _ALWAYS_KEEP}
        if isinstance(node, list):
            return [_strip(item) for item in node]
        return node

    result: dict[str, Any] = _strip(schema)
    return result


def _render_provider_strict(
    schema: dict[str, Any],
    provider_id: str | None,
) -> RenderResult:
    """Strip unsupported keywords for the target provider's strict subset."""
    unsupported = _PROVIDER_UNSUPPORTED.get(
        (provider_id or "").lower(),
        frozenset(),
    )
    # Combine provider-specific and advisory keywords
    strip_set = unsupported | _ADVISORY_KEYWORDS

    warnings: list[RenderWarning] = []

    # FIX 3: collect warnings via shared _walk_schema traversal
    def _warn_visitor(key: str, value: Any, path: str) -> None:
        if key in strip_set and key not in _ALWAYS_KEEP:
            warnings.append(
                RenderWarning(
                    keyword=key,
                    path=path,
                    message=f"Keyword '{key}' stripped for provider '{provider_id}'",
                )
            )

    _walk_schema(schema, "#", _warn_visitor)

    # Strip the flagged keywords from the output schema
    processed = _strip_keywords(schema, strip_set)

    return RenderResult(
        schema=processed,
        profile="provider-strict",
        warnings=warnings,
    )


def _render_runtime_sdk(
    schema: dict[str, Any],
    target: str | None,
) -> RenderResult:
    """Render schema for the target runtime's form.

    Currently: identity (runtime-sdk profiles pass through the schema as-is).
    Future runtimes may add transformation passes here.
    """
    _ = target  # reserved for future runtime-specific transforms
    return RenderResult(
        schema=copy.deepcopy(schema),
        profile="runtime-sdk",
    )


# ---------------------------------------------------------------------------
# Abstract schema detection
# ---------------------------------------------------------------------------


def _is_abstract_schema(schema: dict[str, Any]) -> bool:
    """Return True when a schema has no concrete definition.

    An abstract schema is one that only contains ``$ref`` or composition
    keywords (``anyOf``, ``oneOf``, ``allOf``) without any concrete type
    or properties — it describes a shape but doesn't define one.
    """
    non_meta_keys = {k for k in schema if not k.startswith("$") and k not in ("title", "description")}
    return not non_meta_keys


# ---------------------------------------------------------------------------
# Memoisation cache (LRU, mutation-safe)
# ---------------------------------------------------------------------------


class _RenderCache:
    """Bounded LRU cache for render_for_profile results.

    Keyed by (content_sha256, profile, provider_id, renderer_version).
    Mutation-safe: stores a deep copy of the result.
    """

    def __init__(self, max_size: int = _LRU_MAX_SIZE) -> None:
        self._cache: OrderedDict[str, RenderResult] = OrderedDict()
        self._max_size = max_size

    @staticmethod
    def _make_key(
        schema: dict[str, Any],
        profile: SchemaProfile,
        provider_id: str | None,
    ) -> str:
        """Compute the cache key from schema content SHA-256 + profile + provider."""
        content = json.dumps(schema, sort_keys=True, default=str).encode("utf-8")
        sha = hashlib.sha256(content).hexdigest()
        return f"{sha}:{profile}:{provider_id or 'none'}:{RENDERER_VERSION}"

    def get(
        self,
        schema: dict[str, Any],
        profile: SchemaProfile,
        provider_id: str | None,
    ) -> RenderResult | None:
        """Retrieve a cached result, or None on miss."""
        key = self._make_key(schema, profile, provider_id)
        if key in self._cache:
            self._cache.move_to_end(key)
            # Return a deep copy to prevent mutation of cached data
            return copy.deepcopy(self._cache[key])
        return None

    def put(
        self,
        schema: dict[str, Any],
        profile: SchemaProfile,
        provider_id: str | None,
        result: RenderResult,
    ) -> None:
        """Store a result in the cache (deep copy)."""
        key = self._make_key(schema, profile, provider_id)
        self._cache[key] = copy.deepcopy(result)
        if len(self._cache) > self._max_size:
            self._cache.popitem(last=False)

    def clear(self) -> None:
        """Evict all cached results."""
        self._cache.clear()


# Module-level cache instance
_render_cache = _RenderCache()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def render_for_profile(
    schema: dict[str, Any],
    profile: SchemaProfile,
    provider_id: str | None = None,
) -> RenderResult:
    """Render a JSON Schema for a target profile.

    Best-effort: never raises, never blocks.  Falls back to ``verbatim`` on
    any error or when the schema input is empty/None.

    Args:
        schema: The JSON Schema to translate.  May contain ``$ref``/``$defs``.
        profile: Target profile: ``verbatim``, ``provider-strict``, or
            ``runtime-sdk``.
        provider_id: The provider identifier (e.g. ``"openai"``, ``"anthropic"``).
            Used by ``provider-strict`` to select the unsupported-keyword set.

    Returns:
        A ``RenderResult`` with the translated schema, warnings, and skip info.
    """
    # --- Input validation ---
    if not isinstance(schema, dict) or not schema:
        return RenderResult(
            schema={},
            profile=profile,
            skipped=True,
            skip_reason="empty_schema",
        )

    # --- Verbatim: identity, no processing ---
    if profile == "verbatim":
        return _render_verbatim(schema)

    # --- Check cache ---
    cached = _render_cache.get(schema, profile, provider_id)
    if cached is not None:
        return cached

    try:
        result = _translate(schema, profile, provider_id)
    except Exception:
        _log.exception(
            "render_for_profile: translation failed; falling back to verbatim",
        )
        result = _render_verbatim(schema)
        result.warnings = []
        result.skipped = True
        result.skip_reason = "translation_error"  # vulture: skip_reason is read by callers

    # --- Cache the result ---
    _render_cache.put(schema, profile, provider_id, result)
    return result


def _translate(
    schema: dict[str, Any],
    profile: SchemaProfile,
    provider_id: str | None,
) -> RenderResult:
    """Core translation logic (may raise)."""

    # --- Abstract schema detection ---
    if _is_abstract_schema(schema):
        return RenderResult(
            schema=copy.deepcopy(schema),
            profile=profile,
            skipped=True,
            skip_reason="abstract_schema",
        )

    # --- $ref flattening (for non-verbatim profiles) ---
    try:
        flattened, _node_count = _flatten_refs(schema)
    except _RefFlattenError as exc:
        _log.warning("render_for_profile: $ref flattening failed (%s); falling back to verbatim", exc)
        return RenderResult(
            schema=copy.deepcopy(schema),
            profile=profile,
            skipped=True,
            skip_reason=f"ref_flatten_error: {exc}",
        )

    # Strip $defs/definitions after flattening
    flattened = _strip_local_defs(flattened)

    # --- Profile-specific rendering ---
    if profile == "provider-strict":
        return _render_provider_strict(flattened, provider_id)
    if profile == "runtime-sdk":
        return _render_runtime_sdk(flattened, provider_id)

    # Unknown profile: fall back to verbatim
    return _render_verbatim(schema)


# ---------------------------------------------------------------------------
# Design-time helpers
# ---------------------------------------------------------------------------


def preview_strip_warnings(
    schema: dict[str, Any],
    profile: SchemaProfile,
    provider_id: str | None = None,
) -> list[RenderWarning]:
    """Preview which keywords would be stripped for the given profile.

    Used at pipeline save/validate to emit a ``schema_translation_report``
    without actually modifying the schema.
    """
    if profile != "provider-strict" or not schema:
        return []

    unsupported = _PROVIDER_UNSUPPORTED.get(
        (provider_id or "").lower(),
        frozenset(),
    )
    strip_set = unsupported | _ADVISORY_KEYWORDS
    warnings: list[RenderWarning] = []

    # FIX 3: shared _walk_schema traversal (side-effect-only visitor)
    def _warn_visitor(key: str, value: Any, path: str) -> None:
        if key in strip_set and key not in _ALWAYS_KEEP:
            warnings.append(
                RenderWarning(
                    keyword=key,
                    path=path,
                    message=f"Keyword '{key}' will be stripped for provider '{provider_id}'",
                )
            )

    _walk_schema(schema, "#", _warn_visitor)
    return warnings
