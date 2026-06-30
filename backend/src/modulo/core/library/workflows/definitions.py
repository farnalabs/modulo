"""12 canonical library workflow primitives.

Each dict defines a pre-built pipeline template that combines agents,
schemas, and connector bindings into a reusable workflow.

Workflows are registered as :class:`~modulo.db.models.library_primitive.LibraryPrimitive`
with ``primitive_type='workflow'``.
"""

from __future__ import annotations

from typing import Any

# ---------------------------------------------------------------------------
# 1. incident-to-deploy
# ---------------------------------------------------------------------------
INCIDENT_TO_DEPLOY: dict[str, Any] = {
    "name": "Incident to Deploy",
    "description": (
        "End-to-end incident response pipeline. Ingests an alert or "
        "incident report, triages severity, proposes a fix, sends the "
        "fix through code review, and deploys to the target environment."
    ),
    "version": "1.0.0",
    "author": "Modulo",
    "tags": ["incident", "response", "deploy", "remediation", "canonical"],
    "pipeline_steps": [
        {
            "id": "incident-triage",
            "agent": "ticket-triager",
            "connector_binding": {"type": "incident_management", "required": True},
            "description": "Analyse incoming incident and classify severity",
        },
        {
            "id": "fix-proposal",
            "agent": "correction-proposer",
            "depends_on": ["incident-triage"],
            "description": "Propose a code or configuration fix for the incident",
        },
        {
            "id": "fix-review",
            "agent": "code-reviewer",
            "depends_on": ["fix-proposal"],
            "description": "Review the proposed fix for quality and security",
        },
        {
            "id": "deploy",
            "agent": None,
            "connector_binding": {"type": "ci_cd", "required": True},
            "depends_on": ["fix-review"],
            "description": "Deploy the approved fix to production",
        },
    ],
    "default_config": {
        "auto_deploy": False,
        "require_approval": True,
        "rollback_on_failure": True,
        "notification_channels": ["slack", "teams"],
        "incident_sources": ["pagerduty", "sentry"],
    },
}

# ---------------------------------------------------------------------------
# 2. feature-proposal
# ---------------------------------------------------------------------------
FEATURE_PROPOSAL: dict[str, Any] = {
    "name": "Feature Proposal",
    "description": (
        "Guided feature proposal pipeline. Takes an idea description, "
        "generates a PRD, reviews it against existing requirements, "
        "reaches approval decisions, and schedules the work."
    ),
    "version": "1.0.0",
    "author": "Modulo",
    "tags": ["feature", "proposal", "prd", "planning", "canonical"],
    "pipeline_steps": [
        {
            "id": "prd-generation",
            "agent": "prd-summarizer",
            "description": "Generate a structured PRD from the idea input",
        },
        {
            "id": "feasibility-review",
            "agent": "compliance-checker",
            "depends_on": ["prd-generation"],
            "description": "Review PRD for feasibility and compliance constraints",
        },
        {
            "id": "approval",
            "agent": None,
            "depends_on": ["feasibility-review"],
            "description": "Human-in-the-loop approval gate",
        },
        {
            "id": "ticket-creation",
            "agent": "ticket-writer",
            "depends_on": ["approval"],
            "connector_binding": {"type": "issue_tracking", "required": True},
            "description": "Create tickets from approved proposal",
        },
    ],
    "default_config": {
        "require_human_approval": True,
        "auto_create_tickets": True,
        "ticket_type": "feature",
        "target_tracking_system": "linear",
    },
}

