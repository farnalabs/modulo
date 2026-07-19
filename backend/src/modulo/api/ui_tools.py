"""UI tool definitions for Remy browser automation commands.

These tool definitions are injected into the LLM's `tools` parameter alongside
MCP tool definitions. When the LLM calls a UI tool, the SSE stream forwards
it to the frontend Vue app as an `event: ui_command_batch`.
"""

from typing import Any

_UI_TOOLS: dict[str, dict[str, Any]] = {
    "navigate": {
        "description": "Navigate to a Modulo page by route path.",
        "parameters": {
            "path": {
                "type": "string",
                "description": "Route path to navigate to (e.g. '/admin/pipelines')",
            },
        },
    },
    "click": {
        "description": "Click an element. Use a data-testid selector when possible.",
        "parameters": {
            "selector": {
                "type": "string",
                "description": "CSS selector or data-testid value for the element",
            },
        },
    },
    "fill": {
        "description": "Type text into an input field.",
        "parameters": {
            "selector": {
                "type": "string",
                "description": "CSS selector or data-testid value for the input",
            },
            "value": {
                "type": "string",
                "description": "Text to type into the field",
            },
        },
    },
    "select": {
        "description": "Select an option in a dropdown.",
        "parameters": {
            "selector": {
                "type": "string",
                "description": "CSS selector or data-testid value for the dropdown",
            },
            "value": {
                "type": "string",
                "description": "Option value or label text to select",
            },
        },
    },
    "extract": {
        "description": "Read visible text from an element.",
        "parameters": {
            "selector": {
                "type": "string",
                "description": "CSS selector or data-testid value for the element",
            },
        },
    },
    "extract_all": {
        "description": "Read visible text from all matching elements.",
        "parameters": {
            "selector": {
                "type": "string",
                "description": "CSS selector or data-testid value for the elements",
            },
        },
    },
    "get_page_interactables": {
        "description": "Discover all interactive elements on the current page with their data-testid selectors.",
        "parameters": {},
    },
    "wait": {
        "description": "Wait for a duration or until an element appears.",
        "parameters": {
            "ms": {
                "type": "number",
                "description": "Milliseconds to wait",
                "default": 500,
            },
            "selector": {
                "type": "string",
                "description": "Optional CSS selector to wait for before continuing",
            },
        },
    },
    "go_back": {
        "description": "Navigate back to the previous page.",
        "parameters": {},
    },
    "get_url": {
        "description": "Get current page URL and route name.",
        "parameters": {},
    },
    "get_manifest": {
        "description": (
            "Get structured app manifest with all pages, routes, data-testid selectors,"
            " and interactive elements. Call before navigating to plan your route."
            " Optional path param filters to one route + elements."
        ),
        "parameters": {
            "path": {
                "type": "string",
                "description": "Optional route path (e.g. /admin/pipelines). Returns route + elements.",
            },
        },
    },
    "press": {
        "description": "Press a keyboard key.",
        "parameters": {
            "key": {
                "type": "string",
                "description": "Key to press (e.g. 'Enter', 'Escape', 'Tab', 'ArrowDown')",
            },
        },
    },
    "undo_last_action": {
        "description": "Undo the last UI action. Navigates back or refills previous form values.",
        "parameters": {},
    },
}

UI_TOOL_NAMES: set[str] = set(_UI_TOOLS.keys())

READ_TOOLS: set[str] = {"extract", "extract_all", "get_page_interactables", "get_url"}

NAV_TOOLS: set[str] = {"navigate", "go_back"}

WRITE_TOOLS: set[str] = {"click", "fill", "select", "press"}

DESTRUCTIVE_PATTERNS: list[str] = [
    "delete",
    "remove",
    "destroy",
    "archive",
    "suspend",
    "ban",
    "terminate",
    "revoke",
    "disable",
    "wipe",
    "clear",
]

NOGO_SELECTOR_PATTERNS: list[str] = [
    "delete-org",
    "destroy",
    "billing-action",
    "danger-zone",
    "delete-account",
    "remove-org",
    "suspend-org",
]

NOGO_PAGE_PATTERNS: list[str] = [
    "/admin/billing",
    "/admin/org/",
]


def build_tool_definitions_for_text() -> str:
    """Generate a human-readable description of all UI tools for non-tool-calling models."""
    lines = [
        "## Browser Tools Available (Text Mode)",
        "",
        "Your provider does not support structured tool calling. Describe the actions",
        "you want to take and the user can confirm. Available commands:",
        "",
    ]
    for name, schema in _UI_TOOLS.items():
        params = []
        for p_name, p_info in schema["parameters"].items():
            p_type = p_info.get("type", "str")
            default = p_info.get("default")
            if default is not None:
                params.append(f"{p_name}: {p_type} (default: {default})")
            else:
                params.append(f"{p_name}: {p_type}")
        params_str = ", ".join(params) if params else "no arguments"
        lines.append(f"- **{name}**({params_str}): {schema['description']}")
    lines.extend(
        [
            "",
            "**Tip:** Before navigating, call get_manifest() to learn page structure and interactable elements.",
            "",
            "Example workflow:",
            "1. get_manifest() — learn the page structure",
            "2. navigate(path: /admin/pipelines) — go to the Pipelines admin page",
            "2. wait(ms: 500) — let the page stabilise",
            "3. extract(selector: [data-testid=pipeline-table]) — read current pipelines",
            "4. click(selector: [data-testid=create-btn]) — click Create button",
            "5. go_back() — return to previous page",
        ]
    )
    return "\n".join(lines)
