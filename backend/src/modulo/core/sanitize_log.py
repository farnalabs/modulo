"""Shared log sanitisation helpers.

Centralises the CR/LF-escaping log sanitiser so it is not copy-pasted across
modules (S5145 logging-injection defence). The implementation lives in the
dependency-free leaf ``modulo.util.sanitise_log_value``; this module re-exports
it (and the shared ``DEFAULT_LOG_LIMIT``) under the original ``modulo.core``
address so existing core/API callers keep a single shared choke-point without
duplicating the logic. The helper neutralises newline and carriage-return
characters so untrusted values cannot forge or split log lines, and caps the
rendered length.
"""

from modulo.util import DEFAULT_LOG_LIMIT, sanitise_log_value

__all__ = ["DEFAULT_LOG_LIMIT", "sanitise_log_value"]
