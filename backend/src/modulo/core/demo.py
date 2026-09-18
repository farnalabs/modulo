"""Demo auto-login gate (FAR-535) — neutral configuration surface.

Lives outside the API and DB layers so both the auth route
(``modulo.api.routes.auth``) and the boot seed (``modulo.db.seed_demo``)
share ONE definition of the feature gate and the demo-org identity
constants. Pure config: imports nothing from the DB or API layers and
reads nothing but ``modulo.settings`` — no secrets are logged here.
"""

from modulo.settings import Settings

DEMO_ORG_SLUG = "demo"
# Read-only: viewer is the bottom of the org-role hierarchy (ADR 017) and every
# mutating route requires runner/operator/admin through require_permission.
DEMO_ORG_ROLE = "viewer"


def demo_login_config(settings: Settings) -> tuple[str, str] | None:
    """Return ``(email, password)`` when the demo experience is fully configured.

    ``None`` when the kill switch is off or either credential env var is empty —
    both the demo endpoint and the seed treat that identically (feature absent).
    """
    if not settings.modulo_demo_enabled:
        return None
    email = settings.modulo_demo_user.strip()
    password = settings.modulo_demo_password
    if not email or not password:
        return None
    return (email, password)
