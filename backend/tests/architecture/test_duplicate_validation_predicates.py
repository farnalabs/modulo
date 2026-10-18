"""Architecture gate: no NEW duplicated validation predicate across modules (FAR-1965).

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

The predicate
-------------
A function is a *validation predicate* when its **name** says it validates
(matches ``_VALIDATION_NAME``) **and** its body carries a non-empty,
user-facing validation error ``detail`` string — either a ``detail=`` keyword
(a raised ``HTTPException``) or a ``"detail":`` key in a returned dict (the MCP
error envelope). Two such predicates are a **duplicate** when they are defined
in **different modules** and carry at least one **identical** ``detail``
string.

The shared, exact, user-facing message is the mechanical proof that the same
check was written twice: it is the observable contract, and it is exactly what
drifts when one copy is widened (`"expires_at must be in the future"` gaining a
tolerance in one caller but not the other). Matching on the name *alone* would
drown in the connector plugins (every provider implements ``health_check`` by
design); matching on the message *alone* would drown in generic repeat
messages (``"Not found"``, ``"Database temporarily unavailable."``). Name **and**
message together is the narrow predicate that catches the observed shape with
no false positives on current main.

Scope
-----
``backend/src/modulo/**`` except:

* ``connectors/**`` and ``model_backends/**`` — pluggable provider trees. Each
  provider is deliberately a self-contained implementation of the same
  protocol, so a per-provider helper (``_has_server_delay``, ``_parse_retry_after``)
  repeating its sibling's shape is a design choice, not drift. Scanning them
  would make the gate fire on every new provider.
* ``migrations/**`` — historical, immutable by policy.

This is a SHRINK-ONLY ratchet, the same shape as the duplicate test-body
baseline (``duplicate_test_body_baseline.txt``): the pre-existing duplicates are
frozen in ``duplicate_validation_predicate_baseline.txt`` so this gate blocks
NEW duplicates immediately while the backlog is untangled; a frozen entry that
is no longer a duplicate must leave the baseline or the stale-entry check fails.

Documented limitations
----------------------
* AST-based, not similarity-based: it catches the *same message written twice*,
  not two predicates that have drifted to different messages. The drift case is
  the downstream consequence of the duplication this gate blocks at birth.
* ``detail`` extraction follows the two shapes the codebase actually uses
  (``detail=`` kwarg, ``"detail":`` dict key). A validator that surfaces its
  message through a different channel (a constant, an f-string prefix) is not
  scanned; the f-string forms the codebase uses are per-call-site and are not
  the drift surface.
* Baseline keys include the defining modules, so a third module joining an
  already-frozen message is a NEW key and fails — the ratchet does not go blind
  once a message is listed.
"""

from __future__ import annotations

import ast
import json
import re
from collections import defaultdict
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[2]
SRC_DIR = BACKEND_DIR / "src" / "modulo"
BASELINE_PATH = Path(__file__).resolve().parent / "duplicate_validation_predicate_baseline.txt"

#: Trees scanned. The pluggable provider trees are excluded (see the module
#: docstring): their per-provider helpers repeat by design.
_SKIP_PREFIXES = ("connectors/", "model_backends/")
#: Any path part that marks a module as out of scope.
_SKIP_PARTS = frozenset({"migrations"})

#: What makes a function a validation *predicate*: its name says so. Kept
#: deliberately broad on the verb family — the message half of the predicate is
#: what does the precision work.
_VALIDATION_NAME = re.compile(
    r"valid|check|verify|_is_|_has_|ensure|enforce|assert|require|_parse|parse_|coerce|canonical",
    re.IGNORECASE,
)


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


def _tree_sources() -> list[tuple[str, str]]:
    """Every in-scope ``(relpath, source)`` under ``src/modulo``."""
    sources: list[tuple[str, str]] = []
    for path in sorted(SRC_DIR.rglob("*.py")):
        rel = path.relative_to(SRC_DIR).as_posix()
        if rel.startswith(_SKIP_PREFIXES) or _SKIP_PARTS & set(path.relative_to(SRC_DIR).parts):
            continue
        sources.append((rel, path.read_text(encoding="utf-8")))
    return sources


