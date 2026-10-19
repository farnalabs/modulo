"""Fixture for the mcp-human-only-constant-non-browser rule (ADR 063 step 1).

Mirrors `mcp_server._check_human_only_gate`'s call to the shared pure verdict:
the derived form is OK, a hard-coded literal is a finding.
"""


def constant_keyword_arg(config, gate_fired):
    # ruleid: mcp-human-only-constant-non-browser
    return human_only_denial(config, non_browser_credential=True, gate_fired=gate_fired)


def constant_assignment(config, gate_fired):
    # ruleid: mcp-human-only-constant-non-browser
    non_browser_credential = True
    return human_only_denial(config, non_browser_credential=non_browser_credential, gate_fired=gate_fired)


def derived_from_linkage(config, gate_fired, linkage, flag_enabled, action):
    # ok: mcp-human-only-constant-non-browser
    allow_via_linkage = linkage.linked and flag_enabled and action != "deliver_manual"
    non_browser_credential = not allow_via_linkage
    return human_only_denial(config, non_browser_credential=non_browser_credential, gate_fired=gate_fired)
