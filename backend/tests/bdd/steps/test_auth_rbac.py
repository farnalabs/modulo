"""Auth RBAC scenario loader.

The ``auth/rbac.feature`` scenarios are loaded and owned by
``tests/bdd/steps/test_auth.py``, which defines every step the feature needs
(role-cap unit steps, team CRUD, membership, feature gating, and deletion).
This module intentionally registers nothing so the feature is loaded exactly
once — a duplicate ``scenarios(...)`` call here would re-register every
scenario under a sibling module that has no step-definition ancestors.
"""
