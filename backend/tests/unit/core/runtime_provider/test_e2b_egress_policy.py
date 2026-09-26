"""FAR-1050 R5: ``WorkspaceSpec.egress_policy`` reaches the E2B create call.

``E2BRuntimeProvider.create_workspace`` is the flag-ON dispatch's provision
primitive, and it previously ignored ``spec.egress_policy`` — a configured
``deny_all`` would have been silently granted unrestricted internet, exactly
the ADR 040 defect class. These tests pin the carrier:

1. ``deny_all`` / ``selected`` (and the ``none`` restrictive synonym) map to
   ``allow_internet_access=False``; ``default`` / ``None`` map to ``True``;
   an unrecognised value fails CLOSED.
2. The selected-mode allowlist reaches the sandbox metadata under the same
   key the legacy create stamps (``egress_allowlist``) — the SDK has no
   native allowlist control, so metadata is the carrier on both paths.
"""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import Generator
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from modulo.core.runtime_provider import WorkspaceSpec
from modulo.core.runtime_provider.e2b import E2BRuntimeProvider, _egress_allows_internet

_FIXTURE_ALLOWLIST: list[dict[str, Any]] = [{"host": "api.example.com", "port": 443}]


def _spec(**overrides: Any) -> WorkspaceSpec:
    kwargs: dict[str, Any] = {
        "environment_profile_id": uuid.uuid4(),
        "organisation_id": uuid.uuid4(),
        "image_ref": "ubuntu-22.04",
    }
    kwargs.update(overrides)
    return WorkspaceSpec(**kwargs)


@pytest.fixture
def mock_sandbox_cls() -> Generator[MagicMock, None, None]:
    sandbox = MagicMock()
    sandbox.sandbox_id = "sbx-egress-001"
    with patch("e2b.AsyncSandbox") as mock_cls:
        mock_cls.create = AsyncMock(return_value=sandbox)
        yield mock_cls


# ---------------------------------------------------------------------------
# allow_internet_access mapping
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("policy", ["deny_all", "selected", "none"])
async def test_create_workspace_denies_internet_for_restrictive_policies(
    mock_sandbox_cls: MagicMock,
    policy: str,
) -> None:
    """A restrictive policy is carried into the create call (never dropped)."""
    provider = E2BRuntimeProvider(api_key="sk-test")
    await provider.create_workspace(_spec(egress_policy=policy))

    mock_sandbox_cls.create.assert_awaited_once()
    assert mock_sandbox_cls.create.await_args.kwargs["allow_internet_access"] is False


@pytest.mark.parametrize("policy", [None, "default", "outbound"])
async def test_create_workspace_allows_internet_for_permissive_policies(
    mock_sandbox_cls: MagicMock,
    policy: str | None,
) -> None:
    """The provider default / unrestricted dialects allow internet — the same
    boolean the legacy create computes from a ``None`` resolved policy."""
    provider = E2BRuntimeProvider(api_key="sk-test")
    await provider.create_workspace(_spec(egress_policy=policy))

    mock_sandbox_cls.create.assert_awaited_once()
    assert mock_sandbox_cls.create.await_args.kwargs["allow_internet_access"] is True


async def test_create_workspace_fails_closed_on_unrecognised_policy(
    mock_sandbox_cls: MagicMock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An unknown policy string never silently grants egress (ADR 040):
    internet is denied AND the anomaly is logged."""
    provider = E2BRuntimeProvider(api_key="sk-test")
    with caplog.at_level(logging.WARNING, logger="modulo.core.runtime_provider.e2b"):
        await provider.create_workspace(_spec(egress_policy="not-a-policy"))

    assert mock_sandbox_cls.create.await_args.kwargs["allow_internet_access"] is False
    assert any("not-a-policy" in record.getMessage() for record in caplog.records)


@pytest.mark.parametrize(
    ("policy", "expected"),
    [
        pytest.param(None, True, id="none-policy-allows"),
        pytest.param("", True, id="empty-string-allows"),
        pytest.param("default", True, id="default-allows"),
        pytest.param("outbound", True, id="outbound-allows"),
        pytest.param("deny_all", False, id="deny_all-denies"),
        pytest.param("selected", False, id="selected-denies"),
        pytest.param("none", False, id="none-str-denies"),
        pytest.param("DENY_ALL", False, id="uppercase-denies"),
        pytest.param("  selected  ", False, id="padded-denies"),
        pytest.param("banana", False, id="unknown-fails-closed"),
    ],
)
def test_egress_allows_internet_mapping(policy: str | None, expected: bool) -> None:
    """The pure mapping: permissive set -> True, restrictive set -> False,
    unrecognised -> fail closed (False)."""
    assert _egress_allows_internet(policy) is expected


# ---------------------------------------------------------------------------
# selected-mode allowlist -> sandbox metadata (legacy-key parity)
# ---------------------------------------------------------------------------


async def test_create_workspace_carries_selected_allowlist_in_metadata(
    mock_sandbox_cls: MagicMock,
) -> None:
    """The selected-mode allowlist reaches ``AsyncSandbox.create(metadata=)``
    under the legacy key (``egress_allowlist``), while the boolean denies
    internet — same shape as the flag-OFF create."""
    allowlist_json = json.dumps(_FIXTURE_ALLOWLIST)
    provider = E2BRuntimeProvider(api_key="sk-test")
    spec = _spec(
        egress_policy="selected",
        workspace_metadata={
            "egress_allowlist": allowlist_json,
            "resource_limits": json.dumps({"memory_mb": 512}),
        },
    )

    await provider.create_workspace(spec)

    kwargs = mock_sandbox_cls.create.await_args.kwargs
    assert kwargs["allow_internet_access"] is False
    assert json.loads(kwargs["metadata"]["egress_allowlist"]) == _FIXTURE_ALLOWLIST
    assert json.loads(kwargs["metadata"]["resource_limits"]) == {"memory_mb": 512}


async def test_create_workspace_omits_metadata_when_spec_carries_none(
    mock_sandbox_cls: MagicMock,
) -> None:
    """Parity with the legacy ``metadata=_metadata or None``: an empty
    ``workspace_metadata`` sends no metadata kwarg at all."""
    provider = E2BRuntimeProvider(api_key="sk-test")
    await provider.create_workspace(_spec(egress_policy="deny_all"))

    assert "metadata" not in mock_sandbox_cls.create.await_args.kwargs
