"""Woodpecker CI runner — trigger, poll, log and list Woodpecker pipelines.

Woodpecker (the community fork of Drone) is the natural self-hosted CI for
Gitea/Forgejo stacks and a leading air-gapped CI (FAR-1766).

**API shape** — verified against the Woodpecker server source (the router in
``server/router/api.go``, ``session.SetRepo`` in
``server/router/middleware/session/repo.go``, ``CreatePipeline`` /
``GetPipeline`` / ``GetStepLogs`` in ``server/api/pipeline.go``, and
``model.PipelineOptions`` / ``model.LogEntry``), not from a summary:

* ``GET /api/repos/lookup/{owner}/{name}`` — resolves the repo **slug** to the
  numeric ``repo_id`` every other route takes. The ``:repo_id`` path segment is
  parsed with ``strconv.ParseInt`` and answered ``400`` when it is not numeric,
  and ``/api/repos/{owner}/{name}/...`` does not even match the route — so
  slug-addressed repo paths do NOT work (the Drone-era addressing the ticket
  assumed). Each operation resolves the slug through this lookup first.
* ``POST /api/repos/{id}/pipelines`` — trigger. Body is
  ``{branch, message, variables}`` (``model.PipelineOptions``); the branch is
  resolved against the forge server-side and an unresolvable one fails the
  request, so an empty caller branch falls back to the repository's default
  branch (read from the lookup the trigger already makes), then ``main``. The
  server answers ``204 No Content`` when it created no pipeline — that carries
  no id to qualify the run with, so it fails loud instead of returning an
  unusable id.
* ``GET /api/repos/{id}/pipelines/{number}`` — status. The response embeds the
  workflow/step tree (``workflows[].children[]``) that the log reader
  enumerates.
* ``GET /api/repos/{id}/pipelines`` — list; ``page``/``perPage`` pagination
  (the server clamps the page size to 50) and a ``status`` filter.
* ``GET /api/repos/{id}/logs/{number}/{step_id}`` — per-step logs. Each entry
  is ``{time, line, data, type}`` where ``data`` is **base64-encoded** (Go
  marshals ``[]byte``) and ``type`` is ``0`` stdout, ``1`` stderr, ``2`` exit
  code, ``3`` metadata, ``4`` progress.

**Spec deviation, verified:** FAR-1766's prose named
``GET /api/repos/{repo_id}/pipelines/{number}/logs/{step}``. That route does
not exist in Woodpecker — the step-log route is ``/logs/{number}/{step_id}``
with a numeric step id — so this connector implements the verified route.

**Run-id contract (FAR-1141):** ``<owner>/<name>/<build_number>``. The id
``trigger_run`` / ``list_runs`` / ``get_run_status`` produce is exactly what
``get_run_status`` / ``get_run_logs`` consume: the slug is re-resolved per
call (no in-process id map, so ids stay valid across processes).

``base_url`` is the Woodpecker **instance root** (e.g.
``https://ci.example.com``); the ``/api`` prefix is appended here, and a
configured base that already ends in ``/api`` is accepted as-is rather than
double-prefixed.
"""

from __future__ import annotations

import base64
import logging
import re
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

import httpx

from modulo.connectors._ci_test_double import CITestDoubleMixin
from modulo.connectors._egress import egress_ssl_context
from modulo.connectors._safe_int import safe_int as _safe_int
from modulo.connectors._safe_page import safe_records_list as _safe_records_list
from modulo.connectors.base import CIRun, CIRunLog, CIRunStatus, HealthResult
from modulo.connectors.ci_runner.base import CIRunnerBase
from modulo.core.ssrf import pinned_async_client_sync

logger = logging.getLogger(__name__)

#: API prefix appended to the configured instance root.
_API_PREFIX = "/api"

#: Server-side maximum page size for ``GET /api/repos/{id}/pipelines``
#: (``session.Pagination`` clamps anything above 50 to 50).
_MAX_PAGE_SIZE = 50

