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
inventory gains a row-lock call, if a route arm fronting a bound CRUD helper
stops classifying 55P03, or if a deliberately-unbounded site becomes reachable
from a request path.

Known residual: a NEW transitive helper that calls a row lock from a file not in
``_ROW_LOCK_FILES`` fails the repo-wide file-set assertion, so the inventory and
this guard must be updated in the same change. ``set_parent_node``'s no-caller
check is alias-aware for the direct ``from ... import`` / module-attribute
shapes; a call reached through a hand-built string or dynamic ``getattr`` is out
of scope.
"""

from __future__ import annotations

import ast
from pathlib import Path

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

# Route arms that front a now-bounded CRUD helper: a 55P03 reaching the arm's
# local ``except SQLAlchemyError`` must be classified as a 409 conflict by
# ``raise_lock_timeout_conflict_if`` before the arm's own 503/500 reply. Without
# it the busy-row conflict is reported as a database outage (FAR-1825 pre-PR
# gate finding 1). The bound itself is pinned by the category-C tests above;
# this pins the MAPPING at each reachable surface.
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
    ("api/routes/lifecycle_maps.py", "graduate_lifecycle_map_stage_endpoint"),
    ("api/routes/runs.py", "observe_run_node"),
    ("api/routes/auth.py", "accept_invite"),
    ("api/routes/in_app_notifications.py", "update_preferences"),
    ("api/routes/admin_assistant.py", "set_org_context_source"),
)
_CLASSIFIER_NAME = "raise_lock_timeout_conflict_if"

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
    """Every route arm in front of a bounded CRUD helper maps 55P03 to 409.

    The pre-PR gate found the bounds correct but the promised mapping absent on
    most swept routes: their local ``except SQLAlchemyError`` arms answered
    503/500, so a busy-row conflict read as a database outage. A representative
    route test proves the mapping at runtime; this guard proves it is present at
    EVERY reachable arm, so a new arm cannot silently reintroduce the defect.
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
