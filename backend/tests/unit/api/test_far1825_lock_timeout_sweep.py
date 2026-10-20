"""FAR-1825 structural guard: every swept category-C CRUD lock stays bounded.

The FAR-1801 lock-point inventory (``docs/architecture/lock-point-inventory.md``)
enumerates the row locks in ``backend/src/modulo``. Its §3 category **C** (entity
CRUD) sites run in a CALLER-owned transaction, so they are only as bounded as
their least careful caller. FAR-1825 swept the sites a request path can reach
and gave each one a transaction-scoped ``set_mutation_row_lock_timeout`` (the
``SET LOCAL lock_timeout`` in ``db/crud/row_lock.py``) before its first lock, so
a contended row raises SQLSTATE 55P03 (mapped to a visible 409) instead of
parking a pooled connection indefinitely.

This test is an **AST scanner** over the source (not a text scanner): a bound or
lock named only in a comment/docstring cannot satisfy it, and an import alias is
resolved. It re-derives the category-C disposition for every site rather than
trusting a prose list, and fails if any swept site loses its bound, if a
category-C file gains/loses a row-lock call, if a NEW file outside the known
inventory gains a row-lock call, if a JSON/API route DIRECTLY calling a swept
helper stops classifying 55P03 (the caller set is derived from the route
module's imports, NOT from a hand-maintained arm list, so a new caller cannot be
missed by forgetting to enumerate it), if an enumerated arm stops classifying,
or if a deliberately-unbounded site becomes reachable from a request path.

The mapping guard's coverage is stated precisely: it proves the classifier is
present on every ENUMERATED arm and on every route function that DIRECTLY calls
a swept helper. It does NOT prove it on a **transitive service-level caller** —
see the residuals below.

Known residuals (accepted and recorded, not claimed closed):

* **Transitive service callers are out of the derived check.** A route that
  reaches a bounded lock only through an intermediate service function is not
  discovered. The concrete case is
  ``api/routes/library.py::create_lifecycle_map_from_primitive_endpoint``: it
  reaches C20 via ``core/lifecycle_map/import_export.py::
  materialize_map_from_primitive`` → ``save_map_version`` →
  ``get_lifecycle_map_for_update``, and its local ``except SQLAlchemyError`` arm
  still maps to 503. The lock is on a row INSERTED IN THE SAME TRANSACTION (the
  copy path creates the map, then saves its first version), so no other
  transaction can hold it and 55P03 is not practically reachable; the arm is an
  explicit, pinned exception
  (``test_library_materialise_arm_is_a_recorded_transitive_residual``) rather
  than a conversion.
* A NEW transitive helper that calls a row lock from a file not in
  ``_ROW_LOCK_FILES`` fails the repo-wide file-set assertion, so the inventory
  and this guard must be updated in the same change.
* ``set_parent_node``'s no-caller check is alias-aware for the direct
  ``from ... import`` / module-attribute shapes; a call reached through a
  hand-built string or dynamic ``getattr`` is out of scope.
"""

from __future__ import annotations

import ast
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession

_BACKEND_ROOT = Path(__file__).resolve().parents[3]
_SRC = _BACKEND_ROOT / "src" / "modulo"
_BOUND_FUNC = "set_mutation_row_lock_timeout"
_LOCK_ATTR = "with_for_update"
_LOCK_KWARG = "with_for_update"

# Every category-C site's row-lock call count, by file. The sum is the §3
# category-C total (20 SQLAlchemy sites). A count drift is a sweep failure:
# either a new lock needs a bound (and an inventory row) or a lock was removed.
_C_SITE_LOCK_COUNTS: dict[str, int] = {
    "api/routes/model_backends.py": 1,  # C4 create_model_backend_endpoint
    "db/crud/organisation.py": 2,  # C5 get_organisation(for_update) + C6 generic fallback
    "db/crud/parameter_set.py": 1,  # C7 update_set
    "db/crud/account.py": 1,  # C8 _get_account_locked
    "db/crud/node_observation.py": 1,  # C9 observe_node
    "db/crud/node_composite.py": 2,  # C10/C11 set_parent_node (no production caller)
    "db/crud/sso_provider.py": 2,  # C12 create_provider + C13 update_provider
    "db/crud/parameter_schema.py": 1,  # C14 update_schema
    "db/crud/agent.py": 2,  # C15 add_prompt_version + C16 rollback_prompt_version
    "db/crud/invitations.py": 2,  # C17 get_live_for_email + C18 get_valid_by_token_hash
    "db/crud/agent_runner_binding.py": 1,  # C19 replace_agent_bindings
    "core/lifecycle_map/service.py": 1,  # C20 get_lifecycle_map_for_update
    "core/workflow_import_export/__init__.py": 1,  # C21 _get_existing_names (opt-in, unused)
    "core/assistant/context_source_service.py": 2,  # C22/C23 _upsert_context_source
}

