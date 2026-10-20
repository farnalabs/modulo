"""FAR-1825 structural guard: every swept category-C CRUD lock stays bounded.

The FAR-1801 lock-point inventory (``docs/architecture/lock-point-inventory.md``)
enumerates the row locks in ``backend/src/modulo``. Its §3 category **C** (entity
CRUD) sites run in a CALLER-owned transaction, so they are only as bounded as
their least careful caller. FAR-1825 swept the sites a request path can reach
and gave each one a transaction-scoped ``set_mutation_row_lock_timeout`` (the
``SET LOCAL lock_timeout`` in ``db/crud/row_lock.py``) before its first lock, so
a contended row raises SQLSTATE 55P03 (mapped to a visible 409) instead of
parking a pooled connection indefinitely.

This test is a TOKEN SCANNER over the source, in the shape of
``test_far1832_lock_timeout.py``: it re-derives the category-C disposition for
every site rather than trusting a prose list, and fails if any swept site loses
its bound, if a category-C file gains/loses a row-lock token, or if a
deliberately-unbounded site becomes reachable from a request path.

Known residual (accepted): a NEW transitive helper that calls
``with_for_update`` from a file not listed in ``_C_SITE_LOCK_COUNTS`` is not
discovered here. Adding a category-C lock site means adding it to this file and
to the inventory in the same change (the same discipline FAR-1832 pinned).
"""

from __future__ import annotations

import ast
from pathlib import Path

_BACKEND_ROOT = Path(__file__).resolve().parents[3]
_SRC = _BACKEND_ROOT / "src" / "modulo"
_ROW_LOCK_TOKEN = ".with_for_update("
_GET_LOCK_TOKEN = "with_for_update="
_BOUND_TOKEN = "set_mutation_row_lock_timeout("

# Every category-C site's row-lock token count, by file. The sum is the §3
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


def _read(rel: str) -> str:
    return (_SRC / rel).read_text(encoding="utf-8")


def _function_source(rel: str, function_name: str) -> tuple[str, str]:
    """Return ``(whole_module_source, function_source)`` for *rel*/*function_name*."""
    source = _read(rel)
    module = ast.parse(source)
    for node in ast.walk(module):
        if isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef) and node.name == function_name:
            segment = ast.get_source_segment(source, node)
            assert segment is not None, f"could not segment {rel}::{function_name}"
            return source, segment
    raise AssertionError(f"{rel}::{function_name} not found")


def _lock_index(body: str) -> int:
    """Index of the first row-lock token in *body* (or -1)."""
    candidates = [body.find(token) for token in (_ROW_LOCK_TOKEN, _GET_LOCK_TOKEN)]
    present = [index for index in candidates if index != -1]
    return min(present) if present else -1


def test_category_c_site_counts_match_the_inventory() -> None:
    """The §3 category-C row-lock set is exactly 20 SQLAlchemy sites."""
    measured: dict[str, int] = {}
    for rel in _C_SITE_LOCK_COUNTS:
        body = _read(rel)
        count = body.count(_ROW_LOCK_TOKEN) + body.count(_GET_LOCK_TOKEN)
        if count:
            measured[rel] = count

    assert measured == _C_SITE_LOCK_COUNTS
    assert sum(measured.values()) == 20  # §3 category C total


def test_every_swept_site_sets_the_lock_bound_before_its_first_lock() -> None:
    """Each swept function must issue the bound BEFORE the row lock it guards."""
    failures: list[str] = []
    for rel, function_name in _BOUND_SITES:
        _, body = _function_source(rel, function_name)
        lock_index = _lock_index(body)
        bound_index = body.find(_BOUND_TOKEN)
        if lock_index == -1:
            failures.append(f"{rel}::{function_name} no longer takes a row lock")
        elif bound_index == -1:
            failures.append(f"{rel}::{function_name} lost set_mutation_row_lock_timeout")
        elif bound_index > lock_index:
            failures.append(f"{rel}::{function_name} sets the bound AFTER its first lock")
    assert not failures, "\n".join(failures)


def test_c5_c6_generic_fallback_is_bounded_by_the_shared_patch_caller() -> None:
    """The generic-dialect fallback's locks are bounded one frame up."""
    rel, caller = _CALLER_BOUND_CALLER
    _, caller_body = _function_source(rel, caller)
    bound_index = caller_body.find("_take_settings_patch_lock_wait(")
    dispatch_index = caller_body.find(f"{_GENERIC_FALLBACK}(")
    assert bound_index != -1, f"{rel}::{caller} no longer bounds the shared patch"
    assert dispatch_index != -1, f"{rel}::{caller} no longer reaches the generic fallback"
    assert bound_index < dispatch_index, "the generic fallback is dispatched before the bound"

    # The only caller of the locked generic helper is that bounded dispatcher.
    call_sites = [line for line in _read(_CALLER_BOUND_CALLEE).splitlines() if f"await {_GENERIC_FALLBACK}(" in line]
    assert len(call_sites) == 1, f"expected one bounded call to {_GENERIC_FALLBACK}, got {len(call_sites)}"


def test_no_production_source_calls_the_deliberately_unbounded_sites() -> None:
    """A no-caller lock cannot produce a user-facing wait; keep it that way.

    ``set_parent_node`` has no production call site at all, and
    ``_get_existing_names`` only takes its lock behind ``for_update=True`` —
    which no production caller passes. If either becomes reachable, this test
    fails and the site must be swept (and the inventory updated) instead of
    silently leaving a request path unbounded.
    """
    for rel, function_name in _NO_PRODUCTION_CALLER:
        _function_source(rel, function_name)  # the recorded function still exists

    source_files = list(_SRC.rglob("*.py"))

    parent_calls = [
        path
        for path in source_files
        if "set_parent_node(" in path.read_text(encoding="utf-8") and path.name != "node_composite.py"
    ]
    assert parent_calls == [], f"set_parent_node gained a production caller: {parent_calls}"

    import_export = _read("core/workflow_import_export/__init__.py")
    assert "for_update=True" not in import_export, "_get_existing_names enabled its FOR UPDATE lock"
    assert "get_existing_pipeline_names(session, org_id)" in import_export


def test_row_lock_helper_remains_transaction_scoped_and_postgres_only() -> None:
    """Pin the mechanism the sweep relies on (not a per-connection timeout)."""
    helper = _read("db/crud/row_lock.py")
    assert "set_config('lock_timeout', :val, true)" in helper
    assert '== "postgresql"' in helper
