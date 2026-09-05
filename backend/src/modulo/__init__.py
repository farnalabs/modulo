import os

from modulo.version import get_version

__version__ = get_version()


def _get_build_tag() -> str:
    sha = os.environ.get("GIT_SHA", "")
    if sha and len(sha) >= 7:
        return f"build-{sha[:7]}"
    return "build-local-dev"


__build_tag__ = _get_build_tag()
