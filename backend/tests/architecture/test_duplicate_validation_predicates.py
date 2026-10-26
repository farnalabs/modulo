"""Architecture gate: duplicated logic written independently across modules (FAR-1965).

The ``duplicate-validation`` failure mode is the same validation predicate
written independently in more than one caller: a fix or a widening applied to
one copy silently leaves the other stale, so the two drift apart while every
test stays green. The 30-day delivery-feedback baseline surfaced it as a
recurring review theme (18 findings across 15 PRs) with no prose rule and no
structural check. The concrete live instance on main is
``_parse_future_expires_at``: defined in ``api/routes/api_keys.py`` and
``api/routes/service_accounts.py`` with the *same* user-facing detail
(``"expires_at must be in the future"``) and near-identical logic, plus a third
inline copy in ``api/mcp_server.py`` under a different name. A widening applied
to one copy leaves the others stale.

This module ships FOUR probes for distinct observed shapes of that failure
mode. All four share one shrink-only baseline
(``duplicate_validation_predicate_baseline.txt``) so a sweep can never silently
re-open:

1. **Duplicated validation predicates** (:func:`_duplicate_validation_predicates_in`).
   A function whose name says it validates and whose body carries a non-empty,
   user-facing validation-error ``detail`` string. Two such predicates in
   different modules carrying the *same* detail are the same check written
   twice: the shared message is the observable contract and is exactly what
   drifts when one copy is widened. Matching on the name alone drowns in the
   connector plugins (every provider implements ``health_check`` by design);
   matching on the message alone drowns in generic repeat messages
   (``"Not found"``). Name **and** message together is the narrow predicate.

2. **Duplicated private string constants** (:func:`_duplicate_private_constants_in`).
   A module-level ``_UPPER_CASE`` constant whose value is a **string literal**
   re-declared with the same name and value in a second module. A shared,
   identity-bearing string (a permission code, a user-facing message, an env
   var name, an ``accounts.id`` FK target, a ``SELECT set_config(...)`` RLS
   fragment) duplicated across modules is the same contract written twice —
   change one and the other silently diverges. The string restriction is
   load-bearing: numeric/bool constants collide by coincidence
   (``_MAX_RETRIES = 3`` means different things in two subsystems), so matching
   them produces false positives and is deliberately NOT scanned (see the
   refusal below).

3. **Duplicated block bodies** (:func:`_duplicate_block_bodies_in`).
   A contiguous run of statements (docstring-stripped, position-insensitive)
   that is an exact copy of some function's *complete* body in a different
   module — the helper was inlined instead of called, or copied wholesale.
   ``ast.dump`` comparison includes every identifier and literal, so a match is
   a verbatim copy, not a loose structural coincidence.

4. **Near-identical bodies** (:func:`_near_body_groups_in`, probe 4).
   Two docstring-stripped function bodies in different modules that are
   near-identical but not identical (``difflib`` ratio >= 0.90) — the same rule
   copied and then edited, so the copies have already started to drift. Raw
   similarity alone was measured and refused: at 0.90 it puts per-entity CRUD
   scaffolding in the same band as genuine near-copies (see the measurement
   history below). The shipped lens pairs similarity with a **subject
   discriminator**: a pair is scaffolding, not duplication, when

   * the two bodies reference **disjoint ORM model classes** (per-entity data
     access — ``get_run`` vs ``get_mcp_server``), or
   * they are module-level functions in the **same package** with a
     data-access name (``get_``/``list_``/``update_``/``delete_``/... ) and
     **no shared ORM model** (per-entity CRUD siblings that delegate to a
     per-entity getter, such as ``delete_agent`` / ``delete_set``), or
   * they are **same-named methods of classes sharing a base class**
     (protocol implementations — the per-provider ``RuntimeProvider.supports``
     bodies repeat by design, the same rationale that excludes the connector
     and model-backend provider trees from this module's scope).

   Exact copies (ratio 1.0) are deliberately left to probe 3 — this lens is
   for the drift class only. On current main the lens admits 12 groups and
   rejects 9 scaffolding pairs, with zero boilerplate admitted and zero
   genuine near-copies rejected (see the measurement history).

Measurement history (why raw similarity alone was refused, and what fixed it)
------------------------------------------------------------------------------
Character-level ``difflib`` similarity over normalised bodies (docstring
stripped, ``ast.unparse`` per statement, >= 4 statements) was measured first.
It does not separate the classes. On current main it yields:

* ``>= 0.98`` — 0 non-identical pairs (only exact copies, which probe 3 owns);
* ``>= 0.95`` — 3 non-identical pairs, all genuine near-copies;
* ``>= 0.90`` — 21 non-identical pairs: the genuine class (e.g.
  ``launcher/entry.py:resolve_bin_dir`` / ``launcher/upgrade.py:_resolve_pg_bin_dir``
  at 0.963 and the two ``core/cleanup_jobs/`` functions at 0.970) plus 9
  per-entity scaffolding pairs (``db/crud/run.py:get_run`` /
  ``db/crud/mcp_server.py:get_mcp_server`` at 0.902, the two ``list_*`` queries
  at 0.944, the two ``update_folder`` bodies at 0.905, ...). No similarity
  threshold separates them, which is why the earlier note refused the shape.

Six discriminators were measured on that 21-pair band and rejected:

* **shared exact string literal** — 14 groups admitted, 3 boilerplate (the two
  ``update_*`` CRUD pairs share their user-facing messages; the per-provider
  ``supports`` methods share the profile field names) and it misses a genuine
  pair (the two ``_csv_stream_line`` helpers share no literal).
* **shared domain identifier** — 21 groups admitted, all 9 scaffolding pairs
  still admitted: scaffolding and duplication share the same operator
  vocabulary.
* **structural signature with identifiers/literals normalised** — 10 groups
  admitted, 7 boilerplate, and 9 genuine pairs missed: normalisation makes the
  two classes *more* alike, not less.
* **accessor-shape exclusion** (canonical ``select`` -> optional org filter ->
  ``execute`` -> ``scalar_one_or_none``) — removes the getter pair(s) only; the
  list/folder/update scaffolding remains.
* **rename-invariance** (the differing tokens form a consistent bijection) —
  rejects nothing: every near-identical pair's differences are a consistent
  mapping, so it cannot tell a rename from a drift.
* **disjoint ORM model classes alone** — 13 groups admitted, one boilerplate
  pair left: the per-provider ``supports`` methods name no ORM model. It also
  leaks same-package CRUD siblings that delegate to a per-entity getter
  (``delete_agent`` / ``delete_set``) once the threshold drops.

The shipped combination (disjoint ORM subjects + same-package per-entity CRUD
siblings + same-named methods of a shared base) admits 12 groups and rejects 9
on current main, with zero scaffolding admitted and zero genuine near-copies
rejected. A 0.85 threshold was also measured: 42 non-identical pairs become 16
admitted groups with the same zero-boilerplate result, but the pairwise scan
costs ~18x more (~12 minutes on this tree) for no new genuine class, so the
gate ships at 0.90.

Scope
-----
``backend/src/modulo/**`` except:

* ``connectors/**``, ``model_backends/**`` and ``core/error_tracking/forwarders/**``
  — pluggable provider trees. Each provider is deliberately a self-contained
  implementation of the same protocol, so a per-provider helper or constant
  (``_has_server_delay``, ``_parse_retry_after``, ``_CONNECTOR_TYPE``) repeating
  its sibling's shape is a design choice, not drift. Scanning them would make
  the gate fire on every new provider.
* ``migrations/**`` — historical, immutable by policy.

This is a SHRINK-ONLY ratchet, the same shape as the duplicate test-body
baseline (``duplicate_test_body_baseline.txt``): the pre-existing duplicates are
frozen in ``duplicate_validation_predicate_baseline.txt`` so this gate blocks
NEW duplicates immediately while the backlog is untangled; a frozen entry that
is no longer a duplicate must leave the baseline or the stale-entry check fails.

Documented limitations
----------------------
* AST-based, not similarity-based: it catches the *same message written twice*
  (probe 1), the *same string constant written twice* (probe 2) and the *same
  statement block written twice* (probe 3), not two predicates that have drifted
  to different messages/values/bodies. Drift is the downstream consequence of the
  duplication this gate blocks at birth.
* Probe 1 ``detail`` extraction follows the two shapes the codebase uses
  (``detail=`` kwarg, ``"detail":`` dict key); a validator surfacing its message
  through a constant or f-string prefix is not scanned.
* Probe 2 scans **string** constants only. A shared numeric sentinel/limit
  re-declared per module is not caught (its name+value coincidence is not
  evidence of a shared contract — see the refusal above).
* Probe 3 is bounded to blocks of 3-12 statements and to a function-body helper
  in a *different* module; a longer inlined helper, or one inlined within the
  same module, is not caught.
* Probe 4 is bounded to docstring-stripped bodies of >= 4 statements in
  different modules and to a 0.90 similarity floor: a copy that has drifted
  further, or a two-statement duplication, is not caught.
* Probe 4's subject discriminator is deliberately conservative: a genuine
  duplication that operates over two *different* ORM models, or between two
  same-package data-access-named helpers over no ORM model, is treated as
  per-entity scaffolding and NOT flagged. That can only miss a duplicate, never
  raise a false alarm.
* Baseline block and near-body keys are the member ``module:function`` set, so
  a body edit that keeps the same functions duplicated stays frozen (by design)
  and a function rename surfaces as a new group.
"""

