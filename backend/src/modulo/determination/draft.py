"""Pipeline draft generator — converts determination findings into an editable pipeline graph."""

from modulo.connectors.base import Capability
from modulo.determination.inference import Finding
from modulo.determination.scanner import ScanSample


class DraftNode:
    """A node in the draft pipeline."""

    def __init__(
        self,
        id: str,
        node_type: str,
        label: str,
        connector_type: str | None = None,
        required_capabilities: list[str] | None = None,
    ) -> None:
        self.id = id
        self.node_type = node_type
        self.label = label
        self.connector_type = connector_type
        self.required_capabilities = required_capabilities or []


class DraftEdge:
    """An edge in the draft pipeline."""

    def __init__(
        self,
        source: str,
        target: str,
        edge_type: str = "normal",
        hitl_gate: bool = False,
    ) -> None:
        self.source = source
        self.target = target
        self.edge_type = edge_type
        self.hitl_gate = hitl_gate


class PipelineDraft:
    """A full draft pipeline generated from determination data."""

    def __init__(
        self,
        nodes: list[DraftNode],
        edges: list[DraftEdge],
        findings: list[Finding],
        automation_suggestions: list[dict[str, str]],
    ) -> None:
        self.nodes = nodes
        self.edges = edges
        self.findings = findings
        self.automation_suggestions = automation_suggestions


def generate_draft(samples: list[ScanSample], findings: list[Finding]) -> PipelineDraft:
    """Generate an editable pipeline draft from scanned data and inference findings."""
    nodes: list[DraftNode] = []
    edges: list[DraftEdge] = []
    automation_suggestions: list[dict[str, str]] = []

    sampled = {s.resource for s in samples if s.records}
    has_data = bool(sampled & {"repos", "projects", "issues", "pulls", "mrs"})
    if not has_data:
        return PipelineDraft(nodes=[], edges=[], findings=findings, automation_suggestions=[])

    stage_nodes = []
    has_planning = any(f.category == "stage" and "Planning" in f.finding for f in findings)
    has_development = any(f.category == "stage" and "Development" in f.finding for f in findings)
    has_review = any(f.category == "stage" and "Code review" in f.finding for f in findings)
    has_ci = any(f.category == "automation" and "CI/CD" in f.finding for f in findings)

    start_node = DraftNode(id="start", node_type="placeholder", label="Start")
    nodes.append(start_node)

    if has_planning:
        n = DraftNode(
            id="planning",
            node_type="manual",
            label="Planning (Ticket Triage)",
        )
        nodes.append(n)
        stage_nodes.append("planning")

        automation_suggestions.append(
            {
                "stage": "planning",
                "suggestion": "Auto-assign issues to team members based on workload and expertise",
                "connector_type": ("jira" if any(s.connector_type.value == "jira" for s in samples) else "linear"),
            }
        )

    if has_development:
        # Determine which connector types are available
        git_providers = {s.connector_type.value for s in samples if s.resource in ("repos", "projects")}
        connector_type = next(iter(git_providers), "github")

        n = DraftNode(
            id="development",
            node_type="agent",
            label="Development (Code Generation)",
            connector_type=connector_type,
            required_capabilities=[Capability.READ, Capability.WRITE],
        )
        nodes.append(n)
        stage_nodes.append("development")

        if has_planning:
            edges.append(DraftEdge(source="planning", target="development", hitl_gate=True))

    if has_review:
        git_providers = {s.connector_type.value for s in samples if s.resource in ("pulls", "mrs")}
        connector_type = next(iter(git_providers), "github")

        n = DraftNode(
            id="review",
            node_type="agent",
            label="Code Review",
            connector_type=connector_type,
            required_capabilities=[Capability.READ, Capability.CREATE_PR],
        )
        nodes.append(n)
        stage_nodes.append("review")

        if has_development:
            review_source = "development"
        elif len(stage_nodes) > 0:
            review_source = stage_nodes[-2]
        else:
            review_source = "start"
        edges.append(DraftEdge(source=review_source, target="review", hitl_gate=True))

        automation_suggestions.append(
            {
                "stage": "review",
                "suggestion": "Auto-request reviews from matching code owners based on changed files",
                "connector_type": connector_type,
            }
        )

    if has_ci:
        n = DraftNode(
            id="ci_cd",
            node_type="agent",
            label="CI/CD Pipeline",
            required_capabilities=[Capability.READ],
        )
        nodes.append(n)
        stage_nodes.append("ci_cd")

        ci_source = stage_nodes[-2] if len(stage_nodes) > 1 else "start"
        edges.append(DraftEdge(source=ci_source, target="ci_cd"))

    end_node = DraftNode(id="end", node_type="placeholder", label="End")
    nodes.append(end_node)

    if stage_nodes:
        last_stage = stage_nodes[-1]
        edges.append(DraftEdge(source=last_stage, target="end"))

    if not edges:
        edges.append(DraftEdge(source="start", target="end"))

    return PipelineDraft(
        nodes=nodes,
        edges=edges,
        findings=findings,
        automation_suggestions=automation_suggestions,
    )
