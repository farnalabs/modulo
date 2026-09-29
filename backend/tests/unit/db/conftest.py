"""Test configuration for db unit tests.

``tests/unit/db/test_hitl_review_window_resolution.py`` calls ``get_settings()``
at test time; the backend ``Settings`` model requires
DATABASE_URL/SECRET_KEY/FERNET_KEY and there is no ``.env`` in worktrees, so
provide the minimum env the same way as ``tests/unit/core/conftest.py`` —
setdefault so explicit CI values always win.
"""

import os

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://localhost/test")
os.environ.setdefault("SECRET_KEY", "a" * 32)
os.environ.setdefault("FERNET_KEY", "a" * 32)
os.environ.setdefault("REDIS_URL", "")
os.environ.setdefault("MODULO_ADMIN_PASSWORD", "test")
os.environ.setdefault("MODULO_CSRF_ENABLED", "false")