from __future__ import annotations

import ast
import difflib
import json
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from functools import cache
from pathlib import Path

from tests.unit._source_scan import iter_source_text

BACKEND_DIR = Path(__file__).resolve().parents[2]
SRC_DIR = BACKEND_DIR / "src" / "modulo"
BASELINE_PATH = Path(__file__).resolve().parent / "duplicate_validation_predicate_baseline.txt"

#: Trees scanned. The pluggable provider trees are excluded (see the module
#: docstring): their per-provider helpers and constants repeat by design.
_SKIP_PREFIXES = ("connectors/", "model_backends/", "core/error_tracking/forwarders/")
#: Any path part that marks a module as out of scope.
_SKIP_PARTS = frozenset({"migrations"})

#: What makes a function a validation *predicate*: its name says so. Kept
#: deliberately broad on the verb family — the message half of the predicate is
#: what does the precision work.
_VALIDATION_NAME = re.compile(
    r"valid|check|verify|_is_|_has_|ensure|enforce|assert|require|_parse|parse_|coerce|canonical",
    re.IGNORECASE,
)

#: Probe 2: a private module-level constant name (``_UPPER_CASE``).
_PRIVATE_CONSTANT_NAME = re.compile(r"^_+[A-Z][A-Z0-9_]*$")

#: Probe 3: statement-block size bounds (see the docstring's limitations).
_BLOCK_MIN_STATEMENTS = 3
_BLOCK_MAX_STATEMENTS = 12

#: Probe 4: minimum body size and the measured similarity floor (see the
#: docstring's measurement history for why 0.90 and not 0.85 or 0.95).
_NEAR_BODY_MIN_STATEMENTS = 4
_NEAR_BODY_SIMILARITY = 0.90

#: Probe 4: name prefixes that mark a per-entity data-access helper. Two such
#: helpers in the same package over no shared ORM model are CRUD scaffolding,
#: not a duplicated rule.
_DATA_ACCESS_PREFIXES = (
    "get_",
    "list_",
    "update_",
    "delete_",
    "soft_delete_",
    "create_",
    "upsert_",
    "fetch_",
    "count_",
)


# ---------------------------------------------------------------------------
# Shared enumeration
# ---------------------------------------------------------------------------


def _tree_sources() -> list[tuple[str, str]]:
    """Every in-scope ``(relpath, source)`` under ``src/modulo``."""
    sources: list[tuple[str, str]] = []
    for path, source in iter_source_text(SRC_DIR):
        rel = path.relative_to(SRC_DIR).as_posix()
        if rel.startswith(_SKIP_PREFIXES) or _SKIP_PARTS & set(path.relative_to(SRC_DIR).parts):
            continue
        sources.append((rel, source))
    return sources


# ---------------------------------------------------------------------------
# Probe 1 — duplicated validation predicates (same user-facing detail)
# ---------------------------------------------------------------------------


