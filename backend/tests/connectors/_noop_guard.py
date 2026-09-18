"""Inert no-op SecurityGuard helper shared by the REST connector tests.

Extracted to a single module so the connector conformance conftest and the
REST connector unit tests reuse one definition instead of copy-pasting the
guard body (which tripped SonarCloud's copy-paste/duplication gate).
"""

from modulo.connectors.rest import SecurityGuard


def make_noop_security_guard() -> SecurityGuard:
    """Return a SecurityGuard that enforces nothing (no SSRF/injection checks)."""

    async def validate_url(url: str) -> None:
        return None

    def filter_strings(values: list[str], resource: str) -> None:
        return None

    return SecurityGuard(validate_url=validate_url, filter_strings=filter_strings)
