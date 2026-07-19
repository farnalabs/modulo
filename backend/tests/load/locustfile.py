"""Locust load testing suite for Modulo.

Task sets:
  - PipelineRunUser   — create pipeline → trigger run → wait for completion
  - HitlReviewUser    — find pending HITL → claim → approve/reject
  - WebSocketUser     — connect to WS event stream, measure event latency

Usage:
  locust -f tests/load/locustfile.py
  locust -f tests/load/locustfile.py --headless -u 50 -r 5 --run-time 5m

Environment variables:
  BASE_URL         - API root (default: http://localhost:8000/api/v1)
  ADMIN_EMAIL      - login email (default: admin@modulo.test)
  ADMIN_PASSWORD   - login password (default: test-password-123)
"""

from __future__ import annotations

import json
import logging
import os
import random
import time
from collections import deque
from typing import Any

import websocket
from locust import HttpUser, between, events, task
from websocket import WebSocket

from tests.load.conftest import (
    DEFAULT_ADMIN_EMAIL,
    DEFAULT_ADMIN_PASSWORD,
    DEFAULT_BASE_URL,
    DEFAULT_TIMEOUT,
    approve_hitl,
    build_ws_url,
    claim_hitl,
    create_pipeline,
    get_pending_hitl,
    get_ws_token,
    login,
    reject_hitl,
    trigger_run,
    wait_for_run,
)

_log = logging.getLogger(__name__)

BASE_URL = os.environ.get("BASE_URL", DEFAULT_BASE_URL)
ADMIN_EMAIL = os.environ.get("ADMIN_EMAIL", DEFAULT_ADMIN_EMAIL)
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", DEFAULT_ADMIN_PASSWORD)


# ---------------------------------------------------------------------------
# Custom event tracking helpers
# ---------------------------------------------------------------------------


def _fire_event(
    request_type: str,
    name: str,
    start: float,
    exception: Exception | None = None,
    length: int = 0,
) -> None:
    elapsed = int((time.time() - start) * 1000)
    if exception is None:
        events.request_success.fire(
            request_type=request_type,
            name=name,
            response_time=elapsed,
            response_length=length,
        )
    else:
        events.request_failure.fire(
            request_type=request_type,
            name=name,
            response_time=elapsed,
            exception=exception,
        )


# ---------------------------------------------------------------------------
# Waitlist for run IDs shared across user types
# ---------------------------------------------------------------------------

_run_waitlist: deque[str] = deque(maxlen=5000)


def _waitlist_ready() -> bool:
    return len(_run_waitlist) > 0


# ---------------------------------------------------------------------------
# Base load user (shared on_start)
# ---------------------------------------------------------------------------


class BaseLoadUser(HttpUser):
    abstract = True
    host = BASE_URL.replace("/api/v1", "")

    def on_start(self) -> None:
        self.token = login(self.client, ADMIN_EMAIL, ADMIN_PASSWORD, BASE_URL)


def _close_ws(ws: Any) -> None:
    if ws is not None:
        try:
            ws.close()
        except Exception:
            _log.warning("WebSocket close failed", exc_info=True)


# ---------------------------------------------------------------------------
# PipelineRunUser (weight: 3, ramp 1->50)
# ---------------------------------------------------------------------------


class PipelineRunUser(BaseLoadUser):
    """Simulates pipeline creation, run triggering, and completion polling."""

    weight = 3
    wait_time = between(5, 15)

    @task
    def pipeline_full_lifecycle(self) -> None:
        start = time.time()
        pipeline_name = f"load-test-pipeline-{random.randrange(10_000_000):07d}"
        try:
            pipeline = create_pipeline(self.client, self.token, pipeline_name, base_url=BASE_URL)
            pipeline_id = pipeline["id"]

            run = trigger_run(self.client, self.token, pipeline_id, base_url=BASE_URL)
            run_id = run["run_id"]

            wait_for_run(self.client, self.token, run_id, timeout=60, base_url=BASE_URL)
            _run_waitlist.append(run_id)

            _fire_event("pipeline", "pipeline_full_lifecycle", start)
        except Exception as exc:
            _fire_event("pipeline", "pipeline_full_lifecycle", start, exception=exc)
            raise


# ---------------------------------------------------------------------------
# HitlReviewUser (weight: 2, ramp 1->20)
# ---------------------------------------------------------------------------


class HitlReviewUser(BaseLoadUser):
    """Simulates human-in-the-loop review workflow."""

    weight = 2
    wait_time = between(2, 8)

    @task
    def hitl_review_cycle(self) -> None:
        if not _waitlist_ready():
            time.sleep(2)
            return

        try:
            run_id = _run_waitlist.popleft()
        except IndexError:
            return
        start = time.time()
        try:
            gates = get_pending_hitl(self.client, self.token, run_id, base_url=BASE_URL)
            if not gates:
                _fire_event("hitl", "hitl_no_pending_gates", start)
                return

            gate = gates[0]
            gate_id = gate["gate_id"]
            claim_token = claim_hitl(self.client, self.token, run_id, gate_id, base_url=BASE_URL)

            if random.random() < 0.8:
                approve_hitl(self.client, self.token, run_id, gate_id, claim_token, base_url=BASE_URL)
                _fire_event("hitl", "hitl_approve", start)
            else:
                reject_hitl(self.client, self.token, run_id, gate_id, claim_token, base_url=BASE_URL)
                _fire_event("hitl", "hitl_reject", start)
        except Exception as exc:
            _fire_event("hitl", "hitl_review_cycle", start, exception=exc)
            raise


# ---------------------------------------------------------------------------
# WebSocketUser (weight: 1, ramp 1->10)
# ---------------------------------------------------------------------------


class WebSocketUser(BaseLoadUser):
    """Simulates WebSocket event stream subscribers."""

    weight = 1
    wait_time = between(10, 30)

    def on_start(self) -> None:
        super().on_start()
        self._ws: WebSocket | None = None

    def on_stop(self) -> None:
        _close_ws(self._ws)

    @task
    def ws_event_subscribe(self) -> None:
        if not _waitlist_ready():
            time.sleep(2)
            return

        try:
            run_id = _run_waitlist[-1]
        except IndexError:
            return
        start = time.time()
        try:
            ws_token = get_ws_token(self.client, self.token, base_url=BASE_URL)
            ws_url = build_ws_url(BASE_URL, run_id, ws_token, since_event_seq=0)

            self._ws = websocket.create_connection(ws_url, timeout=DEFAULT_TIMEOUT)
            self._ws.settimeout(5)

            first_event = self._ws.recv()
            event_latency = int((time.time() - start) * 1000)
            events.request_success.fire(
                request_type="ws",
                name="ws_first_event_latency",
                response_time=event_latency,
                response_length=len(first_event),
            )

            listen_deadline = time.time() + 15
            event_count = 1
            while time.time() < listen_deadline:
                try:
                    data = self._ws.recv()
                    parsed = json.loads(data)
                    event_count += 1
                    if parsed.get("status") == "terminal":
                        break
                except (websocket.WebSocketTimeoutException, TimeoutError):
                    break

            _fire_event("ws", "ws_subscribe_session", start, length=event_count)
        except Exception as exc:
            _fire_event("ws", "ws_subscribe_session", start, exception=exc)
            raise
        finally:
            _close_ws(self._ws)
            self._ws = None


# ---------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------


@events.init.add_listener
def on_locust_init(environment, **_kwargs):
    _log.info("Locust load test initialised")
    _log.info("  BASE_URL    = %s", BASE_URL)
    _log.info("  UserClasses = PipelineRun (w=3), HITL (w=2), WS (w=1)")
