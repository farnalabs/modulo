"""Version info."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("farnalabs-modulo")
except PackageNotFoundError:
    __version__ = "0.0.0-dev"


def get_version() -> str:
    return __version__

# rebuild trigger
# deploy 20260703193139
