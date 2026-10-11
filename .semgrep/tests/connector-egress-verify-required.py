# Fixture proving the `connector-egress-verify-required` gate fires on a
# connector egress factory call that omits an explicit `verify=` argument and
# stays silent when one is passed (or when the callee is unrelated).
#
# This file is NOT imported by the app or the test suite; it exists only for the
# semgrep rule test runner, which reads the expected-finding / no-finding
# annotation markers (ruleid / ok) on each case. A pinned egress call without
# `verify=` is exactly the regression the gate is designed to catch: the pinned
# transport then rebuilds a fresh TLS context on every call (the CA-bundle load
# the per-instance egress cache exists to eliminate) and verify policy can drift
# between connectors. The invariant the rule enforces is an EXPLICIT verify —
# the canonical `egress_ssl_context(self)` and a deliberate operator override
# (e.g. `self._verify_tls`) both satisfy it.


def unsafe_pinned_client_without_verify(base_url: str):
    # ruleid: connector-egress-verify-required
    return pinned_async_client_sync(base_url, timeout=30)


def unsafe_pinned_transport_without_verify(base_url: str):
    # ruleid: connector-egress-verify-required
    return pinned_async_transport_sync(base_url, timeout=30)


def unsafe_pinned_client_implicit_verify(base_url, **kwargs):
    # A `**kwargs` forwarding call MIGHT carry `verify`, but the invariant is an
    # EXPLICIT `verify=` argument at the call site — implicit is flagged.
    # ruleid: connector-egress-verify-required
    return pinned_async_client_sync(base_url, **kwargs)


def safe_pinned_client_with_cached_verify(base_url: str):
    # ok: connector-egress-verify-required
    return pinned_async_client_sync(
        base_url, timeout=30, verify=egress_ssl_context(self)
    )


def safe_pinned_transport_with_operator_verify(base_url: str):
    # ok: connector-egress-verify-required
    return pinned_async_transport_sync(base_url, verify=self._verify_tls)


def safe_unrelated_callee(base_url: str):
    # ok: connector-egress-verify-required
    return build_ordinary_http_client(base_url, timeout=30)