def _current_duplicates() -> dict[str, list[tuple[str, str, int]]]:
    return _duplicate_validation_predicates_in(_tree_sources())


def _baseline_key(detail: str, modules: list[str]) -> str:
    """Canonical baseline entry for one duplicate group.

    JSON-encoded so the key is unambiguous whatever characters the detail
    message contains, and stable because the module list is sorted and the
    object keys are sorted.
    """
    return json.dumps({"detail": detail, "modules": sorted(set(modules))}, sort_keys=True)


def _current_baseline_keys() -> set[str]:
    """Every current duplicate group, as baseline keys."""
    keys: set[str] = set()
    for detail, members in _current_duplicates().items():
        keys.add(_baseline_key(detail, [m[0] for m in members]))
    return keys


_BASELINE_HEADER = (
    "# Duplicate-validation-predicate baseline (FAR-1965).\n"
    "#\n"
    "# A validation-named function (see _VALIDATION_NAME in\n"
    "# test_duplicate_validation_predicates.py) that carries a non-empty\n"
    "# validation-error `detail` string which a validation-named function in a\n"
    "# DIFFERENT module carries too: the same predicate written independently in\n"
    "# more than one caller, so a widening in one copy silently leaves the other\n"
    "# stale. The pluggable provider trees (connectors/**, model_backends/**) are\n"
    "# out of scope by design.\n"
    "#\n"
    "# Entry format (one JSON object per line, sorted):\n"
    '#   {"detail": "<message>", "modules": ["<relpath>", ...]}\n'
    "#\n"
    "# This list can only SHRINK:\n"
    "#   * test_no_duplicate_validation_predicates fails on any group NOT listed\n"
    "#     here (a NEW duplicate), and\n"
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


def test_duplicate_validation_predicate_baseline_has_no_stale_entries() -> None:
    """A group listed in the baseline but no longer duplicated (untangled or
    removed) must leave the baseline: the ratchet only shrinks, so a completed
    sweep is recorded and never silently re-opens."""
    stale = _read_baseline() - _current_baseline_keys()
    assert not stale, (
        f"{len(stale)} duplicate-validation baseline entries are no longer duplicates - regenerate\n"
        "the baseline with `cd backend && uv run python scripts/update_duplicate_validation_predicate_baseline.py`:\n  "
        + "\n  ".join(sorted(stale))
    )


def test_duplicate_validation_predicate_baseline_is_sorted_and_regenerable() -> None:
    """The baseline must be byte-stable (fixed header + sorted entries) so
    regeneration is idempotent and review diffs stay minimal."""
    current = BASELINE_PATH.read_text(encoding="utf-8")
    assert current == _render_baseline(_read_baseline()), (
        "the duplicate-validation baseline is not in canonical form - regenerate it with "
        "`cd backend && uv run python scripts/update_duplicate_validation_predicate_baseline.py`"
    )


def test_scan_finds_the_known_live_duplicates() -> None:
    """Guard the enumerator itself: a scan that silently matched nothing would
    make every other gate pass vacuously. The detail in this assertion is the
    live ``_parse_future_expires_at`` duplication the FAR-1965 investigation
    started from; it must still be seen by the scan."""
    duplicates = _current_duplicates()
    asserting = duplicates.get("expires_at must be in the future", [])
    modules = {module for module, _name, _lineno in asserting}
    assert {"api/routes/api_keys.py", "api/routes/service_accounts.py"} <= modules


def test_provider_plugin_trees_are_excluded() -> None:
    """The scope exclusion is load-bearing: the connector/model-backend trees
    repeat protocol helpers by design, and scanning them would make the gate
    fire on every new provider. Prove the scan drops them, so the exclusion
    cannot silently widen to swallow the whole tree."""
    scanned = {module for module, _source in _tree_sources()}
    assert not {module for module in scanned if module.startswith(_SKIP_PREFIXES)}, (
        "the provider-plugin trees leaked into the scanned set"
    )


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
