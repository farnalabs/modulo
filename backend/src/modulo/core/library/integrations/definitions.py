"""24 canonical library integration primitives.

Each dict provides the metadata, default configuration, credential
field descriptions, and tool-group classification needed to register
a :class:`~modulo.db.models.library_primitive.LibraryPrimitive` with
``primitive_type='integration'``.

``credential_fields`` is the single source of truth for the credential keys a
connector type consumes: ``_build_connector`` in ``modulo.core.connector_hub``
must read exactly these keys via ``_get_cred``. The parity guard in
``tests/unit/connector_hub/test_definitions_credential_parity.py`` enforces this
for every type that has a direct hub read. A key declared here but read under a
different name in the hub means a connector configured via its definition is
silently skipped at ``initialise()``.
"""

from __future__ import annotations

from typing import Any

# ---------------------------------------------------------------------------
# 1. sentry
# ---------------------------------------------------------------------------
SENTRY_INTEGRATION: dict[str, Any] = {
    "name": "Sentry",
    "description": (
        "Error tracking and performance monitoring integration. Captures "
        "exceptions, transactions, and crash reports from applications and "
        "surfaces them in the Modulo pipeline for triage and alerting."
    ),
    "version": "1.0.0",
    "author": "Modulo",
    "tags": ["monitoring", "error-tracking", "observability", "canonical"],
    "connector_type": "sentry",
    "default_config": {
        "organization_slug": "",
        "project_slug": "",
        "base_url": "https://sentry.io/api/0",
        "poll_interval_seconds": 60,
        "max_issues_per_poll": 50,
        "request_timeout_seconds": 30,
        "retry_max_attempts": 3,
        "retry_backoff_base_seconds": 1,
    },
    "credential_fields": {
        "token": {
            "type": "string",
            "description": "Sentry authentication token (org-level)",
            "required": True,
        },
    },
    "tool_group": "monitoring",
}

# ---------------------------------------------------------------------------
# 2. bitbucket
# ---------------------------------------------------------------------------
BITBUCKET_INTEGRATION: dict[str, Any] = {
    "name": "Bitbucket",
    "description": (
        "Bitbucket Cloud git hosting integration. Provides repository "
        "access, pull request management, and code review workflows."
    ),
    "version": "1.0.0",
    "author": "Modulo",
    "tags": ["git", "hosting", "code-review", "canonical"],
    "connector_type": "bitbucket",
    "default_config": {
        "workspace": "",
        "base_url": "https://api.bitbucket.org/2.0",
        "default_branch": "main",
    },
    "credential_fields": {
        "token": {
            "type": "string",
            "description": "Bitbucket app password or OAuth 2.0 bearer token with repo and PR permissions",
            "required": True,
        },
    },
    "tool_group": "source_control",
}

# ---------------------------------------------------------------------------
# 3. azure-devops
# ---------------------------------------------------------------------------
AZURE_DEVOPS_INTEGRATION: dict[str, Any] = {
    "name": "Azure DevOps",
    "description": (
        "Azure DevOps Services integration including Azure Repos (git), Azure Pipelines (CI/CD), Boards, and Wiki."
    ),
    "version": "1.0.0",
    "author": "Modulo",
    "tags": ["devops", "ci-cd", "azure", "repos", "canonical"],
    "connector_type": "azure_pipelines",
    "default_config": {
        "organization": "",
        "project": "",
        "base_url": "https://dev.azure.com",
        "api_version": "7.1",
    },
    "credential_fields": {
        "token": {
            "type": "string",
            "description": "Azure DevOps PAT with Code (Read/Write) and Build (Read) scopes",
            "required": True,
        },
    },
    "tool_group": "devops",
}

