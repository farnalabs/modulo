"""21 canonical library agent primitives.

Each dict follows the pattern established in
:mod:`modulo.core.library.complexity_reviewer` — a
:class:`dict[str, Any]` that supplies all the metadata,
prompt template, input/output schemas, and tags needed to
register a :class:`~modulo.db.models.library_primitive.LibraryPrimitive`.
"""

from __future__ import annotations

from typing import Any

# ---------------------------------------------------------------------------
# 1. ticket-triager
# ---------------------------------------------------------------------------
TICKET_TRIAGER: dict[str, Any] = {
    "name": "Ticket Triager",
    "description": (
        "Analyses incoming tickets, categorises by type (bug/feature/enhancement), "
        "determines priority, and suggests labels. Falls back to a general "
        "classification when the input is ambiguous."
    ),
    "node_type": "agent",
    "role": "triage",
    "prompt_template": (
        "You are a ticket triager for an agentic SDLC pipeline.\n\n"
        "Analyse the following ticket and classify it.\n\n"
        "Title: {title}\n"
        "Description: {description}\n\n"
        "Respond with a JSON object containing:\n"
        "- type: string — one of 'bug', 'feature', 'enhancement', 'documentation', 'question'\n"
        "- priority: string — one of 'critical', 'high', 'medium', 'low'\n"
        "- suggested_labels: array of strings — relevant tags\n"
        "- summary: string — one-sentence summary of the ticket"
    ),
    "input_schema": {
        "type": "object",
        "required": ["title", "description"],
        "properties": {
            "title": {"type": "string", "description": "Ticket title"},
            "description": {"type": "string", "description": "Ticket body / description"},
        },
    },
    "output_schema": {
        "type": "object",
        "required": ["type", "priority", "suggested_labels", "summary"],
        "properties": {
            "type": {
                "type": "string",
                "enum": ["bug", "feature", "enhancement", "documentation", "question"],
                "description": "Categorisation of the ticket",
            },
            "priority": {
                "type": "string",
                "enum": ["critical", "high", "medium", "low"],
                "description": "Assigned priority",
            },
            "suggested_labels": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Suggested label tags",
            },
            "summary": {
                "type": "string",
                "description": "One-sentence summary of the ticket",
            },
        },
    },
    "tags": ["triage", "canonical", "library", "ticket"],
    "version": "1.0.0",
    "author": "Modulo",
}

# ---------------------------------------------------------------------------
# 2. compliance-checker
# ---------------------------------------------------------------------------
COMPLIANCE_CHECKER: dict[str, Any] = {
    "name": "Compliance Checker",
    "description": (
        "Reviews code or configuration for compliance with regulatory "
        "standards including GDPR, SOC2, and HIPAA. Flags violations "
        "with severity and remediation guidance."
    ),
    "node_type": "agent",
    "role": "reviewer",
    "prompt_template": (
        "You are a compliance reviewer.\n\n"
        "Review the provided code/config against compliance standards "
        "({standards}) and report any violations.\n\n"
        "Content to review:\n"
        "---\n"
        "{content}\n"
        "---\n\n"
        "Respond with a JSON object:\n"
        "- violations: array of objects with 'rule', 'severity' (low/medium/high/critical), "
        "'location', 'description', 'remediation'\n"
        "- summary: string — overall compliance assessment"
    ),
    "input_schema": {
        "type": "object",
        "required": ["content", "standards"],
        "properties": {
            "content": {"type": "string", "description": "Code or configuration to review"},
            "standards": {
                "type": "array",
                "items": {"type": "string", "enum": ["GDPR", "SOC2", "HIPAA"]},
                "description": "Standards to check against",
            },
        },
    },
    "output_schema": {
        "type": "object",
        "required": ["violations", "summary"],
        "properties": {
            "violations": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "rule": {"type": "string"},
                        "severity": {"type": "string", "enum": ["low", "medium", "high", "critical"]},
                        "location": {"type": "string"},
                        "description": {"type": "string"},
                        "remediation": {"type": "string"},
                    },
                    "required": ["rule", "severity", "description"],
                },
                "description": "List of compliance violations found",
            },
            "summary": {"type": "string", "description": "Overall compliance assessment"},
        },
    },
    "tags": ["reviewer", "canonical", "library", "compliance", "security"],
    "version": "1.0.0",
    "author": "Modulo",
}

