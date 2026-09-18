"""Test configuration for core unit tests.

test_eval_regressions.py and test_okr_progress.py import
``modulo.api.main`` at module-import time; that import chain constructs a
``Settings`` instance requiring DATABASE_URL/SECRET_KEY/FERNET_KEY. There is
no ``.env`` in worktrees, so provide the minimum env the same way as
``tests/unit/api/conftest.py`` - setdefault so explicit CI values always win.
"""

import os

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://localhost/test")
os.environ.setdefault("SECRET_KEY", "a" * 32)
os.environ.setdefault("FERNET_KEY", "a" * 32)
os.environ.setdefault("REDIS_URL", "")
os.environ.setdefault("MODULO_ADMIN_PASSWORD", "test")
os.environ.setdefault("MODULO_CSRF_ENABLED", "false")
