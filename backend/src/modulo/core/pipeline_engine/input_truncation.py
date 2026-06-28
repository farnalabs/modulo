"""Input truncation for agent node invocation.

Truncates input text that exceeds an agent's configured ``max_input_length``,
appending a notification message so the LLM is aware of the truncation.
"""


def truncate_input(text: str, max_length: int | None) -> str:
    """Truncate *text* to *max_length* characters if set.

    When truncation occurs, a note is appended:
    ``[Input truncated to {max_length} characters]``

    If *max_length* is ``None``, *text* is returned unchanged (backward
    compatible default).
    """
    if max_length is None:
        return text

    if len(text) <= max_length:
        return text

    truncated = text[:max_length]
    return f"{truncated}\n\n[Input truncated to {max_length} characters]"