def _validation_details(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> set[str]:
    """Non-empty validation-error ``detail`` strings carried by *fn*.

    Two shapes are recognised, matching how the codebase emits validation
    errors: a ``detail=`` keyword on any call (``HTTPException`` and helpers),
    and a ``"detail":`` key in any dict literal (the MCP error envelope). The
    empty string is ignored: it is a sentinel, not a message.
    """
    details: set[str] = set()
    for node in ast.walk(fn):
        if isinstance(node, ast.keyword) and node.arg == "detail":
            value = node.value
            if isinstance(value, ast.Constant) and isinstance(value.value, str) and value.value:
                details.add(value.value)
        elif isinstance(node, ast.Dict):
            for key, value in zip(node.keys, node.values, strict=False):
                if (
                    isinstance(key, ast.Constant)
                    and key.value == "detail"
                    and isinstance(value, ast.Constant)
                    and isinstance(value.value, str)
                    and value.value
                ):
                    details.add(value.value)
    return details


def _duplicate_validation_predicates_in(sources: list[tuple[str, str]]) -> dict[str, list[tuple[str, str, int]]]:
    """Group validation predicates that share a ``detail`` across modules.

    *sources* is a list of ``(module_path, source_text)`` pairs, so the real
    tree and a synthetic control run through the exact same code. The return is
    ``detail string -> [(module_path, function_name, lineno), ...]`` for every
    detail carried by a validation-named function in two or more distinct
    modules.
    """
    by_detail: dict[str, list[tuple[str, str, int]]] = defaultdict(list)
    for module, source in sources:
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if _VALIDATION_NAME.search(node.name) is None:
                continue
            for detail in _validation_details(node):
                by_detail[detail].append((module, node.name, node.lineno))
    return {detail: members for detail, members in by_detail.items() if len({m[0] for m in members}) > 1}


@cache
def _current_duplicates() -> dict[str, list[tuple[str, str, int]]]:
    return _duplicate_validation_predicates_in(_tree_sources())


# ---------------------------------------------------------------------------
# Probe 2 — duplicated private string constants
# ---------------------------------------------------------------------------


def _module_private_string_constants(tree: ast.Module) -> list[tuple[str, str, int]]:
    """``(name, value, lineno)`` for every module-level private string constant.

    Recognises a single-target ``Assign`` and an ``AnnAssign`` at module level
    whose target is a private ``_UPPER_CASE`` name and whose value is a
    non-empty string literal. Only string literals qualify (see the docstring).
    """
    found: list[tuple[str, str, int]] = []
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            name, value_node = node.targets[0].id, node.value
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            name, value_node = node.target.id, node.value
        else:
            continue
        if _PRIVATE_CONSTANT_NAME.match(name) is None:
            continue
        if not (isinstance(value_node, ast.Constant) and isinstance(value_node.value, str) and value_node.value):
            continue
        found.append((name, value_node.value, node.lineno))
    return found


def _duplicate_private_constants_in(
    sources: list[tuple[str, str]],
) -> dict[tuple[str, str], list[tuple[str, str, int]]]:
    """Group private string constants re-declared across modules.

    The return is ``(name, value) -> [(module_path, name, lineno), ...]`` for
    every private string constant whose exact name and value also appear in a
    distinct module.
    """
    by_key: dict[tuple[str, str], list[tuple[str, str, int]]] = defaultdict(list)
    for module, source in sources:
        tree = ast.parse(source)
        for name, value, lineno in _module_private_string_constants(tree):
            by_key[(name, value)].append((module, name, lineno))
    return {key: members for key, members in by_key.items() if len({m[0] for m in members}) > 1}


@cache
def _current_private_string_constants() -> dict[tuple[str, str], list[tuple[str, str, int]]]:
    return _duplicate_private_constants_in(_tree_sources())


# ---------------------------------------------------------------------------
# Probe 3 — duplicated block bodies
# ---------------------------------------------------------------------------


def _body_without_docstring(node: ast.FunctionDef | ast.AsyncFunctionDef) -> list[ast.stmt]:
    """*node*'s statements with a leading docstring removed.

    Position-insensitive matching must ignore the docstring (it is prose, not
    behaviour); only a *leading* string-Expr is stripped so a deliberate
    standalone string expression is still compared.
    """
    body = list(node.body)
    if (
        body
        and isinstance(body[0], ast.Expr)
        and isinstance(body[0].value, ast.Constant)
        and isinstance(body[0].value.value, str)
    ):
        body = body[1:]
    return body


def _statement_fingerprints(node: ast.FunctionDef | ast.AsyncFunctionDef) -> tuple[str, ...]:
    """Position-insensitive fingerprint of each statement in *node*'s body.

    ``ast.dump`` includes every identifier, attribute, literal and operator, so
    two equal fingerprints are a verbatim copy (formatting and docstrings
    aside).
    """
    return tuple(
        ast.dump(statement, annotate_fields=False, include_attributes=False)
        for statement in _body_without_docstring(node)
    )


def _duplicate_block_bodies_in(sources: list[tuple[str, str]]) -> dict[tuple[str, ...], set[tuple[str, str]]]:
    """Group statement blocks that are an exact copy of a function body elsewhere.

    A function whose *complete* body is 3-12 statements is a *helper*. Any
    contiguous run of 3-12 statements inside a function in a DIFFERENT module
    whose fingerprint equals a helper's body is flagged: the helper was inlined
    (or copied wholesale) instead of called. The return is
    ``body_fingerprint -> {(module_path, function_name), ...}``.
    """
    functions: list[tuple[str, str, int, tuple[str, ...]]] = []
    for module, source in sources:
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                functions.append((module, node.name, node.lineno, _statement_fingerprints(node)))

    helpers: dict[tuple[str, ...], list[tuple[str, str]]] = defaultdict(list)
    for module, name, _lineno, fingerprints in functions:
        if _BLOCK_MIN_STATEMENTS <= len(fingerprints) <= _BLOCK_MAX_STATEMENTS:
            helpers[fingerprints].append((module, name))

    groups: dict[tuple[str, ...], set[tuple[str, str]]] = defaultdict(set)
    for module, name, _lineno, fingerprints in functions:
        count = len(fingerprints)
        if count < _BLOCK_MIN_STATEMENTS:
            continue
        for start in range(count):
            max_length = min(_BLOCK_MAX_STATEMENTS, count - start)
            for length in range(_BLOCK_MIN_STATEMENTS, max_length + 1):
                key = fingerprints[start : start + length]
                helper_members = helpers.get(key)
                if helper_members is None:
                    continue
                if all(helper_module == module for helper_module, _helper_name in helper_members):
                    continue
                groups[key].add((module, name))
                groups[key].update(helper_members)
    return {key: members for key, members in groups.items() if len({m[0] for m in members}) > 1}


@cache
def _current_block_bodies() -> dict[tuple[str, ...], set[tuple[str, str]]]:
    return _duplicate_block_bodies_in(_tree_sources())


# ---------------------------------------------------------------------------
# Probe 4 — near-identical (not identical) bodies
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _FunctionBody:
    """One function body considered by probe 4, with its subject metadata."""

    module: str
    name: str
    signature: str
    char_counts: Counter[str]
    models: frozenset[str]
    class_name: str | None
    class_bases: frozenset[str]


def _enclosing_classes(tree: ast.Module) -> dict[int, tuple[str, frozenset[str]]]:
    """``id(function) -> (class name, base names)`` for every class method.

    Probe 4 needs the class context to recognise a protocol method implemented
    once per provider: two same-named methods whose classes share a base class
    are implementations of one contract by design, not drift.
    """
    context: dict[int, tuple[str, frozenset[str]]] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        bases = frozenset(ast.unparse(base) for base in node.bases)
        for child in node.body:
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                context[id(child)] = (node.name, bases)
    return context


@cache
def _orm_model_names() -> frozenset[str]:
    """Class names declared under ``db/models/`` — the ORM subject vocabulary.

    Probe 4 uses the model a body *touches* as its subject: two near-identical
    bodies that select, filter or construct disjoint model classes are
    per-entity data access, not one rule written twice.
    """
    names: set[str] = set()
    for module, source in _tree_sources():
        if not module.startswith("db/models/"):
            continue
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.ClassDef):
                names.add(node.name)
    return frozenset(names)


