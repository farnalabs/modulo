"""InferenceEngine — transforms ScanSample data into SDLC assessment findings.

Every finding carries evidence and a confidence level (high/medium/low).
Uncertainty is surfaced explicitly; gaps are preferred over fabrication.
"""

import uuid
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from modulo.determination.scanner import ScanSample, _repo_name

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

_PLANNING_STATUSES = {"backlog", "to do", "todo", "ready", "selected for development"}


@dataclass
class _InferenceData:
    """Aggregated sample data consumed by the per-stage finding builders."""

    repo_names: list[str] = field(default_factory=list)
    pull_requests: list[dict[str, Any]] = field(default_factory=list)
    issue_statuses: list[str] = field(default_factory=list)
    has_ci_config: bool = False
    pr_ages: list[float] = field(default_factory=list)
    stale_pr_count: int = 0


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


def _aggregate(samples: list[ScanSample]) -> _InferenceData:
    """Fold sampled connector records into a single :class:`_InferenceData`."""
    data = _InferenceData()
    for s in samples:
        match s.resource:
            case "repos" | "projects":
                _aggregate_repos(data, s.records)
            case "pulls" | "mrs":
                _aggregate_pull_requests(data, s.records)
            case "issues":
                _aggregate_issues(data, s.records)
    return data


def _aggregate_repos(data: _InferenceData, records: list[dict[str, Any]]) -> None:
    for rec in records:
        name = _repo_name(rec)
        if name:
            data.repo_names.append(name)
        desc = (rec.get("description") or "").lower()
        if any(ci in desc or ci in name.lower() for ci in _CI_FILES):
            data.has_ci_config = True


def _aggregate_pull_requests(data: _InferenceData, records: list[dict[str, Any]]) -> None:
    data.pull_requests.extend(records)
    for pr in records:
        created = pr.get("created_at") or pr.get("createdAt")
        days = _age_days(created)
        if days is not None:
            data.pr_ages.append(days)
            if days > 5:
                data.stale_pr_count += 1


def _aggregate_issues(data: _InferenceData, records: list[dict[str, Any]]) -> None:
    for iss in records:
        fields = iss.get("fields") or {}
        status_obj = fields.get("status")
        status = status_obj.get("name") if isinstance(status_obj, dict) else (status_obj or "")
        if not status:
            state = iss.get("state")
            status = state.get("name") if isinstance(state, dict) else (state or "")
        if status:
            data.issue_statuses.append(status.lower())


def _has_planning_status(data: _InferenceData) -> bool:
    return any(st in _PLANNING_STATUSES for st in data.issue_statuses)


def _find_planning_stage(data: _InferenceData) -> list[Finding]:
    """Planning stage: issues sitting in backlog/todo statuses."""
    if not _has_planning_status(data):
        return []
    planning_count = sum(1 for st in data.issue_statuses if st in _PLANNING_STATUSES)
    return [
        Finding(
            category="stage",
            finding="Planning stage detected: issues in backlog/todo statuses exist",
            evidence=f"{planning_count} issues in planning statuses",
            confidence="high",
            uncertainty="Status taxonomy varies by tool; mapped via common aliases",
        )
    ]


def _find_code_review(data: _InferenceData) -> list[Finding]:
    """Code review stage: open pull/merge requests were observed."""
    if not data.pull_requests:
        return []
    return [
        Finding(
            category="stage",
            finding="Code review stage detected: open pull/merge requests found",
            evidence=f"{len(data.pull_requests)} open PRs/MRs across repos",
            confidence="high",
        )
    ]


def _find_development(data: _InferenceData) -> list[Finding]:
    """Development stage: accessible source repositories were observed."""
    if not data.repo_names:
        return []
    repo_label = "repository" if len(data.repo_names) == 1 else "repositories"
    return [
        Finding(
            category="stage",
            finding="Development stage detected: source repositories found",
            evidence=f"{len(data.repo_names)} {repo_label} accessible",
            confidence="high",
        )
    ]


