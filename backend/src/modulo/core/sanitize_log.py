"""Shared log sanitisation helpers.

Centralises the CR/LF-escaping log sanitiser so it is not copy-pasted across
modules (S5145 logging-injection defence). The helper neutralises newline and
carriage-return characters so untrusted values cannot forge or split log lines,
and caps the rendered length.
"""

DEFAULT_LOG_LIMIT = 200


def sanitise_log_value(value: object, limit: int = DEFAULT_LOG_LIMIT) -> str:
    """Sanitise a value for logging: strip CR/LF and cap length.

    Newline (``\\n``) and carriage-return (``\\r``) characters are escaped to
    their literal ``\\n`` / ``\\r`` forms so a malicious value cannot break a
    log line. The rendered string is capped at ``limit`` code points. Never
    raises — non-str input is coerced via ``str()``.
    """
    return str(value).replace("\r", "\\r").replace("\n", "\\n")[:limit]
