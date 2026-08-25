"""Pipeline draft generator — converts determination findings into an editable pipeline graph."""

from dataclasses import dataclass, field

from modulo.connectors.base import Capability
from modulo.determination.inference import Finding
from modulo.determination.scanner import ScanSample


@dataclass
class DraftNode:
    """A node in the draft pipeline."""

    id: str
    node_type: str
    label: str
    connector_type: str | None = None
    required_capabilities: list[str] = field(default_factory=list)


@dataclass
class DraftEdge:
    """An edge in the draft pipeline."""

    source: str
    target: str
    edge_type: str = "normal"
    hitl_gate: bool = False


@dataclass
class PipelineDraft:
    """A full draft pipeline generated from determination data."""

    nodes: list[DraftNode]
    edges: list[DraftEdge]
    findings: list[Finding]
    automation_suggestions: list[dict[str, str]]


def _has_sampled_data(samples: list[ScanSample]) -> bool:
    """Return True when any scanned sample carries a recognised SDLC data resource."""
    sampled = {s.resource for s in samples if s.records}
    data_resources = {"repos", "projects", "issues", "pulls", "mrs"}
    return bool(sampled & data_resources)


def _detect_stages(findings: list[Finding]) -> tuple[bool, bool, bool, bool]:
    """Resolve which pipeline stages are implied by the inference findings."""
    has_planning = any(f.category == "stage" and "Planning" in f.finding for f in findings)
    has_development = any(f.category == "stage" and "Development" in f.finding for f in findings)
    has_review = any(f.category == "stage" and "Code review" in f.finding for f in findings)
    has_ci = any(
        f.category == "automation" and "CI/CD configuration detected in repository metadata" in f.finding
        for f in findings
    )
    return has_planning, has_development, has_review, has_ci


def _git_provider(samples: list[ScanSample], resources: tuple[str, ...]) -> str:
    """Pick the dominant git connector type from samples of the given resources."""
    git_providers = {s.connector_type.value for s in samples if s.resource in resources}
    return next(iter(git_providers), "github")


def _add_planning_stage(
    nodes: list[DraftNode],
    edges: list[DraftEdge],
    automation_suggestions: list[dict[str, str]],
    stage_node_ids: list[str],
) -> None:
    nodes.append(DraftNode(id="planning", node_type="manual", label="Planning (Ticket Triage)"))
    stage_node_ids.append("planning")
    automation_suggestions.append(
        {
            "stage": "planning",
            "suggestion": "Auto-assign issues to team members based on workload and expertise",
            "connector_type": "jira",
        }
    )


def _add_development_stage(
    samples: list[ScanSample],
    nodes: list[DraftNode],
    edges: list[DraftEdge],
    stage_node_ids: list[str],
    has_planning: bool,
) -> None:
    connector_type = _git_provider(samples, ("repos", "projects"))
    nodes.append(
        DraftNode(
            id="development",
            node_type="agent",
            label="Development (Code Generation)",
            connector_type=connector_type,
            required_capabilities=[Capability.READ, Capability.WRITE],
        )
    )
    stage_node_ids.append("development")
    if has_planning:
        edges.append(DraftEdge(source="planning", target="development", hitl_gate=True))


def _add_review_stage(
    samples: list[ScanSample],
    nodes: list[DraftNode],
    edges: list[DraftEdge],
    automation_suggestions: list[dict[str, str]],
    stage_node_ids: list[str],
    has_development: bool,
) -> None:
    connector_type = _git_provider(samples, ("pulls", "mrs"))
    nodes.append(
        DraftNode(
            id="review",
            node_type="agent",
            label="Code Review",
            connector_type=connector_type,
            required_capabilities=[Capability.READ, Capability.CREATE_PR],
        )
    )
    if has_development:
        review_source = "development"
    elif stage_node_ids:
        review_source = stage_node_ids[-1]
    else:
        review_source = "start"
    stage_node_ids.append("review")
    edges.append(DraftEdge(source=review_source, target="review", hitl_gate=True))
    automation_suggestions.append(
        {
            "stage": "review",
            "suggestion": "Auto-request reviews from matching code owners based on changed files",
            "connector_type": connector_type,
        }
    )


def _add_ci_stage(
    nodes: list[DraftNode],
    edges: list[DraftEdge],
    stage_node_ids: list[str],
) -> None:
    nodes.append(
        DraftNode(
            id="ci_cd",
            node_type="agent",
            label="CI/CD Pipeline",
            required_capabilities=[Capability.READ],
        )
    )
    stage_node_ids.append("ci_cd")
    ci_source = stage_node_ids[-2] if len(stage_node_ids) > 1 else "start"
    edges.append(DraftEdge(source=ci_source, target="ci_cd"))


def generate_draft(samples: list[ScanSample], findings: list[Finding]) -> PipelineDraft:
    """Generate an editable pipeline draft from scanned data and inference findings."""
    if not _has_sampled_data(samples):
        return PipelineDraft(nodes=[], edges=[], findings=findings, automation_suggestions=[])

    nodes: list[DraftNode] = [DraftNode(id="start", node_type="placeholder", label="Start")]
    edges: list[DraftEdge] = []
    automation_suggestions: list[dict[str, str]] = []
    stage_node_ids: list[str] = []

    has_planning, has_development, has_review, has_ci = _detect_stages(findings)

    if has_planning:
        _add_planning_stage(nodes, edges, automation_suggestions, stage_node_ids)
    if has_development:
        _add_development_stage(samples, nodes, edges, stage_node_ids, has_planning)
    if has_review:
        _add_review_stage(samples, nodes, edges, automation_suggestions, stage_node_ids, has_development)
    if has_ci:
        _add_ci_stage(nodes, edges, stage_node_ids)

    nodes.append(DraftNode(id="end", node_type="placeholder", label="End"))

    if stage_node_ids:
        edges.append(DraftEdge(source=stage_node_ids[-1], target="end"))
    if not edges:
        edges.append(DraftEdge(source="start", target="end"))

    return PipelineDraft(
        nodes=nodes,
        edges=edges,
        findings=findings,
        automation_suggestions=automation_suggestions,
    )
