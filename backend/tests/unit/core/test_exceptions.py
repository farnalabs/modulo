"""Unit tests for modulo.core.exceptions.

QA lens pass (correctness, bugs, maintainability, deps) on the core exception
types. Every exception here is a cross-cutting contract: it is caught or
inspected by name (and, for ``OrgDeletedError``/``TriggersPausedError``, by
attribute) in 33+ call sites across the API routes, the MCP server, the cron
helpers, and the trigger engine. These tests lock the attribute surface and
message shapes those call sites depend on so a rename or a message-format
change is caught at the unit layer instead of by a production regression.
"""

import uuid

import pytest

from modulo.core.exceptions import (
    OrgDeletedError,
    SnapshotLockNotAvailableError,
    TriggersPausedError,
)


class TestSnapshotLockNotAvailableError:
    def test_is_an_exception(self) -> None:
        assert issubclass(SnapshotLockNotAvailableError, Exception)

    def test_instantiates_without_message(self) -> None:
        exc = SnapshotLockNotAvailableError()
        assert isinstance(exc, Exception)
        assert not str(exc)

    def test_instantiates_with_message(self) -> None:
        # pipeline_snapshot.py raises with a composed message.
        msg = "Cannot acquire snapshot lock for pipeline 00000000-0000-0000-0000-000000000001"
        exc = SnapshotLockNotAvailableError(msg)
        assert str(exc) == msg

    def test_is_catchable_by_type(self) -> None:
        with pytest.raises(SnapshotLockNotAvailableError):
            raise SnapshotLockNotAvailableError("busy")


class TestTriggersPausedError:
    def test_subclass_of_runtime_error(self) -> None:
        assert issubclass(TriggersPausedError, RuntimeError)

    def test_defaults_all_attributes_to_none(self) -> None:
        exc = TriggersPausedError()
        assert exc.trigger_id is None
        assert exc.org_id is None
        assert exc.trigger_type is None

    def test_message_uses_org_id(self) -> None:
        org_id = uuid.uuid4()
        exc = TriggersPausedError(org_id=org_id)
        assert str(exc) == f"Triggers paused for org {org_id}"

    def test_message_without_org_id(self) -> None:
        exc = TriggersPausedError()
        assert str(exc) == "Triggers paused for org None"

    def test_preserves_trigger_context(self) -> None:
        trigger_id = uuid.uuid4()
        org_id = uuid.uuid4()
        exc = TriggersPausedError(trigger_id=trigger_id, org_id=org_id, trigger_type="webhook")
        assert exc.trigger_id == trigger_id
        assert exc.org_id == org_id
        assert exc.trigger_type == "webhook"

    def test_is_catchable_by_type(self) -> None:
        with pytest.raises(TriggersPausedError):
            raise TriggersPausedError(trigger_type="cron")

    def test_is_runtime_error(self) -> None:
        with pytest.raises(RuntimeError):
            raise TriggersPausedError


class TestOrgDeletedError:
    def test_subclass_of_runtime_error(self) -> None:
        assert issubclass(OrgDeletedError, RuntimeError)

    def test_deleted_defaults_true(self) -> None:
        org_id = uuid.uuid4()
        exc = OrgDeletedError(org_id=org_id)
        assert exc.org_id == org_id
        assert exc.deleted is True

    def test_deleted_message(self) -> None:
        org_id = uuid.uuid4()
        exc = OrgDeletedError(org_id=org_id, deleted=True)
        assert str(exc) == f"cannot create run: organisation {org_id} is deleted"

    def test_missing_message(self) -> None:
        org_id = uuid.uuid4()
        exc = OrgDeletedError(org_id=org_id, deleted=False)
        assert str(exc) == f"cannot create run: organisation {org_id} is missing"

    def test_message_without_org_id(self) -> None:
        exc = OrgDeletedError()
        assert str(exc) == "cannot create run: organisation None is deleted"

    def test_attributes_drive_route_mapping(self) -> None:
        """runs.py / mcp_server.py branch on ``exc.deleted`` to pick 409 vs 404."""
        deleted = OrgDeletedError(org_id=uuid.uuid4(), deleted=True)
        missing = OrgDeletedError(org_id=uuid.uuid4(), deleted=False)
        assert deleted.deleted is True
        assert missing.deleted is False

    def test_is_catchable_by_type(self) -> None:
        with pytest.raises(OrgDeletedError):
            raise OrgDeletedError(org_id=uuid.uuid4(), deleted=True)
