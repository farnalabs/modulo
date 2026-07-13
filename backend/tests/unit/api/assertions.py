"""Shared assertions for stable API error response contracts."""

from httpx import Response


def assert_feature_requires_database_update(response: Response) -> None:
    """Assert the public response used when a feature needs a DB update."""
    assert response.status_code == 501
    detail = response.json()["detail"].lower()
    assert "feature is not available" in detail
    assert "database update" in detail
