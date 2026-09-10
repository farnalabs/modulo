"""Seed data for the library service — built-in Modulo and community primitives.

Pure data plus the tiny builders used to construct in-memory ``LibraryPrimitive``
objects. Kept separate from ``__init__`` so the service module holds only runtime
logic (cohesion); ``__init__`` re-imports the data names so existing import paths
and module-level cache mutations keep working unchanged.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from modulo.db.models.library_primitive import LibraryPrimitive

# Fixed sentinel used as organisation_id for modulo (built-in) primitives.
MODULO_ORG_ID: uuid.UUID = uuid.UUID("00000000-0000-0000-0000-000000000001")


_EPOCH = datetime(2024, 1, 1, tzinfo=UTC)


# Repeated seed/label values (S1192). Pure aliases — the strings are part of
# the seeded primitive content and the log-name contract.
_SYSTEM_PROMPT_LABEL = "System Prompt"
_EGRESS_GITHUB = "egress:github.com"


# ---------------------------------------------------------------------------
# Contribution status constants
# ---------------------------------------------------------------------------

CONTRIBUTION_DRAFT = "draft"
CONTRIBUTION_REVIEW_QUEUE = "review_queue"
CONTRIBUTION_PUBLISHED = "published"


def _build_builtin(
    pid: str,
    primitive_type: str,
    name: str,
    slug: str,
    description: str,
    content_json: dict[str, Any],
    tags: list[str],
    *,
    source: str,
    author: str,
    verified: bool | None,
    contribution_status: str | None,
) -> LibraryPrimitive:
    """Construct an in-memory built-in primitive (modulo or community source).

    ``source="modulo"`` primitives are the Native library; ``source="community"``
    are opinionated example pipelines (ADR 010 §2) shown as distinct, clearly
    labelled items. Both live in the module/community sentinel org so they are
    visible to every organisation, and both are ``tier="native"``.
    """
    p = LibraryPrimitive(
        id=uuid.UUID(pid),
        organisation_id=MODULO_ORG_ID,
        source=source,
        primitive_type=primitive_type,
        name=name,
        slug=slug,
        description=description,
        author=author,
        version="1.0",
        tags=tags,
        content_json=content_json,
        source_url=None,
        forked_from=None,
        checksum=None,
        ed25519_signature=None,
        verified=verified,
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
    p.contribution_status = contribution_status
    p.tier = "native"
    return p


def _make_community_db_item(
    pid: str,
    primitive_type: str,
    name: str,
    slug: str,
    description: str,
    content_json: dict[str, Any],
    tags: list[str],
) -> LibraryPrimitive:
    """Build an in-memory "community database" example pipeline (ADR 010 §2).

    Community-database items use ``source="community"`` (distinct from
    ``source="modulo"`` used by the Native library) and are always
    ``verified=False`` so the UI can render a clear "not verified by Modulo"
    indicator.
    """
    return _build_builtin(
        pid,
        primitive_type,
        name,
        slug,
        description,
        content_json,
        tags,
        source="community",
        author="community",
        verified=False,
        contribution_status=None,
    )


def _make_modulo(
    pid: str,
    primitive_type: str,
    name: str,
    slug: str,
    description: str,
    content_json: dict[str, Any],
    tags: list[str],
) -> LibraryPrimitive:
    return _build_builtin(
        pid,
        primitive_type,
        name,
        slug,
        description,
        content_json,
        tags,
        source="modulo",
        author="modulo",
        verified=None,
        contribution_status=CONTRIBUTION_PUBLISHED if primitive_type == "test_fixture" else None,
    )


# ---------------------------------------------------------------------------
# Built-in Modulo primitives (in-memory, no DB row required)
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
        pid="00000000-0000-0000-0000-000000000012",
        primitive_type="schema",
        name="PR Review Decision",
        slug="pr-review-decision",
        description=(
            "Structured verdict for automated PR reviews: APPROVE or"
            " REQUEST_CHANGES plus summary and per-finding details."
        ),
        content_json={
            "fields": [
                {
                    "name": "decision",
                    "type": "string",
                    "enum": ["APPROVE", "REQUEST_CHANGES"],
                    "required": True,
                },
                {"name": "summary", "type": "string", "required": True},
                {
                    "name": "findings",
                    "type": "array",
                    "items": {
                        "severity": {"type": "string", "enum": ["critical", "major", "minor", "nit"]},
                        "file": {"type": "string"},
                        "line": {"type": "integer"},
                        "comment": {"type": "string"},
                    },
                    "required": False,
                },
            ]
        },
        tags=["schema", "code-review", "pr", "decision"],
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
    # Simplest Workflow primitives (agent + workflow)
    # -----------------------------------------------------------------------
    _make_modulo(
        pid="00000000-0000-0000-0000-000000000065",
        primitive_type="agent",
        name="Spec Implementer",
        slug="spec-implementer",
        description=(
            "Reads a freeform specification (markdown), implements the required "
            "changes in a target code path, runs a test command to validate, "
            "and optionally git-commits the result."
        ),
        content_json={
            "input_schema": None,
            "output_schema": None,
            "prompt_template": (
                "You are an AI software engineer implementing a specification.\n\n"
                "SPECIFICATION:\n{{ input }}\n\n"
                "Read the specification above, understand the codebase at the code path, "
                "implement the required changes, run the test command to validate the changes, "
                "and fix any test failures. "
                "If auto-commit is enabled and the project is a git repository, stage all "
                "changes and commit them with a descriptive message.\n\n"
                "Report what files were changed, the test results (pass/fail counts, "
                "any error messages), and whether a commit was made."
            ),
            "connector_type_refs": [],
            "required_environment_capabilities": [],
            "model_backend_id": None,
            "retry_policy": {},
            "token_budget": None,  # nosec B105 — None sentinel for "no budget", not a credential
        },
        tags=["agent", "spec-implementer", "simplest-workflow", "day-1"],
    ),
    _make_modulo(
        pid="00000000-0000-0000-0000-000000000071",
        primitive_type="workflow",
        name="Simplest Workflow",
        slug="simplest-workflow",
        description=(
            "The absolute simplest Modulo pipeline — a single agent that reads a "
            "freeform specification (markdown), implements the required changes in "
            "a target code path, runs a test command to validate, and optionally "
            "git-commits the result. No connectors, no cloud services, no setup "
            "beyond a model backend. Perfect for day-1 evaluation."
        ),
        content_json={
            "nodes": [
                {"id": "spec-implementer", "agent": "spec-implementer"},
            ],
            "edges": [],
            "entry": "spec-implementer",
        },
        tags=["workflow", "simplest-workflow", "day-1", "getting-started"],
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
                {
                    "name": "system_prompt",
                    "label": _SYSTEM_PROMPT_LABEL,
                    "type": "string",
                    "required": True,
                    "description": "Instructions for what to approve/reject",
                    "default_value": (
                        "You are an approver. Respond with APPROVED or REJECTED"
                        " as the first word, followed by your reasoning."
                    ),
                    "target_injection": {
                        "mode": "prompt_replace",
                        "node_id": "decision-agent",
                        "injection_point": "prompt_template",
                    },
                },
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
                "eval_definitions": [
                    {
                        "name": "first_word_approved_rejected",
                        "type": "regex",
                        "config": {"pattern": "^(APPROVED|REJECTED)\\b", "field": "result"},
                        "failure_behaviour": "retry",
                    }
                ],
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
                {
                    "name": "system_prompt",
                    "label": _SYSTEM_PROMPT_LABEL,
                    "type": "string",
                    "required": True,
                    "description": "Instructions for what to evaluate as true or false",
                    "default_value": (
                        "You are a boolean evaluator. Respond with TRUE"
                        " or FALSE as the first word, followed by your reasoning."
                    ),
                    "target_injection": {
                        "mode": "prompt_replace",
                        "node_id": "decision-agent",
                        "injection_point": "prompt_template",
                    },
                },
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
                "eval_definitions": [
                    {
                        "name": "first_word_true_false",
                        "type": "regex",
                        "config": {"pattern": "^(TRUE|FALSE)\\b", "field": "result"},
                        "failure_behaviour": "retry",
                    }
                ],
                "max_validation_retries": 2,
            },
        },
        tags=["composite", "boolean", "decision", "validation"],
    ),
    _make_modulo(
        pid="00000000-0000-0000-0000-000000000092",
        primitive_type="composite",
        name="Devil\u2019s Advocate",
        slug="devils-advocate",
        description=(
            "Takes a position, argues for it, then argues against it. Synthesises both sides"
            " into balanced advice. Use when you need rigorous critique of a plan."
        ),
        content_json={
            "parameter_ports": [
                {
                    "name": "position",
                    "label": "Position to Challenge",
                    "type": "string",
                    "required": True,
                    "description": "The plan, argument, or decision to scrutinise",
                    "default_value": "We should migrate our monolith to microservices.",
                    "target_injection": {
                        "mode": "prompt_replace",
                        "node_id": "advocate-for",
                        "injection_point": "prompt_template",
                    },
                },
                {
                    "name": "advocate_prompt",
                    "label": "Advocate Instructions",
                    "type": "string",
                    "required": False,
                    "description": "Prompt shaping how the pro side argues",
                    "default_value": (
                        "You are an advocate. Argue strongly in favour of this position: {{parameter.position}}"
                    ),
                    "target_injection": {
                        "mode": "prompt_replace",
                        "node_id": "advocate-for",
                        "injection_point": "prompt_template",
                    },
                },
                {
                    "name": "critic_prompt",
                    "label": "Critic Instructions",
                    "type": "string",
                    "required": False,
                    "description": "Prompt shaping how the con side argues",
                    "default_value": "You are a critic. Argue strongly against this position: {{parameter.position}}",
                    "target_injection": {
                        "mode": "prompt_replace",
                        "node_id": "advocate-against",
                        "injection_point": "prompt_template",
                    },
                },
                {
                    "name": "mediator_prompt",
                    "label": "Mediator Instructions",
                    "type": "string",
                    "required": False,
                    "description": "Prompt shaping how the mediator synthesises",
                    "default_value": (
                        "You are a mediator. Below are two arguments about: {{parameter.position}}"
                        "\n\n--- PRO ---\n{{nodes.advocate-for.output}}"
                        "\n\n--- CON ---\n{{nodes.advocate-against.output}}"
                        "\n\nSynthesise both sides into balanced, actionable advice."
                    ),
                    "target_injection": {
                        "mode": "prompt_replace",
                        "node_id": "mediator",
                        "injection_point": "prompt_template",
                    },
                },
            ],
            "sub_pipeline_graph_json": {
                "nodes": [
                    {"id": "advocate-for", "node_type": "agent", "label": "Advocate For"},
                    {"id": "advocate-against", "node_type": "agent", "label": "Advocate Against"},
                    {"id": "mediator", "node_type": "agent", "label": "Mediator"},
                ],
                "edges": [
                    {"source": "advocate-for", "target": "mediator", "edge_type": "normal"},
                    {"source": "advocate-against", "target": "mediator", "edge_type": "normal"},
                ],
            },
            "input_schema_id": None,
            "output_schema": {
                "type": "object",
                "properties": {
                    "synthesis": {"type": "string"},
                    "pro_arguments": {"type": "string"},
                    "con_arguments": {"type": "string"},
                },
                "required": ["synthesis"],
            },
        },
        tags=["composite", "devils-advocate", "critique", "decision", "strategy"],
    ),
    _make_modulo(
        pid="00000000-0000-0000-0000-000000000093",
        primitive_type="composite",
        name="Triage",
        slug="triage",
        description="Classifies into BUG, FEATURE, INFRA, DOCS. First word is forced to one of the four.",
        content_json={
            "parameter_ports": [
                {
                    "name": "system_prompt",
                    "label": _SYSTEM_PROMPT_LABEL,
                    "type": "string",
                    "required": True,
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
                    },
                },
            ],
            "sub_pipeline_graph_json": {
                "nodes": [{"id": "classifier-agent", "node_type": "agent", "label": "Classifier Agent"}],
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
                "eval_definitions": [
                    {
                        "name": "first_word_category",
                        "type": "regex",
                        "config": {"pattern": "^(BUG|FEATURE|INFRA|DOCS)\\b", "field": "result"},
                        "failure_behaviour": "retry",
                    }
                ],
                "max_validation_retries": 2,
            },
        },
        tags=["composite", "triage", "classification", "bug", "feature"],
    ),
    _make_modulo(
        pid="00000000-0000-0000-0000-000000000094",
        primitive_type="composite",
        name="LLM Council",
        slug="llm-council",
        description=(
            "Runs N parallel LLM calls with the same prompt, then a mediator synthesises their"
            " responses into a single output. Configure model count and backends."
        ),
        content_json={
            "parameter_ports": [
                {
                    "name": "council_prompt",
                    "label": "Council Prompt",
                    "type": "string",
                    "required": True,
                    "description": "The prompt each council member responds to",
                    "default_value": "Analyse the following and provide your best recommendation.",
                    "target_injection": {
                        "mode": "prompt_replace",
                        "node_id": "member-1",
                        "injection_point": "prompt_template",
                    },
                },
                {
                    "name": "member_count",
                    "label": "Number of Members",
                    "type": "number",
                    "required": True,
                    "description": "How many LLM calls to run in parallel (1-5)",
                    "default_value": 3,
                    "target_injection": {"mode": "run_context_key", "key": "council_member_count"},
                },
                {
                    "name": "mediator_instructions",
                    "label": "Mediator Instructions",
                    "type": "string",
                    "required": False,
                    "description": "How the mediator should combine responses",
                    "default_value": (
                        "Below are {{council_member_count}} responses from different AI council"
                        " members.\n\n{{nodes.council.output}}\n\nSynthesise them into a single"
                        " coherent recommendation, noting areas of agreement and disagreement."
                    ),
                    "target_injection": {
                        "mode": "prompt_replace",
                        "node_id": "council-mediator",
                        "injection_point": "prompt_template",
                    },
                },
            ],
            "sub_pipeline_graph_json": {
                "nodes": [
                    {"id": "member-1", "node_type": "agent", "label": "Council Member 1"},
                    {"id": "member-2", "node_type": "agent", "label": "Council Member 2"},
                    {"id": "member-3", "node_type": "agent", "label": "Council Member 3"},
                    {"id": "council-mediator", "node_type": "agent", "label": "Council Mediator"},
                ],
                "edges": [
                    {"source": "member-1", "target": "council-mediator", "edge_type": "normal"},
                    {"source": "member-2", "target": "council-mediator", "edge_type": "normal"},
                    {"source": "member-3", "target": "council-mediator", "edge_type": "normal"},
                ],
            },
            "input_schema_id": None,
            "output_schema": {
                "type": "object",
                "properties": {
                    "synthesis": {"type": "string"},
                    "agreement_points": {"type": "array", "items": {"type": "string"}},
                    "disagreement_points": {"type": "array", "items": {"type": "string"}},
                    "member_count": {"type": "integer"},
                },
                "required": ["synthesis"],
            },
        },
        tags=["composite", "llm-council", "ensemble", "consensus", "decision"],
    ),
    _make_modulo(
        pid="00000000-0000-0000-0000-000000000095",
        primitive_type="composite",
        name="Structured Output Enforcer",
        slug="structured-output-enforcer",
        description=(
            "Takes free-form text and restructures it according to a target JSON Schema."
            " Retries if the output doesn\u2019t conform."
            " Use when you need guaranteed structural consistency."
        ),
        content_json={
            "parameter_ports": [
                {
                    "name": "system_prompt",
                    "label": _SYSTEM_PROMPT_LABEL,
                    "type": "string",
                    "required": True,
                    "description": "Instructions describing how to structure the output",
                    "default_value": (
                        "Restructure the input text into the required JSON format."
                        " Ensure all required fields are present and correctly typed."
                    ),
                    "target_injection": {
                        "mode": "prompt_replace",
                        "node_id": "structurer",
                        "injection_point": "prompt_template",
                    },
                },
            ],
            "sub_pipeline_graph_json": {
                "nodes": [{"id": "structurer", "node_type": "agent", "label": "Structurer"}],
                "edges": [],
            },
            "input_schema_id": None,
            "output_schema": {
                "type": "object",
                "properties": {
                    "structured": {"type": "object"},
                    "original": {"type": "string"},
                },
                "required": ["structured"],
            },
            "output_validation": {
                "eval_definitions": [
                    {
                        "name": "valid_json_schema",
                        "type": "json_schema",
                        "config": {
                            "schema": {
                                "type": "object",
                                "properties": {
                                    "structured": {"type": "object"},
                                    "original": {"type": "string"},
                                },
                                "required": ["structured"],
                            },
                        },
                        "failure_behaviour": "retry",
                    }
                ],
                "max_validation_retries": 3,
            },
        },
        tags=["composite", "structuring", "json", "schema", "enforcer"],
    ),
    _make_modulo(
        pid="00000000-0000-0000-0000-000000000096",
        primitive_type="composite",
        name="Complexity Estimator",
        slug="complexity-estimator",
        description=(
            "Estimates work complexity as XS, S, M, L, or XL with structured reasoning."
            " Forces a valid size as the first word. Self-corrects on invalid output."
        ),
        content_json={
            "parameter_ports": [
                {
                    "name": "system_prompt",
                    "label": _SYSTEM_PROMPT_LABEL,
                    "type": "string",
                    "required": True,
                    "description": "Instructions describing what to estimate complexity for",
                    "default_value": (
                        "Analyse the following work item and estimate its complexity."
                        " Respond with exactly one of XS, S, M, L, or XL as the first word,"
                        " followed by your reasoning."
                    ),
                    "target_injection": {
                        "mode": "prompt_replace",
                        "node_id": "estimator",
                        "injection_point": "prompt_template",
                    },
                },
            ],
            "sub_pipeline_graph_json": {
                "nodes": [{"id": "estimator", "node_type": "agent", "label": "Estimator"}],
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
                "eval_definitions": [
                    {
                        "name": "valid_complexity_size",
                        "type": "regex",
                        "config": {"pattern": "^(XS|S|M|L|XL)\\b", "field": "result"},
                        "failure_behaviour": "retry",
                    }
                ],
                "max_validation_retries": 2,
            },
        },
        tags=["composite", "complexity", "estimation", "sizing", "planning"],
    ),
]


_PR_TEMPLATE_AGENTS = [
    {
        "name": "Issue Reader",
        "description": "Reads a GitHub issue via GitHubConnector and extracts structured requirements.",
        "prompt_template": (
            "Read the following GitHub issue and extract structured requirements for code review."
            "\n\nIssue:\n{{ input }}"
        ),
        "connector_type_refs": [{"connector_type": "github", "capabilities": ["issue_read"]}],
        "required_environment_capabilities": [_EGRESS_GITHUB],
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
        "required_environment_capabilities": [_EGRESS_GITHUB],
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
        "required_environment_capabilities": [_EGRESS_GITHUB],
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
        "required_environment_capabilities": [_EGRESS_GITHUB],
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
            "human_only": True,
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
            "human_only": True,
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
            "human_only": True,
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
        _make_modulo(
            pid="00000000-0000-0000-0000-000000000097",
            primitive_type="agent",
            name="PR Review Agent",
            slug="pr-review-agent",
            description=(
                "Reviews a GitHub PR diff and returns a structured"
                " APPROVE/REQUEST_CHANGES verdict following the PR Review"
                " Decision schema."
            ),
            content_json={
                "output_schema": "pr-review-decision",
                "prompt_template": (
                    "You are a senior code reviewer. Review the following"
                    " GitHub PR diff for bugs, security issues, style"
                    " violations, and correctness problems.\n\n"
                    "For each issue found, provide:\n"
                    "- severity: critical, major, minor, or nit\n"
                    "- file: the file path\n"
                    "- line: the line number (approximate is fine)\n"
                    "- message: a clear description of the issue\n\n"
                    "Return a JSON verdict with:\n"
                    "- decision: APPROVE (no critical/major issues) or"
                    " REQUEST_CHANGES\n"
                    "- summary: a 1-3 sentence overall assessment\n"
                    "- findings: array of issues found (empty if APPROVE)\n\n"
                    "PR Diff:\n{{ input }}"
                ),
                "connector_type_refs": ["github"],
            },
            tags=["agent", "code-review", "pr", "github"],
        ),
        _make_modulo(
            pid="00000000-0000-0000-0000-000000000098",
            primitive_type="library_collection",
            name="GitHub PR Reviewer",
            slug="github-pr-reviewer",
            description=(
                "One-click AI PR review for GitHub repos. Installs a review"
                " agent, decision schema, and pipeline. Requires: model"
                " backend + GitHub token."
            ),
            content_json={
                "manifest_pins": [
                    {"slug": "pr-review-decision", "version": "1.0"},
                    {"slug": "pr-review-agent", "version": "1.0"},
                    {"slug": "pr-review-pipeline", "version": "1.0"},
                ],
                "connector_requirements": [
                    {
                        "connector_type_id": "github",
                        "description": ("GitHub token for reading PR diffs and posting review comments"),
                        "capabilities": ["code_review", "write"],
                    }
                ],
                "trust_header": {"source": "modulo", "verified": True},
            },
            tags=["library_collection", "code-review", "pr", "github", "quick-start"],
        ),
        # -------------------------------------------------------------------
        # FAR-787: Prompt-to-PR bundle (FAR-781)
        # -------------------------------------------------------------------
        _make_modulo(
            pid="00000000-0000-0000-0000-0000000000A0",
            primitive_type="agent",
            name="Prompt-to-PR Implement Agent",
            slug="prompt-to-pr-implement-agent",
            description=(
                "Reads a task description, explores the codebase, implements the"
                " change, runs tests, commits, pushes, and creates a pull request."
            ),
            content_json={
                "input_schema": None,
                "output_schema": None,
                "prompt_template": (
                    "You are an AI software engineer implementing a development task.\n\n"
                    "TASK:\n{{ input }}\n\n"
                    "Follow these steps:\n"
                    "1. Understand the task and explore the relevant codebase files.\n"
                    "2. Implement the required changes in the target code path.\n"
                    "3. Run the project's test command to validate the changes.\n"
                    "4. Fix any test failures before proceeding.\n"
                    "5. Stage all changes, commit with a descriptive Conventional Commits message.\n"
                    "6. Push the branch to the remote repository.\n"
                    "7. Create a pull request with a structured description (What/Why/How/Testing).\n\n"
                    "Report: files changed, test results (pass/fail), commit SHA, and PR URL."
                ),
                "connector_type_refs": [
                    {"connector_type": "github", "capabilities": ["read", "write", "code_review"]},
                ],
                "required_environment_capabilities": [_EGRESS_GITHUB],
                "model_backend_id": None,
                "retry_policy": {},
                "token_budget": None,
            },
            tags=["agent", "prompt-to-pr", "code-generation", "github", "sandbox"],
        ),
        _make_modulo(
            pid="00000000-0000-0000-0000-0000000000A1",
            primitive_type="pipeline_template",
            name="Prompt-to-PR Pipeline",
            slug="prompt-to-pr-pipeline",
            description=(
                "End-to-end pipeline: implement a task from a prompt, review the"
                " resulting PR, and create it automatically."
            ),
            content_json={
                "agents": [
                    {
                        "name": "Implement Agent",
                        "description": "Implements the task in a worktree branch and creates a PR.",
                        "prompt_template": (
                            "You are an AI software engineer implementing a development task.\n\n"
                            "TASK:\n{{ input }}\n\n"
                            "Follow these steps:\n"
                            "1. Understand the task and explore the relevant codebase files.\n"
                            "2. Implement the required changes in the target code path.\n"
                            "3. Run the project's test command to validate the changes.\n"
                            "4. Fix any test failures before proceeding.\n"
                            "5. Stage all changes, commit with a descriptive Conventional Commits message.\n"
                            "6. Push the branch to the remote repository.\n"
                            "7. Create a pull request with a structured description (What/Why/How/Testing).\n\n"
                            "Report: files changed, test results (pass/fail), commit SHA, and PR URL."
                        ),
                        "connector_type_refs": [
                            {"connector_type": "github", "capabilities": ["read", "write", "code_review"]},
                        ],
                        "required_environment_capabilities": [_EGRESS_GITHUB],
                    },
                    {
                        "name": "Review Agent",
                        "description": "Reviews the created PR and returns APPROVE or REQUEST_CHANGES.",
                        "prompt_template": (
                            "You are a senior code reviewer. Review the following"
                            " GitHub PR diff for bugs, security issues, style"
                            " violations, and correctness problems.\n\n"
                            "Return a JSON verdict with:\n"
                            "- decision: APPROVE or REQUEST_CHANGES\n"
                            "- summary: a 1-3 sentence overall assessment\n"
                            "- findings: array of issues found (empty if APPROVE)\n\n"
                            "PR Diff:\n{{ input }}"
                        ),
                        "connector_type_refs": ["github"],
                        "required_environment_capabilities": [_EGRESS_GITHUB],
                    },
                ],
                "graph_nodes": [
                    {
                        "id": "implement",
                        "node_type": "agent",
                        "agent_index": 0,
                        "label": "Implement",
                        "position": {"x": 50, "y": 100},
                    },
                    {
                        "id": "review",
                        "node_type": "agent",
                        "agent_index": 1,
                        "label": "Review",
                        "position": {"x": 350, "y": 100},
                    },
                ],
                "edges": [
                    {
                        "source_node_id": "implement",
                        "target_node_id": "review",
                        "edge_type": "normal",
                    },
                ],
                "connector_type_refs": ["github"],
                "schema_refs": ["pr-review-decision"],
                "category": "code-generation",
            },
            tags=["pipeline_template", "prompt-to-pr", "code-generation", "pr", "github"],
        ),
        _make_modulo(
            pid="00000000-0000-0000-0000-000000000099",
            primitive_type="library_collection",
            name="Prompt-to-PR",
            slug="prompt-to-pr",
            description=(
                "Turn a task description into a GitHub PR. Installs an"
                " implement agent, review agent, and pipeline. Requires:"
                " model backend + GitHub token."
            ),
            content_json={
                "manifest_pins": [
                    {"slug": "pr-review-decision", "version": "1.0"},
                    {"slug": "pr-review-agent", "version": "1.0"},
                    {"slug": "prompt-to-pr-implement-agent", "version": "1.0"},
                    {"slug": "prompt-to-pr-pipeline", "version": "1.0"},
                ],
                "connector_requirements": [
                    {
                        "connector_type_id": "github",
                        "description": "GitHub token for reading code, pushing branches, and creating PRs",
                        "capabilities": ["read", "write", "code_review"],
                    }
                ],
                "trust_header": {"source": "modulo", "verified": True},
            },
            tags=["library_collection", "code-generation", "pr", "github", "sandbox"],
        ),
        # -------------------------------------------------------------------
        # FAR-787: Changelog Generator bundle (FAR-782)
        # -------------------------------------------------------------------
        _make_modulo(
            pid="00000000-0000-0000-0000-0000000000A2",
            primitive_type="agent",
            name="Changelog Agent",
            slug="changelog-agent",
            description=(
                "Reads merged PRs since a given tag and generates a"
                " conventional-commits markdown changelog grouped by type."
            ),
            content_json={
                "input_schema": None,
                "output_schema": None,
                "prompt_template": (
                    "You are a release engineer. Given a list of merged PRs since"
                    " a given tag, generate a changelog entry in Conventional Commits"
                    " markdown format.\n\n"
                    "Group entries by type: feat, fix, chore, docs, refactor, test, perf.\n"
                    "Each entry should reference the PR number and title.\n"
                    "Include a '## Unreleased' header and a link to compare against"
                    " the previous tag.\n\n"
                    "Tag: {{ parameter.tag }}\nPRs:\n{{ input }}"
                ),
                "connector_type_refs": [
                    {"connector_type": "github", "capabilities": ["read"]},
                ],
                "required_environment_capabilities": [_EGRESS_GITHUB],
                "model_backend_id": None,
                "retry_policy": {},
                "token_budget": None,
            },
            tags=["agent", "changelog", "release", "github"],
        ),
        _make_modulo(
            pid="00000000-0000-0000-0000-0000000000A3",
            primitive_type="pipeline_template",
            name="Changelog Pipeline",
            slug="changelog-pipeline",
            description=("Generates a changelog from merged PRs with a HITL gate before publishing."),
            content_json={
                "agents": [
                    {
                        "name": "Changelog Agent",
                        "description": "Generates a conventional-commits changelog from merged PRs.",
                        "prompt_template": (
                            "You are a release engineer. Given a list of merged PRs since"
                            " a given tag, generate a changelog entry in Conventional Commits"
                            " markdown format.\n\n"
                            "Group entries by type: feat, fix, chore, docs, refactor, test, perf.\n"
                            "Each entry should reference the PR number and title.\n\n"
                            "Tag: {{ parameter.tag }}\nPRs:\n{{ input }}"
                        ),
                        "connector_type_refs": [
                            {"connector_type": "github", "capabilities": ["read"]},
                        ],
                        "required_environment_capabilities": [_EGRESS_GITHUB],
                    },
                ],
                "graph_nodes": [
                    {
                        "id": "changelog-agent",
                        "node_type": "agent",
                        "agent_index": 0,
                        "label": "Changelog Agent",
                        "position": {"x": 50, "y": 100},
                    },
                    {
                        "id": "hitl-gate",
                        "node_type": "manual",
                        "label": "Review Gate",
                        "position": {"x": 350, "y": 100},
                    },
                ],
                "edges": [
                    {
                        "source_node_id": "changelog-agent",
                        "target_node_id": "hitl-gate",
                        "edge_type": "normal",
                        "hitl_gate_config": {
                            "label": "Approve Changelog",
                            "description": "Review the generated changelog before publishing.",
                            "claim_expiry_minutes": 60,
                            "human_only": True,
                        },
                    },
                ],
                "connector_type_refs": ["github"],
                "schema_refs": [],
                "category": "release",
            },
            tags=["pipeline_template", "changelog", "release", "github"],
        ),
        _make_modulo(
            pid="00000000-0000-0000-0000-00000000009A",
            primitive_type="library_collection",
            name="Changelog Generator",
            slug="changelog-generator",
            description=(
                "Auto-generate a conventional-commits changelog from merged PRs."
                " Installs a changelog agent and pipeline. Requires: GitHub token."
            ),
            content_json={
                "manifest_pins": [
                    {"slug": "changelog-agent", "version": "1.0"},
                    {"slug": "changelog-pipeline", "version": "1.0"},
                ],
                "connector_requirements": [
                    {
                        "connector_type_id": "github",
                        "description": "GitHub token for reading merged PRs and creating releases",
                        "capabilities": ["read", "write"],
                    }
                ],
                "trust_header": {"source": "modulo", "verified": True},
            },
            tags=["library_collection", "changelog", "release", "github"],
        ),
        # -------------------------------------------------------------------
        # FAR-787: Release Notes bundle (FAR-783)
        # -------------------------------------------------------------------
        _make_modulo(
            pid="00000000-0000-0000-0000-0000000000AA",
            primitive_type="agent",
            name="Release Notes Agent",
            slug="release-notes-agent",
            description=(
                "Reads merged PRs and closed issues for a milestone and"
                " generates marketing-ready release notes with highlights,"
                " breaking changes, and categorized features/fixes."
            ),
            content_json={
                "input_schema": None,
                "output_schema": None,
                "prompt_template": (
                    "You are a technical writer. Given merged PRs and closed issues"
                    " for a release milestone, generate marketing-ready release notes.\n\n"
                    "Include sections: Highlights (top 3-5 features), Breaking Changes,"
                    " New Features, Bug Fixes, Improvements, and Deprecations.\n"
                    "Write for a developer audience — clear, concise, and actionable.\n\n"
                    "Milestone: {{ parameter.milestone }}\nPRs & Issues:\n{{ input }}"
                ),
                "connector_type_refs": [
                    {"connector_type": "github", "capabilities": ["read", "write"]},
                ],
                "required_environment_capabilities": [_EGRESS_GITHUB],
                "model_backend_id": None,
                "retry_policy": {},
                "token_budget": None,
            },
            tags=["agent", "release-notes", "github", "linear"],
        ),
        _make_modulo(
            pid="00000000-0000-0000-0000-00000000009F",
            primitive_type="pipeline_template",
            name="Release Notes Pipeline",
            slug="release-notes-pipeline",
            description=("Generates release notes from PRs/issues with a HITL gate before publishing."),
            content_json={
                "agents": [
                    {
                        "name": "Release Notes Agent",
                        "description": "Generates marketing-ready release notes from PRs and issues.",
                        "prompt_template": (
                            "You are a technical writer. Given merged PRs and closed issues"
                            " for a release milestone, generate marketing-ready release notes.\n\n"
                            "Include sections: Highlights, Breaking Changes, New Features,"
                            " Bug Fixes, Improvements, Deprecations.\n\n"
                            "Milestone: {{ parameter.milestone }}\nPRs & Issues:\n{{ input }}"
                        ),
                        "connector_type_refs": [
                            {"connector_type": "github", "capabilities": ["read", "write"]},
                        ],
                        "required_environment_capabilities": [_EGRESS_GITHUB],
                    },
                ],
                "graph_nodes": [
                    {
                        "id": "release-notes-agent",
                        "node_type": "agent",
                        "agent_index": 0,
                        "label": "Release Notes Agent",
                        "position": {"x": 50, "y": 100},
                    },
                    {
                        "id": "hitl-gate",
                        "node_type": "manual",
                        "label": "Review Gate",
                        "position": {"x": 350, "y": 100},
                    },
                ],
                "edges": [
                    {
                        "source_node_id": "release-notes-agent",
                        "target_node_id": "hitl-gate",
                        "edge_type": "normal",
                        "hitl_gate_config": {
                            "label": "Approve Release Notes",
                            "description": "Review the release notes before publishing.",
                            "claim_expiry_minutes": 60,
                            "human_only": True,
                        },
                    },
                ],
                "connector_type_refs": ["github"],
                "schema_refs": [],
                "category": "release",
            },
            tags=["pipeline_template", "release-notes", "github", "linear"],
        ),
        _make_modulo(
            pid="00000000-0000-0000-0000-00000000009B",
            primitive_type="library_collection",
            name="Release Notes Generator",
            slug="release-notes-generator",
            description=(
                "Generate marketing-ready release notes from merged PRs and"
                " closed issues. Installs a release-notes agent and pipeline."
                " Requires: GitHub or Linear token."
            ),
            content_json={
                "manifest_pins": [
                    {"slug": "release-notes-agent", "version": "1.0"},
                    {"slug": "release-notes-pipeline", "version": "1.0"},
                ],
                "connector_requirements": [
                    {
                        "connector_type_id": "github",
                        "description": "GitHub token for reading PRs/issues and writing release notes",
                        "capabilities": ["read", "write"],
                    }
                ],
                "trust_header": {"source": "modulo", "verified": True},
            },
            tags=["library_collection", "release-notes", "github", "linear"],
        ),
        # -------------------------------------------------------------------
        # FAR-787: PR Description Writer bundle (FAR-784)
        # -------------------------------------------------------------------
        _make_modulo(
            pid="00000000-0000-0000-0000-0000000000A6",
            primitive_type="agent",
            name="PR Description Agent",
            slug="pr-description-agent",
            description=(
                "Reads a branch diff and writes a structured PR description"
                " with What/Why/How/Testing/Related Issues sections."
            ),
            content_json={
                "input_schema": None,
                "output_schema": None,
                "prompt_template": (
                    "You are a technical writer. Given a branch diff, write a"
                    " structured pull request description.\n\n"
                    "Use these sections:\n"
                    "## What\nBrief summary of the changes.\n"
                    "## Why\nThe motivation or problem being solved.\n"
                    "## How\nImplementation approach and key decisions.\n"
                    "## Testing\nHow the changes were tested and what tests pass.\n"
                    "## Related Issues\nLinks to related issues (use #NNN format).\n\n"
                    "Diff:\n{{ input }}"
                ),
                "connector_type_refs": [
                    {"connector_type": "github", "capabilities": ["read", "write"]},
                ],
                "required_environment_capabilities": [_EGRESS_GITHUB],
                "model_backend_id": None,
                "retry_policy": {},
                "token_budget": None,
            },
            tags=["agent", "pr-description", "github"],
        ),
        _make_modulo(
            pid="00000000-0000-0000-0000-0000000000A7",
            primitive_type="pipeline_template",
            name="PR Description Pipeline",
            slug="pr-description-pipeline",
            description="Writes a structured PR description from a branch diff.",
            content_json={
                "agents": [
                    {
                        "name": "PR Description Agent",
                        "description": "Writes a structured PR description from a branch diff.",
                        "prompt_template": (
                            "You are a technical writer. Given a branch diff, write a"
                            " structured pull request description.\n\n"
                            "Use these sections: What, Why, How, Testing, Related Issues.\n\n"
                            "Diff:\n{{ input }}"
                        ),
                        "connector_type_refs": [
                            {"connector_type": "github", "capabilities": ["read", "write"]},
                        ],
                        "required_environment_capabilities": [_EGRESS_GITHUB],
                    },
                ],
                "graph_nodes": [
                    {
                        "id": "pr-description-agent",
                        "node_type": "agent",
                        "agent_index": 0,
                        "label": "PR Description Agent",
                        "position": {"x": 50, "y": 100},
                    },
                ],
                "edges": [],
                "connector_type_refs": ["github"],
                "schema_refs": [],
                "category": "pr-description",
            },
            tags=["pipeline_template", "pr-description", "github"],
        ),
        _make_modulo(
            pid="00000000-0000-0000-0000-00000000009C",
            primitive_type="library_collection",
            name="PR Description Writer",
            slug="pr-description-writer",
            description=(
                "Auto-generate structured PR descriptions (What/Why/How/Testing)"
                " from a branch diff. Installs a PR description agent and pipeline."
                " Requires: GitHub token."
            ),
            content_json={
                "manifest_pins": [
                    {"slug": "pr-description-agent", "version": "1.0"},
                    {"slug": "pr-description-pipeline", "version": "1.0"},
                ],
                "connector_requirements": [
                    {
                        "connector_type_id": "github",
                        "description": "GitHub token for reading diffs and writing PR descriptions",
                        "capabilities": ["read", "write"],
                    }
                ],
                "trust_header": {"source": "modulo", "verified": True},
            },
            tags=["library_collection", "pr-description", "github"],
        ),
        # -------------------------------------------------------------------
        # FAR-787: Issue Triage bundle (FAR-785)
        # -------------------------------------------------------------------
        _make_modulo(
            pid="00000000-0000-0000-0000-0000000000A8",
            primitive_type="agent",
            name="Issue Triage Agent",
            slug="issue-triage-agent",
            description=(
                "Reads a new issue and categorizes it (bug/feature/question/docs),"
                " suggests priority P0-P3, writes acceptance criteria, and suggests"
                " labels."
            ),
            content_json={
                "input_schema": None,
                "output_schema": None,
                "prompt_template": (
                    "You are an issue triage specialist. Read the following new issue"
                    " and produce a structured triage report.\n\n"
                    "For the issue, determine:\n"
                    "- category: bug, feature, question, or docs\n"
                    "- priority: P0 (critical/blocker), P1 (high), P2 (medium), P3 (low)\n"
                    "- acceptance_criteria: list of conditions that must be met to close this issue\n"
                    "- suggested_labels: list of labels to apply (e.g. bug, enhancement, help-wanted)\n"
                    "- summary: 1-2 sentence summary of the issue\n"
                    "- reasoning: brief explanation of your categorization and priority\n\n"
                    "Issue:\n{{ input }}"
                ),
                "connector_type_refs": [
                    {"connector_type": "github", "capabilities": ["read", "write"]},
                ],
                "required_environment_capabilities": [_EGRESS_GITHUB],
                "model_backend_id": None,
                "retry_policy": {},
                "token_budget": None,
            },
            tags=["agent", "triage", "issues", "github"],
        ),
        _make_modulo(
            pid="00000000-0000-0000-0000-0000000000A9",
            primitive_type="pipeline_template",
            name="Issue Triage Pipeline",
            slug="issue-triage-pipeline",
            description=(
                "Triages new issues with categorization, priority, and"
                " label suggestions, with a HITL gate before applying labels."
            ),
            content_json={
                "agents": [
                    {
                        "name": "Issue Triage Agent",
                        "description": "Categorizes issues and suggests priority, labels, and acceptance criteria.",
                        "prompt_template": (
                            "You are an issue triage specialist. Read the following new issue"
                            " and produce a structured triage report.\n\n"
                            "Determine: category (bug/feature/question/docs), priority (P0-P3),"
                            " acceptance criteria, suggested labels, summary, and reasoning.\n\n"
                            "Issue:\n{{ input }}"
                        ),
                        "connector_type_refs": [
                            {"connector_type": "github", "capabilities": ["read", "write"]},
                        ],
                        "required_environment_capabilities": [_EGRESS_GITHUB],
                    },
                ],
                "graph_nodes": [
                    {
                        "id": "issue-triage-agent",
                        "node_type": "agent",
                        "agent_index": 0,
                        "label": "Issue Triage Agent",
                        "position": {"x": 50, "y": 100},
                    },
                    {
                        "id": "hitl-gate",
                        "node_type": "manual",
                        "label": "Triage Review Gate",
                        "position": {"x": 350, "y": 100},
                    },
                ],
                "edges": [
                    {
                        "source_node_id": "issue-triage-agent",
                        "target_node_id": "hitl-gate",
                        "edge_type": "normal",
                        "hitl_gate_config": {
                            "label": "Approve Triage",
                            "description": "Review the triage report before applying labels to the issue.",
                            "claim_expiry_minutes": 60,
                            "human_only": True,
                        },
                    },
                ],
                "connector_type_refs": ["github"],
                "schema_refs": [],
                "category": "triage",
            },
            tags=["pipeline_template", "triage", "issues", "github"],
        ),
        _make_modulo(
            pid="00000000-0000-0000-0000-00000000009D",
            primitive_type="library_collection",
            name="Issue Triage",
            slug="issue-triage",
            description=(
                "Auto-triage new GitHub issues with category, priority,"
                " acceptance criteria, and label suggestions. Installs a"
                " triage agent and pipeline. Requires: GitHub token."
            ),
            content_json={
                "manifest_pins": [
                    {"slug": "issue-triage-agent", "version": "1.0"},
                    {"slug": "issue-triage-pipeline", "version": "1.0"},
                ],
                "connector_requirements": [
                    {
                        "connector_type_id": "github",
                        "description": "GitHub token for reading issues and applying labels",
                        "capabilities": ["read", "write"],
                    }
                ],
                "trust_header": {"source": "modulo", "verified": True},
            },
            tags=["library_collection", "triage", "issues", "github"],
        ),
        # -------------------------------------------------------------------
        # FAR-787: License Checker bundle (FAR-786)
        # -------------------------------------------------------------------
        _make_modulo(
            pid="00000000-0000-0000-0000-0000000000A4",
            primitive_type="agent",
            name="License Checker Agent",
            slug="license-checker-agent",
            description=(
                "Scans dependency manifests (build.gradle, package.json,"
                " requirements.txt, go.mod, Cargo.toml, pyproject.toml),"
                " resolves licenses via SPDX/API, classifies by restrictiveness,"
                " and generates a structured report with commercial-use flags."
            ),
            content_json={
                "input_schema": None,
                "output_schema": None,
                "prompt_template": (
                    "You are a software license compliance analyst. Scan the following"
                    " dependency manifest(s) and produce a structured license report.\n\n"
                    "For each dependency:\n"
                    "- name: package name\n"
                    "- version: resolved version\n"
                    "- license: SPDX license identifier\n"
                    "- restrictiveness: permissive, weak-copyleft, strong-copyleft,\n"
                    "  non-commercial, custom, or unknown\n"
                    "- commercial_use_ok: true/false/unknown\n"
                    "- notes: any special restrictions or concerns\n\n"
                    "At the end, provide:\n"
                    "- total_dependencies: count\n"
                    "- permissive_count / weak_copyleft_count / strong_copyleft_count\n"
                    "- non_commercial_count / unknown_count\n"
                    "- risk_level: low / medium / high (based on presence of strong-copyleft or non-commercial)\n"
                    "- summary: 2-3 sentence overall assessment\n\n"
                    "Dependency manifests:\n{{ input }}"
                ),
                "connector_type_refs": [
                    {"connector_type": "github", "capabilities": ["read"]},
                ],
                "required_environment_capabilities": [_EGRESS_GITHUB],
                "model_backend_id": None,
                "retry_policy": {},
                "token_budget": None,
            },
            tags=["agent", "license", "compliance", "dependencies"],
        ),
        _make_modulo(
            pid="00000000-0000-0000-0000-0000000000A5",
            primitive_type="pipeline_template",
            name="License Checker Pipeline",
            slug="license-checker-pipeline",
            description=(
                "Scans a repo's dependency manifests for license compliance"
                " with a HITL gate before finalizing the report."
            ),
            content_json={
                "agents": [
                    {
                        "name": "License Checker Agent",
                        "description": "Scans dependency manifests and produces a license compliance report.",
                        "prompt_template": (
                            "You are a software license compliance analyst. Scan the following"
                            " dependency manifests and produce a structured license report.\n\n"
                            "For each dependency, report: name, version, SPDX license, restrictiveness"
                            " (permissive/weak-copyleft/strong-copyleft/non-commercial/custom/unknown),"
                            " commercial_use_ok, and notes.\n\n"
                            "Provide a summary with counts per restrictiveness category and an overall risk level.\n\n"
                            "Dependency manifests:\n{{ input }}"
                        ),
                        "connector_type_refs": [
                            {"connector_type": "github", "capabilities": ["read"]},
                        ],
                        "required_environment_capabilities": [_EGRESS_GITHUB],
                    },
                ],
                "graph_nodes": [
                    {
                        "id": "license-checker-agent",
                        "node_type": "agent",
                        "agent_index": 0,
                        "label": "License Checker Agent",
                        "position": {"x": 50, "y": 100},
                    },
                    {
                        "id": "hitl-gate",
                        "node_type": "manual",
                        "label": "Compliance Review Gate",
                        "position": {"x": 350, "y": 100},
                    },
                ],
                "edges": [
                    {
                        "source_node_id": "license-checker-agent",
                        "target_node_id": "hitl-gate",
                        "edge_type": "normal",
                        "hitl_gate_config": {
                            "label": "Approve License Report",
                            "description": "Review the license compliance report before finalizing.",
                            "claim_expiry_minutes": 60,
                            "human_only": True,
                        },
                    },
                ],
                "connector_type_refs": ["github"],
                "schema_refs": [],
                "category": "compliance",
            },
            tags=["pipeline_template", "license", "compliance", "dependencies"],
        ),
        _make_modulo(
            pid="00000000-0000-0000-0000-00000000009E",
            primitive_type="library_collection",
            name="License Checker",
            slug="license-checker",
            description=(
                "Scan a repo's dependency manifests for license compliance."
                " Classifies each dependency by restrictiveness and flags"
                " commercial-use concerns. Installs a license-checker agent"
                " and pipeline. Requires: GitHub token or filesystem access."
            ),
            content_json={
                "manifest_pins": [
                    {"slug": "license-checker-agent", "version": "1.0"},
                    {"slug": "license-checker-pipeline", "version": "1.0"},
                ],
                "connector_requirements": [
                    {
                        "connector_type_id": "github",
                        "description": "GitHub token for reading repository dependency files",
                        "capabilities": ["read"],
                    }
                ],
                "trust_header": {"source": "modulo", "verified": True},
            },
            tags=["library_collection", "license", "compliance", "dependencies"],
        ),
    ]
)

# The library_collection type requires status="published" to be installable.
for _p in _MODULO_PRIMITIVES:
    if _p.primitive_type == "library_collection":
        _p.status = "published"

# Indexes for O(1) community lookup
_MODULO_BY_ID: dict[uuid.UUID, LibraryPrimitive] = {p.id: p for p in _MODULO_PRIMITIVES}
_MODULO_BY_SLUG: dict[tuple[str, str], LibraryPrimitive] = {(p.primitive_type, p.slug): p for p in _MODULO_PRIMITIVES}

# ---------------------------------------------------------------------------
# Community database — opinionated, narrower example pipelines contributed
# by users (ADR 010 §2). Launch-seeded with a small curated starter set;
# NOT marketed as "community-driven" until real external contributions
# exist. Never mixed into the Native library — always rendered as a
# separate, clearly-labelled UI section (source == "community").
# ---------------------------------------------------------------------------

_COMMUNITY_PRIMITIVES: list[LibraryPrimitive] = [
    _make_community_db_item(
        pid="00000000-0000-0000-0000-0000000000c1",
        primitive_type="workflow",
        name="Translate to French",
        slug="translate-to-french",
        description=(
            "Translates freeform input text into French. A narrow, "
            "single-purpose example pipeline — not maintained to Native "
            "library standards. Use as a starting point and adapt as needed."
        ),
        content_json={
            "nodes": [{"id": "translator", "agent": "french-translator"}],
            "edges": [],
            "entry": "translator",
            "agents": [
                {
                    "name": "French Translator",
                    "description": "Translates the given text into French.",
                    "prompt_template": (
                        "Translate the following text into French. Preserve tone and "
                        "meaning; do not add commentary.\n\nText:\n{{ input }}"
                    ),
                    "connector_type_refs": [],
                    "required_environment_capabilities": [],
                }
            ],
        },
        tags=["community", "translation", "french", "example"],
    ),
    _make_community_db_item(
        pid="00000000-0000-0000-0000-0000000000c2",
        primitive_type="workflow",
        name="QA Reviewer",
        slug="qa-reviewer",
        description=(
            "Reviews a code diff or PR description and flags likely bugs, "
            "missing tests, and style issues. Opinionated and narrow — "
            "contributed by users, not verified by Modulo."
        ),
        content_json={
            "nodes": [{"id": "reviewer", "agent": "qa-review-agent"}],
            "edges": [],
            "entry": "reviewer",
            "agents": [
                {
                    "name": "QA Review Agent",
                    "description": "Reviews a code diff for bugs, missing tests, and style issues.",
                    "prompt_template": (
                        "You are a QA reviewer. Review the following code diff and list: "
                        "1) likely bugs, 2) missing test coverage, 3) style issues.\n\n"
                        "Diff:\n{{ input }}"
                    ),
                    "connector_type_refs": [],
                    "required_environment_capabilities": [],
                }
            ],
        },
        tags=["community", "qa", "code-review", "example"],
    ),
    _make_community_db_item(
        pid="00000000-0000-0000-0000-0000000000c3",
        primitive_type="workflow",
        name="Commit Message Linter",
        slug="commit-message-linter",
        description=(
            "Checks a commit message against Conventional Commits style and "
            "suggests a corrected version. A small, illustrative example "
            "pipeline — not held to Native maintenance standards."
        ),
        content_json={
            "nodes": [{"id": "linter", "agent": "commit-lint-agent"}],
            "edges": [],
            "entry": "linter",
            "agents": [
                {
                    "name": "Commit Lint Agent",
                    "description": "Checks a commit message against Conventional Commits style.",
                    "prompt_template": (
                        "Check whether the following commit message follows the Conventional "
                        "Commits style (type(scope): summary). If it does not, suggest a "
                        "corrected version.\n\nCommit message:\n{{ input }}"
                    ),
                    "connector_type_refs": [],
                    "required_environment_capabilities": [],
                }
            ],
        },
        tags=["community", "git", "linting", "example"],
    ),
]

_COMMUNITY_BY_ID: dict[uuid.UUID, LibraryPrimitive] = {p.id: p for p in _COMMUNITY_PRIMITIVES}
_COMMUNITY_BY_SLUG: dict[tuple[str, str], LibraryPrimitive] = {
    (p.primitive_type, p.slug): p for p in _COMMUNITY_PRIMITIVES
}