# ---------------------------------------------------------------------------
# 3. dependency-analyzer
# ---------------------------------------------------------------------------
DEPENDENCY_ANALYZER: dict[str, Any] = {
    "name": "Dependency Analyzer",
    "description": (
        "Analyses project dependency manifests (package.json, pyproject.toml, "
        "Cargo.toml, etc.) for conflicts, outdated packages, and known "
        "security vulnerabilities."
    ),
    "node_type": "agent",
    "role": "analyzer",
    "prompt_template": (
        "You are a dependency analyser.\n\n"
        "Analyse the following dependency manifest for conflicts, "
        "outdated packages, and security vulnerabilities.\n\n"
        "Manifest ({manifest_type}):\n"
        "---\n"
        "{manifest}\n"
        "---\n\n"
        "Respond with a JSON object:\n"
        "- dependencies: array of objects with 'name', 'version', 'latest_version' (if known)\n"
        "- outdated: array of dependency names that are behind latest\n"
        "- conflicts: array describing version conflicts\n"
        "- vulnerabilities: array of objects with 'package', 'severity', 'advisory'"
    ),
    "input_schema": {
        "type": "object",
        "required": ["manifest", "manifest_type"],
        "properties": {
            "manifest": {"type": "string", "description": "Raw content of the dependency manifest"},
            "manifest_type": {
                "type": "string",
                "enum": ["package.json", "pyproject.toml", "Cargo.toml", "requirements.txt", "go.mod", "Gemfile"],
                "description": "Type of manifest file",
            },
        },
    },
    "output_schema": {
        "type": "object",
        "required": ["dependencies", "outdated", "conflicts", "vulnerabilities"],
        "properties": {
            "dependencies": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "version": {"type": "string"},
                        "latest_version": {"type": "string"},
                    },
                    "required": ["name", "version"],
                },
            },
            "outdated": {"type": "array", "items": {"type": "string"}},
            "conflicts": {"type": "array", "items": {"type": "string"}},
            "vulnerabilities": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "package": {"type": "string"},
                        "severity": {"type": "string"},
                        "advisory": {"type": "string"},
                    },
                },
            },
        },
    },
    "tags": ["analyzer", "canonical", "library", "dependencies", "security"],
    "version": "1.0.0",
    "author": "Modulo",
}

# ---------------------------------------------------------------------------
# 4. doc-generator
# ---------------------------------------------------------------------------
DOC_GENERATOR: dict[str, Any] = {
    "name": "Doc Generator",
    "description": (
        "Generates human-readable documentation from source code, "
        "extracting comments, function signatures, and type annotations."
    ),
    "node_type": "agent",
    "role": "generator",
    "prompt_template": (
        "You are a documentation generator.\n\n"
        "Generate clear, comprehensive Markdown documentation from the "
        "following source code.\n\n"
        "Source code:\n"
        "---\n"
        "{source_code}\n"
        "---\n\n"
        "Language: {language}\n"
        "Module name: {module_name}\n\n"
        "Respond with a JSON object:\n"
        "- docs: string — full Markdown documentation\n"
        "- sections: array of objects with 'heading' and 'content'"
    ),
    "input_schema": {
        "type": "object",
        "required": ["source_code", "language"],
        "properties": {
            "source_code": {"type": "string", "description": "Source code to document"},
            "language": {"type": "string", "description": "Programming language"},
            "module_name": {"type": "string", "description": "Optional module/package name"},
        },
    },
    "output_schema": {
        "type": "object",
        "required": ["docs", "sections"],
        "properties": {
            "docs": {"type": "string", "description": "Full Markdown documentation"},
            "sections": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "heading": {"type": "string"},
                        "content": {"type": "string"},
                    },
                    "required": ["heading", "content"],
                },
            },
        },
    },
    "tags": ["generator", "canonical", "library", "documentation"],
    "version": "1.0.0",
    "author": "Modulo",
}

# ---------------------------------------------------------------------------
# 5. release-note-generator
# ---------------------------------------------------------------------------
RELEASE_NOTE_GENERATOR: dict[str, Any] = {
    "name": "Release Note Generator",
    "description": (
        "Generates well-structured release notes from git log entries, "
        "changelog fragments, and PR descriptions, organised by type "
        "with highlights and breaking changes called out."
    ),
    "node_type": "agent",
    "role": "generator",
    "prompt_template": (
        "You are a release note generator.\n\n"
        "Create release notes for version {version} from the following "
        "commit/PR data.\n\n"
        "Commits:\n"
        "---\n"
        "{commits}\n"
        "---\n\n"
        "Respond with a JSON object:\n"
        "- release_notes: string — full Markdown release notes\n"
        "- highlights: array of strings — key highlights to call out\n"
        "- breaking_changes: array of strings — any breaking changes"
    ),
    "input_schema": {
        "type": "object",
        "required": ["commits", "version"],
        "properties": {
            "commits": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Array of commit messages or PR titles",
            },
            "version": {"type": "string", "description": "Semantic version string"},
        },
    },
    "output_schema": {
        "type": "object",
        "required": ["release_notes", "highlights", "breaking_changes"],
        "properties": {
            "release_notes": {"type": "string", "description": "Full Markdown release notes"},
            "highlights": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Key highlights to call out",
            },
            "breaking_changes": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Any breaking changes",
            },
        },
    },
    "tags": ["generator", "canonical", "library", "release", "changelog"],
    "version": "1.0.0",
    "author": "Modulo",
}

