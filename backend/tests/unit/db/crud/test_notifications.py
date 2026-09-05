"""Unit tests for notification CRUD paths not covered elsewhere (mocked session)."""

import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.exc import IntegrityError, ProgrammingError
from sqlalchemy.ext.asyncio import AsyncSession

_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")
_NOTIF_ID = uuid.UUID("00000000-0000-0000-0000-000000000003")


@pytest.fixture
def mock_session() -> AsyncMock:
    return AsyncMock(spec=AsyncSession)


def _make_notification(**overrides: object) -> MagicMock:
    notification = MagicMock()
    notification.id = overrides.get("id", _NOTIF_ID)
    notification.organisation_id = _ORG_ID
    notification.scope = overrides.get("scope", "org")
    notification.level = overrides.get("level", "warning")
    notification.category = overrides.get("category", "runs")
    notification.title = "t"
    notification.body = "b"
    notification.dismiss_strategy = overrides.get("dismiss_strategy", "user_only")
    notification.target_user_id = overrides.get("target_user_id")
    return notification


def _listing_result(items: list[object]) -> MagicMock:
    result = MagicMock()
    scalars = MagicMock()
    scalars.all = MagicMock(return_value=items)
    result.scalars = MagicMock(return_value=scalars)
    return result


def _count_result(value: int) -> MagicMock:
    result = MagicMock()
    result.scalar_one = MagicMock(return_value=value)
    return result


class TestCreateNotification:
    async def test_sets_default_expiry_90_days_out(self, mock_session: AsyncMock) -> None:
        with patch("modulo.db.crud.notifications.Notification", return_value=_make_notification()) as notif_cls:
            from modulo.db.crud.notifications import create_notification

            result = await create_notification(
                mock_session,
                org_id=_ORG_ID,
                scope="org",
                level="warning",
                category="runs",
                title="t",
                body="b",
            )
            now = datetime.now(UTC)
            expires = notif_cls.call_args.kwargs["expires_at"]
            delta = expires - (now + timedelta(days=89, hours=23))
            assert timedelta(0) <= delta <= timedelta(hours=2)
            assert result is not None
            mock_session.add.assert_called_once()
            mock_session.flush.assert_awaited_once()

    async def test_explicit_expiry_respected(self, mock_session: AsyncMock) -> None:
        with patch("modulo.db.crud.notifications.Notification", return_value=_make_notification()) as notif_cls:
            from modulo.db.crud.notifications import create_notification

            expiry = datetime(2031, 1, 1, tzinfo=UTC)
            await create_notification(
                mock_session,
                org_id=_ORG_ID,
                scope="org",
                level="info",
                category="other",
                title="t",
                body="b",
                action_url="https://link",
                dismiss_strategy="org_admin",
                dismissible_at_scope=True,
                target_user_id=_USER_ID,
                expires_at=expiry,
            )
        kwargs = notif_cls.call_args.kwargs
        assert kwargs["expires_at"] == expiry
        assert kwargs["action_url"] == "https://link"
        assert kwargs["dismiss_strategy"] == "org_admin"
        assert kwargs["dismissible_at_scope"] is True
        assert kwargs["target_user_id"] == _USER_ID


class TestGetNotification:
    async def test_without_user_id_returns_org_notification(self, mock_session: AsyncMock) -> None:
        notification = _make_notification()
        mock_session.execute = AsyncMock(
            return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=notification))
        )
        from modulo.db.crud.notifications import get_notification

        assert await get_notification(mock_session, org_id=_ORG_ID, notification_id=_NOTIF_ID) is notification

    async def test_with_user_id_applies_visibility_clause(self, mock_session: AsyncMock) -> None:
        mock_session.execute = AsyncMock(return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=None)))
        from modulo.db.crud.notifications import get_notification

        result = await get_notification(mock_session, org_id=_ORG_ID, notification_id=_NOTIF_ID, user_id=_USER_ID)
        assert result is None

    async def test_missing_notification_returns_none(self, mock_session: AsyncMock) -> None:
        mock_session.execute = AsyncMock(return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=None)))
        from modulo.db.crud.notifications import get_notification

        assert await get_notification(mock_session, org_id=_ORG_ID, notification_id=uuid.uuid4()) is None