# ---------------------------------------------------------------------------
# 4. prometheus
# ---------------------------------------------------------------------------
PROMETHEUS_INTEGRATION: dict[str, Any] = {
    "name": "Prometheus",
    "description": (
        "Prometheus monitoring and alerting integration. Queries metrics "
        "via PromQL, retrieves alertmanager alerts, and feeds time-series "
        "data into pipeline analysis."
    ),
    "version": "1.0.0",
    "author": "Modulo",
    "tags": ["monitoring", "metrics", "observability", "promql", "canonical"],
    "connector_type": "custom",
    "default_config": {
        "base_url": "",
        "query_timeout_seconds": 30,
        "max_series": 1000,
        "step": "30s",
    },
    "credential_fields": {
        "basic_auth_user": {
            "type": "string",
            "description": "Basic auth username (if Prometheus is secured)",
            "required": False,
        },
        "basic_auth_password": {
            "type": "string",
            "description": "Basic auth password",
            "required": False,
        },
        "bearer_token": {
            "type": "string",
            "description": "Bearer token for token-based auth",
            "required": False,
        },
    },
    "tool_group": "monitoring",
}

# ---------------------------------------------------------------------------
# 5. datadog
# ---------------------------------------------------------------------------
# FAR-515 compat note: the credential key for the application key is
# ``application_key`` (NOT ``app_key``, which ``_build_connector`` used to read).
# Existing stored credentials keyed as ``app_key`` will no longer resolve at the
# hub — those rows must be re-credentialed under ``application_key``. We do NOT
# migrate stored credentials in this change; any affected connector is skipped at
# ``initialise()`` until re-credentialed.
DATADOG_INTEGRATION: dict[str, Any] = {
    "name": "Datadog",
    "description": (
        "Datadog observability integration. Ingests metrics, traces, "
        "logs, and monitors from Datadog for pipeline-driven alerting "
        "and incident analysis."
    ),
    "version": "1.0.0",
    "author": "Modulo",
    "tags": ["monitoring", "observability", "logs", "metrics", "canonical"],
    "connector_type": "datadog",
    "default_config": {
        "site": "datadoghq.com",
        "poll_interval_seconds": 60,
        "request_timeout_seconds": 30,
        "retry_max_attempts": 3,
        "retry_backoff_base_seconds": 1,
    },
    "credential_fields": {
        "api_key": {
            "type": "string",
            "description": "Datadog API key",
            "required": True,
        },
        "application_key": {
            "type": "string",
            "description": "Datadog application key",
            "required": True,
        },
    },
    "tool_group": "monitoring",
}

# ---------------------------------------------------------------------------
# 6. pagerduty
# ---------------------------------------------------------------------------
PAGERDUTY_INTEGRATION: dict[str, Any] = {
    "name": "PagerDuty",
    "description": (
        "Incident management integration. Creates, acknowledges, and "
        "resolves PagerDuty incidents from pipeline failures and alert "
        "conditions."
    ),
    "version": "1.0.0",
    "author": "Modulo",
    "tags": ["incidents", "alerting", "on-call", "canonical"],
    "connector_type": "pagerduty",
    "default_config": {
        "base_url": "https://api.pagerduty.com",
        "service_id": "",
        "escalation_policy_id": "",
    },
    "credential_fields": {
        "token": {
            "type": "string",
            "description": "PagerDuty API token (v2)",
            "required": True,
        },
    },
    "tool_group": "incident_management",
}

# ---------------------------------------------------------------------------
# 7. jira
# ---------------------------------------------------------------------------
JIRA_INTEGRATION: dict[str, Any] = {
    "name": "Jira",
    "description": (
        "Jira issue and project tracking integration. Supports issue CRUD, sprint management, and custom field mapping."
    ),
    "version": "1.0.0",
    "author": "Modulo",
    "tags": ["issue-tracking", "project-management", "agile", "canonical"],
    "connector_type": "jira",
    "default_config": {
        "base_url": "",
        "project_key": "",
        "api_version": "3",
    },
    "credential_fields": {
        "email": {
            "type": "string",
            "description": "Jira account email",
            "required": True,
        },
        "api_token": {
            "type": "string",
            "description": "Jira API token",
            "required": True,
        },
    },
    "tool_group": "issue_tracking",
}