# ---------------------------------------------------------------------------
# 6. test-generator
# ---------------------------------------------------------------------------
TEST_GENERATOR: dict[str, Any] = {
    "name": "Test Generator",
    "description": (
        "Generates unit test code from function signatures, parameter "
        "descriptions, and return type annotations. Produces ready-to-run "
        "test code for the appropriate testing framework."
    ),
    "node_type": "agent",
    "role": "generator",
    "prompt_template": (
        "You are a test generator.\n\n"
        "Generate unit tests for the following function.\n\n"
        "Function name: {function_name}\n"
        "Parameters: {parameters}\n"
        "Return type: {return_type}\n"
        "Description: {description}\n"
        "Test framework: {framework}\n\n"
        "Respond with a JSON object:\n"
        "- test_code: string — complete test file code\n"
        "- test_cases: array of objects with 'name', 'input', 'expected_output', "
        "'description'"
    ),
    "input_schema": {
        "type": "object",
        "required": ["function_name", "parameters", "return_type", "description"],
        "properties": {
            "function_name": {"type": "string", "description": "Name of the function under test"},
            "parameters": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "type": {"type": "string"},
                        "description": {"type": "string"},
                    },
                },
                "description": "Function parameters",
            },
            "return_type": {"type": "string", "description": "Return type annotation"},
            "description": {"type": "string", "description": "Function description"},
            "framework": {
                "type": "string",
                "description": "Test framework (pytest, unittest, vitest, etc.)",
            },
        },
    },
    "output_schema": {
        "type": "object",
        "required": ["test_code", "test_cases"],
        "properties": {
            "test_code": {"type": "string", "description": "Complete test file code"},
            "test_cases": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "input": {"type": "object"},
                        "expected_output": {},
                        "description": {"type": "string"},
                    },
                    "required": ["name", "input", "expected_output"],
                },
            },
        },
    },
    "tags": ["generator", "canonical", "library", "testing"],
    "version": "1.0.0",
    "author": "Modulo",
}

# ---------------------------------------------------------------------------
# 7. schema-inferrer
# ---------------------------------------------------------------------------
SCHEMA_INFERRER: dict[str, Any] = {
    "name": "Schema Inferrer",
    "description": (
        "Infers a JSON Schema from sample data records. Analyses field "
        "types, nullability, enum values, and nested structures to "
        "produce an accurate schema with field-level descriptions."
    ),
    "node_type": "agent",
    "role": "inferrer",
    "prompt_template": (
        "You are a schema inferrer.\n\n"
        "Infer a JSON Schema from the following sample data records.\n\n"
        "Sample data:\n"
        "---\n"
        "{sample_data}\n"
        "---\n\n"
        "Respond with a JSON object:\n"
        "- schema: object — the inferred JSON Schema (draft-07)\n"
        "- field_descriptions: object — map of field name to natural-language description\n"
        "- confidence: number — confidence score 0-1"
    ),
    "input_schema": {
        "type": "object",
        "required": ["sample_data"],
        "properties": {
            "sample_data": {
                "type": "array",
                "items": {"type": "object"},
                "description": "Array of sample data records",
            },
        },
    },
    "output_schema": {
        "type": "object",
        "required": ["schema", "field_descriptions", "confidence"],
        "properties": {
            "schema": {"type": "object", "description": "Inferred JSON Schema (draft-07)"},
            "field_descriptions": {
                "type": "object",
                "description": "Map of field name to description",
            },
            "confidence": {
                "type": "number",
                "minimum": 0,
                "maximum": 1,
                "description": "Confidence score",
            },
        },
    },
    "tags": ["inferrer", "canonical", "library", "schema"],
    "version": "1.0.0",
    "author": "Modulo",
}

# ---------------------------------------------------------------------------
# 8. changelog-writer
# ---------------------------------------------------------------------------
CHANGELOG_WRITER: dict[str, Any] = {
    "name": "Changelog Writer",
    "description": (
        "Creates a single structured changelog entry from a set of "
        "changes and version information, following Keep a Changelog "
        "conventions."
    ),
    "node_type": "agent",
    "role": "writer",
    "prompt_template": (
        "You are a changelog writer.\n\n"
        "Create a changelog entry for version {version} from the "
        "following changes.\n\n"
        "Changes:\n"
        "---\n"
        "{changes}\n"
        "---\n\n"
        "Respond with a JSON object:\n"
        "- changelog_entry: object with:\n"
        "  - type: string — one of 'added', 'changed', 'fixed', 'deprecated', 'removed', 'security'\n"
        "  - description: string — human-readable description\n"
        "  - references: array of strings — issue/PR references"
    ),
    "input_schema": {
        "type": "object",
        "required": ["changes", "version"],
        "properties": {
            "changes": {
                "type": "array",
                "items": {"type": "string"},
                "description": "List of change descriptions",
            },
            "version": {"type": "string", "description": "Semantic version string"},
        },
    },
    "output_schema": {
        "type": "object",
        "required": ["changelog_entry"],
        "properties": {
            "changelog_entry": {
                "type": "object",
                "required": ["type", "description"],
                "properties": {
                    "type": {
                        "type": "string",
                        "enum": ["added", "changed", "fixed", "deprecated", "removed", "security"],
                    },
                    "description": {"type": "string"},
                    "references": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                },
            },
        },
    },
    "tags": ["writer", "canonical", "library", "changelog"],
    "version": "1.0.0",
    "author": "Modulo",
}