# EVERY file under src/modulo that takes a row lock (AST-detected: a
# ``.with_for_update(...)`` call or a ``with_for_update=`` keyword), across all
# categories. This is the inventory allowlist: a NEW file that takes a row lock
# must be added here AND given an inventory row (and, if category C, a bound).
# Raw-SQL ``FOR UPDATE`` sites are out of this scan by design (they are the two
# §4 sites, and sql_text never matches the AST shapes above).
_ROW_LOCK_FILES: frozenset[str] = frozenset(
    {
        "api/mcp_server.py",
        "api/routes/api_keys.py",
        "api/routes/model_backends.py",
        "api/routes/pipelines.py",
        "api/routes/product_analytics_identity.py",
        "api/routes/triggers.py",
        "auth/api_key.py",
        "auth/oauth.py",
        "core/assistant/context_source_service.py",
        "core/audit_logger/__init__.py",
        "core/cost_controller/__init__.py",
        "core/cost_controller/finalize.py",
        "core/cron_helpers.py",
        "core/hitl_manager/__init__.py",
        "core/lifecycle_map/service.py",
        "core/mcp_setup_handoff/__init__.py",
        "core/pipeline_engine/classify.py",
        "core/pipeline_engine/node_runner.py",
        "core/pipeline_engine/recovery.py",
        "core/reports/scheduler.py",
        "core/secrets_backend/fernet.py",
        "core/workflow_import_export/__init__.py",
        "db/crud/account.py",
        "db/crud/agent.py",
        "db/crud/agent_runner_binding.py",
        "db/crud/daily_run_count.py",
        "db/crud/error_tracking.py",
        "db/crud/invitations.py",
        "db/crud/node_composite.py",
        "db/crud/node_observation.py",
        "db/crud/org_deletion.py",
        "db/crud/organisation.py",
        "db/crud/parameter_schema.py",
        "db/crud/parameter_set.py",
        "db/crud/pipeline.py",
        "db/crud/pipeline_snapshot.py",
        "db/crud/pipeline_snapshot_versioning.py",
        "db/crud/run.py",
        "db/crud/run_node_outputs.py",
        "db/crud/sso_provider.py",
        "db/crud/token_family.py",
        "db/crud/variant_group.py",
    }
)

# (file, function) pairs that must set the bound before their first lock.
# Each entry covers every lock inside that function (C12/C13 and C22/C23 are
# distinct sites in one bounded transaction; the multi-lock functions are
# listed once).
_BOUND_SITES: tuple[tuple[str, str], ...] = (
    ("api/routes/model_backends.py", "create_model_backend_endpoint"),
    ("db/crud/parameter_set.py", "update_set"),
    ("db/crud/account.py", "_get_account_locked"),
    ("db/crud/node_observation.py", "observe_node"),
    ("db/crud/sso_provider.py", "create_provider"),
    ("db/crud/sso_provider.py", "update_provider"),
    ("db/crud/parameter_schema.py", "update_schema"),
    ("db/crud/agent.py", "add_prompt_version"),
    ("db/crud/agent.py", "rollback_prompt_version"),
    ("db/crud/invitations.py", "get_live_for_email"),
    ("db/crud/invitations.py", "get_valid_by_token_hash"),
    ("db/crud/agent_runner_binding.py", "replace_agent_bindings"),
    ("core/lifecycle_map/service.py", "get_lifecycle_map_for_update"),
    ("core/assistant/context_source_service.py", "_upsert_context_source"),
)

# JSON/API route arms that front a now-bounded CRUD helper: a 55P03 reaching
# the arm's local ``except SQLAlchemyError`` must be classified as a 409
# conflict by ``raise_lock_timeout_conflict_if`` before the arm's own 503/500
# reply. Without it the busy-row conflict is reported as a database outage
# (FAR-1825 pre-PR gate finding 1). The bound itself is pinned by the
# category-C tests above; this pins the MAPPING at each reachable JSON/API
# surface. The browser-redirect SSO callback (``api/routes/sso.py``) is the ONE
# documented exception — it cannot deliver JSON and keeps
# ``provider_unavailable``; pinned separately below so it cannot drift.
_MAPPED_55P03_ROUTE_ARMS: tuple[tuple[str, str], ...] = (
    ("api/routes/model_backends.py", "create_model_backend_endpoint"),
    ("api/routes/parameter_schemas.py", "update_parameter_schema_endpoint"),
    ("api/routes/parameter_schemas.py", "update_parameter_set_endpoint"),
    ("api/routes/agents.py", "apply_optimized_prompt"),
    ("api/routes/agents.py", "rollback_prompt"),
    ("api/routes/agents.py", "replace_bindings_endpoint"),
    ("api/routes/admin_sso.py", "create_provider_endpoint"),
    ("api/routes/admin_sso.py", "update_provider_endpoint"),
    ("api/routes/lifecycle_maps.py", "import_lifecycle_map_endpoint"),
    ("api/routes/lifecycle_maps.py", "update_lifecycle_map_endpoint"),
    ("api/routes/lifecycle_maps.py", "update_lifecycle_map_version_endpoint"),
    ("api/routes/lifecycle_maps.py", "save_lifecycle_map_version_endpoint"),
    ("api/routes/lifecycle_maps.py", "graduate_lifecycle_map_stage_endpoint"),
    ("api/routes/runs.py", "observe_run_node"),
    ("api/routes/auth.py", "accept_invite"),
    ("api/routes/in_app_notifications.py", "update_preferences"),
    ("api/routes/admin_assistant.py", "set_org_context_source"),
)
_CLASSIFIER_NAME = "raise_lock_timeout_conflict_if"
# The browser-redirect SSO callback: the one reachable arm deliberately left on
# a generic mapping (see the pinned-exception test at the end of this module).
_SSO_CALLBACK_FUNCTION = "_run_sso_callback_process"