# ---------------------------------------------------------------------------
# 9. gitlab
# ---------------------------------------------------------------------------
GITLAB_INTEGRATION: dict[str, Any] = {
    "name": "GitLab",
    "description": (
        "GitLab git hosting and CI/CD integration. Provides repository "
        "access, merge request management, CI pipeline triggers, and "
        "issue tracking."
    ),
    "version": "1.0.0",
    "author": "Modulo",
    "tags": ["git", "ci-cd", "hosting", "devops", "canonical"],
    "connector_type": "gitlab",
    "default_config": {
        "base_url": "https://gitlab.com/api/v4",
        "project_id": "",
        "default_branch": "main",
    },
    "credential_fields": {
        "token": {
            "type": "string",
            "description": "GitLab personal access token with api scope",
            "required": True,
        },
    },
    "tool_group": "source_control",
}

# ---------------------------------------------------------------------------
# 10. slack
# ---------------------------------------------------------------------------
SLACK_INTEGRATION: dict[str, Any] = {
    "name": "Slack",
    "description": (
        "Slack messaging integration. Sends pipeline notifications, "
        "receives slash-command triggers, and posts structured messages "
        "with blocks and attachments."
    ),
    "version": "1.0.0",
    "author": "Modulo",
    "tags": ["messaging", "notifications", "collaboration", "canonical"],
    "connector_type": "slack",
    "default_config": {
        "base_url": "https://slack.com/api",
        "default_channel": "#general",
        "allow_bot_mention": True,
    },
    "credential_fields": {
        "bot_token": {
            "type": "string",
            "description": "Slack bot token (xoxb-...) with chat:write and channels:read scopes",
            "required": True,
        },
    },
    "tool_group": "messaging",
}

# ---------------------------------------------------------------------------
# 11. confluence
# ---------------------------------------------------------------------------
CONFLUENCE_INTEGRATION: dict[str, Any] = {
    "name": "Confluence",
    "description": (
        "Confluence wiki and documentation integration. Creates, updates, "
        "and retrieves pages, attachments, and blog posts."
    ),
    "version": "1.0.0",
    "author": "Modulo",
    "tags": ["wiki", "documentation", "knowledge-base", "canonical"],
    "connector_type": "confluence",
    "default_config": {
        "base_url": "",
        "space_key": "",
        "api_version": "2",
    },
    "credential_fields": {
        "username": {
            "type": "string",
            "description": "Confluence account username or email",
            "required": True,
        },
        "api_token": {
            "type": "string",
            "description": "Confluence API token",
            "required": True,
        },
    },
    "tool_group": "documentation",
}

# ---------------------------------------------------------------------------
# 12. notion
# ---------------------------------------------------------------------------
NOTION_INTEGRATION: dict[str, Any] = {
    "name": "Notion",
    "description": (
        "Notion knowledge-base integration. Reads and writes pages, "
        "databases, and blocks for documentation and project tracking."
    ),
    "version": "1.0.0",
    "author": "Modulo",
    "tags": ["documentation", "knowledge-base", "project-management", "canonical"],
    "connector_type": "notion",
    "default_config": {
        "base_url": "https://api.notion.com/v1",
        "notion_version": "2022-06-28",
        "database_id": "",
    },
    "credential_fields": {
        "token": {
            "type": "string",
            "description": "Notion internal integration token (secret_...)",
            "required": True,
        },
    },
    "tool_group": "documentation",
}

# ---------------------------------------------------------------------------
# 13. sonarqube
# ---------------------------------------------------------------------------
SONARQUBE_INTEGRATION: dict[str, Any] = {
    "name": "SonarQube",
    "description": (
        "Code quality and static analysis integration. Retrieves quality "
        "gate status, code smells, bugs, vulnerabilities, and coverage "
        "metrics from SonarQube."
    ),
    "version": "1.0.0",
    "author": "Modulo",
    "tags": ["code-quality", "static-analysis", "linting", "canonical"],
    "connector_type": "sonarqube",
    "default_config": {
        "base_url": "",
        "project_key": "",
        "quality_gate": "SQALE",
    },
    "credential_fields": {
        "token": {
            "type": "string",
            "description": "SonarQube user token",
            "required": True,
        },
    },
    "tool_group": "code_quality",
}

