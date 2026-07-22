"""Core-level exception types for Modulo."""


class SnapshotLockNotAvailableError(Exception):
    """Raised when the pipeline snapshot lock cannot be acquired immediately."""
