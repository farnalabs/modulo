"""Shared message constants for the API layer.

These strings are duplicated across many route files (python:S1192). Centralising
them here keeps the literal in one place; route files import the constant instead
of repeating the string. Values are user-facing HTTPException details / API
messages -- do not change the text without checking all consumers.
"""

MSG_FEATURE_NOT_AVAILABLE = "Feature is not available. Run database migrations to enable it."
MSG_THIS_FEATURE_NOT_AVAILABLE = "This feature is not available. Run database migrations to enable it."
MSG_RESOURCE_ALREADY_EXISTS = "A resource with this value already exists"
MSG_INTERNAL_SERVER_ERROR = "Internal server error"
MSG_UNEXPECTED_ERROR = "An unexpected error occurred."
MSG_UNEXPECTED_ERROR_NO_PERIOD = "An unexpected error occurred"
MSG_DB_OPERATION_FAILED = "Database operation failed. Please try again later."
