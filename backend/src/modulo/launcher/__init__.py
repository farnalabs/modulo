"""Native single-install launcher building blocks (ADR 031 / FAR-671).

Slice 1 scope: environment safety (``env_safety``), bundled-postgres initdb
orchestration (``initdb``), data-dir state.json (``state``), and the 0600
secrets file (``secrets_file``). The supervisor, serving, doctor, and CLI
group are slices 2-3 and live elsewhere.

Linux-first; Windows specifics carry explicit TODO(P3) seams and are refused
loudly rather than half-implemented.
"""
