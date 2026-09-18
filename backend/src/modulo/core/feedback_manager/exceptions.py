"""FeedbackManager exceptions."""


class FeedbackManagerError(Exception):
    """Base exception for FeedbackManager errors."""


class FeedbackRecordNotFoundError(FeedbackManagerError):
    """Raised when a FeedbackRecord is not found."""


class FeedbackRecordRunNotFoundError(FeedbackManagerError):
    """Raised when the original run referenced by a FeedbackRecord is not found.

    Distinct from :class:`FeedbackRecordNotFoundError`: the record exists, but
    the run it points at is gone. API routes map this to 404 while leaving the
    base :class:`FeedbackManagerError` catch free for genuinely unexpected
    subclasses (e.g. :class:`ValidationError`) rather than also collapsing them
    to 404.
    """


class InvalidTransitionError(FeedbackManagerError):
    """Raised when a feedback status transition is not allowed."""


class ConcurrentModificationError(FeedbackManagerError):
    """Raised when concurrent modification prevents a status transition."""


class ValidationError(FeedbackManagerError):
    """Raised when input validation fails."""