# Swept helpers a route can call DIRECTLY, keyed by the module that defines them.
# The bound lives inside the helper's caller-owned transaction, so the request
# arm that invoked it is where 55P03 must become a 409. Resolution is
# import-aware: ``api/routes/schemas.py`` also calls an ``update_schema``, but
# its symbol comes from ``db.crud.schema`` (not swept) — a bare token scan would
# falsely flag it, so the callee is resolved through the module's imports.
#
# The lifecycle-map and assistant entries are the SERVICE wrappers that call the
# bounded lock helper (``get_lifecycle_map_for_update`` /
# ``_upsert_context_source``) — a route calls those wrappers, so they are the
# route's direct sweep surface for C20/C22/C23.
_SWEPT_HELPER_MODULES: dict[str, frozenset[str]] = {
    "modulo.db.crud.account": frozenset({"_get_account_locked"}),
    "modulo.db.crud.agent": frozenset({"add_prompt_version", "rollback_prompt_version"}),
    "modulo.db.crud.agent_runner_binding": frozenset({"replace_agent_bindings"}),
    "modulo.db.crud.invitations": frozenset({"get_live_for_email", "get_valid_by_token_hash"}),
    "modulo.db.crud.node_observation": frozenset({"observe_node"}),
    "modulo.db.crud.parameter_schema": frozenset({"update_schema"}),
    "modulo.db.crud.parameter_set": frozenset({"update_set"}),
    "modulo.db.crud.sso_provider": frozenset({"create_provider", "update_provider"}),
    "modulo.core.lifecycle_map.service": frozenset({"update_lifecycle_map", "save_map_version", "graduate_stage"}),
    "modulo.core.assistant.context_source_service": frozenset({"set_user_override", "set_org_default"}),
}
_SWEPT_HELPER_NAMES: frozenset[str] = frozenset(name for names in _SWEPT_HELPER_MODULES.values() for name in names)
_HANDLE_DB_ERRORS_NAME = "handle_db_errors"

# The direct callers the discovery must keep seeing. Asserting a SUPERSET makes a
# stale/over-narrow discovery fail loudly instead of passing on an empty set —
# a discovery bug that silently finds nothing is the failure mode this guard
# exists to prevent.
_KNOWN_SWEPT_CALLERS: frozenset[tuple[str, str]] = frozenset(
    {
        ("api/routes/admin_assistant.py", "set_org_context_source"),
        ("api/routes/admin_sso.py", "create_provider_endpoint"),
        ("api/routes/admin_sso.py", "update_provider_endpoint"),
        ("api/routes/agents.py", "apply_optimized_prompt"),
        ("api/routes/agents.py", "rollback_prompt"),
        ("api/routes/agents.py", "replace_bindings_endpoint"),
        ("api/routes/auth.py", "accept_invite"),
        ("api/routes/lifecycle_maps.py", "graduate_lifecycle_map_stage_endpoint"),
        ("api/routes/lifecycle_maps.py", "save_lifecycle_map_version_endpoint"),
        ("api/routes/lifecycle_maps.py", "update_lifecycle_map_endpoint"),
        ("api/routes/lifecycle_maps.py", "update_lifecycle_map_version_endpoint"),
        ("api/routes/me.py", "set_user_context_source"),
        ("api/routes/parameter_schemas.py", "update_parameter_schema_endpoint"),
        ("api/routes/parameter_schemas.py", "update_parameter_set_endpoint"),
        ("api/routes/runs.py", "observe_run_node"),
    }
)

# (file, function) -> written reason. An entry opts a function OUT of the
# derived classifier check; the reason is part of the contract, and the pinned
# tests below assert each entry still matches the code it excuses.
_MAPPING_EXCEPTION_REASONS: dict[tuple[str, str], str] = {
    (
        "api/routes/sso.py",
        _SSO_CALLBACK_FUNCTION,
    ): (
        "browser-redirect surface: the IdP navigates the user here, so a JSON 409 cannot be "
        "delivered and no SSO contention error code exists; 55P03 stays provider_unavailable"
    ),
    (
        "api/routes/library.py",
        "create_lifecycle_map_from_primitive_endpoint",
    ): (
        "TRANSITIVE residual (not a direct caller): reaches C20 via materialize_map_from_primitive "
        "-> save_map_version -> get_lifecycle_map_for_update; the lock is on a row inserted in the "
        "SAME transaction, so 55P03 is not practically reachable and the 503 arm is accepted"
    ),
}

# C5/C6 are bounded one level up: the shared org JSON-column patch calls
# ``_take_settings_patch_lock_wait`` before dispatching to the locked generic
# fallback (the Postgres path never reaches a row lock at all).
_CALLER_BOUND_CALLER = ("db/crud/organisation.py", "update_org_json_column_patch")
_CALLER_BOUND_CALLEE = "db/crud/organisation.py"
_GENERIC_FALLBACK = "_apply_json_column_patch_generic"

# Deliberately unbounded because NO production request path reaches the lock.
_NO_PRODUCTION_CALLER = (
    ("db/crud/node_composite.py", "set_parent_node"),
    ("core/workflow_import_export/__init__.py", "_get_existing_names"),
)
_NODE_COMPOSITE_MODULE = "modulo.db.crud.node_composite"
_SET_PARENT_SYMBOL = "set_parent_node"


def _read(rel: str) -> str:
    return (_SRC / rel).read_text(encoding="utf-8")


def _parse(rel: str) -> ast.Module:
    return ast.parse(_read(rel))


def _function_node(module: ast.Module, function_name: str) -> ast.FunctionDef | ast.AsyncFunctionDef:
    for node in ast.walk(module):
        if isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef) and node.name == function_name:
            return node
    raise AssertionError(f"{function_name} not found")


def _bound_aliases(module: ast.Module) -> set[str]:
    """Local names bound to ``set_mutation_row_lock_timeout`` in *module*."""
    aliases = {_BOUND_FUNC}
    for node in ast.walk(module):
        if isinstance(node, ast.ImportFrom) and node.module and node.module.endswith("db.crud.row_lock"):
            for imported in node.names:
                if imported.name == _BOUND_FUNC:
                    aliases.add(imported.asname or imported.name)
    return aliases