def _model_references(node: ast.AST, model_names: frozenset[str]) -> frozenset[str]:
    """ORM model classes referenced by *node* (as ``Model`` or ``Model.attr``)."""
    found: set[str] = set()
    for child in ast.walk(node):
        if isinstance(child, ast.Name) and child.id in model_names:
            found.add(child.id)
        elif isinstance(child, ast.Attribute) and isinstance(child.value, ast.Name) and child.value.id in model_names:
            found.add(child.value.id)
    return frozenset(found)


def _module_package(module: str) -> str:
    """The package directory of a module path (``''`` for a top-level module)."""
    return module.rsplit("/", 1)[0] if "/" in module else ""


def _is_data_access_name(name: str) -> bool:
    """Whether *name* is a per-entity data-access helper name."""
    return name.lstrip("_").startswith(_DATA_ACCESS_PREFIXES)


def _function_bodies_in(module: str, source: str, model_names: frozenset[str]) -> list[_FunctionBody]:
    """Every >= 4-statement function body in *source*, with subject metadata."""
    tree = ast.parse(source)
    classes = _enclosing_classes(tree)
    bodies: list[_FunctionBody] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        statements = _body_without_docstring(node)
        if len(statements) < _NEAR_BODY_MIN_STATEMENTS:
            continue
        class_name, class_bases = classes.get(id(node), (None, frozenset()))
        signature = "\n".join(ast.unparse(statement) for statement in statements)
        bodies.append(
            _FunctionBody(
                module=module,
                name=node.name,
                signature=signature,
                char_counts=Counter(signature),
                models=_model_references(node, model_names),
                class_name=class_name,
                class_bases=class_bases,
            )
        )
    return bodies


def _is_per_entity_scaffolding(first: _FunctionBody, second: _FunctionBody) -> bool:
    """Whether a near-identical pair is scaffolding over different subjects.

    Three measured shapes (see the module docstring): per-entity data access
    over disjoint ORM models, same-package CRUD siblings that delegate to a
    per-entity getter (so the body names no model), and same-named methods of
    classes sharing a base class (protocol implementations). All three are
    "the same template applied to a different subject", not "one rule written
    twice", so probe 4 does not flag them.
    """
    shared_models = first.models & second.models
    if first.models and second.models and not shared_models:
        return True
    if (
        _module_package(first.module) == _module_package(second.module)
        and _is_data_access_name(first.name)
        and _is_data_access_name(second.name)
        and not shared_models
    ):
        return True
    return bool(
        first.class_name
        and second.class_name
        and first.name == second.name
        and first.class_name != second.class_name
        and first.class_bases & second.class_bases
    )


def _near_body_groups_in(sources: list[tuple[str, str]]) -> dict[frozenset[tuple[str, str]], float]:
    """Group near-identical function bodies that are NOT per-entity scaffolding.

    *sources* is a list of ``(module_path, source_text)`` pairs, so the real
    tree and a synthetic control run through the exact same code. Two bodies in
    different modules whose normalised text similarity is at least
    ``_NEAR_BODY_SIMILARITY`` are linked, unless the pair is per-entity
    scaffolding. Exact copies (similarity 1.0) are probe 3's shape and are not
    re-flagged here. The return is ``member set -> strongest similarity`` for
    every connected group, where a member is ``(module_path, function_name)``.
    """
    model_names = _orm_model_names()
    functions = [body for module, source in sources for body in _function_bodies_in(module, source, model_names)]
    functions.sort(key=lambda body: len(body.signature))

    parent: dict[tuple[str, str], tuple[str, str]] = {}

    def find(item: tuple[str, str]) -> tuple[str, str]:
        parent.setdefault(item, item)
        root = item
        while parent[root] != root:
            root = parent[root]
        while parent[item] != root:
            parent[item], item = root, parent[item]
        return root

    def union(left: tuple[str, str], right: tuple[str, str]) -> None:
        parent[find(left)] = find(right)

    edges: list[tuple[tuple[str, str], tuple[str, str], float]] = []
    # For a ratio >= threshold the longer signature can be at most
    # ``2 / threshold - 1`` times the shorter, so the length-sorted window is
    # an exact prefilter (no pair that could pass is skipped).
    upper_factor = 2 / _NEAR_BODY_SIMILARITY - 1
    for index, first in enumerate(functions):
        limit = len(first.signature) * upper_factor
        cursor = index + 1
        while cursor < len(functions) and len(functions[cursor].signature) <= limit:
            second = functions[cursor]
            cursor += 1
            if first.module == second.module:
                continue
            # Character-bag upper bound (difflib's quick_ratio semantics): if
            # even the shared-character proportion is below the threshold, the
            # SequenceMatcher cannot reach it. This is the expensive stage's
            # exact prefilter.
            common = sum(
                min(first.char_counts[char], second.char_counts[char])
                for char in first.char_counts.keys() & second.char_counts.keys()
            )
            if 2 * common < _NEAR_BODY_SIMILARITY * (len(first.signature) + len(second.signature)):
                continue
            # autojunk=False: the default heuristic drops matches for a
            # frequently-repeated character on long bodies, which lowers a
            # genuine near-copy's score below the threshold.
            ratio = difflib.SequenceMatcher(None, first.signature, second.signature, autojunk=False).ratio()
            if ratio < _NEAR_BODY_SIMILARITY:
                continue
            if ratio >= 1.0:
                continue
            if _is_per_entity_scaffolding(first, second):
                continue
            left = (first.module, first.name)
            right = (second.module, second.name)
            union(left, right)
            edges.append((left, right, ratio))

    groups: dict[tuple[str, str], set[tuple[str, str]]] = defaultdict(set)
    strengths: dict[tuple[str, str], float] = {}
    for left, right, ratio in edges:
        root = find(left)
        groups[root].update((left, right))
        strengths[root] = max(strengths.get(root, 0.0), ratio)
    return {frozenset(members): strengths[root] for root, members in groups.items()}


@cache
def _current_near_bodies() -> dict[frozenset[tuple[str, str]], float]:
    return _near_body_groups_in(_tree_sources())


# ---------------------------------------------------------------------------
# Baseline
# ---------------------------------------------------------------------------


