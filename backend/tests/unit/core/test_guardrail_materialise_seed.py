"""Unit tests for the FAR-1593 creation-time org-guardrail seed.

``seed_org_guardrail_rows_for_pipeline`` runs inside every pipeline create and
clone. These tests pin its branches: nothing applied (fast no-op), a stored
snapshot the config parser rejects (FAILS CLOSED — HTTP 422 rolls the caller's
transaction back so no unguarded pipeline is ever committed), an empty applied
set, the create path, the update path, and a node-bound name collision that
must be left alone. The end-to-end behaviour — a pipeline created through
``POST /api/v1/pipelines`` after an apply carrying the org rows, and a CLONE
doing the same — is covered by
``tests/integration/test_guardrail_pipeline_creation_freshness.py`` against
real Postgres.
"""

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException, status

from modulo.core.guardrails.config import GuardrailPin
from modulo.core.guardrails.materialise import seed_org_guardrail_rows_for_pipeline

_ORG_ID = uuid.uuid4()
_PIPELINE_ID = uuid.uuid4()
_ACCOUNT_ID = uuid.uuid4()

_VALID_YAML = """version: 1
guardrails:
  - id: no-aws-keys
    name: Block AWS keys
    action: block
    detection:
      type: regex
      pattern: 'AKIA[0-9A-Z]{16}'
      field: body
"""

_EMPTY_YAML = "version: 1\nguardrails: []\n"


def _pin(snapshot: str | None) -> dict:
    return GuardrailPin(
        org_id=_ORG_ID,
        applied_hash="applied-hash",
        applied_at="2026-01-01T00:00:00+00:00",
        serialized_snapshot=snapshot,
        status="clean",
    ).to_json()


def _eval_row(*, name: str, node_id: uuid.UUID | None) -> MagicMock:
    row = MagicMock()
    row.id = uuid.uuid4()
    row.name = name
    row.node_id = node_id
    return row