#: A repository slug segment: the charset Gitea/Forgejo/Drone accept. ``.`` and
#: ``..`` are rejected separately (they match the charset but are path
#: traversal segments).
_SLUG_SEGMENT_RE = re.compile(r"[A-Za-z0-9_.\-]+")

#: Woodpecker pipeline status -> CIRunStatus. The value set the API documents
#: is created / pending / running / success / failure / error / killed /
#: canceled / declined / blocked / skipped; anything else degrades to UNKNOWN
#: rather than being guessed.
_STATUS_MAP: dict[str, CIRunStatus] = {
    "created": CIRunStatus.QUEUED,
    "pending": CIRunStatus.QUEUED,
    "running": CIRunStatus.IN_PROGRESS,
    "success": CIRunStatus.SUCCESS,
    "failure": CIRunStatus.FAILURE,
    "error": CIRunStatus.FAILURE,
    "killed": CIRunStatus.CANCELLED,
    "canceled": CIRunStatus.CANCELLED,
    "declined": CIRunStatus.CANCELLED,
    "blocked": CIRunStatus.PENDING,
    # A skipped pipeline was deliberately not executed and never failed; the
    # GitLab CI runner maps its own ``skipped`` the same way.
    "skipped": CIRunStatus.SUCCESS,
}

#: CIRunStatus -> the ``status`` filter ``GET /api/repos/{id}/pipelines``
#: accepts. The server validates the value against its own status vocabulary,
#: so only values verified in that vocabulary are ever sent; an unmapped
#: request status logs a warning and returns the unfiltered list (never a
#: guessed filter the server would reject with a 400).
_LIST_STATUS_MAP: dict[CIRunStatus, str] = {
    CIRunStatus.PENDING: "pending",
    CIRunStatus.QUEUED: "pending",
    CIRunStatus.IN_PROGRESS: "running",
    CIRunStatus.SUCCESS: "success",
    CIRunStatus.FAILURE: "failure",
    CIRunStatus.CANCELLED: "killed",
    # No Woodpecker status means "timed out" (the agent watchdog kills the
    # pipeline, surfacing as ``killed``), so TIMED_OUT is deliberately unmapped
    # rather than reported as a filter the server would reject.
}

#: ``model.LogEntryType`` values.
_LOG_ENTRY_STDOUT = 0
_LOG_ENTRY_EXIT_CODE = 2
_LOG_ENTRY_METADATA = 3
_LOG_ENTRY_PROGRESS = 4

#: Terminal pipeline statuses — a finished run whose payload carries no step
#: tree is an unexpected shape (see :meth:`WoodpeckerCIRunner.get_run_logs`).
_TERMINAL_STATUSES = frozenset({"success", "failure", "error", "killed", "canceled", "declined", "skipped"})


def _resolve_api_base(base_url: str) -> str:
    """Normalise the configured instance root into the connector's API base.

    ``base_url`` is the instance root (``https://ci.example.com``); ``/api`` is
    appended unless the operator already pointed the connector at ``.../api``
    (a common paste from the docs), which is accepted as-is rather than
    silently producing ``/api/api/...``.
    """
    base = (base_url or "").strip().rstrip("/")
    if not base:
        raise ValueError("base_url is required for the Woodpecker CI runner")
    if base.endswith(_API_PREFIX):
        return base
    return f"{base}{_API_PREFIX}"


def _repo_parts(repo_ref: str, *, label: str) -> tuple[str, str]:
    """Split ``owner/name`` into its parts, failing loud on anything else.

    *label* names the offending input in the error (``pipeline_id`` /
    ``run_id``) so a malformed value is diagnosable at the call site.
    """
    owner, sep, name = repo_ref.partition("/")
    if not sep or not owner or not name or "/" in name:
        raise ValueError(f"Invalid {label}: {repo_ref!r}. Expected 'owner/repo'.")
    for part in (owner, name):
        if part in {".", ".."} or _SLUG_SEGMENT_RE.fullmatch(part) is None:
            raise ValueError(
                f"Invalid {label}: {repo_ref!r} (repository segment {part!r} is not a valid "
                "Gitea/Forgejo owner or repository name)",
            )
    return owner, name


