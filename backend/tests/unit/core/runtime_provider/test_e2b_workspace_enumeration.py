"""FAR-1275: identity-scoped workspace enumeration on the E2B provider.

The non-Docker orphan-reclamation gap: the ABC gained
``list_workspaces_by_identity`` so a sweep can enumerate a deployment's own
workspaces on a substrate with no label-filtered listing. For E2B that
requires two halves, both pinned here:

1. **Stamping** — ``create_workspace`` writes the reserved identity keys
   (``modulo.machine.id`` deployment identity + ``modulo.created_at``) onto
   EVERY sandbox, AFTER the operator's own ``workspace_metadata`` and always
   authoritative (the FAR-1493 ordering mirror of Docker/Kubernetes). Without
   them the enumeration has nothing to match and every sandbox created before
   this change stays unreclaimable (never wrongly reclaimable).
2. **Enumeration** — ``list_workspaces_by_identity`` narrows via the SDK's
   metadata query, RE-CHECKS the identity client-side (fail closed: two
   Modulo deployments can share one E2B account), maps ``run_id`` / age from
   the same reserved keys, propagates listing failures, and bounds both the
   per-page fetch and the pagination loop.
"""

from __future__ import annotations

import time
import uuid
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from modulo.core.runtime_provider import ProviderCapabilityUnsupportedError, WorkspaceSpec
from modulo.core.runtime_provider.e2b import (
    _CREATED_AT_METADATA_KEY,
    _DEPLOYMENT_IDENTITY_ENV,
    _DEPLOYMENT_IDENTITY_METADATA_KEY,
    _LIST_MAX_PAGES,
    _RUN_ID_METADATA_KEY,
    E2BRuntimeProvider,
)

_IDENTITY = "deployment-a"


def _spec(**overrides: Any) -> WorkspaceSpec:
    kwargs: dict[str, Any] = {
        "environment_profile_id": uuid.uuid4(),
        "organisation_id": uuid.uuid4(),
        "image_ref": "ubuntu-22.04",
    }
    kwargs.update(overrides)
    return WorkspaceSpec(**kwargs)


class _FakePaginator:
    """Paginator double: pages returned in order, ``has_next`` until exhausted."""

    def __init__(self, pages: list[list[Any]]) -> None:
        self._pages = pages
        self._index = 0
        self.calls = 0

    @property
    def has_next(self) -> bool:
        return self._index < len(self._pages)

    async def next_items(self) -> list[Any]:
        self.calls += 1
        page = self._pages[self._index]
        self._index += 1
        return page


class _NeverEndingPaginator:
    """A pagination loop the control plane never terminates (page-cap bound)."""

    def __init__(self) -> None:
        self.calls = 0

    @property
    def has_next(self) -> bool:
        return True

    async def next_items(self) -> list[Any]:
        self.calls += 1
        return []


def _fake_sandbox_cls(monkeypatch: pytest.MonkeyPatch, paginator: Any) -> MagicMock:
    """Patch ``e2b.AsyncSandbox`` so ``list`` returns *paginator*."""
    fake_cls = MagicMock()
    fake_cls.list = MagicMock(return_value=paginator)
    monkeypatch.setattr("e2b.AsyncSandbox", fake_cls)
    return fake_cls


def _sandbox_info(sandbox_id: str, metadata: dict[str, str]) -> SimpleNamespace:
    return SimpleNamespace(sandbox_id=sandbox_id, metadata=metadata)