@pytest.mark.asyncio
class TestSeedOrgGuardrailRowsForPipeline:
    async def test_no_pin_is_a_noop(self) -> None:
        session = AsyncMock()
        with (
            patch("modulo.db.crud.guardrail_config.get_guardrail_pin", new_callable=AsyncMock, return_value=None),
            patch(
                "modulo.core.guardrails.materialise.create_or_update_eval",
                new_callable=AsyncMock,
            ) as mock_create,
        ):
            created = await seed_org_guardrail_rows_for_pipeline(
                session,
                org_id=_ORG_ID,
                pipeline_id=_PIPELINE_ID,
                account_id=_ACCOUNT_ID,
            )
        assert created == 0
        mock_create.assert_not_called()
        session.flush.assert_not_awaited()

    async def test_invalid_stored_snapshot_fails_closed(self) -> None:
        """A snapshot the config parser rejects must FAIL CLOSED (FAR-1593 QA).

        Silently seeding nothing would commit the pipeline with zero org-level
        guardrail rows while the drift surface still reads clean — exactly the
        unguarded-pipeline gap this seed exists to close. The seed must raise
        the same typed HTTP 422 the adjacent ``validate_guardrail_request``
        path uses, so the caller's transaction rolls back and no unguarded
        pipeline is ever committed.
        """
        session = AsyncMock()
        with (
            patch(
                "modulo.db.crud.guardrail_config.get_guardrail_pin",
                new_callable=AsyncMock,
                return_value=_pin("not: [valid: yaml"),
            ),
            patch(
                "modulo.core.guardrails.materialise.create_or_update_eval",
                new_callable=AsyncMock,
            ) as mock_create,
            pytest.raises(HTTPException) as exc_info,
        ):
            await seed_org_guardrail_rows_for_pipeline(
                session,
                org_id=_ORG_ID,
                pipeline_id=_PIPELINE_ID,
                account_id=_ACCOUNT_ID,
            )
        assert exc_info.value.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT
        # Actionable: the operator is told what to do, not just that it broke.
        assert "re-apply" in exc_info.value.detail
        mock_create.assert_not_called()

    async def test_pin_that_deserialises_to_none_is_a_noop(self) -> None:
        """An empty (but dict-typed) pin is no applied config — fast no-op.

        ``GuardrailPin.from_json`` returns ``None`` for a falsy mapping, so the
        ``pin is None`` arm of the seed's guard must return 0 without touching
        the pipeline rows.
        """
        session = AsyncMock()
        with (
            patch(
                "modulo.db.crud.guardrail_config.get_guardrail_pin",
                new_callable=AsyncMock,
                return_value={},
            ),
            patch(
                "modulo.core.guardrails.materialise.create_or_update_eval",
                new_callable=AsyncMock,
            ) as mock_create,
        ):
            created = await seed_org_guardrail_rows_for_pipeline(
                session,
                org_id=_ORG_ID,
                pipeline_id=_PIPELINE_ID,
                account_id=_ACCOUNT_ID,
            )
        assert created == 0
        mock_create.assert_not_called()
        session.flush.assert_not_awaited()

    async def test_applied_pin_without_snapshot_is_a_noop(self) -> None:
        """A pin with no ``serialized_snapshot`` carries nothing to materialise.

        This is the ``not pin.serialized_snapshot`` arm of the same guard: the
        pin exists (so it is not the earlier no-pin fast path) but no applied
        config snapshot was recorded, so the seed must return 0.
        """
        session = AsyncMock()
        with (
            patch(
                "modulo.db.crud.guardrail_config.get_guardrail_pin",
                new_callable=AsyncMock,
                return_value=_pin(None),
            ),
            patch(
                "modulo.core.guardrails.materialise.create_or_update_eval",
                new_callable=AsyncMock,
            ) as mock_create,
        ):
            created = await seed_org_guardrail_rows_for_pipeline(
                session,
                org_id=_ORG_ID,
                pipeline_id=_PIPELINE_ID,
                account_id=_ACCOUNT_ID,
            )
        assert created == 0
        mock_create.assert_not_called()
        session.flush.assert_not_awaited()

    async def test_empty_applied_set_is_a_noop(self) -> None:
        session = AsyncMock()
        with (
            patch(
                "modulo.db.crud.guardrail_config.get_guardrail_pin",
                new_callable=AsyncMock,
                return_value=_pin(_EMPTY_YAML),
            ),
            patch(
                "modulo.core.guardrails.materialise.create_or_update_eval",
                new_callable=AsyncMock,
            ) as mock_create,
        ):
            created = await seed_org_guardrail_rows_for_pipeline(
                session,
                org_id=_ORG_ID,
                pipeline_id=_PIPELINE_ID,
                account_id=_ACCOUNT_ID,
            )
        assert created == 0
        mock_create.assert_not_called()

    async def test_creates_missing_org_level_row(self) -> None:
        session = AsyncMock()
        with (
            patch(
                "modulo.db.crud.guardrail_config.get_guardrail_pin",
                new_callable=AsyncMock,
                return_value=_pin(_VALID_YAML),
            ),
            patch(
                "modulo.db.crud.guardrail_config.load_pipeline_guardrail_rows",
                new_callable=AsyncMock,
                return_value=[],
            ),
            patch(
                "modulo.core.guardrails.materialise.create_or_update_eval",
                new_callable=AsyncMock,
            ) as mock_create,
        ):
            created = await seed_org_guardrail_rows_for_pipeline(
                session,
                org_id=_ORG_ID,
                pipeline_id=_PIPELINE_ID,
                account_id=_ACCOUNT_ID,
            )
        assert created == 1
        mock_create.assert_awaited_once()
        kwargs = mock_create.call_args.kwargs
        assert kwargs["name"] == "no-aws-keys"
        assert kwargs["eval_type"] == "guardrail"
        assert kwargs["node_id"] is None
        assert kwargs["pipeline_id"] == _PIPELINE_ID
        assert kwargs["config_json"]["action"] == "block"
        session.flush.assert_awaited()

    async def test_existing_org_level_row_is_updated_not_duplicated(self) -> None:
        session = AsyncMock()
        existing = _eval_row(name="no-aws-keys", node_id=None)
        with (
            patch(
                "modulo.db.crud.guardrail_config.get_guardrail_pin",
                new_callable=AsyncMock,
                return_value=_pin(_VALID_YAML),
            ),
            patch(
                "modulo.db.crud.guardrail_config.load_pipeline_guardrail_rows",
                new_callable=AsyncMock,
                return_value=[existing],
            ),
            patch(
                "modulo.core.guardrails.materialise.create_or_update_eval",
                new_callable=AsyncMock,
            ) as mock_create,
        ):
            created = await seed_org_guardrail_rows_for_pipeline(
                session,
                org_id=_ORG_ID,
                pipeline_id=_PIPELINE_ID,
                account_id=_ACCOUNT_ID,
            )
        assert created == 0
        mock_create.assert_awaited_once()
        assert mock_create.call_args.kwargs["existing_eval_id"] == existing.id

    async def test_node_bound_name_collision_is_left_alone(self) -> None:
        """A node-bound row owns its name — the seed must neither overwrite
        nor count it as materialised."""
        session = AsyncMock()
        node_bound = _eval_row(name="no-aws-keys", node_id=uuid.uuid4())
        with (
            patch(
                "modulo.db.crud.guardrail_config.get_guardrail_pin",
                new_callable=AsyncMock,
                return_value=_pin(_VALID_YAML),
            ),
            patch(
                "modulo.db.crud.guardrail_config.load_pipeline_guardrail_rows",
                new_callable=AsyncMock,
                return_value=[node_bound],
            ),
            patch(
                "modulo.core.guardrails.materialise.create_or_update_eval",
                new_callable=AsyncMock,
            ) as mock_create,
        ):
            created = await seed_org_guardrail_rows_for_pipeline(
                session,
                org_id=_ORG_ID,
                pipeline_id=_PIPELINE_ID,
                account_id=_ACCOUNT_ID,
            )
        assert created == 0
        mock_create.assert_not_called()
        session.flush.assert_not_awaited()