def _split_run_id(run_id: str) -> tuple[str, str]:
    """Split ``owner/name/build_number`` into ``(slug, build_number)``.

    The inverse of the ids :meth:`WoodpeckerCIRunner._parse_run` produces, so
    a bare or partially-qualified id is rejected up front (the run-id contract
    on ``CIRunnerBase``) instead of being sent to the API as a path segment.
    """
    slug, sep, number = run_id.rpartition("/")
    if not sep or not number.isdigit() or slug.count("/") != 1:
        raise ValueError(f"Invalid run_id format: {run_id!r}. Expected 'owner/repo/build_number'.")
    _repo_parts(slug, label="run_id repository")
    return slug, number


def _epoch_iso(value: object) -> str:
    """Render a Woodpecker unix timestamp as an ISO-8601 UTC string (``""`` if absent)."""
    ts = _safe_int(value)
    if ts <= 0:
        return ""
    try:
        return datetime.fromtimestamp(ts, tz=UTC).isoformat().replace("+00:00", "Z")
    except (OverflowError, OSError, ValueError):
        return ""


def _workflow_steps(workflow: dict[str, Any]) -> list[tuple[int, str]]:
    """Extract ``(step_id, step_name)`` pairs from one workflow's ``children``.

    A malformed child (not an object, or carrying no usable ``id``) is skipped
    with a warning rather than crashing the whole log read.
    """
    children = workflow.get("children")
    if not isinstance(children, list):
        return []
    steps: list[tuple[int, str]] = []
    for step in children:
        if not isinstance(step, dict):
            continue
        step_id = _safe_int(step.get("id"))
        if step_id <= 0:
            logger.warning("Skipping step with missing/invalid id in pipeline payload: %r", step.get("id"))
            continue
        name = step.get("name")
        steps.append((step_id, str(name) if name else str(step_id)))
    return steps


def _step_tree(pipeline: object) -> list[tuple[int, str]]:
    """Extract ``(step_id, step_name)`` pairs from a pipeline payload.

    ``GET /pipelines/{number}`` embeds ``workflows[].children[]`` (the step
    tree); a payload without it (a pipeline that has not materialised its
    workflows yet, or a corrupt body) yields no steps, which the caller
    reports as an empty log rather than crashing.
    """
    if not isinstance(pipeline, dict):
        return []
    workflows = pipeline.get("workflows")
    if not isinstance(workflows, list):
        return []
    steps: list[tuple[int, str]] = []
    for workflow in workflows:
        if isinstance(workflow, dict):
            steps.extend(_workflow_steps(workflow))
    return steps


def _decode_entry(entry: object) -> list[str]:
    """Decode one log entry into its output lines.

    ``data`` is base64 (Go marshals ``[]byte``); metadata (3) and progress (4)
    entries are deliberate filters (step markers and spinner frames, not
    command output), the exit-code entry (2) is rendered as a marker line, and
    stdout/stderr (0/1) are the log lines themselves. A corrupt or non-UTF-8
    payload degrades to a logged skip instead of raising, so one bad entry
    cannot take down the whole log read.
    """
    if not isinstance(entry, dict):
        return []
    entry_type = _safe_int(entry.get("type"), default=_LOG_ENTRY_STDOUT)
    if entry_type in (_LOG_ENTRY_METADATA, _LOG_ENTRY_PROGRESS):
        return []
    data = entry.get("data")
    if not isinstance(data, str) or not data:
        return []
    try:
        text = base64.b64decode(data).decode("utf-8", errors="replace")
    except ValueError:
        # ``binascii.Error`` (raised for invalid base64) is a ``ValueError``
        # subclass, so this single handler covers both decode failures.
        logger.warning("Skipping log entry with undecodable base64 data (type=%s)", entry_type)
        return []
    text = text.rstrip("\r\n")
    if entry_type == _LOG_ENTRY_EXIT_CODE:
        return [f"--- exit code: {text} ---"]
    return [text]