def _is_row_lock_call(call: ast.Call) -> bool:
    func = call.func
    if isinstance(func, ast.Attribute) and func.attr == _LOCK_ATTR:
        return True
    return any(keyword.arg == _LOCK_KWARG for keyword in call.keywords)


def _row_lock_calls(node: ast.AST) -> list[ast.Call]:
    return [child for child in ast.walk(node) if isinstance(child, ast.Call) and _is_row_lock_call(child)]


def _first_bound_line(node: ast.AST, aliases: set[str]) -> int | None:
    """Line of the first AWAITED bound call in *node* (comments/docstrings excluded)."""
    lines: list[int] = []
    for child in ast.walk(node):
        if not isinstance(child, ast.Await) or not isinstance(child.value, ast.Call):
            continue
        func = child.value.func
        is_bound_name = isinstance(func, ast.Name) and func.id in aliases
        is_bound_attr = isinstance(func, ast.Attribute) and func.attr == _BOUND_FUNC
        if is_bound_name or is_bound_attr:
            lines.append(child.lineno)
    return min(lines) if lines else None


def _row_lock_file_set() -> set[str]:
    discovered: set[str] = set()
    for path in _SRC.rglob("*.py"):
        if _row_lock_calls(ast.parse(path.read_text(encoding="utf-8"))):
            discovered.add(path.relative_to(_SRC).as_posix())
    return discovered


def _dotted_name(node: ast.expr) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        inner = _dotted_name(node.value)
        return f"{inner}.{node.attr}" if inner else None
    return None


def _set_parent_node_call_sites() -> list[str]:
    """Production call sites of ``set_parent_node`` (import-alias aware)."""
    sites: list[str] = []
    for path in _SRC.rglob("*.py"):
        if path.name == "node_composite.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        imported_symbols: set[str] = set()
        imported_modules: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == _NODE_COMPOSITE_MODULE:
                for imported in node.names:
                    if imported.name == _SET_PARENT_SYMBOL:
                        imported_symbols.add(imported.asname or imported.name)
                    elif imported.name == "node_composite":
                        imported_modules.add(imported.asname or imported.name)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if isinstance(func, ast.Name) and func.id in imported_symbols:
                sites.append(path.relative_to(_SRC).as_posix())
                break
            if isinstance(func, ast.Attribute) and func.attr == _SET_PARENT_SYMBOL:
                owner = _dotted_name(func.value)
                if owner is not None and (owner in imported_modules or owner.endswith("node_composite")):
                    sites.append(path.relative_to(_SRC).as_posix())
                    break
    return sites


def _iter_functions(module: ast.Module) -> list[ast.FunctionDef | ast.AsyncFunctionDef]:
    """Every function/method defined in *module* (routes may nest handlers)."""
    return [node for node in ast.walk(module) if isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef)]


def _imported_symbols(module: ast.Module) -> dict[str, tuple[str, str]]:
    """Local name -> (defining module, original symbol) for ``from X import y``."""
    imported: dict[str, tuple[str, str]] = {}
    for node in ast.walk(module):
        if isinstance(node, ast.ImportFrom) and node.module:
            for name in node.names:
                imported[name.asname or name.name] = (node.module, name.name)
    return imported


def _imported_module_aliases(module: ast.Module) -> dict[str, str]:
    """Local binding -> module path for ``import a.b.c [as x]``."""
    aliases: dict[str, str] = {}
    for node in ast.walk(module):
        if isinstance(node, ast.Import):
            for name in node.names:
                aliases[name.asname or name.name.split(".")[0]] = name.name
    return aliases


def _resolve_attribute_owner(attribute: ast.Attribute, module_aliases: dict[str, str]) -> str | None:
    """Dotted module path for an attribute call's receiver when it is a module."""
    owner = _dotted_name(attribute.value)
    if owner is None:
        return None
    head, _, rest = owner.partition(".")
    root = module_aliases.get(head)
    if root is None:
        return None
    return root if not rest else f"{root}.{rest}"


def _swept_helper_calls(function: ast.AST, module: ast.Module) -> set[str]:
    """Swept helpers *function* calls DIRECTLY, resolved through the module's imports.

    Three call shapes are resolved:

    * ``from modulo.db.crud.X import helper [as y]`` → ``y(...)``;
    * ``import modulo.db.crud.X [as m]`` → ``m.helper(...)`` / ``modulo.db.crud.X.helper(...)``;
    * a METHOD call on a service instance (``service.set_org_default(...)``) —
      accepted when the attribute name is a swept helper AND the module imports
      from at least one swept module (the receiver's concrete type is not
      inferable without type analysis, and these method names are unique).
    """
    imported_symbols = _imported_symbols(module)
    module_aliases = _imported_module_aliases(module)
    imports_a_swept_module = any(dotted[0] in _SWEPT_HELPER_MODULES for dotted in imported_symbols.values())
    hits: set[str] = set()
    for call in (node for node in ast.walk(function) if isinstance(node, ast.Call)):
        callee = call.func
        if isinstance(callee, ast.Name):
            source = imported_symbols.get(callee.id)
            if source is not None and source[1] in _SWEPT_HELPER_MODULES.get(source[0], frozenset()):
                hits.add(f"{source[0]}.{source[1]}")
        elif isinstance(callee, ast.Attribute) and callee.attr in _SWEPT_HELPER_NAMES:
            owner = _resolve_attribute_owner(callee, module_aliases)
            if owner is not None:
                if callee.attr in _SWEPT_HELPER_MODULES.get(owner, frozenset()):
                    hits.add(f"{owner}.{callee.attr}")
            elif imports_a_swept_module:
                hits.add(f"<service>.{callee.attr}")
    return hits


