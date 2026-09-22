"""Live GitHub enrichment for run work-item PR badges (FAR-737).

Display-time, connector-mediated lookup: the Run Detail page's PR badge is
enriched with live GitHub data (title, state, merged) fetched through the
run's ORG-scoped GitHub connector.

Design decision (FAR-737):

* **Display-time lookup, not a persisted snapshot.** Runs are immutable
  records, but PR title/state/merged are mutable facts about an EXTERNAL
  system. Persisting them at work-item derivation time would freeze a live
  external state into an immutable record and go stale silently with no
  refresh path. The run payload keeps the immutable audit facts (repo, PR
  number); the badge enrichment is explicitly ephemeral UI sugar, so a
  display-time lookup with a short TTL keeps the record self-contained AND
  the badge fresh. It also requires no migration and no write path into
  ``runs``.
* **Cache: in-process TTL, successes AND failures.** Run views are hot;
  every lookup is keyed ``(org, canonical_ref)`` and served from
  :data:`_cache` for :data:`ENRICHMENT_TTL_SECONDS`. Failures are negative-
  cached too, so a GitHub outage costs ONE failed upstream fetch per key per
  TTL instead of one per page view. Bounded by :data:`_CACHE_MAX_ENTRIES`
  (cleared wholesale beyond that — the cache is cheap to rebuild).
* **Failure fallback (non-negotiable).** No connector configured, credential
  decrypt error, ACL denial, GitHub unreachable, PR 404 — every class
  degrades to "no enrichment" for that key. This module NEVER raises to the
  route (``asyncio.CancelledError`` propagates as a ``BaseException``), and
  the Run Detail view renders the existing plain linked badge when the item
  list comes back empty.
* **Tenancy.** Connector instances are resolved ONLY via the run's org id
  (``list_connector_instances(organisation_id=...)`` under that org's RLS);
  the hub is constructed from those instances alone, so one org's badge can
  never read through another org's credentials.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from modulo.connectors.base import ConnectorType
from modulo.core.connector_hub import ConnectorDecryptError, ConnectorHub
from modulo.core.secrets_backend import create_secrets_backend
from modulo.db.crud.connector_instance import list_connector_instances
from modulo.db.rls import set_rls_org
from modulo.settings import Settings

_log = logging.getLogger(__name__)

#: TTL for one enrichment entry (success or failure), in seconds.
ENRICHMENT_TTL_SECONDS = 60.0

#: Hard bound on the in-process cache; cleared wholesale beyond this.
_CACHE_MAX_ENTRIES = 512

#: Safety cap on how many PRs one run view may resolve upstream.
_MAX_PR_TARGETS = 10

#: ``owner/repo#123`` — the canonical work-item ref form.
_PR_REF_RE = re.compile(r"^([^/\s#]+/[^/\s#]+)#(\d+)$")

#: Work-item kinds that denote a GitHub PR (aliases normalised onto
#: ``github_pr`` — mirrors the Run Detail view's ``normalizeGithubKind``).
_GITHUB_PR_KINDS = frozenset({"pr", "pull_request", "github_pr"})

# key -> (monotonic expiry, item-or-None). ``None`` is the negative-cache
# sentinel: "looked up and not enrichable within the TTL".
_cache: dict[tuple[str, str], tuple[float, dict[str, Any] | None]] = {}


def clear_enrichment_cache() -> None:
    """Drop every cached entry (tests and operational resets)."""
    _cache.clear()


def _normalize_kind(kind: Any) -> str:
    normalized = str(kind or "").strip().lower()
    if normalized in ("pr", "pull_request"):
        return "github_pr"
    return normalized


def _payload_pr_context(payload: Any) -> tuple[str, int | None]:
    """``(repository.full_name, pull_request.number)`` from the run payload."""
    if not isinstance(payload, dict):
        return "", None
    repo = payload.get("repository")
    full_name = repo.get("full_name") if isinstance(repo, dict) else None
    full_name = full_name.strip() if isinstance(full_name, str) else ""
    number: int | None = None
    pr = payload.get("pull_request")
    if isinstance(pr, dict):
        raw = pr.get("number")
        if isinstance(raw, bool):
            number = None
        elif isinstance(raw, int):
            number = raw
        elif isinstance(raw, str) and raw.strip().isdigit():
            number = int(raw.strip())
    return full_name, number


def derive_pr_targets(
    work_item_refs: Any,
    payload: Any,
) -> list[tuple[str, str, int]]:
    """Resolve the run's PR work items into canonical enrichment targets.

    Returns ``(canonical_key, repo, number)`` tuples where *canonical_key* is
    ``owner/repo#N``. Mirrors the view's ``prEnrichmentKey`` exactly so the
    response keys match what the badge looks itself up by:

    * ``owner/repo#N`` ref → that PR, payload-independent;
    * bare-digit ref → the payload repo, but only when the payload number is
      absent or EQUALS the ref (a mismatch fabricates nothing — same rule as
      the view's link/title guard);
    * otherwise → the payload's PR (covers non-numeric/empty refs).
    """
    if not isinstance(work_item_refs, list):
        return []
    full_name, payload_number = _payload_pr_context(payload)
    payload_number_str = "" if payload_number is None else str(payload_number)
    seen: set[str] = set()
    targets: list[tuple[str, str, int]] = []
    for entry in work_item_refs:
        if not isinstance(entry, dict):
            continue
        if _normalize_kind(entry.get("kind")) not in _GITHUB_PR_KINDS:
            continue
        ref = str(entry.get("ref") or "").strip()
        match = _PR_REF_RE.match(ref)
        if match:
            repo, number_str = match.group(1), match.group(2)
        elif ref.isdigit() and full_name:
            if payload_number_str and payload_number_str != ref:
                continue  # mismatched payload PR — the view links nothing either
            repo, number_str = full_name, ref
        elif full_name and payload_number_str:
            repo, number_str = full_name, payload_number_str
        else:
            continue
        key = f"{repo}#{number_str}"
        if key in seen:
            continue
        seen.add(key)
        targets.append((key, repo, int(number_str)))
        if len(targets) >= _MAX_PR_TARGETS:
            break
    return targets


def _remember(org_key: str, key: str, item: dict[str, Any] | None) -> None:
    """Cache *item* (or the ``None`` failure sentinel) for one TTL window."""
    if len(_cache) >= _CACHE_MAX_ENTRIES:
        _cache.clear()
    _cache[(org_key, key)] = (time.monotonic() + ENRICHMENT_TTL_SECONDS, item)


def _to_item(key: str, repo: str, number: int, record: dict[str, Any]) -> dict[str, Any]:
    """Project a GitHub PR object onto the badge-enrichment shape."""
    title = record.get("title")
    state = record.get("state")
    merged = record.get("merged")
    html_url = record.get("html_url")
    return {
        "ref": key,
        "repo": repo,
        "number": number,
        "title": title if isinstance(title, str) and title else None,
        "state": state if state in ("open", "closed") else None,
        "merged": bool(merged) if isinstance(merged, bool) else None,
        "html_url": html_url if isinstance(html_url, str) and html_url else None,
    }


async def _fetch_pending(
    session: AsyncSession,
    org_id: uuid.UUID,
    pending: list[tuple[str, str, int]],
    settings: Settings,
    org_key: str,
) -> None:
    """Fetch *pending* targets through the org's GitHub connectors.

    Populates the cache for every pending key (success item or ``None``).
    Never raises except ``asyncio.CancelledError``.
    """
    try:
        page = await list_connector_instances(session, organisation_id=org_id, page_size=100)
        github_instances = [ci for ci in page.items if ci.connector_type_id == ConnectorType.GITHUB.value]
    except Exception:
        _log.warning("work_item_enrichment.connector_list_failed", exc_info=True)
        for key, _repo, _number in pending:
            _remember(org_key, key, None)
        return
    if not github_instances:
        # No GitHub connector for this org — negative-cache so a hot view
        # does not re-query the DB row set on every page view either.
        for key, _repo, _number in pending:
            _remember(org_key, key, None)
        return

    secrets_backend = create_secrets_backend(fernet_key=settings.fernet_key, session=session)
    try:
        async with ConnectorHub(secrets_backend=secrets_backend, org_id=str(org_id)) as hub:
            try:
                async with session.begin():
                    await set_rls_org(session, org_id)
                    await hub.initialise(github_instances)
            except ConnectorDecryptError:
                _log.warning("work_item_enrichment.decrypt_failed", exc_info=True)
                for key, _repo, _number in pending:
                    _remember(org_key, key, None)
                return

            for key, repo, number in pending:
                item: dict[str, Any] | None = None
                for ci in github_instances:
                    try:
                        records = await hub.sample(ci.id, "pull", {"repo": repo, "pull_number": str(number)})
                        if records and isinstance(records[0], dict):
                            item = _to_item(key, repo, number, records[0])
                            break
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        _log.warning(
                            "work_item_enrichment.pull_lookup_failed repo=%s number=%s connector=%s",
                            repo,
                            number,
                            ci.id,
                            exc_info=True,
                        )
                _remember(org_key, key, item)
    except asyncio.CancelledError:
        raise
    except Exception:
        # Hub construction / teardown / shared-budget failures: degrade the
        # whole batch to "not enrichable" — never raise into the route.
        _log.warning("work_item_enrichment.hub_failed", exc_info=True)
        for key, _repo, _number in pending:
            _remember(org_key, key, None)


async def enrich_pr_targets(
    session: AsyncSession,
    org_id: uuid.UUID,
    targets: list[tuple[str, str, int]],
    settings: Settings,
) -> list[dict[str, Any]]:
    """Enrich *targets* with live GitHub data, cache-served where possible.

    Returns the successfully-enriched items in *targets* order. Missing /
    failed targets are simply absent — callers fall back to the plain badge.
    """
    if not targets:
        return []
    org_key = org_id.hex
    now = time.monotonic()
    pending: list[tuple[str, str, int]] = []
    for key, repo, number in targets:
        entry = _cache.get((org_key, key))
        if entry is not None and entry[0] > now:
            continue
        pending.append((key, repo, number))
    if pending:
        await _fetch_pending(session, org_id, pending, settings, org_key)

    out: list[dict[str, Any]] = []
    after = time.monotonic()
    for key, _repo, _number in targets:
        entry = _cache.get((org_key, key))
        if entry is not None and entry[0] > after and entry[1] is not None:
            out.append(entry[1])
    return out
