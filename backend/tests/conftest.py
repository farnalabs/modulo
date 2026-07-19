"""
Root conftest for the Modulo backend test suite.

Enable the quarantine plugin so that .quarantine.yml is read automatically.
"""

pytest_plugins = ["tests.quarantine_plugin"]