def _baseline_key(detail: str, modules: list[str]) -> str:
    """Canonical baseline entry for one duplicated-validation group.

    JSON-encoded so the key is unambiguous whatever characters the detail
    message contains, and stable because the module list is sorted and the
    object keys are sorted.
    """
    return json.dumps({"detail": detail, "modules": sorted(set(modules))}, sort_keys=True)


def _constant_baseline_key(name: str, value: str, modules: list[str]) -> str:
    """Canonical baseline entry for one duplicated-constant group."""
    return json.dumps({"constant": name, "value": value, "modules": sorted(set(modules))}, sort_keys=True)


def _block_baseline_key(members: set[tuple[str, str]]) -> str:
    """Canonical baseline entry for one duplicated-block group.

    The group identity is its member ``module:function`` set (a function's body
    is unique, so the members identify the block).
    """
    return json.dumps({"block": sorted(f"{module}:{name}" for module, name in members)}, sort_keys=True)


def _near_body_baseline_key(members: frozenset[tuple[str, str]]) -> str:
    """Canonical baseline entry for one near-identical-body group.

    Like the block family, the group identity is its member ``module:function``
    set (the strongest pair similarity can move as the bodies drift, so it is
    not part of the key).
    """
    return json.dumps({"near_body": sorted(f"{module}:{name}" for module, name in members)}, sort_keys=True)


@cache
def _current_baseline_keys() -> set[str]:
    """Every current duplicate group across all four probes, as baseline keys."""
    keys: set[str] = set()
    for detail, members in _current_duplicates().items():
        keys.add(_baseline_key(detail, [m[0] for m in members]))
    for (name, value), members in _current_private_string_constants().items():
        keys.add(_constant_baseline_key(name, value, [m[0] for m in members]))
    for members in _current_block_bodies().values():
        keys.add(_block_baseline_key(members))
    for members in _current_near_bodies():
        keys.add(_near_body_baseline_key(members))
    return keys


_BASELINE_HEADER = (
    "# Duplicated-logic baseline (FAR-1965).\n"
    "#\n"
    "# Four shrink-only families, all defined in\n"
    "# test_duplicate_validation_predicates.py:\n"
    "#\n"
    '#   {"detail": "<message>", "modules": ["<relpath>", ...]}\n'
    "#     A validation-named function carrying a non-empty validation-error\n"
    "#     `detail` string that a validation-named function in a DIFFERENT module\n"
    "#     carries too: the same predicate written independently in more than one\n"
    "#     caller, so a widening in one copy silently leaves the other stale.\n"
    '#   {"constant": "<NAME>", "value": "<literal>", "modules": [...]}\n'
    "#     A private module-level string constant re-declared with the same name\n"
    "#     and value in a second module: the same contract written twice.\n"
    '#   {"block": ["<module>:<function>", ...]}\n'
    "#     A statement block that is an exact copy of a function's complete body\n"
    "#     in another module: a helper inlined or copied instead of called.\n"
    '#   {"near_body": ["<module>:<function>", ...]}\n'
    "#     Two near-identical (not identical) function bodies in different modules\n"
    "#     that are not per-entity scaffolding: one rule copied and then edited, so\n"
    "#     the copies have already drifted. Exact copies belong to the block family.\n"
    "#\n"
    "# The pluggable provider trees (connectors/**, model_backends/**,\n"
    "# core/error_tracking/forwarders/**) are out of scope by design.\n"
    "#\n"
    "# This list can only SHRINK:\n"
    "#   * test_no_duplicate_validation_predicates,\n"
    "#     test_no_duplicated_private_string_constants and\n"
    "#     test_no_duplicated_block_bodies each fail on a group NOT listed here\n"
    "#     (a NEW duplicate), and\n"
    "#   * test_duplicate_validation_predicate_baseline_has_no_stale_entries fails\n"
    "#     when a listed group is untangled but not removed from the baseline.\n"
    "#\n"
    "# Regenerate after untangling a group:\n"
    "#   cd backend && uv run python scripts/update_duplicate_validation_predicate_baseline.py\n"
)


def _read_baseline() -> set[str]:
    return {
        stripped
        for raw in BASELINE_PATH.read_text(encoding="utf-8").splitlines()
        if (stripped := raw.strip()) and not stripped.startswith("#")
    }


def _render_baseline(keys: set[str]) -> str:
    return _BASELINE_HEADER + "".join(f"{key}\n" for key in sorted(keys))


def _format_offenders(duplicates: dict[str, list[tuple[str, str, int]]], baseline: set[str]) -> list[str]:
    offenders: list[str] = []
    for detail, members in sorted(duplicates.items()):
        if _baseline_key(detail, [m[0] for m in members]) in baseline:
            continue
        where = "\n".join(f"      {module}:{lineno} {name}" for module, name, lineno in sorted(members))
        offenders.append(f"  detail {detail!r} carried by validation predicates in:\n{where}")
    return offenders


def _format_constant_offenders(
    constants: dict[tuple[str, str], list[tuple[str, str, int]]], baseline: set[str]
) -> list[str]:
    offenders: list[str] = []
    for (name, value), members in sorted(constants.items()):
        if _constant_baseline_key(name, value, [m[0] for m in members]) in baseline:
            continue
        where = "\n".join(f"      {module}:{lineno} {member_name}" for module, member_name, lineno in sorted(members))
        offenders.append(f"  constant {name} = {value!r} re-declared in:\n{where}")
    return offenders


def _format_block_offenders(blocks: dict[tuple[str, ...], set[tuple[str, str]]], baseline: set[str]) -> list[str]:
    offenders: list[str] = []
    for members in sorted(blocks.values(), key=lambda m: sorted(m)):
        if _block_baseline_key(members) in baseline:
            continue
        where = "\n".join(f"      {module}:{name}" for module, name in sorted(members))
        offenders.append(f"  a statement block copied verbatim from a helper in another module:\n{where}")
    return offenders


def _format_near_body_offenders(groups: dict[frozenset[tuple[str, str]], float], baseline: set[str]) -> list[str]:
    offenders: list[str] = []
    for members, similarity in sorted(groups.items(), key=lambda item: sorted(item[0])):
        if _near_body_baseline_key(members) in baseline:
            continue
        where = "\n".join(f"      {module}:{name}" for module, name in sorted(members))
        offenders.append(f"  near-identical bodies (similarity {similarity:.3f}):\n{where}")
    return offenders


# ---------------------------------------------------------------------------
# Gates
# ---------------------------------------------------------------------------