# ---------------------------------------------------------------------------
# 14. elastic
# ---------------------------------------------------------------------------
ELASTIC_INTEGRATION: dict[str, Any] = {
    "name": "Elasticsearch",
    "description": (
        "Elasticsearch search and observability integration. Queries "
        "indices for log analytics, application performance data, and "
        "structured search operations."
    ),
    "version": "1.0.0",
    "author": "Modulo",
    "tags": ["search", "observability", "logs", "analytics", "canonical"],
    "connector_type": "custom",
    "default_config": {
        "base_url": "",
        "index_pattern": "",
        "max_results": 1000,
        "request_timeout_seconds": 30,
    },
    "credential_fields": {
        "api_key": {
            "type": "string",
            "description": "Elasticsearch API key (base64-encoded)",
            "required": False,
        },
        "username": {
            "type": "string",
            "description": "Basic auth username",
            "required": False,
        },
        "password": {
            "type": "string",
            "description": "Basic auth password",
            "required": False,
        },
    },
    "tool_group": "observability",
}

# ---------------------------------------------------------------------------
# 15. n8n
# ---------------------------------------------------------------------------
N8N_INTEGRATION: dict[str, Any] = {
    "name": "n8n",
    "description": (
        "n8n workflow automation integration. Triggers and manages n8n "
        "workflows from pipeline events and receives webhook callbacks."
    ),
    "version": "1.0.0",
    "author": "Modulo",
    "tags": ["automation", "workflow", "integration", "canonical"],
    "connector_type": "n8n",
    "default_config": {
        "base_url": "",
        "api_version": "v1",
        "workflow_activation": True,
    },
    "credential_fields": {
        "token": {
            "type": "string",
            "description": "n8n API key",
            "required": True,
        },
    },
    "tool_group": "automation",
}

# ---------------------------------------------------------------------------
# 16. terraform
# ---------------------------------------------------------------------------
TERRAFORM_INTEGRATION: dict[str, Any] = {
    "name": "Terraform",
    "description": (
        "Infrastructure as Code integration. Plans and applies Terraform "
        "configurations, retrieves state, and validates HCL syntax."
    ),
    "version": "1.0.0",
    "author": "Modulo",
    "tags": ["infrastructure", "iac", "terraform", "cloud", "canonical"],
    "connector_type": "custom",
    "default_config": {
        "working_directory": "./terraform",
        "terraform_version": "1.9",
        "auto_approve": False,
        "backend_config": {},
        "var_files": [],
    },
    "credential_fields": {
        "aws_access_key_id": {
            "type": "string",
            "description": "AWS access key ID for Terraform AWS provider",
            "required": False,
        },
        "aws_secret_access_key": {
            "type": "string",
            "description": "AWS secret access key",
            "required": False,
        },
        "gcp_service_account_json": {
            "type": "string",
            "description": "GCP service account key (JSON) for Terraform GCP provider",
            "required": False,
        },
    },
    "tool_group": "infrastructure",
}

# ---------------------------------------------------------------------------
# 17. teams
# ---------------------------------------------------------------------------
TEAMS_INTEGRATION: dict[str, Any] = {
    "name": "Microsoft Teams",
    "description": (
        "Microsoft Teams messaging integration. Sends adaptive card "
        "notifications, receives commands, and posts pipeline updates "
        "to Teams channels."
    ),
    "version": "1.0.0",
    "author": "Modulo",
    "tags": ["messaging", "notifications", "collaboration", "microsoft", "canonical"],
    "connector_type": "microsoft_teams",
    "default_config": {
        "default_channel_id": "",
        "team_id": "",
        "notification_style": "adaptive_card",
    },
    "credential_fields": {
        "token": {
            "type": "string",
            "description": "Teams bot application token (for Microsoft Graph API access)",
            "required": True,
        },
    },
    "tool_group": "messaging",
}

# ---------------------------------------------------------------------------
# 18. discord
# ---------------------------------------------------------------------------
DISCORD_INTEGRATION: dict[str, Any] = {
    "name": "Discord",
    "description": (
        "Discord messaging integration. Sends pipeline notifications "
        "via webhooks, manages threads, and listens for bot commands."
    ),
    "version": "1.0.0",
    "author": "Modulo",
    "tags": ["messaging", "notifications", "collaboration", "canonical"],
    "connector_type": "discord",
    "default_config": {
        "base_url": "https://discord.com/api/v10",
        "default_channel_id": "",
    },
    "credential_fields": {
        "token": {
            "type": "string",
            "description": "Discord bot token",
            "required": True,
        },
    },
    "tool_group": "messaging",
}

