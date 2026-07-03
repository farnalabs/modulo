"""UI tool definitions for Remy browser automation commands.

These tool definitions are injected into the LLM's `tools` parameter alongside
MCP tool definitions. When the LLM calls a UI tool, the SSE stream forwards
it to the frontend Vue app as an `event: ui_command_batch`.
"""

_UI_TOOLS: dict[str, dict] = {
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
    "press": {
        "description": "Press a keyboard key.",
        "parameters": {
            "key": {
                "type": "string",
                "description": "Key to press (e.g. 'Enter', 'Escape', 'Tab', 'ArrowDown')",
            },
        },
    },
}

UI_TOOL_NAMES: set[str] = set(_UI_TOOLS.keys())

READ_TOOLS: set[str] = {"extract", "extract_all", "get_page_interactables", "get_url"}

NAV_TOOLS: set[str] = {"navigate", "go_back"}

WRITE_TOOLS: set[str] = {"click", "fill", "select", "press"}

DESTRUCTIVE_PATTERNS: list[str] = [
    "delete", "remove", "destroy", "archive", "suspend",
    "ban", "terminate", "revoke", "disable", "wipe", "clear",
]
