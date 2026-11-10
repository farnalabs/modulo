"""Version info."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("farnalabs-modulo")
except PackageNotFoundError:
    __version__ = "0.0.0-dev"


def get_version() -> str:
    """Return the installed package version, or ``"0.0.0-dev"`` when uninstalled."""
    return __version__
