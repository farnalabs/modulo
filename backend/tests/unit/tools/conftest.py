"""Environment setup for ``tests/unit/tools``.

The backend ``Settings`` model requires ``DATABASE_URL`` / ``SECRET_KEY`` /
``FERNET_KEY`` at first ``get_settings()`` call (no ``.env`` in worktrees). Use
``setdefault`` so explicit CI values always win — the same convention as
``tests/bdd/conftest.py`` and ``tests/integration/conftest.py``.
"""

import os

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///./tools-test.db")
os.environ.setdefault("SECRET_KEY", "a" * 32)
os.environ.setdefault("FERNET_KEY", "b" * 32)
os.environ.setdefault("REDIS_URL", "")