# ---------------------------------------------------------------------------
# 9. security-reviewer
# ---------------------------------------------------------------------------
SECURITY_REVIEWER: dict[str, Any] = {
    "name": "Security Reviewer",
    "description": (
        "Reviews source code for common security vulnerabilities including "
        "SQL injection, XSS, CSRF, command injection, and insecure "
        "cryptography. Provides CWE identifiers and remediation guidance."
    ),
    "node_type": "agent",
    "role": "reviewer",
    "prompt_template": (
        "You are a security code reviewer.\n\n"
        "Review the following source code for security vulnerabilities.\n\n"
        "Source code:\n"
        "---\n"
        "{source_code}\n"
        "---\n\n"
        "Language: {language}\n\n"
        "Respond with a JSON object:\n"
        "- vulnerabilities: array of objects with:\n"
        "  - title: string\n"
        "  - severity: string (low/medium/high/critical)\n"
        "  - cwe_id: string — CWE identifier (e.g. CWE-79)\n"
        "  - location: string — file/line reference\n"
        "  - description: string\n"
        "  - remediation: string"
    ),
    "input_schema": {
        "type": "object",
        "required": ["source_code", "language"],
        "properties": {
            "source_code": {"type": "string", "description": "Source code to review"},
            "language": {"type": "string", "description": "Programming language"},
        },
    },
    "output_schema": {
        "type": "object",
        "required": ["vulnerabilities"],
        "properties": {
            "vulnerabilities": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": ["title", "severity", "cwe_id", "description", "remediation"],
                    "properties": {
                        "title": {"type": "string"},
                        "severity": {"type": "string", "enum": ["low", "medium", "high", "critical"]},
                        "cwe_id": {"type": "string"},
                        "location": {"type": "string"},
                        "description": {"type": "string"},
                        "remediation": {"type": "string"},
                    },
                },
                "description": "List of security vulnerabilities found",
            },
        },
    },
    "tags": ["reviewer", "canonical", "library", "security", "vulnerability"],
    "version": "1.0.0",
    "author": "Modulo",
}

# ---------------------------------------------------------------------------
# 10. correction-proposer
# ---------------------------------------------------------------------------
CORRECTION_PROPOSER: dict[str, Any] = {
    "name": "Correction Proposer",
    "description": (
        "When a pipeline run fails or produces unexpected output, analyses "
        "the failure context and proposes a correction to the prompt, "
        "configuration, or input data."
    ),
    "node_type": "agent",
    "role": "proposer",
    "prompt_template": (
        "You are a correction proposer.\n\n"
        "Analyse the following run failure and propose a correction.\n\n"
        "Original prompt: {original_prompt}\n"
        "Actual output: {output}\n"
        "Error: {error}\n"
        "Evaluation results: {eval_results}\n\n"
        "Respond with a JSON object:\n"
        "- diagnosis: string — root cause analysis\n"
        "- proposed_correction: object with:\n"
        "  - type: string — 'prompt', 'config', 'input', or 'retry'\n"
        "  - description: string — what to change\n"
        "- confidence: number — 0-1 confidence in this correction"
    ),
    "input_schema": {
        "type": "object",
        "required": ["original_prompt", "output", "error", "eval_results"],
        "properties": {
            "original_prompt": {"type": "string", "description": "The prompt that was sent"},
            "output": {"type": "string", "description": "The output produced (may be empty on error)"},
            "error": {"type": "string", "description": "Error message if any"},
            "eval_results": {
                "type": "object",
                "description": "Evaluation results or scores",
            },
        },
    },
    "output_schema": {
        "type": "object",
        "required": ["diagnosis", "proposed_correction", "confidence"],
        "properties": {
            "diagnosis": {"type": "string", "description": "Root cause analysis"},
            "proposed_correction": {
                "type": "object",
                "required": ["type", "description"],
                "properties": {
                    "type": {"type": "string", "enum": ["prompt", "config", "input", "retry"]},
                    "description": {"type": "string"},
                },
            },
            "confidence": {
                "type": "number",
                "minimum": 0,
                "maximum": 1,
                "description": "Confidence in this correction",
            },
        },
    },
    "tags": ["proposer", "canonical", "library", "correction", "debugging"],
    "version": "1.0.0",
    "author": "Modulo",
}

# ---------------------------------------------------------------------------
# 11. prompt-improver
# ---------------------------------------------------------------------------
PROMPT_IMPROVER: dict[str, Any] = {
    "name": "Prompt Improver",
    "description": (
        "Analyses prompt effectiveness by comparing expected vs actual "
        "outputs and evaluation scores, then suggests concrete improvements "
        "to the prompt template."
    ),
    "node_type": "agent",
    "role": "improver",
    "prompt_template": (
        "You are a prompt improver.\n\n"
        "Analyse the prompt's effectiveness and suggest improvements.\n\n"
        "Current prompt:\n"
        "---\n"
        "{prompt}\n"
        "---\n\n"
        "Expected output: {expected_output}\n"
        "Actual output: {actual_output}\n"
        "Evaluation score: {eval_score}\n\n"
        "Respond with a JSON object:\n"
        "- improvements: array of objects with:\n"
        "  - aspect: string — what aspect to improve\n"
        "  - suggestion: string — concrete suggestion\n"
        "  - expected_impact: string — expected benefit\n"
        "- suggested_prompt: string — revised prompt template"
    ),
    "input_schema": {
        "type": "object",
        "required": ["prompt", "expected_output", "actual_output", "eval_score"],
        "properties": {
            "prompt": {"type": "string", "description": "The current prompt template"},
            "expected_output": {"type": "string", "description": "Expected / ideal output"},
            "actual_output": {"type": "string", "description": "Actual output produced"},
            "eval_score": {
                "type": "number",
                "description": "Evaluation score (0-1 or percentage)",
            },
        },
    },
    "output_schema": {
        "type": "object",
        "required": ["improvements", "suggested_prompt"],
        "properties": {
            "improvements": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": ["aspect", "suggestion"],
                    "properties": {
                        "aspect": {"type": "string"},
                        "suggestion": {"type": "string"},
                        "expected_impact": {"type": "string"},
                    },
                },
            },
            "suggested_prompt": {"type": "string", "description": "Revised prompt template"},
        },
    },
    "tags": ["improver", "canonical", "library", "prompt", "optimisation"],
    "version": "1.0.0",
    "author": "Modulo",
}

