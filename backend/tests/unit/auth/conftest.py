"""Test configuration for auth unit tests.

Sets minimal env vars so ``get_settings()`` can construct a ``Settings``
instance at import time (e.g. when importing ``modulo.api.main.app``).
"""

import os

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://localhost/test")
os.environ.setdefault("SECRET_KEY", "a" * 32)
os.environ.setdefault("FERNET_KEY", "a" * 32)
os.environ.setdefault("REDIS_URL", "")
os.environ.setdefault("MODULO_ADMIN_PASSWORD", "test")
os.environ.setdefault("MODULO_CSRF_ENABLED", "false")