def test_no_duplicate_validation_predicates() -> None:
    """A validation predicate carrying the same user-facing ``detail`` message
    in two different modules is the same check written twice. Untangle it —
    delegate both callers to one shared helper — or, if the shared message is
    genuinely intentional, add the group to the shrink-only baseline with a
    reason. A NEW duplicate fails here until that decision is made."""
    baseline = _read_baseline()
    offenders = _format_offenders(_current_duplicates(), baseline)
    assert not offenders, (
        f"Found {len(offenders)} duplicated validation predicate(s) outside the\n"
        "duplicate_validation_predicate_baseline.txt ratchet.\n"
        "The same validation-error detail message is emitted from a validation predicate in\n"
        "more than one module: extract one shared helper (or a shared constant) and delegate\n"
        "to it, so a widening in one place cannot leave the others stale.\n\n" + "\n\n".join(offenders)
    )


def test_no_duplicated_private_string_constants() -> None:
    """A private module-level string constant re-declared with the same name and
    value in another module is the same contract written twice. Extract one
    shared constant and import it, or freeze the group in the shrink-only
    baseline with a reason."""
    baseline = _read_baseline()
    offenders = _format_constant_offenders(_current_private_string_constants(), baseline)
    assert not offenders, (
        f"Found {len(offenders)} duplicated private string constant(s) outside the\n"
        "duplicate_validation_predicate_baseline.txt ratchet.\n"
        "The same name and string value are declared in a module-level constant in more\n"
        "than one module: import one shared constant instead, so a change in one place\n"
        "cannot leave the others stale.\n\n" + "\n\n".join(offenders)
    )


def test_no_duplicated_block_bodies() -> None:
    """A statement block that is an exact copy of a function's complete body in
    another module is that helper inlined or copied instead of called. Call the
    helper (or extract a shared one), or freeze the group in the shrink-only
    baseline with a reason."""
    baseline = _read_baseline()
    offenders = _format_block_offenders(_current_block_bodies(), baseline)
    assert not offenders, (
        f"Found {len(offenders)} duplicated statement block(s) outside the\n"
        "duplicate_validation_predicate_baseline.txt ratchet.\n"
        "A contiguous block of statements is a verbatim copy of a function's complete body\n"
        "in a different module: call the helper, or extract a shared one, so the two copies\n"
        "cannot drift apart.\n\n" + "\n\n".join(offenders)
    )


def test_no_duplicated_near_bodies() -> None:
    """Two near-identical function bodies in different modules that are not
    per-entity scaffolding are one rule copied and then edited: the copies have
    already started to drift, so a fix in one silently leaves the other stale.
    Delegate to one shared helper, or freeze the group in the shrink-only
    baseline with a reason. A NEW near-copy fails here until that decision is
    made."""
    baseline = _read_baseline()
    offenders = _format_near_body_offenders(_current_near_bodies(), baseline)
    assert not offenders, (
        f"Found {len(offenders)} near-identical body duplication group(s) outside the\n"
        "duplicate_validation_predicate_baseline.txt ratchet.\n"
        "Two function bodies in different modules are near-identical (>= 0.90) but not\n"
        "identical, and they are not per-entity scaffolding (disjoint ORM models / same-package\n"
        "CRUD siblings / protocol implementations). One rule has been copied and edited, so the\n"
        "copies can drift apart: extract one shared helper and delegate to it.\n\n" + "\n\n".join(offenders)
    )


def test_duplicate_validation_predicate_baseline_has_no_stale_entries() -> None:
    """A group listed in the baseline but no longer duplicated (untangled or
    removed) must leave the baseline: the ratchet only shrinks, so a completed
    sweep is recorded and never silently re-opens."""
    stale = _read_baseline() - _current_baseline_keys()
    assert not stale, (
        f"{len(stale)} duplicate-logic baseline entries are no longer duplicates - regenerate\n"
        "the baseline with `cd backend && uv run python scripts/update_duplicate_validation_predicate_baseline.py`:\n  "
        + "\n  ".join(sorted(stale))
    )


def test_duplicate_validation_predicate_baseline_is_sorted_and_regenerable() -> None:
    """The baseline must be byte-stable (fixed header + sorted entries) so
    regeneration is idempotent and review diffs stay minimal."""
    current = BASELINE_PATH.read_text(encoding="utf-8")
    assert current == _render_baseline(_read_baseline()), (
        "the duplicate-logic baseline is not in canonical form - regenerate it with "
        "`cd backend && uv run python scripts/update_duplicate_validation_predicate_baseline.py`"
    )


def test_block_baseline_group_keys_are_unique() -> None:
    """The block family keys a group by its member set, so two distinct blocks
    occurring in exactly the same set of functions would collide and silently
    freeze each other. Assert no such collision exists on the real tree."""
    groups = _current_block_bodies()
    keys = [_block_baseline_key(members) for members in groups.values()]
    assert len(keys) == len(set(keys)), "two distinct block groups share a baseline key"


# ---------------------------------------------------------------------------
# Enumerator guards — a scan that silently matched nothing would pass vacuously
# ---------------------------------------------------------------------------


def test_scan_finds_the_known_live_duplicates() -> None:
    """The detail in this assertion is the live ``_parse_future_expires_at``
    duplication the FAR-1965 investigation started from; it must still be seen
    by the scan."""
    duplicates = _current_duplicates()
    asserting = duplicates.get("expires_at must be in the future", [])
    modules = {module for module, _name, _lineno in asserting}
    assert {"api/routes/api_keys.py", "api/routes/service_accounts.py"} <= modules


def test_scan_finds_the_known_live_constants() -> None:
    """Guard probe 2: the permission code ``cost.manage`` is re-declared in
    several route modules and must still be seen by the scan."""
    constants = _current_private_string_constants()
    members = constants.get(("_CODE_COST_MANAGE", "cost.manage"), [])
    modules = {module for module, _name, _lineno in members}
    assert {"api/mcp_server.py", "api/routes/costs.py"} <= modules


def test_scan_finds_the_known_live_block_copy() -> None:
    """Guard probe 3: the REST<->MCP parity copy of the expires-at parser
    (``api/mcp_server.py`` / ``api/routes/api_keys.py``) must still be seen."""
    found = any(
        ("api/mcp_server.py", "_parse_api_key_expires_at") in members
        and ("api/routes/api_keys.py", "_parse_expires_at") in members
        for members in _current_block_bodies().values()
    )
    assert found, "the live expires-at body duplication was not detected"


def test_scan_finds_the_known_live_near_copies() -> None:
    """Guard probe 4: the genuine near-copies the shape was measured on —
    ``resolve_bin_dir``/``_resolve_pg_bin_dir`` and the two
    ``core/cleanup_jobs/`` functions — must still be seen."""
    members = set().union(*_current_near_bodies())
    assert {("launcher/entry.py", "resolve_bin_dir"), ("launcher/upgrade.py", "_resolve_pg_bin_dir")} <= members
    assert {
        ("core/cleanup_jobs/trigger_events_cleanup.py", "cleanup_old_trigger_events"),
        ("core/cleanup_jobs/webhook_dedup_cleanup.py", "cleanup_old_webhook_events"),
    } <= members


