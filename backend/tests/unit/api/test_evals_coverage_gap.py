"""Unit tests for GET /api/v1/eval-coverage-gap (FAR-381).

The endpoint is a thin org-scoped adapter over ``compute_coverage_gap``. These
tests exercise the routing contract, the parameter pass-through, the 422 guard
for an unscoped call, error mapping, and — critically — organisation isolation:
the endpoint forwards ``principal.organisation_id`` to the signal engine, and
the group-path loader injects the explicit ``organisation_id`` predicate so a
cross-org batch/group can never be read.
"""

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from sqlalchemy.exc import IntegrityError, ProgrammingError, SQLAlchemyError

from modulo.api.routes.evals import eval_coverage_gap
from modulo.core.eval_engine.coverage_gap import CoverageGapSummary
from tests.unit.api.mock_session import configure_mock_session


def make_session_mock() -> AsyncMock:
    session = configure_mock_session(AsyncMock())
    session.execute = AsyncMock()
    begin_ctx = AsyncMock()
    begin_ctx.__aenter__ = AsyncMock(return_value=session)
    begin_ctx.__aexit__ = AsyncMock(return_value=None)
    session.begin = MagicMock(return_value=begin_ctx)
    return session


def make_mock_principal(**kwargs: object) -> MagicMock:
    p = MagicMock()
    p.organisation_id = kwargs.get("org_id", uuid.uuid4())
    p.account_id = kwargs.get("user_id", uuid.uuid4())
    p.username = kwargs.get("username", "test_user")
    p.org_role = kwargs.get("org_role", "admin")
    return p


def _summary(status: str = "complete") -> CoverageGapSummary:
    return CoverageGapSummary(
        status=status,
        batch_id=uuid.uuid4(),
        variant_group_id=None,
        run_count=3,
        min_runs=3,
        variant_divergence=0.0,
        divergence_threshold=0.15,
        differentiation_threshold=0.05,
        evals=[],
    )


