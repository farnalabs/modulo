"""Test configuration for graph_validator unit tests.

The FAR-1743 ``{{ model }}`` coverage tests call ``get_settings()`` at test
time (to point ``model_class_registry_path`` at a temp registry); the backend
``Settings`` model requires DATABASE_URL/SECRET_KEY/FERNET_KEY and there is no
``.env`` in worktrees, so provide the minimum env the same way as
``tests/unit/db/conftest.py`` — setdefault so explicit CI values always win.
"""

import os

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://localhost/test")
os.environ.setdefault("SECRET_KEY", "a" * 32)
os.environ.setdefault("FERNET_KEY", "a" * 32)
os.environ.setdefault("REDIS_URL", "")
os.environ.setdefault("MODULO_ADMIN_PASSWORD", "test")
os.environ.setdefault("MODULO_CSRF_ENABLED", "false")