# ---------------------------------------------------------------------------
# 3. schema-inference-pipeline
# ---------------------------------------------------------------------------
SCHEMA_INFERENCE_PIPELINE: dict[str, Any] = {
    "name": "Schema Inference Pipeline",
    "description": (
        "Automated schema discovery pipeline. Connects to a data source, "
        "samples records, infers a JSON Schema, validates it against "
        "existing schemas, and publishes the result."
    ),
    "version": "1.0.0",
    "author": "Modulo",
    "tags": ["schema", "inference", "discovery", "data", "canonical"],
    "pipeline_steps": [
        {
            "id": "data-ingestion",
            "agent": None,
            "connector_binding": {"type": "filesystem", "required": True},
            "description": "Read sample data records from the source connector",
        },
        {
            "id": "schema-inference",
            "agent": "schema-inferrer",
            "depends_on": ["data-ingestion"],
            "description": "Infer JSON Schema from sampled records",
        },
        {
            "id": "schema-validation",
            "agent": None,
            "depends_on": ["schema-inference"],
            "description": "Validate inferred schema against known patterns and constraints",
        },
        {
            "id": "publish",
            "agent": None,
            "depends_on": ["schema-validation"],
            "connector_binding": {"type": "filesystem", "required": False},
            "description": "Publish validated schema to schema registry or filesystem",
        },
    ],
    "default_config": {
        "sample_size": 100,
        "confidence_threshold": 0.8,
        "publish_format": "json-schema-draft-07",
        "allow_overwrite": False,
    },
}

# ---------------------------------------------------------------------------
# 4. requirements-to-file
# ---------------------------------------------------------------------------
REQUIREMENTS_TO_FILE: dict[str, Any] = {
    "name": "Requirements to File",
    "description": (
        "Full requirements-to-implementation pipeline. Takes requirements "
        "text, generates a design, implements the code, runs tests, and "
        "submits for review."
    ),
    "version": "1.0.0",
    "author": "Modulo",
    "tags": ["requirements", "implementation", "testing", "sdlc", "canonical"],
    "pipeline_steps": [
        {
            "id": "design",
            "agent": "doc-generator",
            "description": "Generate design document from requirements",
        },
        {
            "id": "implementation",
            "agent": None,
            "depends_on": ["design"],
            "description": "Generate implementation code from design (delegated to code-gen agent)",
        },
        {
            "id": "test-generation",
            "agent": "test-generator",
            "depends_on": ["implementation"],
            "description": "Generate unit tests for the implementation",
        },
        {
            "id": "code-review",
            "agent": "code-reviewer",
            "depends_on": ["implementation", "test-generation"],
            "description": "Review implementation and tests for quality",
        },
    ],
    "default_config": {
        "language": "python",
        "test_framework": "pytest",
        "require_tests": True,
        "style_guide": "pep8",
    },
}

# ---------------------------------------------------------------------------
# 5. full-sdlc
# ---------------------------------------------------------------------------
FULL_SDLC: dict[str, Any] = {
    "name": "Full SDLC",
    "description": (
        "Complete software delivery lifecycle pipeline. Covers ideation, "
        "requirements, design, implementation, testing, review, release "
        "notes, and deployment in a single end-to-end workflow."
    ),
    "version": "1.0.0",
    "author": "Modulo",
    "tags": ["sdlc", "full-lifecycle", "delivery", "devops", "canonical"],
    "pipeline_steps": [
        {
            "id": "ideation",
            "agent": "ticket-writer",
            "description": "Capture idea as structured ticket",
        },
        {
            "id": "requirements",
            "agent": "prd-summarizer",
            "depends_on": ["ideation"],
            "description": "Generate PRD from ticket",
        },
        {
            "id": "design",
            "agent": "doc-generator",
            "depends_on": ["requirements"],
            "description": "Generate design document",
        },
        {
            "id": "implementation",
            "agent": None,
            "depends_on": ["design"],
            "description": "Implement the feature",
        },
        {
            "id": "testing",
            "agent": "test-generator",
            "depends_on": ["implementation"],
            "description": "Generate and run tests",
        },
        {
            "id": "review",
            "agent": "code-reviewer",
            "depends_on": ["implementation", "testing"],
            "description": "Full code and test review",
        },
        {
            "id": "release-notes",
            "agent": "release-note-generator",
            "depends_on": ["review"],
            "description": "Generate release notes",
        },
        {
            "id": "deploy",
            "agent": None,
            "depends_on": ["release-notes"],
            "connector_binding": {"type": "ci_cd", "required": True},
            "description": "Deploy to target environment",
        },
    ],
    "default_config": {
        "branch_prefix": "feature/",
        "require_review_approval": True,
        "auto_deploy": False,
        "environments": ["staging", "production"],
        "notification_channels": ["slack"],
    },
}

