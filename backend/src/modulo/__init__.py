import os

from modulo.version import get_version

__version__ = get_version()


def get_build_tag() -> str:
    """Return the short build tag derived from ``GIT_SHA``.

    Uses the first 7 characters when a full SHA is available, otherwise
    falls back to ``"build-local-dev"`` for local development.
    """
    sha = os.environ.get("GIT_SHA", "")
    if sha and len(sha) >= 7:
        return f"build-{sha[:7]}"
    return "build-local-dev"


__build_tag__ = get_build_tag()
