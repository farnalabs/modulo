"""InferenceEngine — transforms ScanSample data into SDLC assessment findings.

Every finding carries evidence and a confidence level (high/medium/low).
Uncertainty is surfaced explicitly; gaps are preferred over fabrication.
"""

import uuid
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from modulo.determination.scanner import ScanSample

_VALID_CONFIDENCES = frozenset({"high", "medium", "low"})


@dataclass
class Finding:
    """A single finding about the SDLC."""

    category: str
    finding: str
    evidence: str
    confidence: str
    uncertainty: str = ""
    related_connector: uuid.UUID | None = None

    def __post_init__(self) -> None:
        if self.confidence not in _VALID_CONFIDENCES:
            raise ValueError(f"confidence must be one of {sorted(_VALID_CONFIDENCES)}, got {self.confidence!r}")


_CI_FILES = {
    ".github/workflows",
    ".gitlab-ci.yml",
    "Jenkinsfile",
    "circleci",
    ".circleci",
    "azure-pipelines",
}


def _age_days(value: Any) -> float | None:
    """Parse ISO datetime string and return days since then.

    Naive timestamps (no timezone suffix) are interpreted as UTC so that
    connectors returning timezone-less ISO strings cannot crash inference.
    """
    if not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return (datetime.now(UTC) - dt).total_seconds() / 86400


def infer(samples: list[ScanSample]) -> list[Finding]:
    """Run inference on a list of ScanSample objects and return findings."""
    findings: list[Finding] = []

    repo_names: list[str] = []
    pull_requests: list[dict[str, Any]] = []
    issue_statuses: list[str] = []
    has_ci_config = False
    has_planning_stage = False
    pr_ages: list[float] = []
    stale_pr_count = 0

    for s in samples:
        match s.resource:
            case "repos" | "projects":
                for rec in s.records:
                    name = rec.get("name") or rec.get("path_with_namespace") or rec.get("full_name") or ""
                    if name:
                        repo_names.append(name)

            case "pulls" | "mrs":
                pull_requests.extend(s.records)
                for pr in s.records:
                    created = pr.get("created_at") or pr.get("createdAt")
                    days = _age_days(created)
                    if days is not None:
                        pr_ages.append(days)
                        if days > 5:
                            stale_pr_count += 1

            case "issues":
                for iss in s.records:
                    fields = iss.get("fields") or {}
                    status_obj = fields.get("status")
                    status = status_obj.get("name") if isinstance(status_obj, dict) else (status_obj or "")
                    if not status:
                        state = iss.get("state")
                        status = state.get("name") if isinstance(state, dict) else (state or "")
                    if status:
                        issue_statuses.append(status.lower())

    # --- Stage: Planning ---
    planning_statuses = {"backlog", "to do", "todo", "ready", "selected for development"}
    if any(st in planning_statuses for st in issue_statuses):
        has_planning_stage = True
        planning_count = sum(1 for st in issue_statuses if st in planning_statuses)
        findings.append(
            Finding(
                category="stage",
                finding="Planning stage detected: issues in backlog/todo statuses exist",
                evidence=f"{planning_count} issues in planning statuses",
                confidence="high",
                uncertainty="Status taxonomy varies by tool; mapped via common aliases",
            )
        )

    # --- Stage: Code Review ---
    if pull_requests:
        findings.append(
            Finding(
                category="stage",
                finding="Code review stage detected: open pull/merge requests found",
                evidence=f"{len(pull_requests)} open PRs/MRs across repos",
                confidence="high",
            )
        )

    # --- Stage: Development ---
    if repo_names:
        findings.append(
            Finding(
                category="stage",
                finding="Development stage detected: source repositories found",
                evidence=f"{len(repo_names)} {'repository' if len(repo_names) == 1 else 'repositories'} accessible",
                confidence="high",
            )
        )

    # --- Stage: CI/CD ---
    for s in samples:
        if s.resource in ("repos", "projects"):
            for rec in s.records:
                desc = (rec.get("description") or "").lower()
                name = (rec.get("name") or rec.get("path_with_namespace") or rec.get("full_name") or "").lower()
                if any(ci in desc or ci in name for ci in _CI_FILES):
                    has_ci_config = True
                    break

    if has_ci_config:
        findings.append(
            Finding(
                category="automation",
                finding="CI/CD configuration detected in repository metadata",
                evidence="Repository metadata references CI tooling (GitHub Actions, GitLab CI, Jenkins, CircleCI)",
                confidence="medium",
                uncertainty="Cannot verify CI is actively running; only config references were checked",
            )
        )
    else:
        findings.append(
            Finding(
                category="automation",
                finding="No CI/CD configuration detected in sampled repo metadata",
                evidence="Sampled repo metadata does not reference known CI tooling",
                confidence="low",
                uncertainty="CI config may exist in files not sampled; only repo metadata was scanned",
            )
        )

    # --- Bottleneck: Stale PRs ---
    if pr_ages:
        avg_age = sum(pr_ages) / len(pr_ages)
        evidence_detail = f"Average PR/MR age: {avg_age:.1f} days, {stale_pr_count}/{len(pr_ages)} open for >5 days"
        if stale_pr_count > 0:
            findings.append(
                Finding(
                    category="bottleneck",
                    finding=f"Potential review bottleneck: {stale_pr_count} PRs/MRs open for >5 days without merge",
                    evidence=evidence_detail,
                    confidence="medium",
                    uncertainty="Cannot determine if PRs are waiting for review "
                    "or intentionally long-lived (e.g., draft PRs, WIP)",
                )
            )
        else:
            findings.append(
                Finding(
                    category="bottleneck",
                    finding="No stale PRs detected — all sampled PRs/MRs are recent",
                    evidence=evidence_detail,
                    confidence="low",
                    uncertainty="Small sample may miss long-lived PRs on other branches or repos",
                )
            )

    # --- Transition: Issue lifecycle ---
    if issue_statuses:
        status_counts = Counter(issue_statuses)
        transitions = " → ".join(
            sorted(
                {st for st in issue_statuses if st not in planning_statuses}
                | {"planning" for st in issue_statuses if st in planning_statuses}
            )
        )
        findings.append(
            Finding(
                category="transition",
                finding=f"Issue lifecycle observed: {transitions}",
                evidence=f"Issue statuses found: {dict(status_counts)}",
                confidence="medium",
                uncertainty="Cannot infer transition order or speed from a single scan; "
                "would need status change history or webhook events",
            )
        )

    # --- Overall assessment ---
    stages_found = []
    if repo_names:
        stages_found.append("development")
    if has_planning_stage:
        stages_found.append("planning")
    if pull_requests:
        stages_found.append("code review")
    if has_ci_config:
        stages_found.append("ci/cd")

    if stages_found:
        findings.append(
            Finding(
                category="overview",
                finding=f"SDLC stages detected: {', '.join(stages_found)}",
                evidence=f"Out of 5 common stages (planning, development, "
                f"code review, ci/cd, deployment), {len(stages_found)} "
                f"{'was' if len(stages_found) == 1 else 'were'} detected",
                confidence="medium",
                uncertainty="Deployment stage cannot be assessed "
                "without deployment tool connector; "
                "monitoring/incident stages not detectable from sampled data",
            )
        )
    else:
        findings.append(
            Finding(
                category="overview",
                finding="No SDLC stages could be detected from connected tools",
                evidence="No connector produced stage-identifying records",
                confidence="low",
                uncertainty="Connectors may not be configured, or sampled data may not contain stage metadata",
            )
        )

    return findings
