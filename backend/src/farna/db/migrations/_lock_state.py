"""Process-wide migration-advisory-lock ownership flag.

Alembic loads ``env.py`` as a *fresh* module via
``alembic.util.pyfiles.load_python_file`` (module id ``env_py``); it never
reuses the cached ``modulo.db.migrations.env`` object. A lock-ownership flag
stored as a global inside ``env.py`` therefore does not propagate:
``modulo.api.main`` sets the flag on the cached module while ``command.upgrade``
executes a different copy that still reads ``False`` -- so the app-lifespan
migration run re-acquires the advisory lock it already holds on a second
connection and blocks on itself (observed as ``Timed out waiting for the
migration advisory lock`` after the 240s poll in the nightly Schemathesis fuzz
job).

Keeping the flag in this separate module fixes the propagation: both the
cached ``modulo.db.migrations.env`` and the freshly-loaded ``env_py`` import the
same cached ``modulo.db.migrations._lock_state`` object, so the caller-held
flag the app sets is the one the executing ``env.py`` reads.
"""

_held_by_caller = False


def set_held_by_caller(held: bool) -> None:
    """Record whether the current process already holds the migration lock."""
    global _held_by_caller
    _held_by_caller = held


def is_held_by_caller() -> bool:
    """Return True when the calling process already holds the migration lock."""
    return _held_by_caller