@pytest.mark.asyncio
class TestEvalCoverageGap:
    async def test_returns_summary_dict(self) -> None:
        principal = make_mock_principal()
        mock_session = make_session_mock()

        summary = _summary()
        with patch(
            "modulo.api.routes.evals.compute_coverage_gap",
            new_callable=AsyncMock,
            return_value=summary,
        ) as mock_compute:
            result = await eval_coverage_gap(
                variant_group_id=None,
                batch_id=summary.batch_id,
                min_runs=3,
                threshold=0.2,
                session=mock_session,
                principal=principal,
            )

        assert result["status"] == "complete"
        assert result["batch_id"] == str(summary.batch_id)
        mock_compute.assert_awaited_once()

    async def test_forwards_org_id_for_org_isolation(self) -> None:
        """The endpoint must always scope the signal to the caller's org.

        A cross-org query can never happen because ``compute_coverage_gap`` is
        awaited with ``org_id == principal.organisation_id`` (and, downstream,
        the explicit ``organisation_id`` predicate is the BYPASSRLS isolation
        control). Asserting the forwarded org pins that boundary.
        """
        principal = make_mock_principal()
        mock_session = make_session_mock()
        batch_id = uuid.uuid4()

        with patch(
            "modulo.api.routes.evals.compute_coverage_gap",
            new_callable=AsyncMock,
            return_value=_summary(),
        ) as mock_compute:
            await eval_coverage_gap(
                variant_group_id=None,
                batch_id=batch_id,
                min_runs=3,
                threshold=0.15,
                session=mock_session,
                principal=principal,
            )

        kwargs = mock_compute.await_args.kwargs
        assert kwargs["org_id"] == principal.organisation_id
        assert kwargs["batch_id"] == batch_id
        assert kwargs["variant_group_id"] is None

    async def test_forwards_threshold_and_min_runs(self) -> None:
        principal = make_mock_principal()
        mock_session = make_session_mock()

        with patch(
            "modulo.api.routes.evals.compute_coverage_gap",
            new_callable=AsyncMock,
            return_value=_summary(),
        ) as mock_compute:
            await eval_coverage_gap(
                variant_group_id=None,
                batch_id=uuid.uuid4(),
                min_runs=5,
                threshold=0.4,
                session=mock_session,
                principal=principal,
            )

        kwargs = mock_compute.await_args.kwargs
        assert kwargs["min_runs"] == 5
        assert kwargs["divergence_threshold"] == pytest.approx(0.4)

    async def test_raises_422_when_unscoped(self) -> None:
        principal = make_mock_principal()
        mock_session = make_session_mock()

        with patch(
            "modulo.api.routes.evals.compute_coverage_gap",
            new_callable=AsyncMock,
        ) as mock_compute:
            with pytest.raises(HTTPException) as exc:
                await eval_coverage_gap(
                    variant_group_id=None,
                    batch_id=None,
                    min_runs=3,
                    threshold=0.15,
                    session=mock_session,
                    principal=principal,
                )
            assert exc.value.status_code == 422
            mock_compute.assert_not_called()

    async def test_raises_503_on_sqlalchemy_error(self) -> None:
        principal = make_mock_principal()
        mock_session = make_session_mock()

        with patch(
            "modulo.api.routes.evals.compute_coverage_gap",
            new_callable=AsyncMock,
            side_effect=SQLAlchemyError("mock", "mock", "mock"),
        ):
            with pytest.raises(HTTPException) as exc:
                await eval_coverage_gap(
                    variant_group_id=None,
                    batch_id=uuid.uuid4(),
                    min_runs=3,
                    threshold=0.15,
                    session=mock_session,
                    principal=principal,
                )
            assert exc.value.status_code == 503

    async def test_raises_500_on_unexpected_error(self) -> None:
        principal = make_mock_principal()
        mock_session = make_session_mock()

        with patch(
            "modulo.api.routes.evals.compute_coverage_gap",
            new_callable=AsyncMock,
            side_effect=ValueError("unexpected"),
        ):
            with pytest.raises(HTTPException) as exc:
                await eval_coverage_gap(
                    variant_group_id=None,
                    batch_id=uuid.uuid4(),
                    min_runs=3,
                    threshold=0.15,
                    session=mock_session,
                    principal=principal,
                )
            assert exc.value.status_code == 500

    async def test_raises_501_on_programming_error(self) -> None:
        principal = make_mock_principal()
        mock_session = make_session_mock()

        with patch(
            "modulo.api.routes.evals.compute_coverage_gap",
            new_callable=AsyncMock,
            side_effect=ProgrammingError("mock", "mock", "mock"),
        ):
            with pytest.raises(HTTPException) as exc:
                await eval_coverage_gap(
                    variant_group_id=None,
                    batch_id=uuid.uuid4(),
                    min_runs=3,
                    threshold=0.15,
                    session=mock_session,
                    principal=principal,
                )
            assert exc.value.status_code == 501

    async def test_raises_409_on_integrity_error(self) -> None:
        principal = make_mock_principal()
        mock_session = make_session_mock()

        with patch(
            "modulo.api.routes.evals.compute_coverage_gap",
            new_callable=AsyncMock,
            side_effect=IntegrityError("mock", "mock", "mock"),
        ):
            with pytest.raises(HTTPException) as exc:
                await eval_coverage_gap(
                    variant_group_id=None,
                    batch_id=uuid.uuid4(),
                    min_runs=3,
                    threshold=0.15,
                    session=mock_session,
                    principal=principal,
                )
            assert exc.value.status_code == 409


@pytest.mark.asyncio
class TestComputeCoverageGapOrgIsolation:
    async def test_group_path_injects_org_predicate(self) -> None:
        """The group-path loader must scope runs by ``organisation_id``.

        BYPASSRLS ``modulo_app`` means the explicit predicate is the ONLY
        isolation control; asserting it is present in the emitted SQL proves a
        cross-org variant group can never be read.
        """
        from modulo.core.eval_engine.coverage_gap import _load_runs

        mock_session = make_session_mock()
        org_id = uuid.uuid4()
        group_id = uuid.uuid4()

        # Capture the select statement emitted by the group-path loader.
        class _Result:
            def scalars(self) -> "_Result":
                return self

            def all(self) -> list[object]:
                return []

        captured: list[object] = []
        mock_result = _Result()

        async def execute(stmt: object, params: dict | None = None) -> _Result:
            captured.append(stmt)
            return mock_result

        mock_session.execute = execute

        runs = await _load_runs(mock_session, org_id=org_id, batch_id=None, variant_group_id=group_id)
        assert runs == []

        sql = str(captured[0])
        compiled = captured[0].compile().params
        assert "runs.organisation_id" in sql
        assert org_id in compiled.values()
        assert group_id in compiled.values()