# ---------------------------------------------------------------------------
# 19. jenkins
# ---------------------------------------------------------------------------
JENKINS_INTEGRATION: dict[str, Any] = {
    "name": "Jenkins",
    "description": (
        "Jenkins CI/CD integration. Triggers builds, retrieves build logs and status, and monitors job health."
    ),
    "version": "1.0.0",
    "author": "Modulo",
    "tags": ["ci-cd", "automation", "jenkins", "build", "canonical"],
    "connector_type": "jenkins",
    "default_config": {
        "base_url": "",
        "job_name": "",
        "poll_interval_seconds": 30,
        "request_timeout_seconds": 30,
        "retry_max_attempts": 3,
        "retry_backoff_base_seconds": 1,
    },
    "credential_fields": {
        "username": {
            "type": "string",
            "description": "Jenkins username",
            "required": True,
        },
        "token": {
            "type": "string",
            "description": "Jenkins API token (preferred over password)",
            "required": True,
        },
    },
    "tool_group": "ci_cd",
}

# ---------------------------------------------------------------------------
# 20. github-actions
# ---------------------------------------------------------------------------
GITHUB_ACTIONS_INTEGRATION: dict[str, Any] = {
    "name": "GitHub Actions",
    "description": (
        "GitHub Actions CI/CD integration. Triggers workflow runs, "
        "monitors run status and logs, and dispatches repository events."
    ),
    "version": "1.0.0",
    "author": "Modulo",
    "tags": ["ci-cd", "github", "automation", "workflows", "canonical"],
    "connector_type": "ci_runner",
    "default_config": {
        "base_url": "https://api.github.com",
        "owner": "",
        "repo": "",
        "default_branch": "main",
        "poll_interval_seconds": 15,
        "request_timeout_seconds": 30,
        "retry_max_attempts": 3,
        "retry_backoff_base_seconds": 1,
    },
    "credential_fields": {
        "github_token": {
            "type": "string",
            "description": "GitHub personal access token with repo and workflow scopes",
            "required": True,
        },
    },
    "tool_group": "ci_cd",
}

# ---------------------------------------------------------------------------
# 21. snyk
# ---------------------------------------------------------------------------
SNYK_INTEGRATION: dict[str, Any] = {
    "name": "Snyk",
    "description": (
        "Vulnerability scanning integration. Tests dependencies and "
        "container images for known CVEs, retrieves advisory details, "
        "and monitors project risk."
    ),
    "version": "1.0.0",
    "author": "Modulo",
    "tags": ["security", "vulnerability", "dependencies", "scanning", "canonical"],
    "connector_type": "snyk",
    "default_config": {
        "base_url": "https://api.snyk.io/v1",
        "org_id": "",
        "project_id": "",
        "severity_threshold": "medium",
    },
    "credential_fields": {
        "token": {
            "type": "string",
            "description": "Snyk API token",
            "required": True,
        },
    },
    "tool_group": "security",
}

# ---------------------------------------------------------------------------
# 22. kubernetes
# ---------------------------------------------------------------------------
KUBERNETES_INTEGRATION: dict[str, Any] = {
    "name": "Kubernetes",
    "description": (
        "Kubernetes container orchestration integration. Retrieves pod "
        "status, deployment health, events, and logs for pipeline-driven "
        "deployment monitoring and incident analysis."
    ),
    "version": "1.0.0",
    "author": "Modulo",
    "tags": ["kubernetes", "containers", "orchestration", "infrastructure", "canonical"],
    "connector_type": "custom",
    "default_config": {
        "kubeconfig_path": "",
        "context": "",
        "namespace": "default",
        "cluster_url": "",
        "poll_interval_seconds": 30,
        "request_timeout_seconds": 30,
        "retry_max_attempts": 3,
        "retry_backoff_base_seconds": 1,
    },
    "credential_fields": {
        "kubeconfig_data": {
            "type": "string",
            "description": "Base64-encoded kubeconfig file content",
            "required": False,
        },
        "service_account_token": {
            "type": "string",
            "description": "Kubernetes service account bearer token",
            "required": False,
        },
        "client_certificate_data": {
            "type": "string",
            "description": "Base64-encoded client certificate",
            "required": False,
        },
        "client_key_data": {
            "type": "string",
            "description": "Base64-encoded client key",
            "required": False,
        },
    },
    "tool_group": "infrastructure",
}