# ---------------------------------------------------------------------------
# 6. ticket-to-pr
# ---------------------------------------------------------------------------
TICKET_TO_PR: dict[str, Any] = {
    "name": "Ticket to PR",
    "description": (
        "Automated ticket-to-pull-request pipeline. Reads a ticket from "
        "an issue tracker, creates a feature branch, implements the "
        "changes, runs tests, and opens a PR."
    ),
    "version": "1.0.0",
    "author": "Modulo",
    "tags": ["ticket", "pr", "automation", "github", "gitlab", "canonical"],
    "pipeline_steps": [
        {
            "id": "ticket-fetch",
            "agent": None,
            "connector_binding": {"type": "issue_tracking", "required": True},
            "description": "Fetch ticket details from issue tracker",
        },
        {
            "id": "ticket-analysis",
            "agent": "ticket-triager",
            "depends_on": ["ticket-fetch"],
            "description": "Analyse and estimate the ticket",
        },
        {
            "id": "branch-creation",
            "agent": None,
            "depends_on": ["ticket-analysis"],
            "connector_binding": {"type": "source_control", "required": True},
            "description": "Create feature branch from ticket",
        },
        {
            "id": "implementation",
            "agent": None,
            "depends_on": ["branch-creation"],
            "description": "Implement code changes",
        },
        {
            "id": "testing",
            "agent": "test-generator",
            "depends_on": ["implementation"],
            "description": "Generate and run tests",
        },
        {
            "id": "pr-creation",
            "agent": None,
            "depends_on": ["implementation", "testing"],
            "connector_binding": {"type": "source_control", "required": True},
            "description": "Open pull request against base branch",
        },
    ],
    "default_config": {
        "branch_naming": "{ticket_id}-{slug}",
        "base_branch": "main",
        "auto_test": True,
        "pr_template": "default",
    },
}

# ---------------------------------------------------------------------------
# 7. adr
# ---------------------------------------------------------------------------
ADR_WORKFLOW: dict[str, Any] = {
    "name": "ADR Workflow",
    "description": (
        "Architecture Decision Record pipeline. Guides the creation, "
        "review, approval, and storage of ADRs following the standard "
        "ADR format."
    ),
    "version": "1.0.0",
    "author": "Modulo",
    "tags": ["adr", "architecture", "decisions", "documentation", "canonical"],
    "pipeline_steps": [
        {
            "id": "adr-creation",
            "agent": "doc-generator",
            "description": "Generate ADR from context and decision description",
        },
        {
            "id": "adr-review",
            "agent": "code-reviewer",
            "depends_on": ["adr-creation"],
            "description": "Review ADR for clarity, completeness, and trade-offs",
        },
        {
            "id": "approval",
            "agent": None,
            "depends_on": ["adr-review"],
            "description": "Human-in-the-loop approval gate",
        },
        {
            "id": "storage",
            "agent": None,
            "depends_on": ["approval"],
            "connector_binding": {"type": "source_control", "required": True},
            "description": "Commit ADR to docs/adr/ directory",
        },
    ],
    "default_config": {
        "adr_directory": "docs/adr/",
        "numbering_prefix": True,
        "require_review": True,
        "commit_on_approve": True,
    },
}

# ---------------------------------------------------------------------------
# 8. meeting-to-tickets
# ---------------------------------------------------------------------------
MEETING_TO_TICKETS: dict[str, Any] = {
    "name": "Meeting to Tickets",
    "description": (
        "Meeting action-item extraction pipeline. Takes meeting notes, "
        "extracts action items and decisions, creates tickets in the "
        "connected issue tracker."
    ),
    "version": "1.0.0",
    "author": "Modulo",
    "tags": ["meetings", "actions", "tickets", "productivity", "canonical"],
    "pipeline_steps": [
        {
            "id": "notes-ingestion",
            "agent": None,
            "connector_binding": {"type": "filesystem", "required": False},
            "description": "Read meeting notes from connected docs source",
        },
        {
            "id": "action-extraction",
            "agent": "ticket-writer",
            "depends_on": ["notes-ingestion"],
            "description": "Extract action items and decisions from notes",
        },
        {
            "id": "ticket-creation",
            "agent": None,
            "depends_on": ["action-extraction"],
            "connector_binding": {"type": "issue_tracking", "required": True},
            "description": "Create tickets for each action item",
        },
        {
            "id": "summary",
            "agent": "status-reporter",
            "depends_on": ["ticket-creation"],
            "description": "Generate summary of created tickets and post to channel",
        },
    ],
    "default_config": {
        "notes_source": "confluence",
        "ticket_type": "task",
        "assign_on_create": False,
        "post_summary_to": "slack",
        "default_priority": "medium",
    },
}

