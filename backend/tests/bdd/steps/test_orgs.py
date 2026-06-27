"""Step definitions for organisation management features: onboarding, membership."""

from pytest_bdd import given, scenarios

# ---------------------------------------------------------------------------
# Register feature files
# ---------------------------------------------------------------------------
try:
    scenarios("../../features/organisation/org_scoping.feature")
except (FileNotFoundError, OSError):
    pass
try:
    scenarios("../../features/organisation/rls_isolation.feature")
except (FileNotFoundError, OSError):
    pass

# ===========================================================================
# orgs/org_onboarding.feature  —  TODO stub (no scenarios yet)
# ===========================================================================


@given("a new organisation signs up")
def step_new_org_signup() -> bool:
    """Placeholder: new org onboarding flow."""
    return True


@given("the initial admin user is created")
def step_initial_admin() -> bool:
    """Placeholder: initial admin user creation during onboarding."""
    return True


@given("the welcome flow is completed")
def step_welcome_flow() -> bool:
    """Placeholder: post-onboarding welcome flow."""
    return True


# ===========================================================================
# orgs/member_management.feature  —  TODO stub (no scenarios yet)
# ===========================================================================


@given("I invite a user to my organisation")
def step_invite_user() -> bool:
    """Placeholder: member invitation scenario."""
    return True


@given("I remove a user from my organisation")
def step_remove_user() -> bool:
    """Placeholder: member removal scenario."""
    return True


@given("I change a user's role")
def step_change_role() -> bool:
    """Placeholder: role change scenario."""
    return True


@given("the organisation is at its seat limit")
def step_seat_limit() -> bool:
    """Placeholder: seat limit enforcement scenario."""
    return True
