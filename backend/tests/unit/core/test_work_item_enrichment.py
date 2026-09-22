"""Unit tests for the FAR-737 work-item PR enrichment service.

Covers target derivation (the server-side mirror of the view's
``prEnrichmentKey``) and the TTL/negative cache behaviour of
``enrich_pr_targets``.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Generator
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from modulo.core import work_item_enrichment
from modulo.core.connector_hub import ConnectorDecryptError
from modulo.core.work_item_enrichment import (
    clear_enrichment_cache,
    derive_pr_targets,
    enrich_pr_targets,
)
from modulo.settings import Settings

_VALID_32 = "a" * 32
_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_TARGETS = [("acme/repo#42", "acme/repo", 42)]


def _settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key=_VALID_32,
        fernet_key=_VALID_32,
        modulo_admin_password="testpass",
    )


@pytest.fixture(autouse=True)
def _clean_cache():
    clear_enrichment_cache()
    yield
    clear_enrichment_cache()


# ---------------------------------------------------------------------------
# derive_pr_targets
# ---------------------------------------------------------------------------


def test_derive_canonical_ref_is_payload_independent() -> None:
    targets = derive_pr_targets(
        [{"kind": "github_pr", "ref": "acme/repo#42"}],
        {"repository": {"full_name": "other/repo"}, "pull_request": {"number": 7}},
    )
    assert targets == [("acme/repo#42", "acme/repo", 42)]


def test_derive_normalises_kind_aliases() -> None:
    for kind in ("pr", "pull_request", "github_pr", "PR", "Pull_Request"):
        targets = derive_pr_targets(
            [{"kind": kind, "ref": "acme/repo#9"}],
            None,
        )
        assert targets == [("acme/repo#9", "acme/repo", 9)]


def test_derive_non_pr_kinds_are_ignored() -> None:
    targets = derive_pr_targets(
        [
            {"kind": "linear", "ref": "FAR-1"},
            {"kind": "github_issue", "ref": "acme/repo#3"},
            {"kind": "github", "ref": "acme/repo"},
        ],
        None,
    )
    assert not targets


def test_derive_bare_number_uses_matching_payload() -> None:
    payload = {"repository": {"full_name": "acme/widgets"}, "pull_request": {"number": 206}}
    targets = derive_pr_targets([{"kind": "pr", "ref": "206"}], payload)
    assert targets == [("acme/widgets#206", "acme/widgets", 206)]


def test_derive_bare_number_mismatched_payload_yields_nothing() -> None:
    """A ref/payload number mismatch fabricates nothing (mirrors the view)."""
    payload = {"repository": {"full_name": "acme/widgets"}, "pull_request": {"number": 206}}
    targets = derive_pr_targets([{"kind": "pr", "ref": "999"}], payload)
    assert not targets


def test_derive_bare_number_without_payload_yields_nothing() -> None:
    targets = derive_pr_targets([{"kind": "pr", "ref": "206"}], None)
    assert not targets


def test_derive_non_numeric_ref_falls_back_to_payload_pr() -> None:
    payload = {"repository": {"full_name": "acme/widgets"}, "pull_request": {"number": 5}}
    targets = derive_pr_targets([{"kind": "github_pr", "ref": ""}], payload)
    assert targets == [("acme/widgets#5", "acme/widgets", 5)]


def test_derive_deduplicates_repeated_targets() -> None:
    refs = [
        {"kind": "github_pr", "ref": "acme/repo#1"},
        {"kind": "pr", "ref": "acme/repo#1"},
    ]
    targets = derive_pr_targets(refs, None)
    assert len(targets) == 1


def test_derive_caps_target_count() -> None:
    refs = [{"kind": "github_pr", "ref": f"acme/repo#{i}"} for i in range(25)]
    targets = derive_pr_targets(refs, None)
    assert len(targets) == 10


def test_derive_rejects_non_list_and_malformed_entries() -> None:
    assert not derive_pr_targets(None, None)
    assert not derive_pr_targets("refs", None)
    assert not derive_pr_targets(["not-a-dict", None, 7], None)
    assert not derive_pr_targets([{"kind": "github_pr"}], None)


# ---------------------------------------------------------------------------
# enrich_pr_targets — cache behaviour
# ---------------------------------------------------------------------------


def _begin_cm() -> MagicMock:
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=None)
    cm.__aexit__ = AsyncMock(return_value=False)
    return cm


def _session() -> AsyncMock:
    session = AsyncMock()
    session.begin = MagicMock(return_value=_begin_cm())
    return session


def _github_instance() -> SimpleNamespace:
    return SimpleNamespace(id=uuid.uuid4(), connector_type_id="github")


def _hub(sample: AsyncMock) -> MagicMock:
    hub = MagicMock()
    hub.__aenter__ = AsyncMock(return_value=hub)
    hub.__aexit__ = AsyncMock(return_value=False)
    hub.initialise = AsyncMock()
    hub.sample = sample
    return hub


@contextmanager
def _env(
    *,
    instances: list,
    hub: MagicMock,
) -> Generator[tuple[AsyncMock, MagicMock], None, None]:
    """Patch the service's connector/Hub/secrets/RLS deps.

    Yields ``(lister, hub_cls)`` mocks.
    """
    lister = AsyncMock(return_value=SimpleNamespace(items=instances))
    hub_cls = MagicMock(return_value=hub)
    with (
        patch("modulo.core.work_item_enrichment.list_connector_instances", new=lister),
        patch("modulo.core.work_item_enrichment.ConnectorHub", hub_cls),
        patch(
            "modulo.core.work_item_enrichment.create_secrets_backend",
            MagicMock(return_value=MagicMock()),
        ),
        patch("modulo.core.work_item_enrichment.set_rls_org", new_callable=AsyncMock),
    ):
        yield lister, hub_cls


async def test_enrich_success_is_cached_across_calls() -> None:
    sample = AsyncMock(
        return_value=[{"number": 42, "title": "Fix", "state": "open", "merged": False}],
    )
    with _env(instances=[_github_instance()], hub=_hub(sample)) as (lister, _hub_cls):
        first = await enrich_pr_targets(_session(), _ORG_ID, _TARGETS, _settings())
        second = await enrich_pr_targets(_session(), _ORG_ID, _TARGETS, _settings())

    assert len(first) == 1
    assert first[0]["title"] == "Fix"
    assert second == first
    assert sample.call_count == 1
    assert lister.call_count == 1


async def test_enrich_failure_is_negative_cached() -> None:
    """A failed upstream lookup is remembered — no repeat hub/connector work."""
    sample = AsyncMock(side_effect=RuntimeError("github down"))
    with _env(instances=[_github_instance()], hub=_hub(sample)) as (lister, _hub_cls):
        first = await enrich_pr_targets(_session(), _ORG_ID, _TARGETS, _settings())
        second = await enrich_pr_targets(_session(), _ORG_ID, _TARGETS, _settings())

    assert not first
    assert not second
    assert lister.call_count == 1
    assert sample.call_count == 1


async def test_enrich_without_connector_returns_empty_and_negative_caches() -> None:
    with _env(instances=[], hub=_hub(AsyncMock())) as (lister, hub_cls):
        first = await enrich_pr_targets(_session(), _ORG_ID, _TARGETS, _settings())
        second = await enrich_pr_targets(_session(), _ORG_ID, _TARGETS, _settings())

    assert not first
    assert not second
    assert lister.call_count == 1
    hub_cls.assert_not_called()


async def test_enrich_hub_construction_failure_degrades_without_raising() -> None:
    with (
        _env(instances=[_github_instance()], hub=_hub(AsyncMock())) as (lister, _hub_cls),
        patch(
            "modulo.core.work_item_enrichment.ConnectorHub",
            MagicMock(side_effect=RuntimeError("hub blew up")),
        ),
    ):
        result = await enrich_pr_targets(_session(), _ORG_ID, _TARGETS, _settings())

    assert not result
    assert lister.call_count == 1


async def test_enrich_empty_targets_short_circuits() -> None:
    with patch(
        "modulo.core.work_item_enrichment.list_connector_instances",
        new_callable=AsyncMock,
    ) as lister:
        result = await enrich_pr_targets(_session(), _ORG_ID, [], _settings())
    assert not result
    lister.assert_not_awaited()


async def test_enrich_scopes_connector_lookup_to_the_run_org() -> None:
    """The connector query is org-scoped — tenancy is never ambient."""
    sample = AsyncMock(return_value=[{"number": 42, "title": "Fix", "state": "closed", "merged": True}])
    with _env(instances=[_github_instance()], hub=_hub(sample)) as (lister, _hub_cls):
        await enrich_pr_targets(_session(), _ORG_ID, _TARGETS, _settings())

    assert lister.call_count == 1
    _, kwargs = lister.call_args
    assert kwargs["organisation_id"] == _ORG_ID


# ---------------------------------------------------------------------------
# derive_pr_targets — payload PR-number parsing edge cases
# ---------------------------------------------------------------------------


def test_derive_payload_without_pull_request_key_contributes_no_number() -> None:
    """A payload dict with no ``pull_request`` key must not fabricate a PR."""
    targets = derive_pr_targets(
        [{"kind": "pr", "ref": "206"}],
        {"repository": {"full_name": "acme/widgets"}},
    )
    assert targets == [("acme/widgets#206", "acme/widgets", 206)]


def test_derive_payload_number_bool_is_not_a_number() -> None:
    """``True`` is an ``int`` in Python but must NOT be read as a PR number."""
    targets = derive_pr_targets(
        [{"kind": "pr", "ref": "206"}],
        {"repository": {"full_name": "acme/widgets"}, "pull_request": {"number": True}},
    )
    assert targets == [("acme/widgets#206", "acme/widgets", 206)]


def test_derive_payload_number_numeric_string_is_parsed() -> None:
    targets = derive_pr_targets(
        [{"kind": "pr", "ref": "206"}],
        {"repository": {"full_name": "acme/widgets"}, "pull_request": {"number": "206"}},
    )
    assert targets == [("acme/widgets#206", "acme/widgets", 206)]


def test_derive_payload_number_non_numeric_string_is_ignored() -> None:
    targets = derive_pr_targets(
        [{"kind": "pr", "ref": "206"}],
        {"repository": {"full_name": "acme/widgets"}, "pull_request": {"number": "abc"}},
    )
    assert targets == [("acme/widgets#206", "acme/widgets", 206)]


# ---------------------------------------------------------------------------
# enrich_pr_targets — defensive fallbacks (never raise to the route)
# ---------------------------------------------------------------------------


async def test_enrich_connector_list_failure_negative_caches() -> None:
    """A DB/list error degrades to a negative cache entry, not a raise."""
    lister = AsyncMock(side_effect=RuntimeError("db down"))
    with patch("modulo.core.work_item_enrichment.list_connector_instances", new=lister):
        first = await enrich_pr_targets(_session(), _ORG_ID, _TARGETS, _settings())
        second = await enrich_pr_targets(_session(), _ORG_ID, _TARGETS, _settings())

    assert not first
    assert not second
    assert lister.call_count == 1


async def test_enrich_decrypt_failure_negative_caches() -> None:
    """An undecryptable credential degrades to the plain badge for the TTL."""
    hub = _hub(AsyncMock())
    hub.initialise = AsyncMock(side_effect=ConnectorDecryptError(uuid.uuid4()))
    with _env(instances=[_github_instance()], hub=hub) as (lister, _hub_cls):
        first = await enrich_pr_targets(_session(), _ORG_ID, _TARGETS, _settings())
        second = await enrich_pr_targets(_session(), _ORG_ID, _TARGETS, _settings())

    assert not first
    assert not second
    assert lister.call_count == 1


async def test_enrich_empty_records_is_not_enriched() -> None:
    """A connector returning no records leaves the key unenriched."""
    sample = AsyncMock(return_value=[])
    with _env(instances=[_github_instance()], hub=_hub(sample)):
        result = await enrich_pr_targets(_session(), _ORG_ID, _TARGETS, _settings())

    assert not result


async def test_enrich_cancelled_error_propagates() -> None:
    """``CancelledError`` is a ``BaseException`` — it must NOT be swallowed."""
    sample = AsyncMock(side_effect=asyncio.CancelledError())
    with (
        _env(instances=[_github_instance()], hub=_hub(sample)),
        pytest.raises(asyncio.CancelledError),
    ):
        await enrich_pr_targets(_session(), _ORG_ID, _TARGETS, _settings())


async def test_enrich_cache_overflow_clears_wholesale(monkeypatch: pytest.MonkeyPatch) -> None:
    """The cache is wholesale-cleared once it exceeds its hard bound."""
    monkeypatch.setattr(work_item_enrichment, "_CACHE_MAX_ENTRIES", 1)
    with _env(instances=[], hub=_hub(AsyncMock())):
        await enrich_pr_targets(_session(), _ORG_ID, [("a/b#1", "a/b", 1)], _settings())
        await enrich_pr_targets(_session(), _ORG_ID, [("c/d#2", "c/d", 2)], _settings())

    assert [key for _org, key in work_item_enrichment._cache] == ["c/d#2"]