# ---------------------------------------------------------------------------
# 12. eval-proposal-writer
# ---------------------------------------------------------------------------
EVAL_PROPOSAL_WRITER: dict[str, Any] = {
    "name": "Eval Proposal Writer",
    "description": (
        "Proposes new evaluation cases based on run feedback, uncovered "
        "behaviours, and edge cases observed during pipeline execution."
    ),
    "node_type": "agent",
    "role": "writer",
    "prompt_template": (
        "You are an eval proposal writer.\n\n"
        "Propose new evaluation cases based on the following context.\n\n"
        "Run context: {run_context}\n"
        "User feedback: {feedback}\n"
        "Uncovered behaviours: {uncovered_behaviours}\n\n"
        "Respond with a JSON object:\n"
        "- proposed_evals: array of objects with:\n"
        "  - name: string\n"
        "  - description: string\n"
        "  - input: object — the eval input\n"
        "  - expected_output: object — the expected output\n"
        "  - rationale: string — why this eval is needed"
    ),
    "input_schema": {
        "type": "object",
        "required": ["run_context", "feedback", "uncovered_behaviours"],
        "properties": {
            "run_context": {"type": "object", "description": "Context from the run"},
            "feedback": {"type": "string", "description": "User or system feedback"},
            "uncovered_behaviours": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Behaviours not covered by existing evals",
            },
        },
    },
    "output_schema": {
        "type": "object",
        "required": ["proposed_evals"],
        "properties": {
            "proposed_evals": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": ["name", "description", "input", "expected_output", "rationale"],
                    "properties": {
                        "name": {"type": "string"},
                        "description": {"type": "string"},
                        "input": {"type": "object"},
                        "expected_output": {"type": "object"},
                        "rationale": {"type": "string"},
                    },
                },
            },
        },
    },
    "tags": ["writer", "canonical", "library", "eval", "testing"],
    "version": "1.0.0",
    "author": "Modulo",
}

# ---------------------------------------------------------------------------
# 13. feedback-analyzer
# ---------------------------------------------------------------------------
FEEDBACK_ANALYZER: dict[str, Any] = {
    "name": "Feedback Analyzer",
    "description": (
        "Analyses user feedback (ratings, comments, surveys) and extracts "
        "actionable patterns, sentiment trends, and top issues to "
        "prioritise."
    ),
    "node_type": "agent",
    "role": "analyzer",
    "prompt_template": (
        "You are a feedback analyst.\n\n"
        "Analyse the following user feedback items and extract patterns.\n\n"
        "Feedback items:\n"
        "---\n"
        "{feedback_items}\n"
        "---\n\n"
        "Respond with a JSON object:\n"
        "- patterns: array of objects with 'pattern' (string) and 'frequency' (number)\n"
        "- sentiment: string — overall sentiment (positive/neutral/negative)\n"
        "- sentiment_score: number — 0-1 score\n"
        "- top_issues: array of strings — most mentioned issues"
    ),
    "input_schema": {
        "type": "object",
        "required": ["feedback_items"],
        "properties": {
            "feedback_items": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "rating": {"type": "number"},
                        "comment": {"type": "string"},
                        "category": {"type": "string"},
                    },
                },
                "description": "Array of user feedback items",
            },
        },
    },
    "output_schema": {
        "type": "object",
        "required": ["patterns", "sentiment", "sentiment_score", "top_issues"],
        "properties": {
            "patterns": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": ["pattern", "frequency"],
                    "properties": {
                        "pattern": {"type": "string"},
                        "frequency": {"type": "number"},
                    },
                },
            },
            "sentiment": {"type": "string", "enum": ["positive", "neutral", "negative"]},
            "sentiment_score": {"type": "number", "minimum": 0, "maximum": 1},
            "top_issues": {"type": "array", "items": {"type": "string"}},
        },
    },
    "tags": ["analyzer", "canonical", "library", "feedback", "sentiment"],
    "version": "1.0.0",
    "author": "Modulo",
}

# ---------------------------------------------------------------------------
# 14. changelog-aggregator
# ---------------------------------------------------------------------------
CHANGELOG_AGGREGATOR: dict[str, Any] = {
    "name": "Changelog Aggregator",
    "description": (
        "Aggregates multiple changelog entries into a structured release "
        "summary organised by conventional commit categories: added, "
        "changed, fixed, deprecated, removed, security."
    ),
    "node_type": "agent",
    "role": "aggregator",
    "prompt_template": (
        "You are a changelog aggregator.\n\n"
        "Aggregate the following changelog entries into a release summary.\n\n"
        "Entries:\n"
        "---\n"
        "{entries}\n"
        "---\n\n"
        "Respond with a JSON object:\n"
        "- summary: string — overall release summary\n"
        "- categories: object with keys 'added', 'changed', 'fixed', "
        "'deprecated', 'removed', 'security', each an array of strings"
    ),
    "input_schema": {
        "type": "object",
        "required": ["entries"],
        "properties": {
            "entries": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "type": {"type": "string"},
                        "description": {"type": "string"},
                        "references": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                    },
                },
                "description": "Array of changelog entries",
            },
        },
    },
    "output_schema": {
        "type": "object",
        "required": ["summary", "categories"],
        "properties": {
            "summary": {"type": "string", "description": "Overall release summary"},
            "categories": {
                "type": "object",
                "properties": {
                    "added": {"type": "array", "items": {"type": "string"}},
                    "changed": {"type": "array", "items": {"type": "string"}},
                    "fixed": {"type": "array", "items": {"type": "string"}},
                    "deprecated": {"type": "array", "items": {"type": "string"}},
                    "removed": {"type": "array", "items": {"type": "string"}},
                    "security": {"type": "array", "items": {"type": "string"}},
                },
                "description": "Changes grouped by category",
            },
        },
    },
    "tags": ["aggregator", "canonical", "library", "changelog", "release"],
    "version": "1.0.0",
    "author": "Modulo",
}