# ---------------------------------------------------------------------------
# 23. docker
# ---------------------------------------------------------------------------
DOCKER_INTEGRATION: dict[str, Any] = {
    "name": "Docker",
    "description": (
        "Docker container management integration. Builds, pulls, and "
        "manages container images and containers via the Docker API."
    ),
    "version": "1.0.0",
    "author": "Modulo",
    "tags": ["containers", "docker", "build", "deployment", "canonical"],
    "connector_type": "custom",
    "default_config": {
        "base_url": "",
        "api_version": "1.45",
        "registry_url": "",
    },
    "credential_fields": {
        "registry_username": {
            "type": "string",
            "description": "Container registry username",
            "required": False,
        },
        "registry_password": {
            "type": "string",
            "description": "Container registry password or token",
            "required": False,
        },
        "docker_config": {
            "type": "string",
            "description": "Base64-encoded Docker config.json with auths",
            "required": False,
        },
    },
    "tool_group": "containers",
}

# ---------------------------------------------------------------------------
# 24. circleci
# ---------------------------------------------------------------------------
CIRCLECI_INTEGRATION: dict[str, Any] = {
    "name": "CircleCI",
    "description": (
        "CircleCI CI/CD integration. Triggers pipeline runs, monitors job status, retrieves test results and artifacts."
    ),
    "version": "1.0.0",
    "author": "Modulo",
    "tags": ["ci-cd", "automation", "testing", "build", "canonical"],
    "connector_type": "circleci",
    "default_config": {
        "base_url": "https://circleci.com/api/v2",
        "project_slug": "",
        "branch": "main",
        "poll_interval_seconds": 15,
        "request_timeout_seconds": 30,
        "retry_max_attempts": 3,
        "retry_backoff_base_seconds": 1,
    },
    "credential_fields": {
        "token": {
            "type": "string",
            "description": "CircleCI personal API token",
            "required": True,
        },
    },
    "tool_group": "ci_cd",
}