def _sandbox_cls(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Patch ``e2b.AsyncSandbox`` for a ``create_workspace`` call."""
    fake_cls = MagicMock()
    fake_cls.create = AsyncMock(return_value=SimpleNamespace(sandbox_id="sbx-new"))
    monkeypatch.setattr("e2b.AsyncSandbox", fake_cls)
    return fake_cls


# ---------------------------------------------------------------------------
# 1. Stamping: the reserved identity keys reach every created sandbox
# ---------------------------------------------------------------------------


async def test_create_workspace_stamps_the_reserved_identity_keys(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(_DEPLOYMENT_IDENTITY_ENV, _IDENTITY)
    fake_cls = _sandbox_cls(monkeypatch)

    provider = E2BRuntimeProvider(api_key="sk-test")
    await provider.create_workspace(_spec(workspace_metadata={_RUN_ID_METADATA_KEY: "run-1", "egress_allowlist": "[]"}))

    metadata = fake_cls.create.call_args.kwargs["metadata"]
    assert metadata[_DEPLOYMENT_IDENTITY_METADATA_KEY] == _IDENTITY
    assert int(metadata[_CREATED_AT_METADATA_KEY]) > 0
    # The spec's own keys flow through unchanged alongside the stamps.
    assert metadata[_RUN_ID_METADATA_KEY] == "run-1"
    assert metadata["egress_allowlist"] == "[]"


async def test_create_workspace_identity_keys_win_over_operator_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """FAR-1493 ordering on this tier: a spoofed ``modulo.machine.id`` would
    hide the sandbox from this deployment's enumeration, and a backdated
    ``modulo.created_at`` would defeat the sweep's grace window — the
    provider's own values are assigned AFTER the operator metadata."""
    monkeypatch.setenv(_DEPLOYMENT_IDENTITY_ENV, _IDENTITY)
    fake_cls = _sandbox_cls(monkeypatch)

    provider = E2BRuntimeProvider(api_key="sk-test")
    await provider.create_workspace(
        _spec(
            workspace_metadata={
                _DEPLOYMENT_IDENTITY_METADATA_KEY: "spoofed-deployment",
                _CREATED_AT_METADATA_KEY: "1",
            }
        )
    )

    metadata = fake_cls.create.call_args.kwargs["metadata"]
    assert metadata[_DEPLOYMENT_IDENTITY_METADATA_KEY] == _IDENTITY
    # The spoofed creation marker is replaced by a real epoch-seconds stamp.
    assert int(metadata[_CREATED_AT_METADATA_KEY]) > 1_700_000_000


async def test_create_workspace_stamps_identity_even_with_empty_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The stamps ride EVERY create — an empty ``workspace_metadata`` still
    sends them (the enumeration filter always has something to match)."""
    monkeypatch.setenv(_DEPLOYMENT_IDENTITY_ENV, _IDENTITY)
    fake_cls = _sandbox_cls(monkeypatch)

    provider = E2BRuntimeProvider(api_key="sk-test")
    await provider.create_workspace(_spec())

    metadata = fake_cls.create.call_args.kwargs["metadata"]
    assert metadata[_DEPLOYMENT_IDENTITY_METADATA_KEY] == _IDENTITY
    assert _CREATED_AT_METADATA_KEY in metadata


# ---------------------------------------------------------------------------
# 2. Enumeration: filter, mapping, bounds, failure honesty
# ---------------------------------------------------------------------------


async def test_list_maps_matching_sandboxes_and_filters_on_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Identity-matched sandboxes map to the ABC carrier (ref / run id /
    age), foreign ones are dropped client-side, and the SDK call carries the
    server-side metadata filter plus this provider's API key."""
    created_marker = str(int(time.time()) - 600)
    paginator = _FakePaginator(
        [
            [
                _sandbox_info(
                    "sbx-1",
                    {
                        _DEPLOYMENT_IDENTITY_METADATA_KEY: _IDENTITY,
                        _RUN_ID_METADATA_KEY: "run-1",
                        _CREATED_AT_METADATA_KEY: created_marker,
                    },
                ),
                # Matching identity, no run attribution -> run_id "" (the
                # CALLER decides whether unattributed workspaces matter).
                _sandbox_info(
                    "sbx-2",
                    {_DEPLOYMENT_IDENTITY_METADATA_KEY: _IDENTITY},
                ),
                # Foreign identity: dropped by the client-side re-check even
                # if the server-side query ever over-returned.
                _sandbox_info(
                    "sbx-foreign",
                    {_DEPLOYMENT_IDENTITY_METADATA_KEY: "deployment-b"},
                ),
            ]
        ]
    )
    fake_cls = _fake_sandbox_cls(monkeypatch, paginator)

    provider = E2BRuntimeProvider(api_key="sk-test")
    entries = await provider.list_workspaces_by_identity(_IDENTITY)

    query = fake_cls.list.call_args.kwargs["query"]
    assert query.metadata == {_DEPLOYMENT_IDENTITY_METADATA_KEY: _IDENTITY}
    assert fake_cls.list.call_args.kwargs["api_key"] == "sk-test"

    assert len(entries) == 2
    assert entries[0].provider_ref == "sbx-1"
    assert entries[0].run_id == "run-1"
    assert entries[0].created_age_s >= 600
    assert entries[1].provider_ref == "sbx-2"
    assert not entries[1].run_id
    assert entries[1].created_age_s == 0.0


async def test_list_paginates_across_pages(monkeypatch: pytest.MonkeyPatch) -> None:
    paginator = _FakePaginator(
        [
            [_sandbox_info("sbx-1", {_DEPLOYMENT_IDENTITY_METADATA_KEY: _IDENTITY})],
            [_sandbox_info("sbx-2", {_DEPLOYMENT_IDENTITY_METADATA_KEY: _IDENTITY})],
        ]
    )
    _fake_sandbox_cls(monkeypatch, paginator)

    provider = E2BRuntimeProvider(api_key="sk-test")
    entries = await provider.list_workspaces_by_identity(_IDENTITY)

    assert [entry.provider_ref for entry in entries] == ["sbx-1", "sbx-2"]
    assert paginator.calls == 2


async def test_list_with_an_empty_identity_enumerates_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fail closed: an empty/whitespace identity returns no entries and never
    touches the API — an unfiltered listing could only surface unattributed,
    foreign or pre-stamp sandboxes."""
    fake_cls = _fake_sandbox_cls(monkeypatch, _FakePaginator([[]]))

    provider = E2BRuntimeProvider(api_key="sk-test")
    entries = await provider.list_workspaces_by_identity("   ")

    assert not entries
    fake_cls.list.assert_not_called()


async def test_list_propagates_a_listing_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """A configured-but-unreachable substrate must surface as a reported
    sweep failure — never a silent empty list."""

    class _ExplodingPaginator:
        @property
        def has_next(self) -> bool:
            return True

        async def next_items(self) -> list[Any]:
            raise RuntimeError("e2b api unreachable")

    _fake_sandbox_cls(monkeypatch, _ExplodingPaginator())

    provider = E2BRuntimeProvider(api_key="sk-test")
    with pytest.raises(RuntimeError, match="e2b api unreachable"):
        await provider.list_workspaces_by_identity(_IDENTITY)


async def test_list_stops_at_the_page_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    """A pagination loop the control plane never terminates cannot run the
    sweep forever: the cap is hit, logged and the listing ends."""
    paginator = _NeverEndingPaginator()
    _fake_sandbox_cls(monkeypatch, paginator)

    provider = E2BRuntimeProvider(api_key="sk-test")
    entries = await provider.list_workspaces_by_identity(_IDENTITY)

    assert not entries
    assert paginator.calls == _LIST_MAX_PAGES


async def test_list_skips_a_sandbox_with_no_id(monkeypatch: pytest.MonkeyPatch) -> None:
    paginator = _FakePaginator([[_sandbox_info("", {_DEPLOYMENT_IDENTITY_METADATA_KEY: _IDENTITY})]])
    _fake_sandbox_cls(monkeypatch, paginator)

    provider = E2BRuntimeProvider(api_key="sk-test")
    entries = await provider.list_workspaces_by_identity(_IDENTITY)

    assert not entries


async def test_list_tolerates_a_malformed_creation_marker(monkeypatch: pytest.MonkeyPatch) -> None:
    """A non-numeric ``modulo.created_at`` reads as "no age" (0.0), never a
    crash — the sweep then refuses to destroy it (cannot establish a grace
    age), exactly like Docker's no-marker case."""
    paginator = _FakePaginator(
        [
            [
                _sandbox_info(
                    "sbx-bad-marker",
                    {
                        _DEPLOYMENT_IDENTITY_METADATA_KEY: _IDENTITY,
                        _CREATED_AT_METADATA_KEY: "not-a-number",
                    },
                )
            ]
        ]
    )
    _fake_sandbox_cls(monkeypatch, paginator)

    provider = E2BRuntimeProvider(api_key="sk-test")
    entries = await provider.list_workspaces_by_identity(_IDENTITY)

    assert len(entries) == 1
    assert entries[0].created_age_s == 0.0


async def test_abc_default_on_a_non_overriding_provider_is_typed() -> None:
    """The refusal a tier without an identity surface (e.g. Local) surfaces
    is the typed capability error, never NotImplementedError."""
    from modulo.core.runtime_provider.local import LocalRuntimeProvider

    provider = LocalRuntimeProvider()
    with pytest.raises(ProviderCapabilityUnsupportedError, match="list_workspaces_by_identity"):
        await provider.list_workspaces_by_identity(_IDENTITY)
