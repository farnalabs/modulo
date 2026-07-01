"""pytest fixtures for trigger engine tests."""
import os

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://localhost/test")
os.environ.setdefault("SECRET_KEY", "temp-secret-key-for-migration-xxx")
os.environ.setdefault("FERNET_KEY", "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=")