class TestDashboardNotifications:
    async def test_returns_visible_notifications(self, mock_session: AsyncMock) -> None:
        notifications = [_make_notification(), _make_notification(level="error")]
        mock_session.execute = AsyncMock(return_value=_listing_result(notifications))
        from modulo.db.crud.notifications import get_dashboard_notifications

        result = await get_dashboard_notifications(mock_session, org_id=_ORG_ID, user_id=_USER_ID)
        assert result == notifications

    async def test_limit_clamped_to_five_when_out_of_range(self, mock_session: AsyncMock) -> None:
        mock_session.execute = AsyncMock(return_value=_listing_result([]))
        from modulo.db.crud.notifications import get_dashboard_notifications

        assert not await get_dashboard_notifications(mock_session, org_id=_ORG_ID, user_id=_USER_ID, limit=0)
        assert not await get_dashboard_notifications(mock_session, org_id=_ORG_ID, user_id=_USER_ID, limit=500)

    async def test_min_level_filter_admits_lower_levels(self, mock_session: AsyncMock) -> None:
        mock_session.execute = AsyncMock(return_value=_listing_result([]))
        from modulo.db.crud.notifications import get_dashboard_notifications

        assert not await get_dashboard_notifications(mock_session, org_id=_ORG_ID, user_id=_USER_ID, min_level="debug")

    async def test_programming_error_returns_empty_list(self, mock_session: AsyncMock) -> None:
        mock_session.execute = AsyncMock(side_effect=ProgrammingError("42P01", None, Exception("boom")))
        from modulo.db.crud.notifications import get_dashboard_notifications

        assert not await get_dashboard_notifications(mock_session, org_id=_ORG_ID, user_id=_USER_ID)


class TestNotificationsForUser:
    async def test_all_filters_applied(self, mock_session: AsyncMock) -> None:
        mock_session.execute = AsyncMock(return_value=_listing_result([]))
        from modulo.db.crud.notifications import get_notifications_for_user

        result = await get_notifications_for_user(
            mock_session,
            org_id=_ORG_ID,
            user_id=_USER_ID,
            level="error",
            scope="user",
            category="runs",
            status_filter="active",
            limit=10,
            offset=5,
        )
        assert result == []

    async def test_status_filters_apply(self, mock_session: AsyncMock) -> None:
        mock_session.execute = AsyncMock(return_value=_listing_result([]))
        from modulo.db.crud.notifications import get_notifications_for_user

        for status in ("active", "dismissed_self", "dismissed_scope"):
            result = await get_notifications_for_user(
                mock_session,
                org_id=_ORG_ID,
                user_id=_USER_ID,
                status_filter=status,
            )
            assert result == []

    async def test_programming_error_returns_empty_list(self, mock_session: AsyncMock) -> None:
        mock_session.execute = AsyncMock(side_effect=ProgrammingError("42P01", None, Exception("boom")))
        from modulo.db.crud.notifications import get_notifications_for_user

        assert not await get_notifications_for_user(mock_session, org_id=_ORG_ID, user_id=_USER_ID)


class TestCountNotifications:
    async def test_counts_with_filters(self, mock_session: AsyncMock) -> None:
        mock_session.execute = AsyncMock(return_value=_count_result(4))
        from modulo.db.crud.notifications import count_notifications_for_user

        result = await count_notifications_for_user(
            mock_session,
            org_id=_ORG_ID,
            user_id=_USER_ID,
            level="warning",
            scope="org",
            category="runs",
            status_filter="dismissed_self",
        )
        assert result == 4

    async def test_programming_error_returns_zero(self, mock_session: AsyncMock) -> None:
        mock_session.execute = AsyncMock(side_effect=ProgrammingError("42P01", None, Exception("boom")))
        from modulo.db.crud.notifications import count_notifications_for_user

        assert await count_notifications_for_user(mock_session, org_id=_ORG_ID, user_id=_USER_ID) == 0

    async def test_unread_count_and_programming_error(self, mock_session: AsyncMock) -> None:
        mock_session.execute = AsyncMock(
            side_effect=[
                _count_result(2),
                ProgrammingError("42P01", None, Exception("boom")),
            ]
        )
        from modulo.db.crud.notifications import get_unread_count

        assert await get_unread_count(mock_session, org_id=_ORG_ID, user_id=_USER_ID) == 2
        assert await get_unread_count(mock_session, org_id=_ORG_ID, user_id=_USER_ID) == 0


