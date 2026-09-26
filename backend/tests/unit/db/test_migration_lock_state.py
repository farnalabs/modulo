"""Regression: the app-held migration-lock flag must reach the env.py alembic runs.

Alembic loads ``env.py`` as a fresh module (``env_py``) via
``alembic.util.pyfiles.load_python_file``; it never reuses the cached
``modulo.db.migrations.env`` object. A lock-ownership flag stored as a global
inside ``env.py`` therefore did not propagate from ``modulo.api.main`` to the
copy ``command.upgrade`` executed, so the app's lifespan migration run
re-acquired the advisory lock it already held and self-deadlocked -- the nightly
Schemathesis fuzz job failed with "Timed out waiting for the migration advisory
lock" for 11 consecutive days.

The flag now lives in ``modulo.db.migrations._lock_state`` so both copies import
the same object; these tests pin that propagation, loading env.py exactly the
way alembic does.
"""

from pathlib import Path
from types import ModuleType
from typing import Any, Self

import pytest

from modulo.db.migrations import _lock_state
from modulo.db.migrations import env as cached_env


@pytest.fixture(autouse=True)
def _reset_flag() -> None:
    """Guarantee the process-wide flag is clear before and after every test."""
    _lock_state.set_held_by_caller(False)
    yield
    _lock_state.set_held_by_caller(False)


def _load_env_as_alembic_does() -> ModuleType:
    """Execute env.py as a brand-new module, mirroring alembic's loader."""
    from alembic.util.pyfiles import load_module_py

    assert cached_env.__file__ is not None
    return load_module_py("env_py", str(Path(cached_env.__file__)))


class _ExplodingEngine:
    def connect(self) -> Any:
        raise AssertionError("env.py opened a lock connection although the caller already holds it")


def test_caller_held_flag_reaches_freshly_loaded_env_module() -> None:
    cached_env.set_lock_held_by_caller(True)
    env_py = _load_env_as_alembic_does()

    # With the caller holding the lock, env.py must skip acquisition entirely and
    # never touch the engine. Before the fix the fresh copy read its own
    # module-global ``False`` and called engine.connect() here, blocking on the
    # advisory lock the caller already held until the 240s poll timed out.
    with env_py._migration_advisory_lock(_ExplodingEngine(), "postgresql+psycopg://u:p@h:5432/db"):
        pass


class _Result:
    def scalar_one(self) -> bool:
        return True


class _RecordingConnection:
    def __init__(self) -> None:
        self.executed = False

    def execute(self, *args: Any, **kwargs: Any) -> _Result:
        self.executed = True
        return _Result()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> bool:
        return False


class _RecordingEngine:
    def __init__(self) -> None:
        self.conn = _RecordingConnection()

    def connect(self) -> _RecordingConnection:
        return self.conn


def test_standalone_env_still_takes_the_lock() -> None:
    env_py = _load_env_as_alembic_does()

    engine = _RecordingEngine()
    with env_py._migration_advisory_lock(engine, "postgresql+psycopg://u:p@h:5432/db"):
        pass

    assert engine.conn.executed is True, "standalone env.py must still acquire the lock"