def _log_lines(payload: object) -> list[str]:
    """Flatten a step-log response body into output lines."""
    return [line for entry in _safe_records_list(payload) for line in _decode_entry(entry)]


class WoodpeckerCIRunner(CIRunnerBase):
    """Woodpecker CI runner (the Drone-derived Woodpecker API).

    Credentials (from credentials_ciphertext):
      "token"  — Woodpecker personal access token (sent as ``Authorization:
                 Bearer <token>``)

    Config (config_json):
      "base_url"  — REQUIRED, the instance root (``https://ci.example.com``)

    Self-hosted / air-gapped instances are the target: the base URL is
    tenant-supplied and every client goes through the pinned SSRF transport.
    """

    def __init__(self, token: str, base_url: str) -> None:
        self._token = token
        self._base_url = base_url
        self._api_base = _resolve_api_base(base_url)

    def _credential_values(self) -> Sequence[str]:
        return (self._token,)

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._token}",
            "Accept": "application/json",
        }

    def _client(self) -> httpx.AsyncClient:
        # PINNED TRANSPORT (FAR-520): the base_url comes from tenant connector
        # config (self-hosted Woodpecker), so it is on the external trust
        # boundary — validate + resolve the host synchronously and pin the
        # validated IP onto the transport so the connection never re-resolves
        # at connect time (closes the DNS-rebinding window).
        return pinned_async_client_sync(
            self._api_base,
            base_url=self._api_base,
            headers=self._headers(),
            timeout=30,
            verify=egress_ssl_context(self),
        )

    # ------------------------------------------------------------------
    # Payload parsing
    # ------------------------------------------------------------------

    def _parse_run(self, raw: dict[str, Any], repo_slug: str) -> CIRun:
        """Parse a pipeline payload into a :class:`CIRun`.

        The id is qualified into ``owner/name/build_number`` — the exact form
        ``get_run_status``/``get_run_logs`` consume (see the run-id contract on
        ``CIRunnerBase``). A payload with no usable ``number`` yields ``""``,
        which ``trigger_run`` refuses to return.
        """
        number = _safe_int(raw.get("number"))
        run_id = f"{repo_slug}/{number}" if number > 0 else ""
        started = _safe_int(raw.get("started"))
        finished = _safe_int(raw.get("finished"))
        duration = finished - started if started > 0 and finished > started else None
        status = _STATUS_MAP.get(str(raw.get("status") or ""), CIRunStatus.UNKNOWN)
        updated = _safe_int(raw.get("updated")) or _safe_int(raw.get("created"))
        return CIRun(
            id=run_id,
            pipeline_id=repo_slug,
            status=status,
            # Woodpecker's payload carries no UI link (only the forge commit
            # URL), so the run URL is left empty rather than guessed.
            url="",
            branch=str(raw.get("branch") or ""),
            commit_sha=str(raw.get("commit") or ""),
            created_at=_epoch_iso(raw.get("created")),
            updated_at=_epoch_iso(updated),
            duration_seconds=duration,
            triggered_by=str(raw.get("author") or ""),
        )

    # ------------------------------------------------------------------
    # Shared request path
    # ------------------------------------------------------------------

    async def _lookup_repo(self, client: httpx.AsyncClient, slug: str) -> dict[str, Any]:
        """Resolve ``owner/name`` to its repository object (id + default branch).

        The numeric ``repo_id`` every repo route requires only exists here —
        slug-addressed repo paths are answered 400 by the server — so every
        operation starts with this one lookup.
        """
        response = await client.get(f"/repos/lookup/{slug}")
        if response.status_code != 200:
            raise ValueError(
                self._redacted_detail(
                    f"Woodpecker API error ({response.status_code}) resolving repository {slug!r}: {response.text}",
                )[:200],
            )
        try:
            body = response.json()
        except ValueError as exc:
            raise ValueError(f"Woodpecker returned a non-JSON body resolving repository {slug!r}") from exc
        if not isinstance(body, dict):
            raise ValueError(f"Woodpecker returned a non-object body resolving repository {slug!r}")
        return body

    @staticmethod
    def _repo_id(repo: dict[str, Any]) -> int:
        """The numeric id of a looked-up repository (never guessed)."""
        repo_id = _safe_int(repo.get("id"))
        if repo_id <= 0:
            raise ValueError("Woodpecker returned no usable repository id for this lookup — refusing to guess one")
        return repo_id

    async def _get_pipeline(self, client: httpx.AsyncClient, repo_id: int, number: str) -> dict[str, Any]:
        """Fetch one pipeline payload (status reads and log step enumeration)."""
        response = await client.get(f"/repos/{repo_id}/pipelines/{number}")
        response.raise_for_status()
        body = response.json()
        if not isinstance(body, dict):
            raise ValueError(f"Woodpecker returned a non-object pipeline body for build {number}")
        return body

    # ------------------------------------------------------------------
    # CIRunnerBase contract
    # ------------------------------------------------------------------

    async def health_check(self) -> HealthResult:
        try:
            async with self._client() as client:
                r = await client.get("/user")
                if r.status_code == 200:
                    return HealthResult(ok=True)
                if r.status_code in (401, 403):
                    return HealthResult(
                        ok=False,
                        detail="Authentication failed: invalid or expired Woodpecker token",
                    )
                return HealthResult(ok=False, detail=self._redacted_detail(f"HTTP {r.status_code}: {r.text}")[:200])
        except httpx.HTTPError as exc:
            return HealthResult(ok=False, detail=self._redacted_detail(f"HTTP error: {exc}")[:200])
        except ValueError as exc:
            # The outbound SSRF guard in _client() rejects a private/internal
            # base_url; report unhealthy with the remediation text instead of
            # raising, matching the other base_url-bearing connectors.
            return HealthResult(ok=False, detail=self._redacted_detail(str(exc))[:200])

    async def trigger_run(
        self,
        pipeline_id: str,
        branch: str = "",
        variables: dict[str, str] | None = None,
    ) -> CIRun:
        if not pipeline_id:
            raise ValueError("pipeline_id is required")
        owner, name = _repo_parts(pipeline_id, label="pipeline_id")
        slug = f"{owner}/{name}"

        try:
            async with self._client() as client:
                repo = await self._lookup_repo(client, slug)
                repo_id = self._repo_id(repo)
                # Woodpecker resolves the trigger branch server-side against
                # the forge, and an unresolvable branch fails the request — so
                # an empty caller branch falls back to the repository default
                # (the lookup returns ``model.Repo``, whose default-branch field
                # is JSON-tagged ``default_branch``) before the "main" default.
                default_branch = str(repo.get("default_branch") or "")
                body: dict[str, Any] = {"branch": branch or default_branch or "main"}
                if variables:
                    body["variables"] = dict(variables)

                r = await client.post(f"/repos/{repo_id}/pipelines", json=body)
                if r.status_code == 204:
                    # The server accepted the request but created no pipeline:
                    # there is no build number to qualify the id with, and an
                    # id the next get_run_status would reject must never be
                    # handed back while the caller believes a job is queued.
                    raise ValueError(
                        f"Woodpecker accepted the pipeline request for {slug!r} but created no "
                        "pipeline (HTTP 204) — refusing to return an unusable run id; "
                        "check the repository's branch configuration",
                    )
                r.raise_for_status()
                data = r.json()
                if not isinstance(data, dict):
                    raise ValueError(f"Woodpecker returned a non-object pipeline body for {slug!r}")
                run = self._parse_run(data, repo_slug=slug)
                # The id must be exactly what get_run_status/get_run_logs
                # consume: '<owner>/<name>/<build_number>' with TWO slashes. A
                # payload that cannot be read that way fails loud here — never
                # an id the next call rejects while the pipeline already runs.
                if run.id.count("/") != 2:
                    raise ValueError(
                        f"Woodpecker accepted the pipeline request for {slug!r} but the response "
                        "did not yield a consumable run id — refusing to return an unusable run id",
                    )
                return run
        except httpx.HTTPStatusError as exc:
            raise ValueError(
                self._redacted_detail(f"Woodpecker API error ({exc.response.status_code}): {exc.response.text}")[:200],
            ) from exc
        except httpx.HTTPError as exc:
            raise ValueError(f"Woodpecker API connection error: {exc}") from exc

    async def get_run_status(self, run_id: str) -> CIRun:
        slug, number = _split_run_id(run_id)
        try:
            async with self._client() as client:
                repo_id = self._repo_id(await self._lookup_repo(client, slug))
                body = await self._get_pipeline(client, repo_id, number)
                return self._parse_run(body, repo_slug=slug)
        except httpx.HTTPStatusError as exc:
            raise ValueError(
                self._redacted_detail(f"Woodpecker API error ({exc.response.status_code}): {exc.response.text}")[:200],
            ) from exc
        except httpx.HTTPError as exc:
            raise ValueError(f"Woodpecker API connection error: {exc}") from exc

    async def get_run_logs(self, run_id: str, cursor: str | None = None) -> CIRunLog:
        slug, number = _split_run_id(run_id)
        start = int(cursor) if cursor and cursor.isdigit() else 0
        try:
            async with self._client() as client:
                repo_id = self._repo_id(await self._lookup_repo(client, slug))
                pipeline = await self._get_pipeline(client, repo_id, number)
                steps = _step_tree(pipeline)
                if (
                    not steps
                    and "workflows" not in pipeline
                    and str(pipeline.get("status") or "") in (_TERMINAL_STATUSES)
                ):
                    # A terminal pipeline with no step tree is a payload shape
                    # we did not expect (the API embeds it on every terminal
                    # read) — empty logs are legal for a PENDING run, but not
                    # silently for a finished one.
                    logger.warning(
                        "Woodpecker pipeline %s is %s but carries no workflow/step tree — "
                        "returning an empty log rather than guessing steps",
                        run_id,
                        pipeline.get("status"),
                    )

                all_lines: list[str] = []
                unreadable_steps = 0
                for step_id, step_name in steps:
                    logs_r = await client.get(f"/repos/{repo_id}/logs/{number}/{step_id}")
                    if logs_r.status_code != 200:
                        unreadable_steps += 1
                        logger.warning(
                            "Failed to fetch logs for step %s (id=%s) of %s (HTTP %s) — skipping (%d skipped so far)",
                            step_name,
                            step_id,
                            run_id,
                            logs_r.status_code,
                            unreadable_steps,
                        )
                        continue
                    try:
                        entries = logs_r.json()
                    except ValueError:
                        unreadable_steps += 1
                        logger.warning(
                            "Step %s (id=%s) of %s returned a non-JSON log body — skipping (%d skipped so far)",
                            step_name,
                            step_id,
                            run_id,
                            unreadable_steps,
                        )
                        continue
                    all_lines.append(f"--- Step {step_name} ---")
                    all_lines.extend(_log_lines(entries))
                    all_lines.append("")

                page = all_lines[start:]
                next_cursor = str(start + len(page)) if cursor is not None and page else None
                return CIRunLog(
                    run_id=run_id,
                    lines=page,
                    next_cursor=next_cursor,
                    truncated=unreadable_steps > 0,
                )
        except httpx.HTTPStatusError as exc:
            raise ValueError(
                self._redacted_detail(f"Woodpecker API error ({exc.response.status_code}): {exc.response.text}")[:200],
            ) from exc
        except httpx.HTTPError as exc:
            raise ValueError(f"Woodpecker API connection error: {exc}") from exc

    async def list_runs(
        self,
        pipeline_id: str | None = None,
        status: CIRunStatus | None = None,
        limit: int = 20,
    ) -> list[CIRun]:
        if not pipeline_id:
            raise ValueError("pipeline_id is required for list_runs")
        owner, name = _repo_parts(pipeline_id, label="pipeline_id")
        slug = f"{owner}/{name}"
        # The server clamps perPage above 50 to 50 — clamp here so the
        # requested limit is the limit we ask for, not one silently changed.
        per_page = max(1, min(limit, _MAX_PAGE_SIZE))
        params: dict[str, Any] = {"page": 1, "perPage": per_page}
        if status:
            wp_status = _LIST_STATUS_MAP.get(status)
            if wp_status:
                params["status"] = wp_status
            elif status == CIRunStatus.UNKNOWN:
                logger.warning("Cannot filter by UNKNOWN status — returning all runs")
            else:
                logger.warning("No Woodpecker mapping for status %s — returning all runs", status)

        try:
            async with self._client() as client:
                repo_id = self._repo_id(await self._lookup_repo(client, slug))
                r = await client.get(f"/repos/{repo_id}/pipelines", params=params)
                r.raise_for_status()
                raw_runs = _safe_records_list(r.json())
                return [self._parse_run(run, repo_slug=slug) for run in raw_runs]
        except httpx.HTTPStatusError as exc:
            raise ValueError(
                self._redacted_detail(f"Woodpecker API error ({exc.response.status_code}): {exc.response.text}")[:200],
            ) from exc
        except httpx.HTTPError as exc:
            raise ValueError(f"Woodpecker API connection error: {exc}") from exc