def _direct_swept_callers() -> dict[tuple[str, str], set[str]]:
    """``(route file, function)`` -> the swept helpers that function calls directly.

    Scans ``api/routes/*.py`` (excluding underscore-prefixed shared helpers,
    which own no request arm). This is the mechanically derived caller set the
    enumerated ``_MAPPED_55P03_ROUTE_ARMS`` list cannot provide.
    """
    routes_dir = _SRC / "api" / "routes"
    discovered: dict[tuple[str, str], set[str]] = {}
    for path in sorted(routes_dir.glob("*.py")):
        if path.name.startswith("_"):
            continue
        module = ast.parse(path.read_text(encoding="utf-8"))
        rel = path.relative_to(_SRC).as_posix()
        for function in _iter_functions(module):
            hits = _swept_helper_calls(function, module)
            if hits:
                discovered[(rel, function.name)] = hits
    return discovered


def _calls_classifier(function: ast.AST) -> bool:
    """Whether *function* itself calls ``raise_lock_timeout_conflict_if``."""
    for node in ast.walk(function):
        if not isinstance(node, ast.Call):
            continue
        callee = node.func
        if isinstance(callee, ast.Name) and callee.id == _CLASSIFIER_NAME:
            return True
        if isinstance(callee, ast.Attribute) and callee.attr == _CLASSIFIER_NAME:
            return True
    return False


def _local_sqlalchemy_arms(function: ast.AST) -> list[ast.ExceptHandler]:
    """The function's OWN ``except SQLAlchemyError`` arms (nested handlers included)."""
    arms: list[ast.ExceptHandler] = []
    for handler in ast.walk(function):
        if not isinstance(handler, ast.ExceptHandler) or handler.type is None:
            continue
        candidates = handler.type.elts if isinstance(handler.type, ast.Tuple) else [handler.type]
        if any(isinstance(candidate, ast.Name) and candidate.id == "SQLAlchemyError" for candidate in candidates):
            arms.append(handler)
    return arms