def test_near_body_lens_rejects_entity_scaffolding_on_the_real_tree() -> None:
    """Guard the subject discriminator: the measured per-entity scaffolding
    pairs must stay out of the admitted set, or the gate would drown in CRUD
    noise. A regression in the discriminator turns this red even while the
    baseline would happily freeze the noise."""
    members = set().union(*_current_near_bodies())
    scaffolding = {
        ("db/crud/run.py", "get_run"),
        ("db/crud/mcp_server.py", "get_mcp_server"),
        ("db/crud/external_agent.py", "list_external_agents"),
        ("db/crud/mcp_server.py", "list_mcp_servers"),
        ("db/crud/pipeline_folder.py", "update_folder"),
        ("db/crud/schema_folder.py", "update_folder"),
        ("core/runtime_provider/docker.py", "supports"),
        ("core/runtime_provider/e2b.py", "supports"),
    }
    leaked = members & scaffolding
    assert not leaked, f"per-entity scaffolding leaked into the near-body lens: {leaked}"


def test_provider_plugin_trees_are_excluded() -> None:
    """The scope exclusion is load-bearing: the connector/model-backend/
    error-forwarder trees repeat protocol helpers and constants by design, and
    scanning them would make the gate fire on every new provider. Prove the
    scan drops them, so the exclusion cannot silently widen to swallow the
    whole tree."""
    scanned = {module for module, _source in _tree_sources()}
    assert not {module for module in scanned if module.startswith(_SKIP_PREFIXES)}, (
        "the provider-plugin trees leaked into the scanned set"
    )


# ---------------------------------------------------------------------------
# Synthetic positive/negative controls for each lens
# ---------------------------------------------------------------------------


def test_duplicate_validation_predicate_lens_flags_copies() -> None:
    """Synthetic positive/negative control for the lens: it must flag the same
    validation-named predicate carrying the same non-empty detail in two
    modules, and must ignore a distinct detail, a distinct name, a single
    module, the empty-string sentinel, and a non-validation name."""
    positive = [
        (
            "api/routes/a.py",
            "def _parse_future_expires_at(v):\n"
            '    raise HTTPException(status_code=422, detail="expires_at must be in the future")\n',
        ),
        (
            "api/routes/b.py",
            "def _parse_future_expires_at(v):\n"
            '    raise HTTPException(status_code=422, detail="expires_at must be in the future")\n',
        ),
    ]
    assert _duplicate_validation_predicates_in(positive), "lens should flag identical detail in two modules"

    mcp_shape = [
        ("api/mcp_server.py", "def _validate_role(r):\n    return None, {'detail': 'role is invalid', 'error': 'x'}\n"),
        (
            "api/routes/c.py",
            "def _validate_role(r):\n    raise HTTPException(status_code=422, detail='role is invalid')\n",
        ),
    ]
    assert _duplicate_validation_predicates_in(mcp_shape), "lens should flag the dict-envelope MCP shape"

    different_names = [
        # The same message from two DIFFERENTLY-named validation predicates is
        # the live `_validate_cron_update` / `_validated_next_fire` shape: the
        # predicate is identified by the message, not by a shared name.
        (
            "api/mcp_server.py",
            "def _validate_cron_update(c):\n    raise HTTPException(detail='Cron expression is required')\n",
        ),
        (
            "api/routes/triggers.py",
            "def _validated_next_fire(c):\n    raise HTTPException(detail='Cron expression is required')\n",
        ),
    ]
    assert _duplicate_validation_predicates_in(different_names), "lens should flag same detail under different names"

    negative = [
        [
            ("api/routes/a.py", "def _check(v):\n    raise HTTPException(detail='too small')\n"),
            ("api/routes/b.py", "def _check(v):\n    raise HTTPException(detail='too large')\n"),
        ],
        [
            # Same message, but only ONE side is a validation predicate: the
            # other is an ordinary helper, so this is not a duplicated
            # *validation* predicate.
            ("api/routes/a.py", "def _check(v):\n    raise HTTPException(detail='same')\n"),
            ("api/routes/b.py", "def do_work(v):\n    raise HTTPException(detail='same')\n"),
        ],
        [
            ("api/routes/a.py", "def _check(v):\n    raise HTTPException(detail='same')\n"),
            ("api/routes/a.py", "def _check_again(v):\n    raise HTTPException(detail='same')\n"),
        ],
        [
            ("api/routes/a.py", 'def _parse_json(v):\n    return {"detail": "", "data": v}\n'),
            ("api/routes/b.py", 'def _parse_json(v):\n    return {"detail": "", "data": v}\n'),
        ],
        [
            ("api/routes/a.py", "def plain_helper(v):\n    raise HTTPException(detail='same message')\n"),
            ("api/routes/b.py", "def other_helper(v):\n    raise HTTPException(detail='same message')\n"),
        ],
    ]
    for sources in negative:
        assert not _duplicate_validation_predicates_in(sources), f"lens should NOT flag:\n{sources}"


def test_duplicate_private_constant_lens_flags_copies() -> None:
    """Synthetic positive/negative control for probe 2: it must flag a private
    string constant declared with the same name and value in two modules, and
    must ignore a different value, a different name, a public constant, a
    non-string value, the empty-string sentinel and a single module."""
    positive = [
        ("core/one.py", '_PERMISSION_CODE = "cost.manage"\n'),
        ("api/two.py", '_PERMISSION_CODE = "cost.manage"\n'),
    ]
    assert _duplicate_private_constants_in(positive), "lens should flag the same private string constant in two modules"

    annotated = [
        ("core/one.py", '_MESSAGE: str = "Database unavailable."\n'),
        ("api/two.py", '_MESSAGE: str = "Database unavailable."\n'),
    ]
    assert _duplicate_private_constants_in(annotated), "lens should flag the AnnAssign spelling"

    negative = [
        [("core/one.py", '_CODE = "cost.manage"\n'), ("api/two.py", '_CODE = "cost.other"\n')],
        [("core/one.py", '_CODE = "cost.manage"\n'), ("api/two.py", '_OTHER = "cost.manage"\n')],
        [("core/one.py", '_CODE = "cost.manage"\n'), ("api/two.py", 'CODE = "cost.manage"\n')],
        [("core/one.py", "_MAX = 3\n"), ("api/two.py", "_MAX = 3\n")],
        [("core/one.py", '_EMPTY = ""\n'), ("api/two.py", '_EMPTY = ""\n')],
        [("core/one.py", '_CODE = "cost.manage"\n')],
        # Same value and name, but only one is module-level: the other is a
        # local inside a function, which is not a module constant.
        [("core/one.py", '_CODE = "cost.manage"\n'), ("api/two.py", 'def f():\n    _CODE = "cost.manage"\n')],
    ]
    for sources in negative:
        assert not _duplicate_private_constants_in(sources), f"lens should NOT flag:\n{sources}"