# ---------------------------------------------------------------------------
# 9. sprint-retrospective
# ---------------------------------------------------------------------------
SPRINT_RETROSPECTIVE: dict[str, Any] = {
    "name": "Sprint Retrospective",
    "description": (
        "Automated sprint retrospective pipeline. Gathers sprint data "
        "(completed tickets, velocity, feedback), analyses patterns, "
        "generates a retrospective report, and shares it with the team."
    ),
    "version": "1.0.0",
    "author": "Modulo",
    "tags": ["sprint", "retrospective", "agile", "metrics", "canonical"],
    "pipeline_steps": [
        {
            "id": "data-gathering",
            "agent": None,
            "connector_binding": {"type": "issue_tracking", "required": True},
            "description": "Fetch sprint data from issue tracker",
        },
        {
            "id": "feedback-analysis",
            "agent": "feedback-analyzer",
            "depends_on": ["data-gathering"],
            "description": "Analyse feedback and sentiment from sprint",
        },
        {
            "id": "metrics-generation",
            "agent": "status-reporter",
            "depends_on": ["data-gathering"],
            "description": "Generate sprint metrics and trends",
        },
        {
            "id": "report-generation",
            "agent": "doc-generator",
            "depends_on": ["feedback-analysis", "metrics-generation"],
            "description": "Generate retrospective report",
        },
        {
            "id": "notification",
            "agent": None,
            "depends_on": ["report-generation"],
            "connector_binding": {"type": "messaging", "required": True},
            "description": "Post retrospective to team channel",
        },
    ],
    "default_config": {
        "sprint_length_days": 14,
        "include_velocity": True,
        "include_sentiment": True,
        "post_to": "slack",
        "generate_action_items": True,
    },
}

# ---------------------------------------------------------------------------
# 10. weekly-quality-report
# ---------------------------------------------------------------------------
WEEKLY_QUALITY_REPORT: dict[str, Any] = {
    "name": "Weekly Quality Report",
    "description": (
        "Automated weekly quality report pipeline. Gathers quality "
        "metrics from connected tools (SonarQube, Sentry, Snyk, CI), "
        "analyses trends, generates a report, and notifies stakeholders."
    ),
    "version": "1.0.0",
    "author": "Modulo",
    "tags": ["quality", "reporting", "metrics", "weekly", "canonical"],
    "pipeline_steps": [
        {
            "id": "metrics-gathering",
            "agent": None,
            "connector_binding": {"type": "monitoring", "required": False},
            "description": "Gather quality metrics from all connected tools",
        },
        {
            "id": "trend-analysis",
            "agent": "status-reporter",
            "depends_on": ["metrics-gathering"],
            "description": "Analyse trends and changes from last week",
        },
        {
            "id": "security-scan",
            "agent": "security-reviewer",
            "depends_on": ["metrics-gathering"],
            "description": "Review security metrics and vulnerabilities",
        },
        {
            "id": "report-generation",
            "agent": "doc-generator",
            "depends_on": ["trend-analysis", "security-scan"],
            "description": "Generate comprehensive quality report",
        },
        {
            "id": "notification",
            "agent": None,
            "depends_on": ["report-generation"],
            "connector_binding": {"type": "messaging", "required": True},
            "description": "Send report to stakeholders",
        },
    ],
    "default_config": {
        "schedule": "cron(0 9 * * 1)",
        "metrics_sources": ["sonarqube", "sentry", "snyk"],
        "include_security": True,
        "include_coverage": True,
        "recipients": ["engineering-leads"],
    },
}