# ---------------------------------------------------------------------------
# 25. generic-rest
# ---------------------------------------------------------------------------
REST_INTEGRATION: dict[str, Any] = {
    "name": "Generic REST",
    "description": (
        "Verb-agnostic generic HTTP integration. Point Modulo at an arbitrary "
        "REST endpoint (URL, method, headers, body, records extraction) and have "
        "pipeline nodes call it with runtime variables rendered into the request. "
        "No per-vendor client — just a templated HTTP call. Supports bearer, "
        "api_key (header or query) and basic auth, SSRF/allowlist guarding, "
        "response-size capping, and idempotent retry."
    ),
    "version": "1.0.0",
    "author": "Modulo",
    "tags": ["generic", "http", "rest", "integration", "canonical"],
    "connector_type": "rest",
    "default_config": {
        "base_url": "",
        "method": "GET",
        "path": "",
        "headers": {},
        "params": {},
        "body": {},
        "records_path": "",
        "next_cursor_path": "",
        "passthrough": False,
        "max_response_size": 10485760,
        "idempotency_header": "",
        "on_unknown": "fail_open",
        "allowed_hosts": [],
        "timeout_seconds": 30,
        "verify_tls": True,
    },
    # Structured operational-config metadata (FAR-466). This is the schema the
    # AdminConnectorsView REST form renders from: flat, discoverable fields are
    # first-class controls; genuinely templated/advanced fields remain a JSON
    # editor (advanced_fields). Authentication is deliberately separated into
    # its own credential payload (auth + credential_fields) — never conflated
    # with operational config.
    "config_schema": {
        "type": "object",
        "title": "Generic REST operational configuration",
        "description": (
            "Modelled on the AdminConnectorsView REST form: the form's flat, "
            "discoverable operational config fields are modelled on the "
            "``fields`` map below, and advanced/templated fields stay in the "
            "JSON editor (``advanced_fields``). Authentication is a separate "
            "credential payload (auth_mode + per-mode secret fields) — the form "
            "never conflates it with operational config. This schema is "
            "advisory documentation, not an authoritative renderer; the form is "
            "the consumer and a parity guard keeps the two in sync."
        ),
        "fields": {
            "base_url": {
                "type": "string",
                "default": "",
                "description": "Root URL of the REST endpoint (no trailing slash).",
            },
            "method": {
                "type": "string",
                "enum": ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"],
                "default": "GET",
            },
            "timeout_seconds": {
                "type": "integer",
                "default": 30,
                "description": "Per-request timeout in seconds.",
            },
            "verify_tls": {
                "type": "boolean",
                "default": True,
                "description": "Verify the TLS certificate of the target.",
            },
            "on_unknown": {
                "type": "string",
                "enum": ["fail_open", "fail_closed", "off"],
                "default": "fail_open",
                "description": (
                    "Behaviour when the response shape is unknown or no records can be extracted at records_path."
                ),
                "help": (
                    "fail_open: return whatever was fetched even if no records were "
                    "extracted (safe when a re-run can recover duplicates). "
                    "fail_closed: fail the run when records cannot be extracted (safe "
                    "when a silent miss is catastrophic). "
                    "off: no on-unknown handling — classic root-mapping only."
                ),
            },
            "records_path": {
                "type": "string",
                "default": "",
                "description": "JMESPath expression (e.g. data.items) locating the records list in the response.",
            },
            "allowed_hosts": {
                "type": "array",
                "items": {"type": "string"},
                "default": [],
                "description": "Egress/SSRF allowlist of hostnames permitted in rendered URLs.",
            },
        },
        # Authentication profile. Stored with the connector credentials (never in
        # operational config). The enum mirrors the REST connector's
        # ``_normalise_auth`` — a public/unauthenticated profile is not supported
        # by the connector, so no 'none' option is offered. Requiredness of the
        # per-mode secret fields is resolved per ``auth_mode`` by
        # ``_normalise_auth`` (a field is required only for its matching mode),
        # not by the static ``required`` flags in ``credential_fields``.
        "auth": {
            "description": "Authentication profile stored with the connector credentials.",
            "auth_mode": {
                "type": "string",
                "enum": ["bearer", "api_key", "basic"],
                "default": "bearer",
            },
            "credential_fields": {
                "bearer": ["token"],
                "basic": ["username", "password"],
                "api_key": ["api_key", "in", "header_name", "query_param_name"],
            },
        },
        "advanced_fields": [
            "path",
            "headers",
            "params",
            "body",
            "operations",
            "next_cursor_path",
            "passthrough",
            "max_response_size",
            "idempotency_header",
            "fan_out",
            "rate_limit",
        ],
    },
    "credential_fields": {
        "auth_mode": {
            "type": "string",
            "description": "Authentication mode: 'bearer', 'api_key', or 'basic'",
            "required": True,
        },
        "token": {
            "type": "string",
            "description": "Bearer token (used when auth_mode='bearer')",
            "required": False,
        },
        "api_key": {
            "type": "string",
            "description": "API key value (used when auth_mode='api_key')",
            "required": False,
        },
        "in": {
            "type": "string",
            "description": "Where to send the API key: 'header' or 'query'",
            "required": False,
        },
        "header_name": {
            "type": "string",
            "description": "Header name for a header-mode API key (default 'X-API-Key')",
            "required": False,
        },
        "query_param_name": {
            "type": "string",
            "description": "Query-parameter name for a query-mode API key (default 'api_key')",
            "required": False,
        },
        "username": {
            "type": "string",
            "description": "Basic auth username (used when auth_mode='basic')",
            "required": False,
        },
        "password": {
            "type": "string",
            "description": "Basic auth password (used when auth_mode='basic')",
            "required": False,
        },
    },
    "tool_group": "integration",
}