def test_duplicate_block_lens_flags_copies() -> None:
    """Synthetic positive/negative control for probe 3: it must flag a block
    inlined from a helper in another module, and must ignore a near-copy that
    differs by an identifier, a same-module copy, a below-threshold fragment
    and a block that no function defines as its complete body."""
    helper = "def helper(value):\n    text = value.strip()\n    if text:\n        return text\n    return None\n"
    inlined = (
        "def caller(value):\n"
        "    prefix = value[:1]\n"
        "    text = value.strip()\n"
        "    if text:\n"
        "        return text\n"
        "    return None\n"
    )
    positive = [("core/one.py", helper), ("api/two.py", inlined)]
    assert _duplicate_block_bodies_in(positive), "lens should flag a helper body inlined into another module"

    whole_body_copy = [
        ("core/one.py", helper),
        (
            "api/two.py",
            "def other(value):\n    text = value.strip()\n    if text:\n        return text\n    return None\n",
        ),
    ]
    assert _duplicate_block_bodies_in(whole_body_copy), "lens should flag a whole-body copy under a different name"

    negative = [
        # Near-copy, not a copy: one identifier differs.
        [("core/one.py", helper), ("api/two.py", inlined.replace("text = value.strip()", "text = value.rstrip()"))],
        # Same module: cross-module duplication is the gate's scope.
        [("core/one.py", helper), ("core/one.py", inlined)],
        # Fragment shorter than the minimum block.
        [
            ("core/one.py", helper),
            ("api/two.py", "def caller(value):\n    prefix = value[:1]\n    return prefix\n"),
        ],
        # A duplicated fragment that no function defines as a complete body is
        # not an inlined *helper*.
        [
            ("core/one.py", "def a(value):\n    x = value.strip()\n    y = x.lower()\n    return y\n"),
            ("api/two.py", "def b(value):\n    x = value.strip()\n    y = x.lower()\n    z = 1\n    return z\n"),
        ],
    ]
    for sources in negative:
        assert not _duplicate_block_bodies_in(sources), f"lens should NOT flag:\n{sources}"


def test_near_body_lens_flags_drifted_copies() -> None:
    """Synthetic positive/negative control for probe 4.

    It must flag a near-identical body that has drifted (a different message
    and exception type) in another module, and must ignore the measured
    scaffolding shapes: disjoint ORM subjects, same-package CRUD siblings with
    no model, same-named protocol methods sharing a base, a below-threshold
    pair, a same-module pair, and an exact copy (probe 3's shape).
    """
    guard = (
        "def _safe_path(path):\n"
        "    resolved = os.path.realpath(path)\n"
        "    base = os.path.realpath(Path.cwd())\n"
        "    if resolved != base and not resolved.startswith(base + os.sep):\n"
        "        raise ValueError(f'path {path!r} is outside the working directory')\n"
        "    return resolved\n"
    )
    drifted = guard.replace("_safe_path", "_safe_config_path").replace(
        "raise ValueError(f'path", "raise RuntimeError(f'config"
    )
    assert _near_body_groups_in([("core/one.py", guard), ("api/two.py", drifted)]), (
        "lens should flag a drifted near-copy in another module"
    )

    getters = [
        (
            "db/crud/run.py",
            "async def get_run(session, run_id):\n"
            "    stmt = select(Run).where(Run.id == run_id)\n"
            "    if run_id is None:\n"
            "        return None\n"
            "    result = await session.execute(stmt)\n"
            "    return result.scalar_one_or_none()\n",
        ),
        (
            "db/crud/mcp_server.py",
            "async def get_mcp_server(session, server_id):\n"
            "    stmt = select(McpServer).where(McpServer.id == server_id)\n"
            "    if server_id is None:\n"
            "        return None\n"
            "    result = await session.execute(stmt)\n"
            "    return result.scalar_one_or_none()\n",
        ),
    ]
    siblings = [
        (
            "db/crud/agent.py",
            "async def delete_agent(session, agent_id):\n"
            "    agent = await get_agent(session, agent_id)\n"
            "    if agent is None:\n"
            "        return False\n"
            "    await session.delete(agent)\n"
            "    await session.flush()\n"
            "    return True\n",
        ),
        (
            "db/crud/parameter_set.py",
            "async def delete_set(session, set_id):\n"
            "    ps = await get_set(session, set_id)\n"
            "    if ps is None:\n"
            "        return False\n"
            "    await session.delete(ps)\n"
            "    await session.flush()\n"
            "    return True\n",
        ),
    ]
    protocol = [
        (
            "core/runtime_provider/docker.py",
            "class DockerProvider(Provider):\n"
            "    def supports(self, profile):\n"
            "        hint = getattr(profile, 'provider_hint', None) or ''\n"
            "        if hint.lower() == 'docker':\n"
            "            return True\n"
            "        image_ref = getattr(profile, 'image_ref', None) or ''\n"
            "        return 'docker' in image_ref.lower()\n",
        ),
        (
            "core/runtime_provider/e2b.py",
            "class E2BProvider(Provider):\n"
            "    def supports(self, profile):\n"
            "        hint = getattr(profile, 'provider_hint', None) or ''\n"
            "        if hint.lower() == 'e2b':\n"
            "            return True\n"
            "        image_ref = getattr(profile, 'image_ref', None) or ''\n"
            "        return 'e2b' in image_ref.lower()\n",
        ),
    ]
    distinct = [
        (
            "core/one.py",
            "def validate_name(value):\n"
            "    text = value.strip()\n"
            "    if not text:\n"
            "        raise ValueError('empty')\n"
            "    return text\n",
        ),
        (
            "core/two.py",
            "def validate_age(value):\n"
            "    number = int(value)\n"
            "    if number < 1:\n"
            "        raise ValueError('too small')\n"
            "    return number\n",
        ),
    ]
    negative = [
        getters,
        siblings,
        protocol,
        distinct,
        # Same module: cross-module duplication is the gate's scope.
        [("core/one.py", guard + "\n\n" + drifted)],
        # Exact copy: probe 3's shape, deliberately not re-flagged here.
        [("core/one.py", guard), ("api/two.py", guard)],
    ]
    for sources in negative:
        assert not _near_body_groups_in(sources), f"lens should NOT flag:\n{sources}"