# ---------------------------------------------------------------------------
# 11. cicd
# ---------------------------------------------------------------------------
CICD_WORKFLOW: dict[str, Any] = {
    "name": "CI/CD Pipeline",
    "description": (
        "Standard CI/CD pipeline template. Runs linting, unit tests, "
        "build, integration tests, security scan, and deploys on success "
        "with optional approval gates."
    ),
    "version": "1.0.0",
    "author": "Modulo",
    "tags": ["ci-cd", "automation", "testing", "deploy", "canonical"],
    "pipeline_steps": [
        {
            "id": "lint",
            "agent": None,
            "connector_binding": {"type": "ci_runner", "required": False},
            "description": "Run linting and static analysis",
        },
        {
            "id": "unit-tests",
            "agent": "test-generator",
            "depends_on": ["lint"],
            "description": "Run unit tests",
        },
        {
            "id": "build",
            "agent": None,
            "depends_on": ["unit-tests"],
            "connector_binding": {"type": "ci_runner", "required": False},
            "description": "Build the application",
        },
        {
            "id": "security-scan",
            "agent": "security-reviewer",
            "depends_on": ["build"],
            "description": "Run security vulnerability scan",
        },
        {
            "id": "deploy-staging",
            "agent": None,
            "depends_on": ["security-scan"],
            "connector_binding": {"type": "ci_cd", "required": True},
            "description": "Deploy to staging environment",
        },
    ],
    "default_config": {
        "python_version": "3.12",
        "node_version": "22",
        "lint_tools": ["ruff", "mypy"],
        "test_command": "pytest",
        "build_command": "npm run build",
        "staging_deploy": True,
        "production_deploy": False,
        "require_approval_for_production": True,
    },
}

# ---------------------------------------------------------------------------
# 12. release-candidate
# ---------------------------------------------------------------------------
RELEASE_CANDIDATE: dict[str, Any] = {
    "name": "Release Candidate",
    "description": (
        "Release candidate management pipeline. Versions the build, "
        "runs full test suite and security scan, deploys to staging, "
        "runs smoke tests, requests approval, and promotes to production."
    ),
    "version": "1.0.0",
    "author": "Modulo",
    "tags": ["release", "candidate", "deploy", "approval", "canonical"],
    "pipeline_steps": [
        {
            "id": "versioning",
            "agent": "changelog-writer",
            "description": "Determine next version and update changelog",
        },
        {
            "id": "build",
            "agent": None,
            "depends_on": ["versioning"],
            "connector_binding": {"type": "ci_cd", "required": True},
            "description": "Build release candidate artifacts",
        },
        {
            "id": "full-test-suite",
            "agent": "test-generator",
            "depends_on": ["build"],
            "description": "Run full test suite including integration tests",
        },
        {
            "id": "security-scan",
            "agent": "security-reviewer",
            "depends_on": ["full-test-suite"],
            "description": "Run comprehensive security scan on build artifacts",
        },
        {
            "id": "deploy-staging",
            "agent": None,
            "depends_on": ["security-scan"],
            "connector_binding": {"type": "ci_cd", "required": True},
            "description": "Deploy to staging for validation",
        },
        {
            "id": "smoke-tests",
            "agent": None,
            "depends_on": ["deploy-staging"],
            "description": "Run smoke tests against staging deployment",
        },
        {
            "id": "approval-gate",
            "agent": None,
            "depends_on": ["smoke-tests"],
            "description": "Human approval gate before production",
        },
        {
            "id": "promote-production",
            "agent": None,
            "depends_on": ["approval-gate"],
            "connector_binding": {"type": "ci_cd", "required": True},
            "description": "Promote release candidate to production",
        },
    ],
    "default_config": {
        "version_strategy": "semver",
        "staging_url": "",
        "production_url": "",
        "require_approval": True,
        "rollback_on_failure": True,
        "notification_channels": ["slack"],
        "smoke_test_endpoints": ["/healthz", "/healthz/ready"],
    },
}
