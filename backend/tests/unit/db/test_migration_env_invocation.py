"""Unit tests for env.py's alembic invocation-direction classification (FAR-967 F2).

Regression: programmatic ``command.downgrade(...)`` calls carry no
``config.cmd_opts``, and ``_invocation_is_upgrade`` returned True for the
no-signal shape (``None != "downgrade"``), so the at-head boot fast-path
SKIPPED every Python-API downgrade — zero "Running downgrade" log lines and
columns surviving a downgrade.  CLI downgrades worked only because the CLI
injects ``cmd_opts``.

The fix resolves direction from the available signals, in order: ``cmd_opts``
when present (CLI tuple shape or test-injected ``SimpleNamespace(command=...)``),
then the active Alembic ``EnvironmentContext``'s ``fn`` closure name via
``context._proxy`` (``upgrade`` / ``downgrade`` / ``do_stamp``).  Only a
positively-identified ``upgrade`` takes the at-head fast-path.
"""

from types import SimpleNamespace
from typing import Any

import pytest

from modulo.db.migrations import env as migration_env


@pytest.fixture(autouse=True)
def _reset_env_globals():
    """Save/restore the module globals these tests stub."""
    saved_config = migration_env.config
    saved_context = migration_env.context
    yield
    migration_env.config = saved_config
    migration_env.context = saved_context


def _classify(
    *,
    cmd_opts: Any = None,
    ctx_fn_name: str | None = None,
    ctx_proxy_present: bool = True,
) -> bool:
    """Run ``_invocation_is_upgrade`` against a stubbed config/context."""
    migration_env.config = SimpleNamespace(cmd_opts=cmd_opts)
    if ctx_proxy_present:
        proxy = (
            None if ctx_fn_name is None else SimpleNamespace(context_opts={"fn": SimpleNamespace(__name__=ctx_fn_name)})
        )
        migration_env.context = SimpleNamespace(_proxy=proxy)
    else:
        migration_env.context = SimpleNamespace()
    return migration_env._invocation_is_upgrade()


# ---------------------------------------------------------------------------
# Signal 1: config.cmd_opts present (CLI shape or test-injected shape)
# ---------------------------------------------------------------------------


def test_cli_tuple_downgrade_is_not_upgrade() -> None:
    """The CLI shape: ``cmd_opts.cmd = (fn, positional, kwarg)`` with
    ``fn.__name__ == "downgrade"`` must classify as a downgrade."""
    opts = SimpleNamespace(cmd=(SimpleNamespace(__name__="downgrade"), [], {}))
    assert _classify(cmd_opts=opts) is False


def test_cli_tuple_upgrade_is_upgrade() -> None:
    opts = SimpleNamespace(cmd=(SimpleNamespace(__name__="upgrade"), [], {}))
    assert _classify(cmd_opts=opts) is True


def test_test_injected_command_downgrade_is_not_upgrade() -> None:
    """The documented test shape (``SimpleNamespace(command="downgrade")``)
    must keep classifying as a downgrade."""
    assert _classify(cmd_opts=SimpleNamespace(command="downgrade")) is False


def test_test_injected_command_upgrade_is_upgrade() -> None:
    assert _classify(cmd_opts=SimpleNamespace(command="upgrade")) is True


def test_cmd_opts_shape_wins_over_alembic_context() -> None:
    """When cmd_opts is present it is authoritative, even if the context
    fn name disagrees (a test that injects cmd_opts must stay in control)."""
    opts = SimpleNamespace(cmd=(SimpleNamespace(__name__="downgrade"), [], {}))
    assert _classify(cmd_opts=opts, ctx_fn_name="upgrade") is False


# ---------------------------------------------------------------------------
# Signal 2: cmd_opts absent — the Python-API case (the F2 regression)
# ---------------------------------------------------------------------------


def test_python_api_downgrade_without_cmd_opts_is_not_upgrade() -> None:
    """THE REGRESSION: a real ``command.downgrade(config, ...)`` leaves
    ``cmd_opts`` unset; the active EnvironmentContext's fn is named
    ``downgrade``.  Must classify as NOT an upgrade so the at-head
    fast-path never swallows it."""
    assert _classify(cmd_opts=None, ctx_fn_name="downgrade") is False


def test_python_api_upgrade_without_cmd_opts_is_upgrade() -> None:
    """The app-lifespan shape (``command.upgrade(config, "heads")``) keeps
    its at-head fast-path: context fn name is "upgrade"."""
    assert _classify(cmd_opts=None, ctx_fn_name="upgrade") is True


def test_python_api_stamp_without_cmd_opts_is_not_upgrade() -> None:
    """``command.stamp`` installs fn "do_stamp" — not an upgrade; the
    migration machinery must run (stamping writes the version table even
    when the schema is unchanged)."""
    assert _classify(cmd_opts=None, ctx_fn_name="do_stamp") is False


def test_unrecognised_context_fn_is_not_upgrade() -> None:
    """Any fn name that is not exactly "upgrade" must not take the
    at-head fast-path — fail towards running the migration."""
    assert _classify(cmd_opts=None, ctx_fn_name="something_else") is False


def test_none_context_fn_is_not_upgrade() -> None:
    """A context whose context_opts carry no fn (defensive) must not be
    treated as an upgrade."""
    proxy = SimpleNamespace(context_opts={})
    migration_env.config = SimpleNamespace(cmd_opts=None)
    migration_env.context = SimpleNamespace(_proxy=proxy)
    assert migration_env._invocation_is_upgrade() is False


# ---------------------------------------------------------------------------
# Signal 3: no signal at all (documented fallback)
# ---------------------------------------------------------------------------


def test_no_signal_falls_back_to_upgrade() -> None:
    """env.py reached with neither cmd_opts nor an active EnvironmentContext
    (imported outside a run) — documented default True; the boot fast-path
    is not a live concern in that shape."""
    assert _classify(cmd_opts=None, ctx_proxy_present=False) is True


def test_config_none_falls_back_to_upgrade() -> None:
    migration_env.config = None
    migration_env.context = SimpleNamespace()
    assert migration_env._invocation_is_upgrade() is True


# ---------------------------------------------------------------------------
# _alembic_context_fn_name helper
# ---------------------------------------------------------------------------


def test_alembic_context_fn_name_reads_proxy_context_opts() -> None:
    migration_env.context = SimpleNamespace(
        _proxy=SimpleNamespace(context_opts={"fn": SimpleNamespace(__name__="downgrade")})
    )
    assert migration_env._alembic_context_fn_name() == "downgrade"


def test_alembic_context_fn_name_none_without_proxy() -> None:
    migration_env.context = SimpleNamespace()
    assert migration_env._alembic_context_fn_name() is None


def test_alembic_context_fn_name_none_when_proxy_is_none() -> None:
    migration_env.context = SimpleNamespace(_proxy=None)
    assert migration_env._alembic_context_fn_name() is None


def test_alembic_context_fn_name_none_for_malformed_context_opts() -> None:
    migration_env.context = SimpleNamespace(_proxy=SimpleNamespace(context_opts="not-a-dict"))
    assert migration_env._alembic_context_fn_name() is None
