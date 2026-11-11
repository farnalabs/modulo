"""Environment defaults required while collecting pipeline API tests."""

import os

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://localhost/test")
os.environ.setdefault("SECRET_KEY", "a" * 32)
os.environ.setdefault("FERNET_KEY", "a" * 32)
os.environ.setdefault("REDIS_URL", "")
os.environ.setdefault("FARNA_CONTROL_MODULO_ADMIN_PASSWORD", "test")
os.environ.setdefault("FARNA_CONTROL_MODULO_CSRF_ENABLED", "false")
