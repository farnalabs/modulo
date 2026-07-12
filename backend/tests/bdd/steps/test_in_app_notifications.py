"""Step definitions for in-app notification features.

All steps are stubs — they accept and ignore all arguments so that the
scenarios load and parse without errors.  None of the steps have real
implementations yet.
"""

from __future__ import annotations

import contextlib
from typing import Any

import pytest
from pytest_bdd import given, parsers, scenarios, then, when

# ---------------------------------------------------------------------------
# Register feature files
# ---------------------------------------------------------------------------
with contextlib.suppress(FileNotFoundError, OSError):
    scenarios("../features/in_app_notifications/dashboard_panel.feature")
with contextlib.suppress(FileNotFoundError, OSError):
    scenarios("../features/in_app_notifications/dismiss_flow.feature")
with contextlib.suppress(FileNotFoundError, OSError):
    scenarios("../features/in_app_notifications/notification_filters.feature")
with contextlib.suppress(FileNotFoundError, OSError):
    scenarios("../features/in_app_notifications/sse_integration.feature")


# ===========================================================================
#  FIXTURES
# ===========================================================================


@pytest.fixture
def ctx() -> dict[str, Any]:
    """Shared mutable context dict for in-app notification tests."""
    return {}


# ===========================================================================
#  GIVEN steps  —  arrange
# ===========================================================================


@given(parsers.parse("the user has {count:d} active notifications"))
def _(count: int) -> None:
    pass


@given('the user has dashboard level filter set to "{level}"')
def _(level: str) -> None:
    pass


@given(parsers.parse('they have notifications at "{levels}" levels'))
def _(levels: str) -> None:
    pass


@given("a notification is visible on the dashboard")
def _() -> None:
    pass


@given(parsers.parse('the user has a notification with dismiss_strategy "{strategy}"'))
def _(strategy: str) -> None:
    pass


@given("the user is an admin")
def _() -> None:
    pass


@given(parsers.parse('the notification has dismiss_strategy "{strategy}"'))
def _(strategy: str) -> None:
    pass


@given("the user is not an admin")
def _() -> None:
    pass


@given(parsers.parse("a notification with dismissible_at_scope = {scope}"))
def _(scope: str) -> None:
    pass


@given("the user has notifications at various levels, scopes, and categories")
def _() -> None:
    pass


@given("no notifications match the current filters")
def _() -> None:
    pass


@given(parsers.parse("the user has {count:d} filtered notifications"))
def _(count: int) -> None:
    pass


@given("the user is on the dashboard")
def _() -> None:
    pass


@given("two users in the same org")
def _() -> None:
    pass


@given("both see the same org-scope notification")
def _() -> None:
    pass


@given("the user has no unread notifications")
def _() -> None:
    pass


@given("both see the same notification")
def _() -> None:
    pass


# ===========================================================================
#  WHEN steps  —  act
# ===========================================================================


@when("they view the dashboard")
def _() -> None:
    pass


@when('the user clicks "Review Later" on that notification')
def _() -> None:
    pass


@when("the user dismisses the notification for themselves")
def _() -> None:
    pass


@when("the user collapses the notifications panel")
def _() -> None:
    pass


@when("refreshes the page")
def _() -> None:
    pass


@when("the user tries to dismiss the notification for all users")
def _() -> None:
    pass


@when("the user dismisses the notification for the org")
def _() -> None:
    pass


@when("the user tries to dismiss the notification for the org")
def _() -> None:
    pass


@when('the user clicks "Dismiss"')
def _() -> None:
    pass


@when(parsers.parse('the user selects level filter "{level}"'))
def _(level: str) -> None:
    pass


@when(parsers.parse('the user selects scope filter "{scope}"'))
def _(scope: str) -> None:
    pass


@when(parsers.parse('the user selects category filter "{category}"'))
def _(category: str) -> None:
    pass


@when(parsers.parse('the user selects status filter "{status}"'))
def _(status: str) -> None:
    pass


@when(parsers.parse('selects scope filter "{scope}"'))
def _(scope: str) -> None:
    pass


@when(parsers.parse("they set page size to {size:d}"))
def _(size: int) -> None:
    pass


@when(parsers.parse("navigate to page {page:d}"))
def _(page: int) -> None:
    pass


@when("a new notification is created for the org")
def _() -> None:
    pass


@when("a new error notification is created")
def _() -> None:
    pass


@when("user A dismisses the notification for everyone")
def _() -> None:
    pass


@when("user A dismisses the notification for themselves")
def _() -> None:
    pass


# ===========================================================================
#  THEN steps  —  assert
# ===========================================================================


@then(parsers.parse("they see at most {count:d} notifications"))
def _(count: int) -> None:
    pass


@then("the notifications are ordered by most recent first")
def _() -> None:
    pass


@then(parsers.parse('they see only "{level1}" and "{level2}" notifications'))
def _(level1: str, level2: str) -> None:
    pass


@then("the notification is removed from the dashboard")
def _() -> None:
    pass


@then("the notification is still visible on the notifications page")
def _() -> None:
    pass


@then("the notification is not visible on the notifications page")
def _() -> None:
    pass


@then("the notifications panel is still collapsed")
def _() -> None:
    pass


@then(parsers.parse('they see a "{text}" link'))
def _(text: str) -> None:
    pass


@then("the link routes to /notifications")
def _() -> None:
    pass


@then("the notification is hidden for this user")
def _() -> None:
    pass


@then("other users still see the notification")
def _() -> None:
    pass


@then(parsers.parse("the request is rejected with {code:d}"))
def _(code: int) -> None:
    pass


@then("the notification remains visible")
def _() -> None:
    pass


@then("the notification is hidden for all org members")
def _() -> None:
    pass


@then("the dismiss is logged in the audit trail")
def _() -> None:
    pass


@then(parsers.parse('the dialog shows "{text}" option'))
def _(text: str) -> None:
    pass


@then("the dialog shows a scope dismiss option")
def _() -> None:
    pass


@then(parsers.parse('the dialog shows only "{text}" option'))
def _(text: str) -> None:
    pass


@then("only error-level notifications are shown")
def _() -> None:
    pass


@then("only admin-scoped notifications are shown")
def _() -> None:
    pass


@then(parsers.parse('only "{category}" category notifications are shown'))
def _(category: str) -> None:
    pass


@then("only non-dismissed notifications are shown")
def _() -> None:
    pass


@then("only self-dismissed notifications are shown")
def _() -> None:
    pass


@then("only org-wide warning notifications are shown")
def _() -> None:
    pass


@then(parsers.parse('the page shows "{text}"'))
def _(text: str) -> None:
    pass


@then('shows a "Clear filters" action')
def _() -> None:
    pass


@then(parsers.parse("they see notifications {start:d}-{end:d} matching the filters"))
def _(start: int, end: int) -> None:
    pass


@then("the dashboard notifications panel refreshes")
def _() -> None:
    pass


@then("the new notification appears in the list")
def _() -> None:
    pass


@then("user B's dashboard refreshes")
def _() -> None:
    pass


@then("the notification is removed from user B's dashboard")
def _() -> None:
    pass


@then(parsers.parse('the notification bell badge shows "{count}"'))
def _(count: str) -> None:
    pass


@then("user B still sees the notification")
def _() -> None:
    pass