# ---------------------------------------------------------------------------
# 15. status-reporter
# ---------------------------------------------------------------------------
STATUS_REPORTER: dict[str, Any] = {
    "name": "Status Reporter",
    "description": (
        "Generates a pipeline status report from run data, including "
        "metrics (pass/fail rates, duration), trends over time, and "
        "actionable recommendations."
    ),
    "node_type": "agent",
    "role": "reporter",
    "prompt_template": (
        "You are a status reporter.\n\n"
        "Generate a pipeline status report from the following run data.\n\n"
        "Runs:\n"
        "---\n"
        "{runs}\n"
        "---\n\n"
        "Period: {period}\n\n"
        "Respond with a JSON object:\n"
        "- report: string — Markdown report\n"
        "- metrics: object with 'total_runs', 'passed', 'failed', "
        "'average_duration_seconds', 'pass_rate'\n"
        "- trends: array of objects with 'period', 'pass_rate', 'total'\n"
        "- recommendations: array of strings"
    ),
    "input_schema": {
        "type": "object",
        "required": ["runs", "period"],
        "properties": {
            "runs": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string"},
                        "status": {"type": "string"},
                        "duration_seconds": {"type": "number"},
                        "created_at": {"type": "string"},
                    },
                },
                "description": "Array of run records",
            },
            "period": {"type": "string", "description": "Time period (e.g. '2026-06', 'Q2 2026')"},
        },
    },
    "output_schema": {
        "type": "object",
        "required": ["report", "metrics", "trends", "recommendations"],
        "properties": {
            "report": {"type": "string", "description": "Markdown report"},
            "metrics": {
                "type": "object",
                "required": ["total_runs", "passed", "failed", "pass_rate"],
                "properties": {
                    "total_runs": {"type": "integer"},
                    "passed": {"type": "integer"},
                    "failed": {"type": "integer"},
                    "average_duration_seconds": {"type": "number"},
                    "pass_rate": {"type": "number"},
                },
            },
            "trends": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "period": {"type": "string"},
                        "pass_rate": {"type": "number"},
                        "total": {"type": "integer"},
                    },
                },
            },
            "recommendations": {"type": "array", "items": {"type": "string"}},
        },
    },
    "tags": ["reporter", "canonical", "library", "status", "metrics"],
    "version": "1.0.0",
    "author": "Modulo",
}

# ---------------------------------------------------------------------------
# 16. migration-planner
# ---------------------------------------------------------------------------
MIGRATION_PLANNER: dict[str, Any] = {
    "name": "Migration Planner",
    "description": (
        "Analyses schema changes between old and new versions, identifies "
        "breaking changes, and generates a structured migration plan with "
        "steps, risks, and rollback instructions."
    ),
    "node_type": "agent",
    "role": "planner",
    "prompt_template": (
        "You are a migration planner.\n\n"
        "Analyse the schema changes and create a migration plan.\n\n"
        "Old schema:\n"
        "---\n"
        "{old_schema}\n"
        "---\n\n"
        "New schema:\n"
        "---\n"
        "{new_schema}\n"
        "---\n\n"
        "Respond with a JSON object:\n"
        "- migration_plan: object with:\n"
        "  - summary: string\n"
        "  - steps: array of objects with 'order', 'action', 'description', 'risk'\n"
        "  - estimated_duration: string\n"
        "- risks: array of strings\n"
        "- rollback: string — rollback instructions"
    ),
    "input_schema": {
        "type": "object",
        "required": ["old_schema", "new_schema"],
        "properties": {
            "old_schema": {
                "type": "object",
                "description": "Current/old schema definition",
            },
            "new_schema": {
                "type": "object",
                "description": "Target/new schema definition",
            },
        },
    },
    "output_schema": {
        "type": "object",
        "required": ["migration_plan", "risks", "rollback"],
        "properties": {
            "migration_plan": {
                "type": "object",
                "required": ["summary", "steps"],
                "properties": {
                    "summary": {"type": "string"},
                    "steps": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "required": ["order", "action", "description"],
                            "properties": {
                                "order": {"type": "integer"},
                                "action": {"type": "string"},
                                "description": {"type": "string"},
                                "risk": {"type": "string", "enum": ["low", "medium", "high"]},
                            },
                        },
                    },
                    "estimated_duration": {"type": "string"},
                },
            },
            "risks": {"type": "array", "items": {"type": "string"}},
            "rollback": {"type": "string", "description": "Rollback instructions"},
        },
    },
    "tags": ["planner", "canonical", "library", "migration", "schema"],
    "version": "1.0.0",
    "author": "Modulo",
}

