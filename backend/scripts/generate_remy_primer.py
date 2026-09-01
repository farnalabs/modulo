"""Auto-generate the product primer Markdown for Remy's system prompt.

Reads:
  - docs/prd.md §5 (glossary, OPTIONAL/legacy) — if present, key concepts and one-line
     definitions; otherwise the built-in glossary is used
  - frontend/src/manifest.yaml — sidebar groups, route names
  - Live DB counts — pipelines, connectors, model backends
  - Live user/org profile — display_name, role, org_name, plan_name

Usage:
    python backend/scripts/generate_remy_primer.py
    python backend/scripts/generate_remy_primer.py --org-id <uuid> --user-id <uuid>
    python backend/scripts/generate_remy_primer.py --org-id <uuid> --user-id <uuid> --output primer.md
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import re
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from modulo.core.manifest import get_manifest
from modulo.db.models import (
    Account,
    ConnectorInstance,
    ModelBackend,
    Organisation,
    OrgMembership,
    Pipeline,
)
from modulo.settings import get_settings

_log = logging.getLogger(__name__)


def _safe_output_path(path: Path) -> Path:
    """Resolve *path* and require it to stay within the working directory."""
    resolved = os.path.realpath(str(path))
    base = os.path.realpath(Path.cwd())
    if resolved != base and not resolved.startswith(base + os.sep):
        raise ValueError(f"output path {str(path)!r} resolves outside the working directory")
    return Path(resolved)


# ---------------------------------------------------------------------------
# 1. Glossary parsing - PRD §5 (optional; falls back to built-in glossary)
# ---------------------------------------------------------------------------

_GLOSSARY_TERMS: dict[str, str] = {
    "Pipeline": "An ordered graph of agents with explicit ConnectorBindings, executed as Runs.",
    "Run": "A single execution against a PipelineSnapshot, with unique ID, trace, cost record, and result.",
    "Agent": (
        "An atomic unit of work that takes defined input, applies a sandboxed prompt against a model backend, "
        "and produces defined output."
    ),
    "Schema": "A versioned, reusable data structure definition that users control.",
    "Connector": "A configured, authenticated binding to an external system such as a git host or issue tracker.",
    "Trigger": "A first-class object that initiates a pipeline run (manual, webhook, cron, polling).",
    "HITL Gate": "A Human-in-the-Loop transition point that pauses execution until a human approves or rejects.",
    "Eval": "An automated quality check on agent output (llm_judge, regex, json_schema, custom_function).",
    "Variant": (
        "A set of runs against the same pipeline and input differing only in context overrides, used for A/B testing."
    ),
    "Library": "Published community primitives — schemas, workflows, agents, and integrations with metadata.",
    "Remy": (
        "Your AI SDLC assistant that can navigate the UI, trigger runs, review results, approve HITL gates, "
        "and configure settings."
    ),
}

_KEY_CONCEPTS = [
    "Pipeline",
    "Run",
    "Agent",
    "Schema",
    "Connector",
    "Trigger",
    "HITL Gate",
    "Eval",
    "Variant",
    "Library",
    "Remy",
]


def _load_prd_glossary(prd_path: Path) -> dict[str, str]:
    """Parse the §5 glossary table from an OPTIONAL prd.md at docs/prd.md (legacy).

    If absent, returns the built-in glossary (_GLOSSARY_TERMS).
    """
    if not prd_path.exists():
        return dict(_GLOSSARY_TERMS)

    text = prd_path.read_text(encoding="utf-8")

    # Find the ## 5. Core Concepts & Glossary section
    m = re.search(r"## 5\. Core Concepts & Glossary\s*\n(.*?)(?=\n## \d)", text, re.DOTALL)
    if not m:
        return dict(_GLOSSARY_TERMS)

    section = m.group(1)
    terms: dict[str, str] = {}
    for raw_line in section.splitlines():
        line = raw_line.strip()
        if not line.startswith("| **"):
            continue
        parts = [p.strip() for p in line.split("|") if p.strip()]
        if len(parts) >= 2:
            term = parts[0].removeprefix("**").removesuffix("**").strip()
            definition = parts[1].strip()
            if term and definition:
                terms[term] = definition

    return terms or dict(_GLOSSARY_TERMS)


# ---------------------------------------------------------------------------
# 2.  Manifest / navigation
# ---------------------------------------------------------------------------

_SIDEBAR_GROUP_MAP: dict[str, str] = {}


def _load_navigation() -> str:
    """Build the Pages & Navigation section from manifest.yaml sidebar groups."""
    manifest = get_manifest()
    if not manifest:
        return ""

    groups = manifest.get("sidebar_groups", {})
    routes = manifest.get("routes", {})

    # Build group -> [page_name, ...]
    group_pages: dict[str, list[tuple[int, str]]] = {}
    for path, route in routes.items():
        sg = route.get("sidebar_group")
        if not sg:
            continue
        name = route.get("breadcrumb", route.get("name", path))
        order = route.get("sidebar_order", 999)
        if sg not in group_pages:
            group_pages[sg] = []
        group_pages[sg].append((order, name))

    lines: list[str] = []
    sorted_groups = sorted(groups.items(), key=lambda x: x[1].get("order", 999))
    for group_id, group_info in sorted_groups:
        label = group_info.get("label", group_id)
        pages = group_pages.get(group_id, [])
        pages.sort(key=lambda x: x[0])
        page_names = [p[1] for p in pages]
        if page_names:
            lines.append(f"- **{label}** — {', '.join(page_names)}")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 3.  DB queries
# ---------------------------------------------------------------------------


async def _get_org_counts(
    session: AsyncSession,
    org_id: str,
) -> dict[str, int]:
    """Query live counts for pipelines, connectors, and model backends."""
    models_and_keys: list[tuple[type, str]] = [
        (Pipeline, "pipelines"),
        (ConnectorInstance, "connectors"),
        (ModelBackend, "model_backends"),
    ]
    counts: dict[str, int] = {}
    for model, key in models_and_keys:
        try:
            stmt = select(func.count()).select_from(model).where(model.organisation_id == org_id)
            result = await session.execute(stmt)
            counts[key] = result.scalar() or 0
        except Exception:
            counts[key] = 0

    return counts


async def _get_user_context(
    session: AsyncSession,
    org_id: str,
    user_id: str,
) -> dict[str, str]:
    """Fetch active user and org context for the primer."""
    ctx: dict[str, str] = {}

    try:
        result = await session.execute(select(Account).where(Account.id == user_id))
        account = result.scalar_one_or_none()
        if account:
            ctx["display_name"] = account.display_name
    except Exception:
        ctx["display_name"] = "User"

    try:
        result = await session.execute(
            select(OrgMembership).where(
                OrgMembership.account_id == user_id,
                OrgMembership.organisation_id == org_id,
            )
        )
        membership = result.scalar_one_or_none()
        if membership:
            ctx["role"] = membership.role
    except Exception:
        ctx["role"] = "unknown"

    try:
        result = await session.execute(select(Organisation).where(Organisation.id == org_id))
        org = result.scalar_one_or_none()
        if org:
            ctx["org_name"] = org.name
            ctx["plan_name"] = org.plan_id or "Community"
    except Exception:
        ctx["org_name"] = "Your Organisation"
        ctx["plan_name"] = "Community"

    return ctx


async def _get_active_context(
    org_id: str | None,
    user_id: str | None,
) -> dict[str, str]:
    """Try to query DB for context; return defaults on failure."""
    ctx: dict[str, str] = {
        "display_name": "User",
        "role": "admin",
        "org_name": "Your Organisation",
        "plan_name": "Community",
        "pipelines": "0",
        "connectors": "0",
        "model_backends": "0",
    }

    if not org_id or not user_id:
        return ctx

    try:
        settings = get_settings()
        engine = create_async_engine(settings.database_url)
        async with AsyncSession(engine) as session:
            counts = await _get_org_counts(session, org_id)
            ctx["pipelines"] = str(counts.get("pipelines", 0))
            ctx["connectors"] = str(counts.get("connectors", 0))
            ctx["model_backends"] = str(counts.get("model_backends", 0))

            user_ctx = await _get_user_context(session, org_id, user_id)
            ctx.update(user_ctx)
    except Exception:
        _log.warning("Failed to query DB context for primer", exc_info=True)

    return ctx


# ---------------------------------------------------------------------------
# 4.  Markdown generation
# ---------------------------------------------------------------------------

_ABOUT_MODULO = (
    "Modulo is an agent governance platform for the software development "
    "lifecycle. It lets teams build, run, and evaluate multi-agent pipelines that "
    "automate SDLC workflows — from code review and testing to deployment and "
    "monitoring — all within a single interface with role-based access control, "
    "audit logging, and cost management."
)


def _estimate_tokens(text: str) -> int:
    """Rough token estimate (words x 1.3)."""
    word_count = len(text.split())
    return int(word_count * 1.3)


_MAX_PRIMER_TOKENS = 800


def _truncate_primer(primer: str, max_tokens: int = _MAX_PRIMER_TOKENS) -> str:
    """Truncate from bottom sections to stay within token budget.

    Truncation order:
      1. Navigation section (first)
      2. Key Concepts section
    The overview section is never truncated.
    """
    tokens = _estimate_tokens(primer)
    if tokens <= max_tokens:
        return primer

    parts = primer.split("\n\n## ")
    if len(parts) <= 2:
        return primer

    # Keep "What is Modulo" (part[0]) and Key Concepts, drop navigation
    overview = parts[0]
    remaining = parts[1:]

    # Try dropping navigation last; if still over, drop key concepts too
    key_concepts_section = None
    active_context_section = None
    other_sections: list[str] = []

    for p in remaining:
        if p.startswith("Key Concepts"):
            key_concepts_section = p
        elif p.startswith("Pages & Navigation"):
            pass
        elif p.startswith("Active Context"):
            active_context_section = p
        else:
            other_sections.append(p)

    candidate = overview
    if key_concepts_section and _estimate_tokens(overview) < max_tokens:
        candidate = f"{overview}\n\n## {key_concepts_section}"

    if _estimate_tokens(candidate) <= max_tokens and active_context_section:
        check = f"{candidate}\n\n## {active_context_section}"
        if _estimate_tokens(check) <= max_tokens:
            candidate = check

    if _estimate_tokens(candidate) > max_tokens:
        # Still over — trim key concepts definitions to one-liners
        lines = candidate.splitlines()
        trimmed: list[str] = []
        in_concepts = False
        for line in lines:
            if line.startswith("## Key Concepts"):
                in_concepts = True
                trimmed.append(line)
            elif in_concepts:
                if line.startswith("## "):
                    in_concepts = False
                    trimmed.append(line)
                elif line.startswith("- **"):
                    term_match = re.match(r"- \*\*(\w+)\*\*", line)
                    if term_match:
                        trimmed.append(f"- {term_match.group(1)}")
                # skip definition lines
            else:
                trimmed.append(line)
        candidate = "\n".join(trimmed)

    return candidate if _estimate_tokens(candidate) <= max_tokens else overview


def _render_key_concepts(glossary: dict[str, str]) -> str:
    """Render one-line definitions for the 11 key concepts."""
    lines: list[str] = []
    for term in _KEY_CONCEPTS:
        definition = glossary.get(term, _GLOSSARY_TERMS.get(term, ""))
        if not definition:
            continue
        # Extract first sentence (split on ". " to keep abbreviations intact)
        first_sentence = definition.split(". ")[0].strip()
        first_sentence = first_sentence.removesuffix(".") + "."
        lines.append(f"- **{term}** — {first_sentence}")
    return "\n".join(lines)


def _build_about_section() -> str:
    return _ABOUT_MODULO


def _build_active_context(ctx: dict[str, str]) -> str:
    lines: list[str] = []
    lines.append(f"- **User:** {ctx.get('display_name', 'User')}")
    lines.append(f"- **Role:** {ctx.get('role', 'admin')}")
    lines.append(f"- **Organisation:** {ctx.get('org_name', 'Your Organisation')}")
    lines.append(f"- **Plan:** {ctx.get('plan_name', 'Community')}")
    lines.append(f"- **Pipelines:** {ctx.get('pipelines', '0')}")
    lines.append(f"- **Connectors:** {ctx.get('connectors', '0')}")
    lines.append(f"- **Model Backends:** {ctx.get('model_backends', '0')}")
    return "\n".join(lines)


async def generate_primer(
    org_id: str | None = None,
    user_id: str | None = None,
) -> str:
    """Generate the complete product primer Markdown string."""
    # Resolve project root
    script_dir = Path(__file__).resolve().parent
    backend_dir = script_dir.parent  # backend/
    project_root = backend_dir.parent  # project root

    # Glossary
    # Optional legacy PRD glossary at docs/prd.md; falls back to built-in _GLOSSARY_TERMS if missing.
    prd_path = project_root / "docs" / "prd.md"
    glossary = _load_prd_glossary(prd_path)

    # Navigation
    nav = _load_navigation()

    # Active context
    ctx = await _get_active_context(org_id, user_id)

    sections: list[str] = []

    # Section 1 — What is Modulo
    sections.append(_build_about_section())

    # Section 2 — Key Concepts
    sections.append(f"## Key Concepts\n\n{_render_key_concepts(glossary)}")

    # Section 3 — Pages & Navigation
    if nav:
        sections.append(f"## Pages & Navigation\n\n{nav}")

    # Section 4 — Active Context
    sections.append(f"## Active Context\n\n{_build_active_context(ctx)}")

    primer = "\n\n".join(sections)

    # Enforce token budget
    return _truncate_primer(primer)


# ---------------------------------------------------------------------------
# 5.  CLI
# ---------------------------------------------------------------------------


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate the Remy product primer Markdown.",
    )
    parser.add_argument(
        "--org-id",
        type=str,
        default=None,
        help="Organisation UUID for live DB counts and context.",
    )
    parser.add_argument(
        "--user-id",
        type=str,
        default=None,
        help="User (account) UUID for live user context.",
    )
    parser.add_argument(
        "--output",
        type=str,
        default=None,
        help="Write primer to file instead of stdout.",
    )
    return parser.parse_args(argv)


async def main() -> None:
    args = parse_args()
    primer = await generate_primer(
        org_id=args.org_id,
        user_id=args.user_id,
    )

    if args.output:
        out_path = _safe_output_path(Path(args.output))
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(primer, encoding="utf-8")
    else:
        print(primer)


if __name__ == "__main__":
    asyncio.run(main())