def _find_ci(data: _InferenceData) -> list[Finding]:
    """CI/CD automation: repo metadata referencing known CI tooling."""
    if data.has_ci_config:
        return [
            Finding(
                category="automation",
                finding="CI/CD configuration detected in repository metadata",
                evidence="Repository metadata references CI tooling (GitHub Actions, GitLab CI, Jenkins, CircleCI)",
                confidence="medium",
                uncertainty="Cannot verify CI is actively running; only config references were checked",
            )
        ]
    return [
        Finding(
            category="automation",
            finding="No CI/CD configuration detected in sampled repo metadata",
            evidence="Sampled repo metadata does not reference known CI tooling",
            confidence="low",
            uncertainty="CI config may exist in files not sampled; only repo metadata was scanned",
        )
    ]


def _find_stale_prs(data: _InferenceData) -> list[Finding]:
    """Review bottlenecks: open PRs/MRs older than five days."""
    if not data.pr_ages:
        return []
    avg_age = sum(data.pr_ages) / len(data.pr_ages)
    evidence_detail = (
        f"Average PR/MR age: {avg_age:.1f} days, {data.stale_pr_count}/{len(data.pr_ages)} open for >5 days"
    )
    if data.stale_pr_count > 0:
        return [
            Finding(
                category="bottleneck",
                finding=f"Potential review bottleneck: {data.stale_pr_count} PRs/MRs open for >5 days without merge",
                evidence=evidence_detail,
                confidence="medium",
                uncertainty="Cannot determine if PRs are waiting for review "
                "or intentionally long-lived (e.g., draft PRs, WIP)",
            )
        ]
    return [
        Finding(
            category="bottleneck",
            finding="No stale PRs detected — all sampled PRs/MRs are recent",
            evidence=evidence_detail,
            confidence="low",
            uncertainty="Small sample may miss long-lived PRs on other branches or repos",
        )
    ]


def _find_issue_lifecycle(data: _InferenceData) -> list[Finding]:
    """Issue lifecycle: observed statuses mapped to planning / non-planning."""
    if not data.issue_statuses:
        return []
    status_counts = Counter(data.issue_statuses)
    transitions = " → ".join(
        sorted(
            {st for st in data.issue_statuses if st not in _PLANNING_STATUSES}
            | {"planning" for st in data.issue_statuses if st in _PLANNING_STATUSES}
        )
    )
    return [
        Finding(
            category="transition",
            finding=f"Issue lifecycle observed: {transitions}",
            evidence=f"Issue statuses found: {dict(status_counts)}",
            confidence="medium",
            uncertainty="Cannot infer transition order or speed from a single scan; "
            "would need status change history or webhook events",
        )
    ]


def _find_overview(data: _InferenceData) -> list[Finding]:
    """Overall SDLC snapshot, or the honest gap signal when no stages matched."""
    stages_found = []
    if data.repo_names:
        stages_found.append("development")
    if _has_planning_status(data):
        stages_found.append("planning")
    if data.pull_requests:
        stages_found.append("code review")
    if data.has_ci_config:
        stages_found.append("ci/cd")

    if stages_found:
        return [
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
        ]
    return [
        Finding(
            category="overview",
            finding="No SDLC stages could be detected from connected tools",
            evidence="No connector produced stage-identifying records",
            confidence="low",
            uncertainty="Connectors may not be configured, or sampled data may not contain stage metadata",
        )
    ]


def infer(samples: list[ScanSample]) -> list[Finding]:
    """Run inference on a list of ScanSample objects and return findings."""
    data = _aggregate(samples)
    return [
        *_find_planning_stage(data),
        *_find_code_review(data),
        *_find_development(data),
        *_find_ci(data),
        *_find_stale_prs(data),
        *_find_issue_lifecycle(data),
        *_find_overview(data),
    ]
