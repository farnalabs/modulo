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
MSG_DB_ERROR_PLEASE_TRY = "Database error. Please try again."
MSG_UNEXPECTED_ERROR_PLEASE_TRY = "An unexpected error occurred. Please try again."
MSG_FEATURE_NOT_AVAILABLE_CONTACT_SUPPORT = (
    "Feature is not available. This feature requires a database update. Please contact support."
)
MSG_DATABASE_TEMPORARILY_UNAVAILABLE = "Database temporarily unavailable."
MSG_DATABASE_TEMPORARILY_UNAVAILABLE_PLEASE = "Database temporarily unavailable. Please try again."
MSG_DATABASE_ERROR_OCCURRED_PLEASE = "Database error occurred. Please try again later."
MSG_NO_ORGANISATION = "No organisation"
MSG_ORGANISATION_NOT_FOUND = "Organisation not found"
MSG_NOT_FOUND = "Not found"
MSG_ERROR_TRACKING_NOT_AVAILABLE = "Error tracking is not available. Run database migrations to enable it."
MSG_ERROR_TRACKING_TEMPORARILY_UNAVAILABLE = "Error tracking is temporarily unavailable. Please try again."
MSG_UNEXPECTED_ERROR_OCCURRED_WHILE = "An unexpected error occurred while processing your request."
MSG_TRIGGER_NOT_FOUND = "Trigger not found"
MSG_TEAM_NAME_ALREADY_EXISTS = "A team with this name already exists in your organisation"
MSG_TEAM_NOT_FOUND = "Team not found"
MSG_PIPELINE_NOT_FOUND = "Pipeline not found"
MSG_FOLDER_NOT_FOUND = "Folder not found"
MSG_ENVIRONMENT_PROFILE_NOT_FOUND = "Environment profile not found"
