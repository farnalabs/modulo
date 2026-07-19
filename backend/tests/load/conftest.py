"""Shared fixtures and helpers for Locust load tests.

Provides authenticated HTTP session helpers, pipeline creation, run trigger,
HITL workflow, and WebSocket token acquisition.  All helpers are synchronous
and designed for Locust's gevent-based execution model.
"""

import logging
import random
import time
from typing import Any
from urllib.parse import quote, urlparse, urlunparse

_log = logging.getLogger(__name__)

DEFAULT_BASE_URL = "http://localhost:8000/api/v1"
DEFAULT_TIMEOUT = 30.0
DEFAULT_ADMIN_EMAIL = "admin@modulo.test"
DEFAULT_ADMIN_PASSWORD = "test-password-123"
_TERMINAL_STATUSES = frozenset({"complete", "failed", "cancelled", "eval_failed"})
_MIN_POLL_INTERVAL = 0.1
_HITL_CLAIM_EXPIRY_MINUTES = 15


def _auth_headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def login(
    client: Any,
    email: str = "admin@modulo.test",
    password: str = "test-password-123",
    base_url: str = DEFAULT_BASE_URL,
) -> str:
    """Authenticate and return a Bearer access token."""
    resp = client.post(f"{base_url}/auth/login", json={"email": email, "password": password}, timeout=DEFAULT_TIMEOUT)
    resp.raise_for_status()
    return resp.json()["access_token"]


def create_pipeline(
    client: Any,
    token: str,
    name: str,
    base_url: str = DEFAULT_BASE_URL,
    **kwargs: Any,
) -> dict[str, Any]:
    """Create a pipeline and return the full response dict."""
    headers = _auth_headers(token)
    payload = {
        "name": name,
        "description": kwargs.get("description", ""),
        "visibility": kwargs.get("visibility", "org"),
        "max_concurrent_runs": kwargs.get("max_concurrent_runs", 5),
        "lock_wait_timeout_seconds": kwargs.get("lock_wait_timeout_seconds", 300),
        "node_timeout_seconds": kwargs.get("node_timeout_seconds", 300),
        "run_context_defaults": kwargs.get("run_context_defaults", {}),
        "default_autonomy_level": kwargs.get("default_autonomy_level", "manual_approval"),
    }
    resp = client.post(f"{base_url}/pipelines", json=payload, headers=headers, timeout=DEFAULT_TIMEOUT)
    resp.raise_for_status()
    return resp.json()


