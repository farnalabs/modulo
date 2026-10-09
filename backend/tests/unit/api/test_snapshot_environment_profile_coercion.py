"""FAR-1614: the SnapshotResponse environment-profile coercion is observable.

``_coerce_snapshot_environment_profile_id`` defensively maps a value that is
neither a ``str`` nor a ``uuid.UUID`` to ``None`` (a partial stand-in that
lacks the column must serialise as "unbound", never fail validation). Because
that coerced the value silently, a malformed read was indistinguishable from a
genuinely unbound snapshot — the coercion now emits a debug log while the
RETURNED value stays exactly as before.

These tests pin both halves: the value contract (uuid / uuid-string pass
through, everything else becomes ``None``) and the observability (the log
fires only when a non-``None`` value is actually coerced — the documented
``None``/unbound read is not a coercion and must stay quiet).
"""

from __future__ import annotations

import logging
import uuid

import pytest

from modulo.api.routes.pipelines import SnapshotResponse

_LOGGER = "modulo.api.routes.pipelines"


def _response(**overrides: object) -> SnapshotResponse:
    """Build a SnapshotResponse from the minimal required field set."""
    fields: dict[str, object] = {
        "id": uuid.uuid4(),
        "pipeline_id": uuid.uuid4(),
        "snapshot_version": 1,
        "tag": None,
        "notes": None,
        "created_at": None,
    }
    fields.update(overrides)
    return SnapshotResponse(**fields)


def _coercion_logs(caplog: pytest.LogCaptureFixture) -> list[str]:
    """Every captured message about the coerced field."""
    return [record.getMessage() for record in caplog.records if "environment_profile_id" in record.getMessage()]


def test_a_uuid_value_passes_through_unchanged_without_a_log(caplog: pytest.LogCaptureFixture) -> None:
    binding = uuid.uuid4()
    with caplog.at_level(logging.DEBUG, logger=_LOGGER):
        response = _response(environment_profile_id=binding)

    assert response.environment_profile_id == binding
    logs = _coercion_logs(caplog)
    assert not logs, logs


def test_a_uuid_string_is_kept_as_the_field_type_without_a_log(caplog: pytest.LogCaptureFixture) -> None:
    binding = uuid.uuid4()
    with caplog.at_level(logging.DEBUG, logger=_LOGGER):
        response = _response(environment_profile_id=str(binding))

    assert response.environment_profile_id == binding
    logs = _coercion_logs(caplog)
    assert not logs, logs


def test_the_documented_unbound_none_read_stays_quiet(caplog: pytest.LogCaptureFixture) -> None:
    """None is the documented legacy/missing-column read, not a coercion."""
    with caplog.at_level(logging.DEBUG, logger=_LOGGER):
        response = _response(environment_profile_id=None)

    assert response.environment_profile_id is None
    logs = _coercion_logs(caplog)
    assert not logs, logs


@pytest.mark.parametrize("unexpected", [123, {"id": "not-a-uuid"}, ["not-a-uuid"], 4.5])
def test_an_unexpected_type_is_coerced_to_none_and_logged(unexpected: object, caplog: pytest.LogCaptureFixture) -> None:
    """Observability only: the returned value is None either way (pin both)."""
    with caplog.at_level(logging.DEBUG, logger=_LOGGER):
        response = _response(environment_profile_id=unexpected)

    assert response.environment_profile_id is None
    logs = _coercion_logs(caplog)
    assert logs, "a coerced (non-None) value must be logged"
    assert type(unexpected).__name__ in logs[0]
