"""Test configuration for MCP unit tests.

Seeds the minimal env vars ``get_settings()`` needs so a standalone
``pytest tests/unit/mcp/`` run works without a ``DATABASE_URL`` /
``SECRET_KEY`` / ``FERNET_KEY`` ``Settings`` ValidationError (FAR-1684).
Only ``tests/unit/api/conftest.py`` seeded these before, so any MCP tool
path that constructs ``Settings`` at request time (e.g. ``create_connector``'s
secretref credential resolution) failed in a clean shell while the rest of
the suite stayed green.

The values are the SAME CI-equivalent ones ``tests/unit/api/conftest.py``
uses. Module-level ``os.environ.setdefault`` only — deliberately no autouse
fixtures here (see the FAR-1229 note in ``tests/unit/api/conftest.py`` about
conftest autouse fixtures dropping under interleaved argv).
"""

import os

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://localhost/test")
os.environ.setdefault("SECRET_KEY", "a" * 32)
os.environ.setdefault("FERNET_KEY", "a" * 32)
os.environ.setdefault("REDIS_URL", "")
os.environ.setdefault("MODULO_ADMIN_PASSWORD", "test")
os.environ.setdefault("MODULO_CSRF_ENABLED", "false")