class _WoodpeckerTestDouble(CITestDoubleMixin, WoodpeckerCIRunner):
    """Minimal test double that does not make HTTP calls.

    ``trigger_run`` / ``list_runs`` scaffolding comes from
    :class:`CITestDoubleMixin`; the ``owner/repo/build_number`` id shape (and
    the empty-pipeline_id fail-loud) is supplied by the ``_double_*`` hooks
    below, derived the same way the real producer derives it so a double's id
    round-trips through the REAL parser (``test_ci_run_id_contract``).
    """

    _double_default_pipeline_id = "acme/widget"

    def __init__(self) -> None:
        import uuid as _uuid

        self._token = "wpt_test"  # nosec - test double, not a real credential
        self._base_url = "https://ci.example.com"
        self._api_base = "https://ci.example.com/api"
        self._uuid = _uuid
        self._status: CIRunStatus = CIRunStatus.QUEUED
        self._run_logs: list[str] = []
        self._triggered: list[dict[str, Any]] = []
        self._double_build_number = 100

    def _client(self) -> httpx.AsyncClient:
        raise RuntimeError("Test double has no HTTP client")

    async def health_check(self) -> HealthResult:
        return HealthResult(ok=True)

    def _record_triggered_run(self, run: CIRun, variables: dict[str, str] | None) -> None:
        self._triggered.append({"run": run, "variables": variables or {}})

    def _double_trigger_id(self, pipeline_id: str) -> str:
        if not pipeline_id:
            raise ValueError("pipeline_id is required")
        self._double_build_number += 1
        return f"{pipeline_id}/{self._double_build_number}"

    def _double_listed_id(self, resolved: str) -> str:
        return f"{resolved}/100"

    async def get_run_status(self, run_id: str) -> CIRun:
        return CIRun(
            id=run_id,
            pipeline_id=run_id.rsplit("/", 1)[0] if "/" in run_id else "",
            status=self._status,
        )

    async def get_run_logs(self, run_id: str, _cursor: str | None = None) -> CIRunLog:
        return CIRunLog(run_id=run_id, lines=self._run_logs)
