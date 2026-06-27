"""Test configuration for CLI unit tests."""

import os
import uuid

import pytest

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///./test.db")
os.environ.setdefault("SECRET_KEY", "a" * 32)
os.environ.setdefault("FERNET_KEY", "a" * 32)
os.environ.setdefault("REDIS_URL", "")
os.environ.setdefault("MODULO_ADMIN_PASSWORD", "test")


class MockColumn:
    def __init__(self, name: str):
        self.name = name


class MockTable:
    def __init__(self, columns: list[str]):
        self.columns = [MockColumn(c) for c in columns]


class MockModel:
    """Model-like object with __table__ introspection for _serialise_row."""

    def __init__(self, **kwargs: object) -> None:
        self.__table__ = MockTable(list(kwargs.keys()))
        for k, v in kwargs.items():
            setattr(self, k, v)


@pytest.fixture
def org_id() -> uuid.UUID:
    return uuid.UUID("00000000-0000-0000-0000-000000000001")


@pytest.fixture
def admin_user_id() -> uuid.UUID:
    return uuid.UUID("00000000-0000-0000-0000-000000000002")


@pytest.fixture
def mock_organisation(org_id: uuid.UUID) -> MockModel:
    return MockModel(
        id=org_id,
        name="Test Org",
        slug="test-org",
        status="active",
        created_at="2024-01-01T00:00:00+00:00",
    )


@pytest.fixture
def mock_admin_user(admin_user_id: uuid.UUID, org_id: uuid.UUID) -> MockModel:
    return MockModel(
        id=admin_user_id,
        organisation_id=org_id,
        email="admin@test.com",
        display_name="Admin",
        org_role="admin",
        active=True,
    )


@pytest.fixture
def mock_pipeline(org_id: uuid.UUID) -> MockModel:
    return MockModel(
        id=uuid.UUID("00000000-0000-0000-0000-000000000010"),
        organisation_id=org_id,
        name="Test Pipeline",
        description="A pipeline",
    )


@pytest.fixture
def mock_user(org_id: uuid.UUID) -> MockModel:
    return MockModel(
        id=uuid.UUID("00000000-0000-0000-0000-000000000020"),
        organisation_id=org_id,
        email="user@test.com",
        display_name="User",
        org_role="runner",
    )