# ---------------------------------------------------------------------------
# 17. rollback-planner
# ---------------------------------------------------------------------------
ROLLBACK_PLANNER: dict[str, Any] = {
    "name": "Rollback Planner",
    "description": (
        "Creates a detailed rollback plan for deployment failures, "
        "including step-by-step instructions, estimated time, and "
        "risk assessment."
    ),
    "node_type": "agent",
    "role": "planner",
    "prompt_template": (
        "You are a rollback planner.\n\n"
        "Create a rollback plan for the following deployment failure.\n\n"
        "Deployment info:\n"
        "---\n"
        "{deployment_info}\n"
        "---\n\n"
        "Failure details:\n"
        "---\n"
        "{failure_details}\n"
        "---\n\n"
        "Respond with a JSON object:\n"
        "- rollback_plan: object with:\n"
        "  - summary: string\n"
        "  - steps: array of objects with 'order', 'action', 'command', 'verification'\n"
        "  - estimated_time: string\n"
        "- risks: array of strings\n"
        "- rollback_strategy: string — 'full', 'partial', or 'blue-green'"
    ),
    "input_schema": {
        "type": "object",
        "required": ["deployment_info", "failure_details"],
        "properties": {
            "deployment_info": {
                "type": "object",
                "description": "Information about the deployment",
            },
            "failure_details": {
                "type": "object",
                "description": "Details about what failed",
            },
        },
    },
    "output_schema": {
        "type": "object",
        "required": ["rollback_plan", "risks", "rollback_strategy"],
        "properties": {
            "rollback_plan": {
                "type": "object",
                "required": ["summary", "steps", "estimated_time"],
                "properties": {
                    "summary": {"type": "string"},
                    "steps": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "required": ["order", "action"],
                            "properties": {
                                "order": {"type": "integer"},
                                "action": {"type": "string"},
                                "command": {"type": "string"},
                                "verification": {"type": "string"},
                            },
                        },
                    },
                    "estimated_time": {"type": "string"},
                },
            },
            "risks": {"type": "array", "items": {"type": "string"}},
            "rollback_strategy": {"type": "string", "enum": ["full", "partial", "blue-green"]},
        },
    },
    "tags": ["planner", "canonical", "library", "rollback", "deployment"],
    "version": "1.0.0",
    "author": "Modulo",
}

# ---------------------------------------------------------------------------
# 18. ticket-writer
# ---------------------------------------------------------------------------
TICKET_WRITER: dict[str, Any] = {
    "name": "Ticket Writer",
    "description": (
        "Creates a well-formatted ticket (issue/story) from a brief "
        "summary and context, including title, description, acceptance "
        "criteria, and suggested labels."
    ),
    "node_type": "agent",
    "role": "writer",
    "prompt_template": (
        "You are a ticket writer.\n\n"
        "Create a well-formatted ticket from the following information.\n\n"
        "Summary: {summary}\n"
        "Type: {type}\n"
        "Context: {context}\n\n"
        "Respond with a JSON object:\n"
        "- ticket: object with:\n"
        "  - title: string\n"
        "  - description: string — detailed description\n"
        "  - acceptance_criteria: array of strings\n"
        "  - labels: array of strings\n"
        "  - estimated_size: string — XS/S/M/L/XL"
    ),
    "input_schema": {
        "type": "object",
        "required": ["summary", "type", "context"],
        "properties": {
            "summary": {"type": "string", "description": "Brief summary of the ticket"},
            "type": {"type": "string", "description": "Ticket type (bug, feature, task, etc.)"},
            "context": {"type": "string", "description": "Additional context or background"},
        },
    },
    "output_schema": {
        "type": "object",
        "required": ["ticket"],
        "properties": {
            "ticket": {
                "type": "object",
                "required": ["title", "description", "acceptance_criteria"],
                "properties": {
                    "title": {"type": "string"},
                    "description": {"type": "string"},
                    "acceptance_criteria": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "labels": {"type": "array", "items": {"type": "string"}},
                    "estimated_size": {
                        "type": "string",
                        "enum": ["XS", "S", "M", "L", "XL"],
                    },
                },
            },
        },
    },
    "tags": ["writer", "canonical", "library", "ticket", "task"],
    "version": "1.0.0",
    "author": "Modulo",
}

