"""API changelog endpoint — lists version history for the Modulo API."""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

router = APIRouter(prefix="/api/v1/changelog", tags=["changelog"])


class ChangelogEntry(BaseModel):
    version: str
    date: str
    summary: str
    changes: list[str]
    deprecations: list[str] | None = None
    migration_url: str | None = None


_SEED_ENTRIES: list[ChangelogEntry] = [
    ChangelogEntry(
        version="1.0",
        date="2026-03-01",
        summary="Initial API release",
        changes=[
            "Pipelines CRUD — create, list, get, update, delete pipelines",
            "Runs — trigger, list, get run status and results",
            "Agents — create, list, get, update, delete agent configurations",
            "Schemas — create, version, list, get schema definitions",
            "Connectors — configure Filesystem, GitHub, GitLab, Linear, Jira connectors",
            "Model Backends — configure Anthropic, OpenAI, Ollama providers",
            "Authentication — JWT-based login, API key auth, SSO/OIDC support",
            "Teams — team CRUD and membership management",
            "HITL — human-in-the-loop gates: claim, approve, reject, list pending",
            "Triggers — manual and webhook-based pipeline triggers",
            "Webhooks — outbound event notifications with HMAC signing",
            "Audit log — tamper-evident event trail with cursor pagination",
            "Dashboard — aggregate pipeline, run, and HITL summary view",
            "Admin — organisation, user, team, billing, and eval management",
            "Library — browse, preview, and copy primitive templates",
            "Feature flags — view flag status and license tier information",
            "Cost controller — token counting and daily budget enforcement",
            "Plugin registry — discover, enable, and configure plugins",
            "Feedback — submit and review pipeline run feedback",
            "Variants — A/B test and compare pipeline variant outputs",
            "Observability — OTel-backed metrics and tracing export",
            "Determination — code review and quality scanner endpoints",
            "Evals — eval definition CRUD, result tracking, OKR progress",
            "Environments — environment variable management for pipelines",
            "Notifications — in-app and webhook notification management",
            "MCP — remote MCP server with SSE transport and API key auth",
        ],
        deprecations=None,
        migration_url=None,
    ),
]


@router.get("", response_model=list[ChangelogEntry])
async def list_changelog() -> list[ChangelogEntry]:
    """Return all changelog entries sorted by date descending."""
    return sorted(_SEED_ENTRIES, key=lambda e: e.date, reverse=True)


@router.get("/latest", response_model=ChangelogEntry)
async def latest_changelog() -> ChangelogEntry:
    """Return the most recent changelog entry."""
    entries = sorted(_SEED_ENTRIES, key=lambda e: e.date, reverse=True)
    if not entries:
        raise HTTPException(status_code=404, detail="No changelog entries")
    return entries[0]