def trigger_run(
    client: Any,
    token: str,
    pipeline_id: str,
    base_url: str = DEFAULT_BASE_URL,
    input_payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Trigger a pipeline run and return the run response (status 202)."""
    headers = _auth_headers(token)
    resp = client.post(
        f"{base_url}/runs",
        json={"pipeline_id": pipeline_id, "input_payload": input_payload or {}},
        headers=headers,
        timeout=DEFAULT_TIMEOUT,
    )
    resp.raise_for_status()
    return resp.json()


def get_run(
    client: Any,
    token: str,
    run_id: str,
    base_url: str = DEFAULT_BASE_URL,
) -> dict[str, Any]:
    """Fetch run status (single request, no polling)."""
    headers = _auth_headers(token)
    resp = client.get(f"{base_url}/runs/{run_id}", headers=headers, timeout=DEFAULT_TIMEOUT)
    resp.raise_for_status()
    return resp.json()


def wait_for_run(
    client: Any,
    token: str,
    run_id: str,
    timeout: float = 60.0,
    poll_interval: float = 1.0,
    base_url: str = DEFAULT_BASE_URL,
) -> dict[str, Any]:
    """Poll a run until it reaches a terminal state or the timeout expires."""
    if timeout <= 0:
        msg = f"wait_for_run timeout must be positive, got {timeout}"
        raise ValueError(msg)
    deadline = time.time() + timeout
    while time.time() < deadline:
        run = get_run(client, token, run_id, base_url)
        status = run.get("status")
        if status is None:
            _log.warning("Run %s response missing 'status' key: %s", run_id, run)
        elif status in _TERMINAL_STATUSES:
            return run
        effective_poll = max(poll_interval, _MIN_POLL_INTERVAL)
        jitter = random.uniform(0, effective_poll * 0.5)
        time.sleep(effective_poll + jitter)
    msg = f"Run {run_id} did not reach terminal state within {timeout}s"
    raise TimeoutError(msg)


def get_ws_token(
    client: Any,
    token: str,
    base_url: str = DEFAULT_BASE_URL,
) -> str:
    """Acquire a short-lived WebSocket token."""
    headers = _auth_headers(token)
    resp = client.post(f"{base_url}/auth/ws-token", json={}, headers=headers, timeout=DEFAULT_TIMEOUT)
    resp.raise_for_status()
    return resp.json()["ws_token"]


def build_ws_url(
    base_url: str,
    run_id: str,
    ws_token: str,
    since_event_seq: int = 0,
) -> str:
    """Build a WebSocket URL for the run event stream."""
    parsed = urlparse(base_url)
    ws_scheme = "wss" if parsed.scheme == "https" else "ws"
    qs = quote(ws_token, safe="")
    return urlunparse(
        (
            ws_scheme,
            parsed.netloc,
            f"/api/v1/runs/{run_id}/ws",
            "",
            f"token={qs}&since_event_seq={since_event_seq}",
            "",
        ),
    )


def get_pending_hitl(
    client: Any,
    token: str,
    run_id: str,
    base_url: str = DEFAULT_BASE_URL,
) -> list[dict[str, Any]]:
    """List all pending (undecided) HITL gates for a run."""
    headers = _auth_headers(token)
    resp = client.get(f"{base_url}/runs/{run_id}/hitl/pending", headers=headers, timeout=DEFAULT_TIMEOUT)
    resp.raise_for_status()
    return resp.json().get("gates", [])


def _hitl_action(
    client: Any,
    token: str,
    run_id: str,
    gate_id: str,
    claim_token: str,
    action: str,
    base_url: str = DEFAULT_BASE_URL,
    extra_payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Shared helper for HITL approve/reject actions."""
    headers = _auth_headers(token)
    payload: dict[str, Any] = {"claim_token": claim_token}
    if extra_payload:
        payload.update(extra_payload)
    resp = client.post(
        f"{base_url}/runs/{run_id}/hitl/{gate_id}/{action}",
        json=payload,
        headers=headers,
        timeout=DEFAULT_TIMEOUT,
    )
    resp.raise_for_status()
    return resp.json()


def claim_hitl(
    client: Any,
    token: str,
    run_id: str,
    gate_id: str,
    base_url: str = DEFAULT_BASE_URL,
) -> str:
    """Claim a HITL gate and return the claim token."""
    headers = _auth_headers(token)
    resp = client.post(
        f"{base_url}/runs/{run_id}/hitl/{gate_id}/claim",
        json={"expiry_minutes": _HITL_CLAIM_EXPIRY_MINUTES},
        headers=headers,
        timeout=DEFAULT_TIMEOUT,
    )
    resp.raise_for_status()
    return resp.json()["claim_token"]


def approve_hitl(
    client: Any,
    token: str,
    run_id: str,
    gate_id: str,
    claim_token: str,
    base_url: str = DEFAULT_BASE_URL,
) -> None:
    """Approve an interrupted HITL gate and resume the run."""
    _hitl_action(client, token, run_id, gate_id, claim_token, "approve", base_url=base_url)


def reject_hitl(
    client: Any,
    token: str,
    run_id: str,
    gate_id: str,
    claim_token: str,
    base_url: str = DEFAULT_BASE_URL,
) -> None:
    """Reject an interrupted HITL gate."""
    _hitl_action(
        client,
        token,
        run_id,
        gate_id,
        claim_token,
        "reject",
        base_url=base_url,
        extra_payload={"reason": "Automated load test rejection"},
    )