# ---------------------------------------------------------------------------
# 19. prd-summarizer
# ---------------------------------------------------------------------------
PRD_SUMMARIZER: dict[str, Any] = {
    "name": "PRD Summarizer",
    "description": (
        "Summarises a Product Requirements Document into key features, "
        "constraints, stakeholders, and decisions — preserving all "
        "critical details while reducing length."
    ),
    "node_type": "agent",
    "role": "summarizer",
    "prompt_template": (
        "You are a PRD summarizer.\n\n"
        "Summarise the following Product Requirements Document into key "
        "points.\n\n"
        "PRD text:\n"
        "---\n"
        "{prd_text}\n"
        "---\n\n"
        "Respond with a JSON object:\n"
        "- summary: string — concise executive summary\n"
        "- key_features: array of strings\n"
        "- constraints: array of strings\n"
        "- stakeholders: array of strings\n"
        "- decisions: array of strings — key design/scope decisions"
    ),
    "input_schema": {
        "type": "object",
        "required": ["prd_text"],
        "properties": {
            "prd_text": {"type": "string", "description": "Full PRD text to summarise"},
        },
    },
    "output_schema": {
        "type": "object",
        "required": ["summary", "key_features", "constraints", "stakeholders", "decisions"],
        "properties": {
            "summary": {"type": "string", "description": "Concise executive summary"},
            "key_features": {"type": "array", "items": {"type": "string"}},
            "constraints": {"type": "array", "items": {"type": "string"}},
            "stakeholders": {"type": "array", "items": {"type": "string"}},
            "decisions": {"type": "array", "items": {"type": "string"}},
        },
    },
    "tags": ["summarizer", "canonical", "library", "prd", "requirements"],
    "version": "1.0.0",
    "author": "Modulo",
}

# ---------------------------------------------------------------------------
# 20. code-reviewer
# ---------------------------------------------------------------------------
CODE_REVIEWER: dict[str, Any] = {
    "name": "Code Reviewer",
    "description": (
        "Reviews pull request code changes for quality, style adherence, "
        "potential bugs, and test coverage. Provides per-file inline "
        "comments with severity ratings and an overall rating."
    ),
    "node_type": "agent",
    "role": "reviewer",
    "prompt_template": (
        "You are a pull request code reviewer.\n\n"
        "Review the following code changes and provide feedback.\n\n"
        "Diff:\n"
        "---\n"
        "{diff}\n"
        "---\n\n"
        "Files changed: {file_changes}\n\n"
        "Respond with a JSON object:\n"
        "- comments: array of objects with:\n"
        "  - file: string\n"
        "  - line: integer\n"
        "  - severity: string (info/warning/error)\n"
        "  - message: string\n"
        "  - category: string (bug/style/performance/security/testability)\n"
        "- overall_rating: string — 'approve', 'changes_requested', or 'comment'\n"
        "- summary: string — overall review summary"
    ),
    "input_schema": {
        "type": "object",
        "required": ["diff", "file_changes"],
        "properties": {
            "diff": {"type": "string", "description": "Unified diff of the PR changes"},
            "file_changes": {
                "type": "array",
                "items": {"type": "string"},
                "description": "List of changed file paths",
            },
        },
    },
    "output_schema": {
        "type": "object",
        "required": ["comments", "overall_rating", "summary"],
        "properties": {
            "comments": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": ["file", "line", "severity", "message"],
                    "properties": {
                        "file": {"type": "string"},
                        "line": {"type": "integer"},
                        "severity": {"type": "string", "enum": ["info", "warning", "error"]},
                        "message": {"type": "string"},
                        "category": {
                            "type": "string",
                            "enum": ["bug", "style", "performance", "security", "testability"],
                        },
                    },
                },
            },
            "overall_rating": {
                "type": "string",
                "enum": ["approve", "changes_requested", "comment"],
            },
            "summary": {"type": "string"},
        },
    },
    "tags": ["reviewer", "canonical", "library", "code-review", "pr"],
    "version": "1.0.0",
    "author": "Modulo",
}

# ---------------------------------------------------------------------------
# 21. ticket-estimator
# ---------------------------------------------------------------------------
TICKET_ESTIMATOR: dict[str, Any] = {
    "name": "Ticket Estimator",
    "description": (
        "Estimates story points for tickets based on description and "
        "acceptance criteria, using T-shirt sizing conventions (XS/S/M/L/XL) "
        "aligned with the Modulo estimation convention."
    ),
    "node_type": "agent",
    "role": "estimator",
    "prompt_template": (
        "You are a ticket estimator.\n\n"
        "Estimate the size of the following ticket using T-shirt sizing "
        "(XS = trivial, S = small, M = medium, L = large, XL = very large).\n\n"
        "Ticket description:\n"
        "---\n"
        "{ticket_description}\n"
        "---\n\n"
        "Acceptance criteria:\n"
        "---\n"
        "{acceptance_criteria}\n"
        "---\n\n"
        "Respond with a JSON object:\n"
        "- estimated_points: string — XS/S/M/L/XL\n"
        "- confidence: number — 0-1 confidence\n"
        "- reasoning: string — justification\n"
        "- risk_factors: array of strings — potential risks"
    ),
    "input_schema": {
        "type": "object",
        "required": ["ticket_description", "acceptance_criteria"],
        "properties": {
            "ticket_description": {"type": "string", "description": "Full ticket description"},
            "acceptance_criteria": {
                "type": "string",
                "description": "Acceptance criteria text",
            },
        },
    },
    "output_schema": {
        "type": "object",
        "required": ["estimated_points", "confidence", "reasoning"],
        "properties": {
            "estimated_points": {
                "type": "string",
                "enum": ["XS", "S", "M", "L", "XL"],
                "description": "T-shirt size estimate",
            },
            "confidence": {
                "type": "number",
                "minimum": 0,
                "maximum": 1,
                "description": "Confidence in the estimate",
            },
            "reasoning": {"type": "string", "description": "Justification for the estimate"},
            "risk_factors": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Potential risk factors",
            },
        },
    },
    "tags": ["estimator", "canonical", "library", "ticket", "planning"],
    "version": "1.0.0",
    "author": "Modulo",
}