class TestDismissNotification:
    async def test_not_found_raises(self, mock_session: AsyncMock) -> None:
        mock_session.execute = AsyncMock(return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=None)))
        from modulo.db.crud.notifications import dismiss_notification

        with pytest.raises(ValueError, match="not found"):
            await dismiss_notification(
                mock_session,
                notification_id=_NOTIF_ID,
                user_id=_USER_ID,
                org_id=_ORG_ID,
            )

    async def test_scope_dismiss_refused_for_user_only_strategy(self, mock_session: AsyncMock) -> None:
        self._patch_notification_lookup(mock_session)
        from modulo.db.crud.notifications import dismiss_notification

        with pytest.raises(ValueError, match="cannot be dismissed for all users"):
            await dismiss_notification(
                mock_session,
                notification_id=_NOTIF_ID,
                user_id=_USER_ID,
                org_id=_ORG_ID,
                dismiss_scope="scope",
            )

    async def test_scope_dismiss_requires_admin_for_org_admin_strategy(self, mock_session: AsyncMock) -> None:
        self._patch_notification_lookup(
            mock_session,
            notification=_make_notification(dismiss_strategy="org_admin"),
        )
        from modulo.db.crud.notifications import dismiss_notification

        with pytest.raises(ValueError, match="Only admins"):
            await dismiss_notification(
                mock_session,
                notification_id=_NOTIF_ID,
                user_id=_USER_ID,
                org_id=_ORG_ID,
                dismiss_scope="scope",
            )

    async def test_scope_dismiss_allowed_for_org_admin(self, mock_session: AsyncMock) -> None:
        admin_notification = _make_notification(dismiss_strategy="org_admin")
        self._patch_notification_lookup(mock_session, notification=admin_notification)
        mock_session.execute = AsyncMock(
            side_effect=[
                MagicMock(scalar_one_or_none=MagicMock(return_value=admin_notification)),
                MagicMock(scalar_one_or_none=MagicMock(return_value=None)),
            ]
        )
        from modulo.db.crud.notifications import dismiss_notification

        result = await dismiss_notification(
            mock_session,
            notification_id=_NOTIF_ID,
            user_id=_USER_ID,
            org_id=_ORG_ID,
            dismiss_scope="scope",
            is_admin=True,
        )
        assert result.notification_id == _NOTIF_ID
        assert result.dismissed_by_user_id == _USER_ID
        assert result.dismiss_scope == "scope"
        assert result.organisation_id == _ORG_ID
        mock_session.add.assert_called_once()

    async def test_already_dismissed_raises(self, mock_session: AsyncMock) -> None:
        self._patch_notification_lookup(mock_session)
        mock_session.execute = AsyncMock(
            side_effect=[
                MagicMock(scalar_one_or_none=MagicMock(return_value=_make_notification())),
                MagicMock(scalar_one_or_none=MagicMock(return_value=MagicMock())),
            ]
        )
        from modulo.db.crud.notifications import dismiss_notification

        with pytest.raises(ValueError, match="already dismissed by this user"):
            await dismiss_notification(
                mock_session,
                notification_id=_NOTIF_ID,
                user_id=_USER_ID,
                org_id=_ORG_ID,
            )

    async def test_integrity_error_wrapped_as_value_error(self, mock_session: AsyncMock) -> None:
        self._patch_notification_lookup(mock_session)
        mock_session.flush = AsyncMock(side_effect=IntegrityError("dup", None, Exception("dup")))
        from modulo.db.crud.notifications import dismiss_notification

        with pytest.raises(ValueError, match="concurrent"):
            await dismiss_notification(
                mock_session,
                notification_id=_NOTIF_ID,
                user_id=_USER_ID,
                org_id=_ORG_ID,
            )

    async def test_successful_self_dismiss(self, mock_session: AsyncMock) -> None:
        self._patch_notification_lookup(mock_session)
        mock_session.execute = AsyncMock(
            side_effect=[
                MagicMock(scalar_one_or_none=MagicMock(return_value=_make_notification())),
                MagicMock(scalar_one_or_none=MagicMock(return_value=None)),
            ]
        )
        from modulo.db.crud.notifications import dismiss_notification

        result = await dismiss_notification(
            mock_session,
            notification_id=_NOTIF_ID,
            user_id=_USER_ID,
            org_id=_ORG_ID,
        )
        assert result.notification_id == _NOTIF_ID
        assert result.dismissed_by_user_id == _USER_ID
        assert result.dismiss_scope == "self"
        assert result.organisation_id == _ORG_ID
        mock_session.add.assert_called_once()

    async def test_review_later_delegates_to_self_dismiss(self, mock_session: AsyncMock) -> None:
        self._patch_notification_lookup(mock_session)
        mock_session.execute = AsyncMock(
            side_effect=[
                MagicMock(scalar_one_or_none=MagicMock(return_value=_make_notification())),
                MagicMock(scalar_one_or_none=MagicMock(return_value=None)),
            ]
        )
        from modulo.db.crud.notifications import review_later

        result = await review_later(
            mock_session,
            notification_id=_NOTIF_ID,
            user_id=_USER_ID,
            org_id=_ORG_ID,
        )
        assert result.dismiss_scope == "self"
        assert result.notification_id == _NOTIF_ID
        assert result.dismissed_by_user_id == _USER_ID

    async def test_review_later_propagates_not_found(self, mock_session: AsyncMock) -> None:
        mock_session.execute = AsyncMock(return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=None)))
        from modulo.db.crud.notifications import review_later

        with pytest.raises(ValueError, match="not found"):
            await review_later(
                mock_session,
                notification_id=_NOTIF_ID,
                user_id=_USER_ID,
                org_id=_ORG_ID,
            )

    @staticmethod
    def _patch_notification_lookup(
        mock_session: AsyncMock,
        *,
        notification: object | None = None,
    ) -> None:
        first = MagicMock(scalar_one_or_none=MagicMock(return_value=notification or _make_notification()))
        second = MagicMock(scalar_one_or_none=MagicMock(return_value=None))
        mock_session.execute = AsyncMock(side_effect=[first, second])


