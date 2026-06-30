"""BDD step loader: auth/rbac.feature."""

from pytest_bdd import scenarios

try:
    scenarios("../../features/auth/rbac.feature")
except (FileNotFoundError, OSError):
    pass
