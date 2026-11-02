"""Support-bundle tooling (FAR-2305).

``modulo.support.bundle`` builds the versioned, allowlist-redacted support
bundle (format ``modulo-support-bundle/v1``) that a self-hoster can inspect
with ``--list`` / ``--show`` before sending it to support. ``modulo doctor
--report`` delegates to it for the native install.

This package deliberately has no import-time dependency on the native
launcher or the API: the collector imports those lazily, so
``python -m modulo.support.bundle`` works inside the app container with no
data dir.
"""
