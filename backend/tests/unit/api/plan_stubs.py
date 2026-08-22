"""Shared plan-context stubs for unit tests that exercise feature gates.

These replace the copy-pasted ``_AllFeatures`` / ``_CommunityPlan`` / ``_TeamPlan``
classes that used to be re-declared in every API test. Centralising them here
keeps the team/community plan shapes in one place and avoids duplicated test
scaffolding (which trips the SonarCloud new-code duplication gate).
"""

from __future__ import annotations


class PlanStub:
    """Minimal plan-context double driven by a single ``enabled`` flag.

    Mirrors the surface the routes read: ``feature_enabled(name)``,
    ``list_enabled_features()``, ``tier()`` and ``has_license_key()``.
    """

    def __init__(self, *, enabled: bool, tier: str) -> None:
        self._enabled = enabled
        self._tier = tier

    def feature_enabled(self, name: str) -> bool:
        return self._enabled

    def list_enabled_features(self) -> list[str]:
        return []

    def tier(self) -> str:
        return self._tier

    def has_license_key(self) -> bool:
        return self._enabled


def all_features() -> PlanStub:
    """Plan stub with every feature enabled (paid team license)."""
    return PlanStub(enabled=True, tier="team")


def community_features() -> PlanStub:
    """Plan stub with every feature disabled (community / no license)."""
    return PlanStub(enabled=False, tier="community")