def _is_handle_db_errors_decorated(function: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """Whether the shared ``@handle_db_errors`` backstop wraps *function*."""
    for decorator in function.decorator_list:
        target = decorator.func if isinstance(decorator, ast.Call) else decorator
        if isinstance(target, ast.Name) and target.id == _HANDLE_DB_ERRORS_NAME:
            return True
        if isinstance(target, ast.Attribute) and target.attr == _HANDLE_DB_ERRORS_NAME:
            return True
    return False


def test_category_c_site_counts_match_the_inventory() -> None:
    """The §3 category-C row-lock set is exactly 20 SQLAlchemy sites."""
    measured: dict[str, int] = {}
    for rel in _C_SITE_LOCK_COUNTS:
        count = len(_row_lock_calls(_parse(rel)))
        if count:
            measured[rel] = count

    assert measured == _C_SITE_LOCK_COUNTS
    assert sum(measured.values()) == 20  # §3 category C total


def test_every_file_taking_a_row_lock_is_in_the_inventory_allowlist() -> None:
    """A NEW file that takes a row lock fails until it is inventoried.

    This is the repo-wide guard the token scanner lacked: a transitive helper in
    a file nobody listed used to be invisible. Add the file to
    ``_ROW_LOCK_FILES`` *and* to the inventory (with a bound when the lock is
    category C).
    """
    discovered = _row_lock_file_set()
    unexpected = sorted(discovered - _ROW_LOCK_FILES)
    stale = sorted(_ROW_LOCK_FILES - discovered)
    assert not unexpected, (
        "these files take a row lock but are absent from the FAR-1825 inventory allowlist: "
        f"{unexpected}. Add each to docs/architecture/lock-point-inventory.md and _ROW_LOCK_FILES "
        "(and bound it if it is category C)."
    )
    assert not stale, f"these allowlisted files no longer take a row lock: {stale}"


def test_every_swept_site_sets_the_lock_bound_before_its_first_lock() -> None:
    """Each swept function must issue the bound BEFORE the row lock it guards."""
    failures: list[str] = []
    for rel, function_name in _BOUND_SITES:
        module = _parse(rel)
        try:
            function = _function_node(module, function_name)
        except AssertionError:
            failures.append(f"{rel}::{function_name} not found")
            continue
        locks = _row_lock_calls(function)
        bound_line = _first_bound_line(function, _bound_aliases(module))
        if not locks:
            failures.append(f"{rel}::{function_name} no longer takes a row lock")
        elif bound_line is None:
            failures.append(f"{rel}::{function_name} lost an awaited set_mutation_row_lock_timeout()")
        elif bound_line >= min(lock.lineno for lock in locks):
            failures.append(f"{rel}::{function_name} sets the bound AFTER its first lock")
    assert not failures, "\n".join(failures)


def test_route_arms_fronting_bounded_crud_classify_55p03_as_a_conflict() -> None:
    """Every ENUMERATED JSON/API route arm in front of a bounded helper maps 55P03 to 409.

    The pre-PR gate found the bounds correct but the promised mapping absent on
    most swept routes: their local ``except SQLAlchemyError`` arms answered
    503/500, so a busy-row conflict read as a database outage. A representative
    route test proves the mapping at runtime; this guard proves it is present on
    every arm in ``_MAPPED_55P03_ROUTE_ARMS``.

    This list is hand-maintained, so on its own it cannot prove completeness —
    a new arm that nobody added would pass silently. That gap is closed by
    ``test_every_direct_route_caller_of_a_swept_helper_classifies_55p03``,
    which DERIVES the caller set from the route modules' imports. Transitive
    service-level callers remain an accepted residual (see this module's
    docstring and ``_MAPPING_EXCEPTION_REASONS``).

    Scope: JSON/API arms only. The SSO callback is a BROWSER-REDIRECT surface
    (the IdP navigates the user there, so a JSON 409 cannot be delivered and no
    SSO contention error code exists); its 55P03 stays deliberately mapped to
    ``provider_unavailable``. That exclusion is pinned explicitly by
    ``test_sso_callback_redirect_surface_is_an_explicit_55p03_exception`` so it
    cannot drift silently.
    """
    failures: list[str] = []
    for rel, function_name in _MAPPED_55P03_ROUTE_ARMS:
        module = _parse(rel)
        try:
            function = _function_node(module, function_name)
        except AssertionError:
            failures.append(f"{rel}::{function_name} not found")
            continue
        classified = any(
            isinstance(handler, ast.ExceptHandler)
            and isinstance(handler.type, ast.Name)
            and handler.type.id == "SQLAlchemyError"
            and any(
                isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == _CLASSIFIER_NAME
                for node in ast.walk(handler)
            )
            for handler in ast.walk(function)
            if isinstance(handler, ast.ExceptHandler)
        )
        if not classified:
            failures.append(f"{rel}::{function_name} has no except SQLAlchemyError arm calling {_CLASSIFIER_NAME}")
    assert not failures, "\n".join(failures)


def test_sso_callback_redirect_surface_is_an_explicit_55p03_exception() -> None:
    """Pin the ONE non-409 arm: the browser-redirect SSO callback stays generic.

    ``api/routes/sso.py::_run_sso_callback_process`` opens the SSO JIT
    transaction, whose first row lock (C17 ``get_live_for_email``) is now
    bounded, so a 55P03 is reachable there too. But the IdP navigates the
    BROWSER to this endpoint: the error surface is a 303 login redirect, not
    JSON, and no SSO contention error code exists. The arm therefore
    deliberately keeps ``provider_unavailable`` rather than adding a new
    SPA-facing code in this ticket.

    This test records the exclusion so it cannot drift: the arm must still
    exist, still raise ``SsoCallbackError(SSO_ERROR_PROVIDER_UNAVAILABLE)``, and
    stay OUT of the JSON/API mapping list (adding it there would fail the
    classifier assertion above, because it does not call
    ``raise_lock_timeout_conflict_if``).
    """
    module = _parse("api/routes/sso.py")
    function = _function_node(module, _SSO_CALLBACK_FUNCTION)
    arms = [
        handler
        for handler in ast.walk(function)
        if isinstance(handler, ast.ExceptHandler)
        and isinstance(handler.type, ast.Name)
        and handler.type.id == "SQLAlchemyError"
    ]
    assert len(arms) == 1, "the SSO callback should keep exactly one SQLAlchemyError arm"

    raised = [
        node
        for node in ast.walk(arms[0])
        if isinstance(node, ast.Raise)
        and isinstance(node.exc, ast.Call)
        and isinstance(node.exc.func, ast.Name)
        and node.exc.func.id == "SsoCallbackError"
    ]
    assert raised, "the SSO callback SQLAlchemyError arm no longer raises SsoCallbackError"
    error_codes = {_dotted_name(arg) for call in raised for arg in call.exc.args if isinstance(arg, ast.expr)}
    assert "SSO_ERROR_PROVIDER_UNAVAILABLE" in error_codes, (
        "the redirect surface's documented 55P03 mapping changed; update the inventory's "
        "'Mapping truth' table and this pinned exception in the same change"
    )
    assert ("api/routes/sso.py", _SSO_CALLBACK_FUNCTION) not in _MAPPED_55P03_ROUTE_ARMS


def test_every_direct_route_caller_of_a_swept_helper_classifies_55p03() -> None:
    """Derive the route callers of every swept helper and require a 55P03 mapping.

    The enumerated ``_MAPPED_55P03_ROUTE_ARMS`` list cannot prove its own
    completeness: a NEW route that calls a bounded helper and forgets
    ``raise_lock_timeout_conflict_if`` is simply absent from it and passes. This
    guard closes that gap mechanically, in the spirit of the FAR-1832
    writer-discovery check: it token-resolves every ``api/routes/*.py`` function
    that DIRECTLY calls a swept helper (``_SWEPT_HELPER_MODULES``, import-aware
    so the unrelated ``db.crud.schema.update_schema`` is not mistaken for the
    swept ``db.crud.parameter_schema.update_schema``).

    A discovered function passes when ANY of these holds:

    * it calls ``raise_lock_timeout_conflict_if`` itself; or
    * it has NO local ``except SQLAlchemyError`` arm — the error escapes to
      ``@handle_db_errors``, whose backstop maps 55P03 → 409; or
    * it is a recorded exception in ``_MAPPING_EXCEPTION_REASONS`` (a written
      reason, pinned by its own test).

    It FAILS when a local ``except SQLAlchemyError`` arm answers 503/500 without
    the classifier, and when a function has neither the classifier nor the
    shared backstop. The known-caller superset assertion keeps the discovery
    itself from silently going blind.

    Not covered (accepted residual, see the module docstring): a TRANSITIVE
    service-level caller — e.g. ``library.py`` reaching C20 through
    ``materialize_map_from_primitive``. That arm is recorded explicitly.
    """
    callers = _direct_swept_callers()

    assert callers, (
        "swept-helper caller discovery found NOTHING — the import resolution or helper table is "
        "stale, which is not the same as every caller being mapped"
    )
    missing_known = _KNOWN_SWEPT_CALLERS - set(callers)
    assert not missing_known, (
        f"discovery no longer sees known direct swept-helper callers {sorted(missing_known)} — "
        "fix the discovery, do not weaken the guard"
    )

    failures: list[str] = []
    for (rel, function_name), helpers in sorted(callers.items()):
        function = _function_node(_parse(rel), function_name)
        if _calls_classifier(function):
            continue
        if (rel, function_name) in _MAPPING_EXCEPTION_REASONS:
            continue
        if _local_sqlalchemy_arms(function):
            failures.append(
                f"{rel}::{function_name} directly calls {sorted(helpers)} and its local "
                f"except SQLAlchemyError arm never calls {_CLASSIFIER_NAME} — a contended row "
                "would answer 503/500 instead of a 409 conflict"
            )
            continue
        if not _is_handle_db_errors_decorated(function):
            failures.append(
                f"{rel}::{function_name} directly calls {sorted(helpers)} but has neither "
                f"{_CLASSIFIER_NAME} nor the @{_HANDLE_DB_ERRORS_NAME} backstop"
            )
    assert not failures, "\n".join(failures)


def test_every_recorded_mapping_exception_still_matches_the_code() -> None:
    """Every exception entry names a real function and states a reason.

    An exception is a promise about code that is deliberately NOT converted; if
    the function is renamed or deleted the entry becomes dead prose that silently
    excuses nothing. The reasons are non-trivial by construction.
    """
    for (rel, function_name), reason in _MAPPING_EXCEPTION_REASONS.items():
        function = _function_node(_parse(rel), function_name)  # raises if renamed/removed
        assert len(reason) >= 40, f"{rel}::{function_name} exception reason is too thin: {reason!r}"
        assert not _calls_classifier(function), (
            f"{rel}::{function_name} now classifies 55P03 — remove its mapping exception and re-record the behaviour"
        )

    # sso.py's callback is pinned separately; library.py's arm is the transitive
    # residual. Both must stay OUT of the enumerated arm list (they do not call
    # the classifier, so adding them there would fail the classifier assertion).
    assert ("api/routes/sso.py", _SSO_CALLBACK_FUNCTION) not in _MAPPED_55P03_ROUTE_ARMS
    assert (
        "api/routes/library.py",
        "create_lifecycle_map_from_primitive_endpoint",
    ) not in _MAPPED_55P03_ROUTE_ARMS


def _unavailable_reply(handler: ast.ExceptHandler) -> bool:
    """Whether *handler* raises the route's generic 503 helper for a DB error."""
    return any(
        isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "_unavailable_error"
        for node in ast.walk(handler)
    )


def test_library_materialise_arm_is_a_recorded_transitive_residual() -> None:
    """Pin the concrete transitive residual so it cannot drift silently.

    ``create_lifecycle_map_from_primitive_endpoint`` reaches C20
    (``get_lifecycle_map_for_update``) only through the materialise/import
    chain, so the derived direct-caller guard does not see it. Its local
    ``except SQLAlchemyError`` arm still answers 503. Accepted because the lock
    is taken on a row INSERTED IN THE SAME TRANSACTION as the lock acquisition —
    no other transaction can hold it, so 55P03 is not practically reachable.

    If that arm is ever converted (the classifier added), this test fails and the
    exception record must be removed in the same change.
    """
    rel = "api/routes/library.py"
    function_name = "create_lifecycle_map_from_primitive_endpoint"
    function = _function_node(_parse(rel), function_name)

    arms = _local_sqlalchemy_arms(function)
    assert len(arms) == 1, "the library materialise arm should keep exactly one SQLAlchemyError handler"
    assert not _calls_classifier(function), (
        f"{rel}::{function_name} now classifies 55P03 — remove it from "
        "_MAPPING_EXCEPTION_REASONS and from the inventory's residual table"
    )
    assert _unavailable_reply(arms[0]), (
        "the recorded residual expects the arm to answer _unavailable_error() (503); if the mapping "
        "changed, re-record the residual"
    )
    assert (rel, function_name) in _MAPPING_EXCEPTION_REASONS


def test_c5_c6_generic_fallback_is_bounded_by_the_shared_patch_caller() -> None:
    """The generic-dialect fallback's locks are bounded one frame up."""
    rel, caller = _CALLER_BOUND_CALLER
    module = _parse(rel)
    caller_node = _function_node(module, caller)
    bound_line = _first_call_line(caller_node, "_take_settings_patch_lock_wait")
    dispatch_line = _first_call_line(caller_node, _GENERIC_FALLBACK)
    assert bound_line is not None, f"{rel}::{caller} no longer bounds the shared patch"
    assert dispatch_line is not None, f"{rel}::{caller} no longer reaches the generic fallback"
    assert bound_line < dispatch_line, "the generic fallback is dispatched before the bound"

    # The generic fallback is defined once and called from exactly that
    # bounded dispatcher (checking the CALL, not a token).
    call_sites = [
        call
        for call in ast.walk(module)
        if isinstance(call, ast.Call)
        and (
            (isinstance(call.func, ast.Name) and call.func.id == _GENERIC_FALLBACK)
            or (isinstance(call.func, ast.Attribute) and call.func.attr == _GENERIC_FALLBACK)
        )
    ]
    assert len(call_sites) == 1, f"expected one bounded call to {_GENERIC_FALLBACK}, got {len(call_sites)}"
    assert bound_line < call_sites[0].lineno <= caller_node.end_lineno, (
        "the single generic-fallback call is no longer inside the bounded dispatcher"
    )


def _first_call_line(node: ast.AST, func_name: str) -> int | None:
    lines = [
        child.lineno
        for child in ast.walk(node)
        if isinstance(child, ast.Call)
        and (
            (isinstance(child.func, ast.Name) and child.func.id == func_name)
            or (isinstance(child.func, ast.Attribute) and child.func.attr == func_name)
        )
    ]
    return min(lines) if lines else None


def test_no_production_source_calls_the_deliberately_unbounded_sites() -> None:
    """A no-caller lock cannot produce a user-facing wait; keep it that way.

    ``set_parent_node`` has no production call site at all, and
    ``_get_existing_names`` only takes its lock behind ``for_update=True`` —
    which no production caller passes. If either becomes reachable, this test
    fails and the site must be swept (and the inventory updated) instead of
    silently leaving a request path unbounded.

    The ``set_parent_node`` check is AST- and alias-aware (``from ... import
    set_parent_node as x`` and ``node_composite.set_parent_node(...)`` both
    count). A dynamic ``getattr`` call is not detected — recorded residual.
    """
    for rel, function_name in _NO_PRODUCTION_CALLER:
        _function_node(_parse(rel), function_name)  # the recorded function still exists

    parent_calls = _set_parent_node_call_sites()
    assert not parent_calls, f"set_parent_node gained a production caller: {parent_calls}"

    import_export = _read("core/workflow_import_export/__init__.py")
    assert "for_update=True" not in import_export, "_get_existing_names enabled its FOR UPDATE lock"
    assert "get_existing_pipeline_names(session, org_id)" in import_export


def test_row_lock_helper_remains_transaction_scoped_and_postgres_only() -> None:
    """Pin the mechanism the sweep relies on (not a per-connection timeout)."""
    helper = _read("db/crud/row_lock.py")
    assert "set_config('lock_timeout', :val, true)" in helper
    assert '== "postgresql"' in helper


# ---------------------------------------------------------------------------
# Runtime coverage for the three swept-line groups the AST guard can only
# assert structurally, and for the C9 CRUD bound (whose only production caller
# patches ``observe_node`` out in its route tests, so no unit test reached the
# helper at all). Each test fails if its bound/classifier line is removed.
# ---------------------------------------------------------------------------


class _LockTimeoutDriverError(Exception):
    """Driver-error double carrying an asyncpg ``sqlstate``."""

    def __init__(self, state: str) -> None:
        super().__init__(f"driver error {state}")
        self.sqlstate = state


def _lock_timeout_error() -> OperationalError:
    return OperationalError("SELECT ... FOR UPDATE", {}, _LockTimeoutDriverError("55P03"))


def _begin_session() -> AsyncMock:
    """An ``AsyncSession`` double whose ``async with session.begin()`` works."""
    session = AsyncMock(spec=AsyncSession)
    begin_cm = MagicMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    return session


async def test_replace_bindings_route_maps_55p03_to_409() -> None:
    """The agents replace-bindings arm classifies a bounded-lock 55P03."""
    from modulo.api.routes import agents as agents_routes

    principal = SimpleNamespace(organisation_id=uuid.uuid4(), account_id=uuid.uuid4(), org_role="admin")
    agent = SimpleNamespace(organisation_id=principal.organisation_id)

    with (
        patch.object(agents_routes, "set_rls_org", AsyncMock()),
        patch.object(agents_routes, "get_agent", AsyncMock(return_value=agent)),
        patch.object(agents_routes, "list_bindings_for_agent", AsyncMock(return_value=[])),
        patch.object(agents_routes, "replace_agent_bindings", AsyncMock(side_effect=_lock_timeout_error())),
        pytest.raises(HTTPException) as exc_info,
    ):
        await agents_routes.replace_bindings_endpoint(
            agent_id=uuid.uuid4(),
            req=SimpleNamespace(bindings=[]),
            session=_begin_session(),
            principal=principal,
        )

    assert exc_info.value.status_code == 409


async def test_in_app_notifications_update_preferences_maps_55p03_to_409() -> None:
    """The dashboard-preferences arm classifies its first-write lock timeout.

    ``update_preferences`` bounds at the top of the transaction because its
    first write (the preference DML) precedes the Account-row lock; a 55P03
    from that first bound must be a 409, not the arm's 500.
    """
    from modulo.api.routes import in_app_notifications as notification_routes

    principal = SimpleNamespace(organisation_id=uuid.uuid4(), account_id=uuid.uuid4(), org_role="admin")
    req = SimpleNamespace(dashboard_level=None, notification_opt_outs=None)
    request = SimpleNamespace(state=SimpleNamespace())

    with (
        patch.object(
            notification_routes,
            "set_mutation_row_lock_timeout",
            AsyncMock(side_effect=_lock_timeout_error()),
        ),
        pytest.raises(HTTPException) as exc_info,
    ):
        await notification_routes.update_preferences(
            req=req,
            request=request,
            session=_begin_session(),
            principal=principal,
        )

    assert exc_info.value.status_code == 409


async def test_observe_node_bounds_the_observation_lock_before_its_query() -> None:
    """C9 ``observe_node`` issues the transaction-scoped bound before its lock.

    Its only production caller is the REST observe-node route, whose unit tests
    patch ``observe_node`` out — so without this direct test the bound is never
    executed by the unit suite (and the changed-lines gate cannot see it).
    """
    from modulo.db.crud.node_observation import observe_node

    existing = SimpleNamespace(id=uuid.uuid4())
    result = MagicMock()
    result.scalar_one_or_none.return_value = existing
    session = AsyncMock(spec=AsyncSession)
    session.execute = AsyncMock(return_value=result)
    bound = AsyncMock()

    with patch("modulo.db.crud.row_lock.set_mutation_row_lock_timeout", bound):
        observed = await observe_node(
            session,
            organisation_id=uuid.uuid4(),
            run_id=uuid.uuid4(),
            node_id=str(uuid.uuid4()),
        )

    bound.assert_awaited_once_with(session)
    assert observed is existing
