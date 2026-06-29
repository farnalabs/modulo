"""Complexity-reviewer canonical library primitive.

A context-setter agent that analyses the most recent artifact in
run_context and estimates the model tier, token count, and complexity
reason.  This information is written back to run_context so downstream
nodes (model backend selection, routing, cost estimation) can make
informed decisions.

Usage:
    from modulo.core.library.complexity_reviewer import COMPLEXITY_REVIEWER

    # Import the primitive into a library entry:
    prim = LibraryPrimitive(
        source="local",
        primitive_type="agent",
        name="Complexity Reviewer",
        slug="complexity-reviewer",
        version="1.0.0",
        author="Modulo",
        content_json=COMPLEXITY_REVIEWER,
        tags=["context-setter", "canonical", "complexity", "cost"],
    )
"""

from __future__ import annotations

from typing import Any

COMPLEXITY_REVIEWER: dict[str, Any] = {
    "name": "Complexity Reviewer",
    "description": (
        "Analyses the most recent artifact in run_context and writes "
        "model_tier/estimated_tokens/complexity_reason to run_context. "
        "Use as a context-setter before the main processing agent to "
        "inform model selection and cost estimation."
    ),
    "node_type": "agent",
    "role": "context_setter",
    "prompt_template": (
        "You are a complexity reviewer for an agentic SDLC pipeline.\n\n"
        "Your task is to analyse the provided artifact and estimate:\n"
        "1. The appropriate model tier (tier-1, tier-2, or tier-3)\n"
        "2. The estimated token count for processing\n"
        "3. A brief complexity reason\n\n"
        "Artifact to review:\n"
        "---\n"
        "{artifact}\n"
        "---\n\n"
        "Respond with a JSON object containing:\n"
        "- model_tier: string ('tier-1' for simple formatting/parsing, "
        "'tier-2' for moderate analysis, 'tier-3' for complex reasoning)\n"
        "- estimated_tokens: integer (approximate token count)\n"
        "- complexity_reason: string (one-sentence justification)"
    ),
    "output_schema": {
        "type": "object",
        "required": ["model_tier", "estimated_tokens", "complexity_reason"],
        "properties": {
            "model_tier": {
                "type": "string",
                "enum": ["tier-1", "tier-2", "tier-3"],
                "description": "Model tier classification for the artifact",
            },
            "estimated_tokens": {
                "type": "integer",
                "minimum": 0,
                "description": "Estimated token count for processing",
            },
            "complexity_reason": {
                "type": "string",
                "maxLength": 500,
                "description": "Brief justification for the complexity assessment",
            },
        },
    },
    "run_context_writes": [
        "model_tier",
        "estimated_tokens",
        "complexity_reason",
    ],
    "tags": ["context-setter", "canonical", "complexity", "cost"],
}
