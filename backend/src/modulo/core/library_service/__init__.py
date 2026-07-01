"""Library service — CRUD and community primitives for library_primitives."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.db.crud.base import PageResult
from modulo.db.crud.library_primitive import (
    create_library_primitive,
    get_library_primitive,
    list_library_primitives,
    list_primitives_by_version_group,
    update_library_primitive,
)
from modulo.db.models.library_primitive import LibraryPrimitive
from modulo.db.rls import set_rls_org

# Fixed sentinel used as organisation_id for modulo (built-in) primitives.
MODULO_ORG_ID: uuid.UUID = uuid.UUID("00000000-0000-0000-0000-000000000001")

_EPOCH = datetime(2024, 1, 1, tzinfo=UTC)


class CommunityPrimitiveReadOnlyError(Exception):
    """Raised when a modulo/community primitive is adapted via MCP — browser UI only."""


def _make_modulo(
    pid: str,
    primitive_type: str,
    name: str,
    slug: str,
    description: str,
    content_json: dict[str, Any],
    tags: list[str],
) -> LibraryPrimitive:
    p = LibraryPrimitive(
        id=uuid.UUID(pid),
        organisation_id=MODULO_ORG_ID,
        source="modulo",
        primitive_type=primitive_type,
        name=name,
        slug=slug,
        description=description,
        author="modulo",
        version="1.0",
        tags=tags,
        content_json=content_json,
        source_url=None,
        forked_from=None,
        checksum=None,
        ed25519_signature=None,
        verified=None,
        download_count=None,
        average_rating=None,
        review_count=None,
        owner_team_id=None,
        visibility="community",
        account_id=None,
    )
    # server_default fields are not populated without a DB flush; set them explicitly.
    p.created_at = _EPOCH
    p.updated_at = _EPOCH
    p.auto_update = True
    p.contribution_status = "published" if primitive_type == "test_fixture" else None
    return p


# ---------------------------------------------------------------------------
# Contribution status constants
# ---------------------------------------------------------------------------

CONTRIBUTION_DRAFT = "draft"
CONTRIBUTION_REVIEW_QUEUE = "review_queue"
CONTRIBUTION_PUBLISHED = "published"


class ContributionNotFoundError(LookupError):
    """Raised when a contribution primitive is not found."""


class ContributionInvalidTransitionError(ValueError):
    """Raised when an invalid contribution status transition is attempted."""


# ---------------------------------------------------------------------------
# Built-in community primitives (in-memory, no DB row required)
# ---------------------------------------------------------------------------

_MODULO_PRIMITIVES: list[LibraryPrimitive] = [
    _make_modulo(
        pid="00000000-0000-0000-0000-000000000010",
        primitive_type="schema",
        name="PRD Input Schema",
        slug="prd-input",
        description="Input schema for a product requirements document.",
        content_json={
            "fields": [
                {"name": "title", "type": "string", "required": True},
                {"name": "problem_statement", "type": "string", "required": True},
                {"name": "goals", "type": "array", "items": "string", "required": False},
                {"name": "non_goals", "type": "array", "items": "string", "required": False},
                {"name": "stakeholders", "type": "array", "items": "string", "required": False},
            ]
        },
        tags=["schema", "product", "prd"],
    ),
    _make_modulo(
        pid="00000000-0000-0000-0000-000000000011",
        primitive_type="schema",
        name="Requirements Output Schema",
        slug="requirements-output",
        description="Structured requirements extracted from a PRD.",
        content_json={
            "fields": [
                {"name": "functional", "type": "array", "items": "string", "required": True},
                {"name": "non_functional", "type": "array", "items": "string", "required": False},
                {
                    "name": "acceptance_criteria",
                    "type": "array",
                    "items": "string",
                    "required": False,
                },
                {"name": "out_of_scope", "type": "array", "items": "string", "required": False},
            ]
        },
        tags=["schema", "requirements", "prd"],
    ),
    _make_modulo(
        pid="00000000-0000-0000-0000-000000000020",
        primitive_type="agent",
        name="PRD Ingestion Agent",
        slug="prd-ingestion",
        description="Reads a PRD document and normalises it into the PRD Input Schema.",
        content_json={
            "input_schema": "prd-input",
            "output_schema": "prd-input",
            "prompt_template": (
                "You are a technical analyst. Read the following product requirements document "
                "and extract the key information into structured form.\n\nDocument:\n{{ input }}"
            ),
        },
        tags=["agent", "prd", "ingestion"],
    ),
    _make_modulo(
        pid="00000000-0000-0000-0000-000000000021",
        primitive_type="agent",
        name="Requirements Writer Agent",
        slug="requirements-writer",
        description="Transforms a normalised PRD into a structured requirements document.",
        content_json={
            "input_schema": "prd-input",
            "output_schema": "requirements-output",
            "prompt_template": (
                "You are a senior software engineer. Given the following product requirements, "
                "produce a structured list of functional and non-functional requirements.\n\n"
                "PRD:\n{{ input }}"
            ),
        },
        tags=["agent", "requirements", "prd"],
    ),
    _make_modulo(
        pid="00000000-0000-0000-0000-000000000030",
        primitive_type="workflow",
        name="PRD to Requirements",
        slug="prd-to-requirements",
        description="End-to-end pipeline: ingest a PRD and produce structured requirements.",
        content_json={
            "nodes": [
                {"id": "ingest", "agent": "prd-ingestion"},
                {"id": "write", "agent": "requirements-writer"},
            ],
            "edges": [{"source": "ingest", "target": "write"}],
            "entry": "ingest",
        },
        tags=["workflow", "prd", "requirements"],
    ),
    _make_modulo(
        pid="00000000-0000-0000-0000-000000000040",
        primitive_type="test_fixture",
        name="Example Test Fixture",
        slug="example-test-fixture",
        description="Example StubModelBackend fixture map for a PRD-to-requirements pipeline run.",
        content_json={
            "fixture_map": {
                "Extract requirements from: Build a login system with SSO": (
                    "Functional: SSO authentication\nNon-functional: 99.9% uptime"
                ),
                "Refine requirements: SSO authentication, 99.9% uptime": (
                    "1. Integrate SAML 2.0 SSO\n2. Support OIDC providers\n3. 99.9% uptime SLA"
                ),
            },
            "pipeline_id": None,
            "run_id": None,
        },
        tags=["test_fixture", "example", "prd"],
    ),
    # -----------------------------------------------------------------------
    # Modulo dogfood pipeline primitives (schemas, agents, workflow)
    # -----------------------------------------------------------------------
    _make_modulo(
        pid="00000000-0000-0000-0000-000000000050",
        primitive_type="schema",
        name="GitHub Issue Input Schema",
        slug="github-issue-input",
        description="Schema for a GitHub issue to be processed by the Modulo dogfood pipeline.",
        content_json={
            "fields": [
                {"name": "issue_number", "type": "integer", "required": True},
                {"name": "title", "type": "string", "required": True},
                {"name": "body", "type": "string", "required": True},
                {"name": "labels", "type": "array", "items": "string", "required": False},
                {"name": "repo", "type": "string", "required": True},
            ]
        },
        tags=["schema", "github", "issue", "dogfood"],
    ),
    _make_modulo(
        pid="00000000-0000-0000-0000-000000000051",
        primitive_type="schema",
        name="Structured Requirements Schema",
        slug="structured-requirements",
        description="Structured requirements extracted from a GitHub issue for code generation.",
        content_json={
            "fields": [
                {"name": "agent_task", "type": "string", "required": True},
                {"name": "feature_area", "type": "string", "required": True},
                {"name": "spec_summary", "type": "string", "required": True},
                {"name": "files_to_change", "type": "array", "items": "string", "required": False},
                {"name": "implementation_notes", "type": "string", "required": False},
            ]
        },
        tags=["schema", "requirements", "spec", "dogfood"],
    ),
    _make_modulo(
        pid="00000000-0000-0000-0000-000000000052",
        primitive_type="schema",
        name="Code Diff Output Schema",
        slug="code-diff-output",
        description="Generated code changes as a list of file diffs.",
        content_json={
            "fields": [
                {
                    "name": "files",
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "path": {"type": "string"},
                            "content": {"type": "string"},
                        },
                    },
                    "required": True,
                },
            ]
        },
        tags=["schema", "code", "diff", "dogfood"],
    ),
    _make_modulo(
        pid="00000000-0000-0000-0000-000000000053",
        primitive_type="schema",
        name="Test Result Output Schema",
        slug="test-result-output",
        description="Result of running tests against generated code.",
        content_json={
            "fields": [
                {"name": "passed", "type": "boolean", "required": True},
                {"name": "failed", "type": "boolean", "required": True},
                {"name": "output", "type": "string", "required": True},
                {"name": "duration_ms", "type": "integer", "required": False},
            ]
        },
        tags=["schema", "test", "result", "dogfood"],
    ),
    _make_modulo(
        pid="00000000-0000-0000-0000-000000000054",
        primitive_type="schema",
        name="PR Output Schema",
        slug="pr-output",
        description="Result of creating a pull request.",
        content_json={
            "fields": [
                {"name": "pr_url", "type": "string", "required": True},
                {"name": "pr_number", "type": "integer", "required": True},
                {"name": "success", "type": "boolean", "required": True},
            ]
        },
        tags=["schema", "pr", "github", "dogfood"],
    ),
    _make_modulo(
        pid="00000000-0000-0000-0000-000000000060",
        primitive_type="agent",
        name="Issue Reader Agent",
        slug="issue-reader",
        description="Reads a GitHub issue via GitHubConnector and extracts a structured spec for code generation.",
        content_json={
            "input_schema": "github-issue-input",
            "output_schema": "structured-requirements",
            "prompt_template": (
                "You are a technical product manager. Read the following GitHub issue "
                "and extract a structured specification for implementation.\n\n"
                "Issue:\n{{ input }}"
            ),
            "connector_type_refs": [{"connector_type": "github", "capabilities": ["issue_read"]}],
            "required_environment_capabilities": ["egress:github.com"],
            "model_backend_id": None,
            "retry_policy": {},
            "token_budget": None,
        },
        tags=["agent", "github", "issue-reader", "dogfood"],
    ),
    _make_modulo(
        pid="00000000-0000-0000-0000-000000000061",
        primitive_type="agent",
        name="Code Generator Agent",
        slug="code-generator",
        description="Generates code changes from structured requirements as file diffs.",
        content_json={
            "input_schema": "structured-requirements",
            "output_schema": "code-diff-output",
            "prompt_template": (
                "You are a senior software engineer. Given the following structured requirements, "
                "generate the necessary code changes. Output a list of files with their full content.\n\n"
                "Requirements:\n{{ input }}"
            ),
            "connector_type_refs": [],
            "required_environment_capabilities": [],
            "model_backend_id": None,
            "retry_policy": {},
            "token_budget": None,
        },
        tags=["agent", "code-generation", "llm", "dogfood"],
    ),
    _make_modulo(
        pid="00000000-0000-0000-0000-000000000062",
        primitive_type="agent",
        name="Code Applier Agent",
        slug="code-applier",
        description="Writes generated code files to the workspace via ShellConnector.",
        content_json={
            "input_schema": "code-diff-output",
            "output_schema": "code-diff-output",
            "prompt_template": (
                "You are a build engineer. Apply the following code changes to the workspace "
                "by writing each file to disk.\n\n"
                "Changes:\n{{ input }}"
            ),
            "connector_type_refs": [{"connector_type": "shell", "capabilities": ["write"]}],
            "required_environment_capabilities": ["filesystem:write"],
            "model_backend_id": None,
            "retry_policy": {},
            "token_budget": None,
        },
        tags=["agent", "code-applier", "shell", "dogfood"],
    ),
    _make_modulo(
        pid="00000000-0000-0000-0000-000000000063",
        primitive_type="agent",
        name="Test Runner Agent",
        slug="test-runner",
        description="Runs unit tests in the workspace via ShellConnector and reports results.",
        content_json={
            "input_schema": "code-diff-output",
            "output_schema": "test-result-output",
            "prompt_template": (
                "You are a QA engineer. Run the unit tests at the workspace root "
                "using the command 'uv run pytest tests/unit -x -q' and report the results.\n\n"
                "Code changes applied:\n{{ input }}"
            ),
            "connector_type_refs": [{"connector_type": "shell", "capabilities": ["read", "write"]}],
            "required_environment_capabilities": ["shell:exec", "python3.12", "uv"],
            "model_backend_id": None,
            "retry_policy": {},
            "token_budget": None,
        },
        tags=["agent", "test-runner", "shell", "dogfood"],
    ),
    _make_modulo(
        pid="00000000-0000-0000-0000-000000000064",
        primitive_type="agent",
        name="PR Creator Agent",
        slug="pr-creator",
        description="Creates a pull request on GitHub with the generated changes and test summary.",
        content_json={
            "input_schema": "test-result-output",
            "output_schema": "pr-output",
            "prompt_template": (
                "You are a release engineer. Create a pull request on GitHub with the changes "
                "that were made and include the test results in the PR body.\n\n"
                "Test results:\n{{ input }}"
            ),
            "connector_type_refs": [{"connector_type": "github", "capabilities": ["create_pr"]}],
            "required_environment_capabilities": ["egress:github.com"],
            "model_backend_id": None,
            "retry_policy": {},
            "token_budget": None,
        },
        tags=["agent", "pr-creator", "github", "dogfood"],
    ),
    _make_modulo(
        pid="00000000-0000-0000-0000-000000000070",
        primitive_type="workflow",
        name="Modulo Dogfood Pipeline",
        slug="modulo-dogfood-pipeline",
        description=(
            "End-to-end pipeline that builds Modulo from a GitHub issue: reads spec, "
            "generates code, applies changes, runs tests, and creates a PR with HITL review."
        ),
        content_json={
            "nodes": [
                {"id": "issue-reader", "agent": "issue-reader"},
                {"id": "code-generator", "agent": "code-generator"},
                {"id": "code-applier", "agent": "code-applier"},
                {"id": "test-runner", "agent": "test-runner"},
                {"id": "pr-creator", "agent": "pr-creator"},
            ],
            "edges": [
                {"source": "issue-reader", "target": "code-generator"},
                {"source": "code-generator", "target": "code-applier"},
                {"source": "code-applier", "target": "test-runner"},
                {
                    "source": "test-runner",
                    "target": "pr-creator",
                    "hitl_gate_config": {
                        "human_only": False,
                        "gate_id": "review_before_pr",
                        "overdue_threshold_minutes": 60,
                    },
                },
            ],
            "entry": "issue-reader",
        },
        tags=["workflow", "dogfood", "modulo", "pipeline"],
    ),
    # -----------------------------------------------------------------------
    # Modulo example composite primitives
    # -----------------------------------------------------------------------
    _make_modulo(
        pid="00000000-0000-0000-0000-000000000090",
        primitive_type="composite",
        name="Approver",
        slug="approver",
        description="Binary approval gate. Output starts with APPROVED or REJECTED. Self-corrects on failure.",
        content_json={
            "parameter_ports": [
                {"name": "system_prompt", "label": "System Prompt", "type": "string", "required": True,
                 "description": "Instructions for what to approve/reject",
                 "default_value": (
                     "You are an approver. Respond with APPROVED or REJECTED"
                     " as the first word, followed by your reasoning."
                 ),
                 "target_injection": {
                     "mode": "prompt_replace",
                     "node_id": "decision-agent",
                     "injection_point": "prompt_template",
                 }},
            ],
            "sub_pipeline_graph_json": {
                "nodes": [{"id": "decision-agent", "node_type": "agent", "label": "Decision Agent"}],
                "edges": [],
            },
            "input_schema_id": None,
            "output_schema": {
                "type": "object",
                "properties": {
                    "result": {"type": "string"},
                    "reasoning": {"type": "string"},
                },
                "required": ["result"],
            },
            "output_validation": {
                "eval_definitions": [{
                    "name": "first_word_approved_rejected",
                    "type": "regex",
                    "config": {"pattern": "^(APPROVED|REJECTED)\\b", "field": "result"},
                    "failure_behaviour": "retry",
                }],
                "max_validation_retries": 2,
            },
        },
        tags=["composite", "approval", "gate", "validation"],
    ),
    _make_modulo(
        pid="00000000-0000-0000-0000-000000000091",
        primitive_type="composite",
        name="Booleaner",
        slug="booleaner",
        description="Forces TRUE or FALSE decision. First word is forced. Useful for conditional routing.",
        content_json={
            "parameter_ports": [
                {"name": "system_prompt", "label": "System Prompt", "type": "string", "required": True,
                 "description": "Instructions for what to evaluate as true or false",
                 "default_value": (
                     "You are a boolean evaluator. Respond with TRUE"
                     " or FALSE as the first word, followed by your reasoning."
                 ),
                 "target_injection": {
                     "mode": "prompt_replace",
                     "node_id": "decision-agent",
                     "injection_point": "prompt_template",
                 }},
            ],
            "sub_pipeline_graph_json": {
                "nodes": [{"id": "decision-agent", "node_type": "agent", "label": "Decision Agent"}],
                "edges": [],
            },
            "input_schema_id": None,
            "output_schema": {
                "type": "object",
                "properties": {
                    "result": {"type": "string"},
                    "reasoning": {"type": "string"},
                },
                "required": ["result"],
            },
            "output_validation": {
                "eval_definitions": [{
                    "name": "first_word_true_false",
                    "type": "regex",
                    "config": {"pattern": "^(TRUE|FALSE)\\b", "field": "result"},
                    "failure_behaviour": "retry",
                }],
                "max_validation_retries": 2,
            },
        },
        tags=["composite", "boolean", "decision", "validation"],
    ),
    _make_modulo(
        pid="00000000-0000-0000-0000-000000000092",
        primitive_type="composite",
        name="d20",
        slug="d20",
        description="Rolls a d20 (1-20). Output is always an integer 1-20. Self-corrects on hallucination.",
        content_json={
            "parameter_ports": [
                {"name": "system_prompt", "label": "System Prompt", "type": "string", "required": True,
                 "description": "Instructions for the dice roll context",
                 "default_value": (
                     "You roll a 20-sided die. Respond with just"
                     " a number between 1 and 20."
                 ),
                 "target_injection": {
                     "mode": "prompt_replace",
                     "node_id": "roll-agent",
                     "injection_point": "prompt_template",
                 }},
            ],
            "sub_pipeline_graph_json": {
                "nodes": [{"id": "roll-agent", "node_type": "agent", "label": "Dice Roll Agent"}],
                "edges": [],
            },
            "input_schema_id": None,
            "output_schema": {
                "type": "object",
                "properties": {
                    "roll": {"type": "integer", "minimum": 1, "maximum": 20},
                },
                "required": ["roll"],
            },
            "output_validation": {
                "eval_definitions": [{
                    "name": "roll_in_range",
                    "type": "json_schema",
                    "config": {
                        "schema": {
                            "type": "object",
                            "properties": {"roll": {"type": "integer", "minimum": 1, "maximum": 20}},
                            "required": ["roll"],
                        },
                    },
                    "failure_behaviour": "retry",
                }],
                "max_validation_retries": 3,
            },
        },
        tags=["composite", "dice", "random", "game"],
    ),
    _make_modulo(
        pid="00000000-0000-0000-0000-000000000093",
        primitive_type="composite",
        name="Triage",
        slug="triage",
        description="Classifies into BUG, FEATURE, INFRA, DOCS. First word is forced to one of the four.",
        content_json={
            "parameter_ports": [
                {"name": "system_prompt", "label": "System Prompt", "type": "string", "required": True,
                 "description": "Instructions for the triage classification",
                 "default_value": (
                     "You are a triage classifier. Respond with one of"
                     " BUG, FEATURE, INFRA, or DOCS as the first word,"
                     " followed by your reasoning."
                 ),
                 "target_injection": {
                     "mode": "prompt_replace",
                     "node_id": "classifier-agent",
                     "injection_point": "prompt_template",
                 }},
            ],
            "sub_pipeline_graph_json": {
                "nodes": [{"id": "classifier-agent", "node_type": "agent", "label": "Classifier Agent"}],
                "edges": []
            },
            "input_schema_id": None,
            "output_schema": {
                "type": "object",
                "properties": {
                    "result": {"type": "string"},
                    "reasoning": {"type": "string"},
                },
                "required": ["result"],
            },
            "output_validation": {
                "eval_definitions": [{
                    "name": "first_word_category",
                    "type": "regex",
                    "config": {"pattern": "^(BUG|FEATURE|INFRA|DOCS)\\b", "field": "result"},
                    "failure_behaviour": "retry",
                }],
                "max_validation_retries": 2,
            },
        },
        tags=["composite", "triage", "classification", "bug", "feature"],
    ),
]

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _filter_modulo(
    *,
    primitive_type: str | None,
    search: str | None,
) -> list[LibraryPrimitive]:
    results = _MODULO_PRIMITIVES
    if primitive_type is not None:
        results = [p for p in results if p.primitive_type == primitive_type]
    if search:
        term = search.strip().lower()
        results = [p for p in results if term in p.name.lower() or term in (p.description or "").lower()]
    return results


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def list_primitives(
    session: AsyncSession,
    org_id: uuid.UUID,
    *,
    primitive_type: str | None = None,
    search: str | None = None,
    page: int = 1,
    page_size: int = 20,
    include_community: bool = True,
    cursor: str | None = None,
) -> PageResult[LibraryPrimitive]:
    """Return org-scoped and community primitives merged into a single page."""
    await set_rls_org(session, org_id)
    org_page = await list_library_primitives(
        session,
        org_id=org_id,
        page=page,
        page_size=page_size,
        primitive_type=primitive_type,
        search=search,
        cursor=cursor,
    )

    modulo: list[LibraryPrimitive] = (
        _filter_modulo(primitive_type=primitive_type, search=search) if include_community else []
    )

    all_items: list[LibraryPrimitive] = list(org_page.items) + modulo
    return PageResult(
        items=all_items,
        total=org_page.total + len(modulo),
        page=page,
        page_size=page_size,
        next_cursor=org_page.next_cursor,
        has_more=org_page.has_more,
    )


async def get_primitive(
    session: AsyncSession,
    org_id: uuid.UUID,
    primitive_id: uuid.UUID,
) -> LibraryPrimitive | None:
    """Return a primitive visible to org_id, or None.

    Checks the org-scoped DB first, then falls back to in-memory modulo primitives.
    """
    async with session.begin():
        await set_rls_org(session, org_id)
        item = await get_library_primitive(session, primitive_id)
    if item is not None:
        return item
    return _MODULO_BY_ID.get(primitive_id)


async def get_primitive_by_slug(
    session: AsyncSession,
    org_id: uuid.UUID,
    primitive_type: str,
    slug: str,
) -> LibraryPrimitive | None:
    """Return a primitive visible to org_id by type and slug, or None.

    Checks the org-scoped DB first, then falls back to in-memory modulo primitives.
    """
    async with session.begin():
        await set_rls_org(session, org_id)
        stmt = select(LibraryPrimitive).where(
            LibraryPrimitive.primitive_type == primitive_type,
            LibraryPrimitive.slug == slug,
        )
        result = await session.execute(stmt)
        item = result.scalar_one_or_none()
    if item is not None:
        return item
    return _MODULO_BY_SLUG.get((primitive_type, slug))


async def copy_to_adapt(
    session: AsyncSession,
    org_id: uuid.UUID,
    primitive_id: uuid.UUID,
    *,
    target_team_id: uuid.UUID | None = None,
    created_by: uuid.UUID | None = None,
    via_mcp: bool = False,
) -> LibraryPrimitive:
    """Clone a primitive into the org workspace.

    Raises CommunityPrimitiveReadOnlyError if via_mcp=True and the source is community.
    Raises LookupError if the primitive does not exist.
    """
    source = await get_primitive(session, org_id, primitive_id)
    if source is None:
        raise LookupError(f"Primitive {primitive_id} not found for org {org_id}")

    if via_mcp and source.visibility == "community":
        raise CommunityPrimitiveReadOnlyError(
            "Community primitives may only be adapted via the browser UI, not via MCP."
        )

    # Increment download count on registry/community primitives.
    if source.source in ("registry", "modulo") and source.download_count is not None:
        async with session.begin():
            await set_rls_org(session, org_id)
            await update_library_primitive(
                session,
                source.id,
                {"download_count": source.download_count + 1},
            )

    # Bump the minor version for the copy.
    parts = source.version.split(".")
    try:
        parts[-1] = str(int(parts[-1]) + 1)
    except (ValueError, IndexError):
        parts = ["1", "0"]
    new_version = ".".join(parts)

    async with session.begin():
        await set_rls_org(session, org_id)
        result = await create_library_primitive(
            session,
            org_id=org_id,
            source="local",
            primitive_type=source.primitive_type,
            name=source.name,
            slug=f"{source.slug}-copy",
            description=source.description,
            author=source.author,
            version=new_version,
            tags=list(source.tags or []),
            content_json=dict(source.content_json),
            source_url=None,
            forked_from=source.id,
            checksum=None,
            ed25519_signature=None,
            verified=None,
            download_count=None,
            average_rating=None,
            review_count=None,
            owner_team_id=target_team_id,
            visibility="org",
            account_id=created_by,
            auto_update=True,
        )
    return result


# ---------------------------------------------------------------------------
# Starter pipeline templates

_PR_TEMPLATE_AGENTS = [
    {
        "name": "Issue Reader",
        "description": "Reads a GitHub issue via GitHubConnector and extracts structured requirements.",
        "prompt_template": (
            "Read the following GitHub issue and extract structured requirements for code review."
            "\n\nIssue:\n{{ input }}"
        ),
        "connector_type_refs": [{"connector_type": "github", "capabilities": ["issue_read"]}],
        "required_environment_capabilities": ["egress:github.com"],
    },
    {
        "name": "Code Diff Analyzer",
        "description": (
            "Analyses code changes and identifies potential issues, style violations, and security concerns."
        ),
        "prompt_template": (
            "Review the following code diff and identify: 1) logic errors, 2) style violations,"
            " 3) security issues, 4) performance concerns.\n\nDiff:\n{{ input }}"
        ),
        "connector_type_refs": [],
        "required_environment_capabilities": [],
    },
    {
        "name": "Comment Generator",
        "description": "Generates actionable review comments from the diff analysis.",
        "prompt_template": (
            "Based on the analysis below, generate clear, actionable PR review comments."
            " Be constructive and specific.\n\nAnalysis:\n{{ input }}"
        ),
        "connector_type_refs": [],
        "required_environment_capabilities": [],
    },
    {
        "name": "PR Poster",
        "description": "Posts the compiled review to the GitHub PR as a review comment.",
        "prompt_template": "Post the following review as a GitHub PR review comment.\n\nReview:\n{{ input }}",
        "connector_type_refs": [{"connector_type": "github", "capabilities": ["create_pr"]}],
        "required_environment_capabilities": ["egress:github.com"],
    },
]

_RELEASE_TEMPLATE_AGENTS = [
    {
        "name": "Version Bumper",
        "description": "Reads the current version from a file and proposes the next semantic version.",
        "prompt_template": (
            "Read the current version and determine the next semantic version based on the changes described."
            "\n\nChanges:\n{{ input }}"
        ),
        "connector_type_refs": [{"connector_type": "github", "capabilities": ["issue_read"]}],
        "required_environment_capabilities": ["egress:github.com"],
    },
    {
        "name": "Changelog Generator",
        "description": "Generates a changelog entry from commit messages or release notes.",
        "prompt_template": "Generate a changelog entry from the following commit history:\n\n{{ input }}",
        "connector_type_refs": [],
        "required_environment_capabilities": [],
    },
    {
        "name": "Release Notes Writer",
        "description": "Polishes changelog entries into formatted release notes.",
        "prompt_template": "Format the following changelog into polished release notes:\n\n{{ input }}",
        "connector_type_refs": [],
        "required_environment_capabilities": [],
    },
    {
        "name": "Tag Creator",
        "description": "Creates a Git tag for the new version via GitHubConnector.",
        "prompt_template": (
            "Create a Git tag for version {{ version }} and push it to the remote repository."
            "\n\nRelease notes:\n{{ input }}"
        ),
        "connector_type_refs": [{"connector_type": "github", "capabilities": ["create_pr"]}],
        "required_environment_capabilities": ["egress:github.com"],
    },
]

_INCIDENT_TEMPLATE_AGENTS = [
    {
        "name": "Alert Ingestor",
        "description": "Ingests an alert from a monitoring system and normalises it.",
        "prompt_template": "Normalise the following alert into the standard incident format:\n\nAlert:\n{{ input }}",
        "connector_type_refs": [],
        "required_environment_capabilities": [],
    },
    {
        "name": "Severity Classifier",
        "description": "Classifies the incident severity based on the alert payload.",
        "prompt_template": (
            "Classify the following incident as CRITICAL, HIGH, MEDIUM, or LOW based on impact and urgency:"
            "\n\nIncident:\n{{ input }}"
        ),
        "connector_type_refs": [],
        "required_environment_capabilities": [],
    },
    {
        "name": "Runbook Matcher",
        "description": "Matches the incident to the most relevant runbook based on patterns.",
        "prompt_template": (
            "Match the following incident to the appropriate runbook based on the alert type and service:"
            "\n\nIncident:\n{{ input }}"
        ),
        "connector_type_refs": [],
        "required_environment_capabilities": [],
    },
    {
        "name": "Remediation Agent",
        "description": "Executes the remediation steps from the matched runbook.",
        "prompt_template": "Execute the following remediation steps and report results:\n\n{{ input }}",
        "connector_type_refs": [],
        "required_environment_capabilities": [],
    },
    {
        "name": "Postmortem Generator",
        "description": "Generates a postmortem document after the incident is resolved.",
        "prompt_template": "Generate a postmortem document for the following incident:\n\nIncident:\n{{ input }}",
        "connector_type_refs": [],
        "required_environment_capabilities": [],
    },
]

_PR_TEMPLATE_NODES = [
    {
        "id": "issue-reader",
        "node_type": "agent",
        "agent_index": 0,
        "label": "Issue Reader",
        "position": {"x": 50, "y": 100},
    },
    {
        "id": "code-diff-analyzer",
        "node_type": "agent",
        "agent_index": 1,
        "label": "Code Diff Analyzer",
        "position": {"x": 350, "y": 100},
    },
    {
        "id": "comment-generator",
        "node_type": "agent",
        "agent_index": 2,
        "label": "Comment Generator",
        "position": {"x": 650, "y": 100},
    },
    {
        "id": "hitl-gate",
        "node_type": "manual",
        "label": "Review Gate",
        "position": {"x": 950, "y": 100},
    },
    {
        "id": "pr-poster",
        "node_type": "agent",
        "agent_index": 3,
        "label": "PR Poster",
        "position": {"x": 1250, "y": 100},
    },
]
_PR_TEMPLATE_EDGES = [
    {
        "source_node_id": "issue-reader",
        "target_node_id": "code-diff-analyzer",
        "edge_type": "normal",
    },
    {
        "source_node_id": "code-diff-analyzer",
        "target_node_id": "comment-generator",
        "edge_type": "normal",
    },
    {"source_node_id": "comment-generator", "target_node_id": "hitl-gate", "edge_type": "normal"},
    {
        "source_node_id": "hitl-gate",
        "target_node_id": "pr-poster",
        "edge_type": "normal",
        "hitl_gate_config": {
            "label": "Approve Review",
            "description": "Review the generated comments before posting to the PR.",
            "claim_expiry_minutes": 60,
            "human_only": False,
        },
    },
]

_RELEASE_TEMPLATE_NODES = [
    {
        "id": "version-bumper",
        "node_type": "agent",
        "agent_index": 0,
        "label": "Version Bumper",
        "position": {"x": 50, "y": 100},
    },
    {
        "id": "changelog-generator",
        "node_type": "agent",
        "agent_index": 1,
        "label": "Changelog Generator",
        "position": {"x": 350, "y": 100},
    },
    {
        "id": "release-notes-writer",
        "node_type": "agent",
        "agent_index": 2,
        "label": "Release Notes Writer",
        "position": {"x": 650, "y": 100},
    },
    {
        "id": "hitl-gate",
        "node_type": "manual",
        "label": "Release Gate",
        "position": {"x": 950, "y": 100},
    },
    {
        "id": "tag-creator",
        "node_type": "agent",
        "agent_index": 3,
        "label": "Tag Creator",
        "position": {"x": 1250, "y": 100},
    },
]
_RELEASE_TEMPLATE_EDGES = [
    {
        "source_node_id": "version-bumper",
        "target_node_id": "changelog-generator",
        "edge_type": "normal",
    },
    {
        "source_node_id": "changelog-generator",
        "target_node_id": "release-notes-writer",
        "edge_type": "normal",
    },
    {
        "source_node_id": "release-notes-writer",
        "target_node_id": "hitl-gate",
        "edge_type": "normal",
    },
    {
        "source_node_id": "hitl-gate",
        "target_node_id": "tag-creator",
        "edge_type": "normal",
        "hitl_gate_config": {
            "label": "Approve Release",
            "description": "Review the release notes before tagging the release.",
            "claim_expiry_minutes": 60,
            "human_only": False,
        },
    },
]

_INCIDENT_TEMPLATE_NODES = [
    {
        "id": "alert-ingestor",
        "node_type": "agent",
        "agent_index": 0,
        "label": "Alert Ingestor",
        "position": {"x": 50, "y": 100},
    },
    {
        "id": "severity-classifier",
        "node_type": "agent",
        "agent_index": 1,
        "label": "Severity Classifier",
        "position": {"x": 350, "y": 100},
    },
    {
        "id": "runbook-matcher",
        "node_type": "agent",
        "agent_index": 2,
        "label": "Runbook Matcher",
        "position": {"x": 650, "y": 100},
    },
    {
        "id": "remediation-agent",
        "node_type": "agent",
        "agent_index": 3,
        "label": "Remediation Agent",
        "position": {"x": 950, "y": 100},
    },
    {
        "id": "hitl-gate",
        "node_type": "manual",
        "label": "Verification Gate",
        "position": {"x": 1250, "y": 100},
    },
    {
        "id": "postmortem-generator",
        "node_type": "agent",
        "agent_index": 4,
        "label": "Postmortem Generator",
        "position": {"x": 1550, "y": 100},
    },
]
_INCIDENT_TEMPLATE_EDGES = [
    {
        "source_node_id": "alert-ingestor",
        "target_node_id": "severity-classifier",
        "edge_type": "normal",
    },
    {
        "source_node_id": "severity-classifier",
        "target_node_id": "runbook-matcher",
        "edge_type": "normal",
    },
    {
        "source_node_id": "runbook-matcher",
        "target_node_id": "remediation-agent",
        "edge_type": "normal",
    },
    {
        "source_node_id": "remediation-agent",
        "target_node_id": "hitl-gate",
        "edge_type": "normal",
    },
    {
        "source_node_id": "hitl-gate",
        "target_node_id": "postmortem-generator",
        "edge_type": "normal",
        "hitl_gate_config": {
            "label": "Verify Resolution",
            "description": "Confirm the incident is resolved before generating the postmortem.",
            "claim_expiry_minutes": 60,
            "human_only": False,
        },
    },
]

_MODULO_PRIMITIVES.extend(
    [
        _make_modulo(
            pid="00000000-0000-0000-0000-000000000080",
            primitive_type="pipeline_template",
            name="PR Review Pipeline",
            slug="pr-review-pipeline",
            description=(
                "Automated PR review pipeline: reads a GitHub issue, analyses the code diff,"
                " generates review comments with a HITL gate, and posts to the PR."
            ),
            content_json={
                "agents": _PR_TEMPLATE_AGENTS,
                "graph_nodes": _PR_TEMPLATE_NODES,
                "edges": _PR_TEMPLATE_EDGES,
                "connector_type_refs": ["github"],
                "schema_refs": [],
                "category": "code-review",
            },
            tags=["pipeline_template", "code-review", "pr", "github"],
        ),
        _make_modulo(
            pid="00000000-0000-0000-0000-000000000081",
            primitive_type="pipeline_template",
            name="Release Checklist Pipeline",
            slug="release-checklist-pipeline",
            description=(
                "Automated release pipeline: bumps the version, generates a changelog,"
                " formats release notes with a HITL gate, and creates a Git tag."
            ),
            content_json={
                "agents": _RELEASE_TEMPLATE_AGENTS,
                "graph_nodes": _RELEASE_TEMPLATE_NODES,
                "edges": _RELEASE_TEMPLATE_EDGES,
                "connector_type_refs": ["github"],
                "schema_refs": [],
                "category": "release",
            },
            tags=["pipeline_template", "release", "changelog", "github"],
        ),
        _make_modulo(
            pid="00000000-0000-0000-0000-000000000082",
            primitive_type="pipeline_template",
            name="Incident Response Pipeline",
            slug="incident-response-pipeline",
            description=(
                "Automated incident response pipeline: ingests alerts, classifies severity,"
                " matches runbooks, applies remediation with a HITL gate, and generates a postmortem."
            ),
            content_json={
                "agents": _INCIDENT_TEMPLATE_AGENTS,
                "graph_nodes": _INCIDENT_TEMPLATE_NODES,
                "edges": _INCIDENT_TEMPLATE_EDGES,
                "connector_type_refs": [],
                "schema_refs": [],
                "category": "incident-response",
            },
            tags=["pipeline_template", "incident-response", "alerting", "runbook"],
        ),
    ]
)

# Indexes for O(1) community lookup
_MODULO_BY_ID: dict[uuid.UUID, LibraryPrimitive] = {p.id: p for p in _MODULO_PRIMITIVES}
_MODULO_BY_SLUG: dict[tuple[str, str], LibraryPrimitive] = {
    (p.primitive_type, p.slug): p for p in _MODULO_PRIMITIVES
}

# Fixture contribution flow
# ---------------------------------------------------------------------------


async def contribute_fixture(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    created_by: uuid.UUID,
    name: str,
    slug: str,
    description: str | None,
    tags: list[str],
    fixture_map: dict[str, str],
    source_run_id: uuid.UUID | None = None,
    source_pipeline_id: uuid.UUID | None = None,
    owner_team_id: uuid.UUID | None = None,
) -> LibraryPrimitive:
    """Create a draft fixture contribution in the org's library.

    The fixture is stored as a test_fixture primitive with contribution_status='draft'.
    It is visible only to the submitting org until published to the community library.
    """
    from modulo.db.crud.library_primitive import create_library_primitive as _create

    content: dict[str, Any] = {
        "fixture_map": fixture_map,
        "source_run_id": str(source_run_id) if source_run_id else None,
        "source_pipeline_id": str(source_pipeline_id) if source_pipeline_id else None,
    }

    async with session.begin():
        await set_rls_org(session, org_id)
        prim = await _create(
            session,
            org_id=org_id,
            source="local",
            primitive_type="test_fixture",
            name=name,
            slug=slug,
            description=description,
            author=created_by.hex,
            version="1.0",
            tags=tags,
            content_json=content,
            source_url=None,
            forked_from=None,
            checksum=None,
            ed25519_signature=None,
            verified=None,
            download_count=None,
            average_rating=None,
            review_count=None,
            owner_team_id=owner_team_id,
            visibility="org",
            account_id=created_by,
        )
        update = await update_library_primitive(
            session,
            prim.id,
            {"contribution_status": CONTRIBUTION_DRAFT},
        )
    if update is None:
        raise ContributionNotFoundError(f"Contribution {prim.id} not found after creation")
    return update


async def submit_contribution_for_review(
    session: AsyncSession,
    org_id: uuid.UUID,
    primitive_id: uuid.UUID,
    *,
    created_by: uuid.UUID,
) -> LibraryPrimitive:
    """Move a draft fixture contribution to the review queue.

    Raises ContributionNotFoundError if the primitive does not exist.
    Raises ContributionInvalidTransitionError if the primitive is not in draft status.
    """
    async with session.begin():
        await set_rls_org(session, org_id)
        prim = await get_library_primitive(session, primitive_id)

    if prim is None:
        raise ContributionNotFoundError(f"Contribution {primitive_id} not found")

    if prim.contribution_status != CONTRIBUTION_DRAFT:
        raise ContributionInvalidTransitionError(
            f"Cannot submit contribution {primitive_id} for review: "
            f"expected status '{CONTRIBUTION_DRAFT}', got '{prim.contribution_status}'"
        )

    async with session.begin():
        await set_rls_org(session, org_id)
        updated = await update_library_primitive(
            session,
            primitive_id,
            {"contribution_status": CONTRIBUTION_REVIEW_QUEUE},
        )
    if updated is None:
        raise ContributionNotFoundError(f"Contribution {primitive_id} not found")
    return updated


async def publish_contribution(
    session: AsyncSession,
    org_id: uuid.UUID,
    primitive_id: uuid.UUID,
    *,
    approved_by: uuid.UUID,
) -> LibraryPrimitive:
    """Publish a reviewed fixture contribution to the community library.

    Changes visibility to 'community' and sets contribution_status to 'published'.
    The primitive is reassigned to the community sentinel org so it appears
    for all users.
    """
    async with session.begin():
        await set_rls_org(session, org_id)
        prim = await get_library_primitive(session, primitive_id)

    if prim is None:
        raise ContributionNotFoundError(f"Contribution {primitive_id} not found")

    if prim.contribution_status != CONTRIBUTION_REVIEW_QUEUE:
        raise ContributionInvalidTransitionError(
            f"Cannot publish contribution {primitive_id}: "
            f"expected status '{CONTRIBUTION_REVIEW_QUEUE}', got '{prim.contribution_status}'"
        )

    async with session.begin():
        await set_rls_org(session, org_id)
        updated = await update_library_primitive(
            session,
            primitive_id,
            {
                "contribution_status": CONTRIBUTION_PUBLISHED,
                "visibility": "community",
                "organisation_id": MODULO_ORG_ID,
            },
        )
    if updated is None:
        raise ContributionNotFoundError(f"Contribution {primitive_id} not found")
    await notify_importers_of_update(session, org_id, primitive_id)
    return updated


async def list_contributions(
    session: AsyncSession,
    org_id: uuid.UUID,
    *,
    contribution_status: str | None = None,
    page: int = 1,
    page_size: int = 20,
) -> PageResult[LibraryPrimitive]:
    """List fixture contributions scoped to the org."""
    async with session.begin():
        await set_rls_org(session, org_id)
        result = await list_library_primitives(
            session,
            page=page,
            page_size=page_size,
            primitive_type="test_fixture",
        )
    if contribution_status is not None:
        result.items = [p for p in result.items if p.contribution_status == contribution_status]
        result.total = len(result.items)
    return result


# ---------------------------------------------------------------------------
# Contribution versioning
# ---------------------------------------------------------------------------


async def submit_contribution_version(
    session: AsyncSession,
    org_id: uuid.UUID,
    primitive_id: uuid.UUID,
    *,
    created_by: uuid.UUID,
    name: str,
    slug: str,
    description: str | None,
    tags: list[str],
    fixture_map: dict[str, str],
    source_run_id: uuid.UUID | None = None,
    source_pipeline_id: uuid.UUID | None = None,
    owner_team_id: uuid.UUID | None = None,
) -> LibraryPrimitive:
    """Submit a new version of an existing published fixture contribution.

    Auto-increments the minor version and creates a new draft row linked
    via version_group_id.  The new version must go through
    review_queue -> published independently.
    """
    from modulo.db.crud.library_primitive import create_library_primitive as _create

    async with session.begin():
        await set_rls_org(session, org_id)
        existing = await get_library_primitive(session, primitive_id)

    if existing is None:
        raise ContributionNotFoundError(f"Contribution {primitive_id} not found")

    if existing.contribution_status != CONTRIBUTION_PUBLISHED:
        raise ContributionInvalidTransitionError(
            f"Cannot version contribution {primitive_id}: "
            f"expected status '{CONTRIBUTION_PUBLISHED}', got '{existing.contribution_status}'"
        )

    # Auto-increment the minor version
    parts = existing.version.split(".")
    try:
        parts[-1] = str(int(parts[-1]) + 1)
    except (ValueError, IndexError):
        parts = ["1", "0"]
    new_version = ".".join(parts)

    # Establish a version group if this is the first versioned submission
    group_id = existing.version_group_id or existing.id

    content: dict[str, Any] = {
        "fixture_map": fixture_map,
        "source_run_id": str(source_run_id) if source_run_id else None,
        "source_pipeline_id": str(source_pipeline_id) if source_pipeline_id else None,
    }

    async with session.begin():
        await set_rls_org(session, org_id)
        # Seed version_group_id on the original if it was created before
        # this feature existed
        if existing.version_group_id is None:
            await update_library_primitive(
                session,
                primitive_id,
                {"version_group_id": group_id},
            )
        prim = await _create(
            session,
            org_id=org_id,
            source="local",
            primitive_type="test_fixture",
            name=name,
            slug=slug,
            description=description,
            author=created_by.hex,
            version=new_version,
            tags=tags,
            content_json=content,
            source_url=None,
            forked_from=primitive_id,
            checksum=None,
            ed25519_signature=None,
            verified=None,
            download_count=None,
            average_rating=None,
            review_count=None,
            owner_team_id=owner_team_id,
            visibility="org",
            account_id=created_by,
        )
        update = await update_library_primitive(
            session,
            prim.id,
            {
                "contribution_status": CONTRIBUTION_DRAFT,
                "version_group_id": group_id,
            },
        )
    if update is None:
        raise ContributionNotFoundError(f"Contribution version {prim.id} not found after creation")
    return update


async def list_contribution_versions(
    session: AsyncSession,
    org_id: uuid.UUID,
    primitive_id: uuid.UUID,
) -> list[LibraryPrimitive]:
    """Return all versions for a contribution primitive, newest first."""
    async with session.begin():
        await set_rls_org(session, org_id)
        prim = await get_library_primitive(session, primitive_id)

    if prim is None:
        raise ContributionNotFoundError(f"Contribution {primitive_id} not found")

    group_id = prim.version_group_id or prim.id

    async with session.begin():
        await set_rls_org(session, org_id)
        results = await list_primitives_by_version_group(session, group_id)

    # If the target primitive has no version_group_id yet, return just itself
    if prim.version_group_id is None:
        return [prim]

    # Include the seed primitive (the one whose version_group_id was set to
    # its own id) — it won't appear in the version-group query because it
    # may not yet have the version_group_id set if it predates the feature.
    if not any(r.id == prim.id for r in results):
        results.append(prim)

    return sorted(results, key=lambda p: p.version, reverse=True)


async def notify_importers_of_update(
    session: AsyncSession,
    org_id: uuid.UUID,
    primitive_id: uuid.UUID,
) -> None:
    """Mark library entries that forked from this primitive as having an update.

    Finds all primitives that were copied (``forked_from``) from any version
    in the same version group and sets their ``update_available_version_id``
    to the newly published version.
    """
    async with session.begin():
        await set_rls_org(session, org_id)
        prim = await get_library_primitive(session, primitive_id)

    if prim is None:
        return

    group_id = prim.version_group_id
    if group_id is None:
        return

    # Find all primitives forked from any version in this group
    stmt = select(LibraryPrimitive).where(
        LibraryPrimitive.forked_from.in_(
            select(LibraryPrimitive.id).where(LibraryPrimitive.version_group_id == group_id)
        )
    )
    result = await session.execute(stmt)
    fork_copies = list(result.scalars())

    for copy in fork_copies:
        if not copy.auto_update:
            continue
        await update_library_primitive(
            session,
            copy.id,
            {"update_available_version_id": prim.id},
        )
