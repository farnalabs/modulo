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

This module ships TWO probes for two distinct observed shapes of that failure
mode, plus a recorded refusal for a third. All three share one shrink-only
baseline (``duplicate_validation_predicate_baseline.txt``) so a sweep can never
silently re-open:

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

Rejected probe (recorded so the measurement is not re-learned)
-------------------------------------------------------------
**Partial-body duplication** — two function bodies that are *near*-identical
but not identical — was measured and refused: no similarity threshold separates
a genuine near-copy from unrelated boilerplate. On current main, character-level
``difflib`` similarity over normalised bodies (>= 4 statements) scores the
genuine near-copies ``launcher/entry.py:_resolve_bin_dir`` /
``launcher/upgrade.py:_resolve_pg_bin_dir`` (0.958) and the two
``core/cleanup_jobs/`` cleanup functions (0.98); dropping the threshold to 0.9
admits the unrelated CRUD getters ``db/crud/run.py:get_run`` /
``db/crud/mcp_server.py:get_mcp_server`` / ``db/crud/external_agent.py:get_external_agent``
(0.854-0.907), which are similar boilerplate over *different* entities. There is
no separation margin between the genuine class and the false-positive class — a
human must decide concept-identity, which the metric cannot see — so no probe is
shipped and nothing is frozen for this shape. (A coarse node-multiset variant is
worse: >= 0.98 already yields 174 candidate pairs.)

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
* Baseline block keys are the member ``module:function`` set, so a body edit
  that keeps the same functions duplicated stays frozen (by design) and a
  function rename surfaces as a new group.
"""

from __future__ import annotations

import ast
import json
import re
from collections import defaultdict
from functools import cache
from pathlib import Path

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


# ---------------------------------------------------------------------------
# Shared enumeration
# ---------------------------------------------------------------------------


def _tree_sources() -> list[tuple[str, str]]:
    """Every in-scope ``(relpath, source)`` under ``src/modulo``."""
    sources: list[tuple[str, str]] = []
    for path in sorted(SRC_DIR.rglob("*.py")):
        rel = path.relative_to(SRC_DIR).as_posix()
        if rel.startswith(_SKIP_PREFIXES) or _SKIP_PARTS & set(path.relative_to(SRC_DIR).parts):
            continue
        sources.append((rel, path.read_text(encoding="utf-8")))
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


@cache
def _current_baseline_keys() -> set[str]:
    """Every current duplicate group across all three probes, as baseline keys."""
    keys: set[str] = set()
    for detail, members in _current_duplicates().items():
        keys.add(_baseline_key(detail, [m[0] for m in members]))
    for (name, value), members in _current_private_string_constants().items():
        keys.add(_constant_baseline_key(name, value, [m[0] for m in members]))
    for members in _current_block_bodies().values():
        keys.add(_block_baseline_key(members))
    return keys


_BASELINE_HEADER = (
    "# Duplicated-logic baseline (FAR-1965).\n"
    "#\n"
    "# Three shrink-only families, all defined in\n"
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