class TestPreferences:
    async def test_get_opted_out_categories(self, mock_session: AsyncMock) -> None:
        result = MagicMock()
        scalars = MagicMock()
        scalars.all = MagicMock(return_value=["billing", "run"])
        result.scalars = MagicMock(return_value=scalars)
        mock_session.execute = AsyncMock(return_value=result)
        from modulo.db.crud.notifications import get_opted_out_categories

        assert await get_opted_out_categories(mock_session, org_id=_ORG_ID, account_id=_USER_ID) == {"billing", "run"}

    async def test_set_preferences_empty_is_noop(self, mock_session: AsyncMock) -> None:
        from modulo.db.crud.notifications import set_notification_preferences

        await set_notification_preferences(mock_session, org_id=_ORG_ID, account_id=_USER_ID, opt_outs={})
        mock_session.execute.assert_not_awaited()
        mock_session.flush.assert_not_awaited()

    async def test_set_preferences_adds_and_removes(self, mock_session: AsyncMock) -> None:
        existing_result = MagicMock()
        scalars = MagicMock()
        scalars.all = MagicMock(return_value=["keep", "drop"])
        existing_result.scalars = MagicMock(return_value=scalars)
        delete_result = MagicMock(rowcount=2)
        mock_session.execute = AsyncMock(side_effect=[existing_result, delete_result])
        from modulo.db.crud.notifications import set_notification_preferences

        await set_notification_preferences(
            mock_session,
            org_id=_ORG_ID,
            account_id=_USER_ID,
            opt_outs={"drop": False, "keep": True, "new": True},
        )
        mock_session.add_all.assert_called_once()
        added = mock_session.add_all.call_args.args[0]
        added_list = list(added)
        assert len(added_list) == 1
        assert added_list[0].category == "new"
        assert added_list[0].organisation_id == _ORG_ID
        assert added_list[0].account_id == _USER_ID
        mock_session.flush.assert_awaited_once()
