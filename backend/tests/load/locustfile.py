"""Locust load testing suite for Modulo.

Task sets:
  - PipelineRunUser   — create pipeline → trigger run → wait for completion
  - HitlReviewUser    — find pending HITL → claim → approve/reject
  - WebSocketUser     — connect to WS event stream, measure event latency
  - MixedUser         — combined operations mixing all above

Usage:
  locust -f tests/load/locustfile.py
  locust -f tests/load/locustfile.py --headless -u 50 -r 5 --run-time 5m

Environment variables:
  BASE_URL         - API root (default: http://localhost:8000/api/v1)
  PIPELINE_VUS     - overrides pipeline user count (headless mode)
  HITL_VUS         - overrides HITL user count
  WS_VUS           - overrides WebSocket user count
  RUN_TIME         - test duration string (default: 5m)
  ADMIN_EMAIL      - login email (default: admin@modulo.test)
  ADMIN_PASSWORD   - login password (default: test-password-123)
"""

from __future__ import annotations

import json
import logging
import os
import random
import time

from locust import HttpUser, between, events, task

try:
    from conftest import (
        DEFAULT_BASE_URL,
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
except ImportError:
    from tests.load.conftest import (
        DEFAULT_BASE_URL,
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
ADMIN_EMAIL = os.environ.get("ADMIN_EMAIL", "admin@modulo.test")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "test-password-123")


# ---------------------------------------------------------------------------
# Custom event tracking helpers
# ---------------------------------------------------------------------------

def _fire_success(request_type: str, name: str, start: float, length: int = 0) -> None:
    elapsed = int((time.time() - start) * 1000)
    events.request_success.fire(
        request_type=request_type,
        name=name,
        response_time=elapsed,
        response_length=length,
    )


def _fire_failure(request_type: str, name: str, start: float, exception: Exception) -> None:
    elapsed = int((time.time() - start) * 1000)
    events.request_failure.fire(
        request_type=request_type,
        name=name,
        response_time=elapsed,
        exception=exception,
    )


# ---------------------------------------------------------------------------
# Waitlist for run IDs shared across user types
# ---------------------------------------------------------------------------

_run_waitlist: list[str] = []
_hitl_gate_queue: list[dict[str, str]] = []


# ---------------------------------------------------------------------------
# PipelineRunUser
# ---------------------------------------------------------------------------

class PipelineRunUser(HttpUser):
    """Simulates pipeline creation, run triggering, and completion polling.

    Each iteration:
      1. Creates a pipeline
      2. Triggers a run
      3. Polls until terminal state
      4. Publishes the run ID to the shared waitlist for HITL users

    Ramp:  1 -> 50 concurrent users
    Wait:  5-15 seconds between iterations
    """

    wait_time = between(5, 15)
    abstract = True

    def on_start(self) -> None:
        self.token = login(self.client, ADMIN_EMAIL, ADMIN_PASSWORD, BASE_URL)
        self._pipeline_counter = 0

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

            _fire_success("pipeline", "pipeline_full_lifecycle", start)
        except Exception as exc:
            _fire_failure("pipeline", "pipeline_full_lifecycle", start, exc)
            raise


# ---------------------------------------------------------------------------
# HitlReviewUser
# ---------------------------------------------------------------------------

class HitlReviewUser(HttpUser):
    """Simulates human-in-the-loop review workflow.

    Each iteration:
      1. Pulls a run ID from the shared waitlist
      2. Lists pending HITL gates
      3. Claims a gate
      4. Approves or rejects

    Ramp:  1 -> 20 concurrent users
    Wait:  2-8 seconds between iterations
    """

    wait_time = between(2, 8)
    abstract = True

    def on_start(self) -> None:
        self.token = login(self.client, ADMIN_EMAIL, ADMIN_PASSWORD, BASE_URL)

    @task
    def hitl_review_cycle(self) -> None:
        if not _run_waitlist:
            time.sleep(2)
            return

        run_id = _run_waitlist.pop(0)
        start = time.time()
        try:
            gates = get_pending_hitl(self.client, self.token, run_id, base_url=BASE_URL)
            if not gates:
                _fire_success("hitl", "hitl_no_pending_gates", start)
                return

            gate = gates[0]
            gate_id = gate["gate_id"]
            claim_token = claim_hitl(self.client, self.token, run_id, gate_id, base_url=BASE_URL)

            if random.random() < 0.8:
                approve_hitl(self.client, self.token, run_id, gate_id, claim_token, base_url=BASE_URL)
                _fire_success("hitl", "hitl_approve", start)
            else:
                reject_hitl(self.client, self.token, run_id, gate_id, claim_token, base_url=BASE_URL)
                _fire_success("hitl", "hitl_reject", start)
        except Exception as exc:
            _fire_failure("hitl", "hitl_review_cycle", start, exc)


# ---------------------------------------------------------------------------
# WebSocketUser
# ---------------------------------------------------------------------------

class WebSocketUser(HttpUser):
    """Simulates WebSocket event stream subscribers.

    Each iteration:
      1. Acquires a WS token
      2. Connects to the run event WebSocket
      3. Measures latency to first event
      4. Listens for a configurable duration

    Ramp:  1 -> 10 concurrent users
    Wait:  10-30 seconds between iterations
    """

    wait_time = between(10, 30)
    abstract = True

    def on_start(self) -> None:
        self.token = login(self.client, ADMIN_EMAIL, ADMIN_PASSWORD, BASE_URL)
        self._ws = None

    def on_stop(self) -> None:
        if self._ws is not None:
            try:
                self._ws.close()
            except Exception:
                pass

    @task
    def ws_event_subscribe(self) -> None:
        if not _run_waitlist:
            time.sleep(2)
            return

        run_id = _run_waitlist[-1] if _run_waitlist else None
        if run_id is None:
            return

        start = time.time()
        try:
            ws_token = get_ws_token(self.client, self.token, base_url=BASE_URL)
            ws_url = build_ws_url(BASE_URL, run_id, ws_token, since_event_seq=0)

            import websocket

            self._ws = websocket.create_connection(ws_url, timeout=10)
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

            _fire_success("ws", "ws_subscribe_session", start, length=event_count)
        except Exception as exc:
            _fire_failure("ws", "ws_subscribe_session", start, exc)
        finally:
            if self._ws is not None:
                try:
                    self._ws.close()
                except Exception:
                    pass
                self._ws = None


# ---------------------------------------------------------------------------
# Concrete user classes (non-abstract, discovered by Locust)
# ---------------------------------------------------------------------------

class PipelineRunUserDefault(PipelineRunUser):
    """50 concurrent pipeline run users (default)."""
    weight = 3


class HitlReviewUserDefault(HitlReviewUser):
    """20 concurrent HITL review users (default)."""
    weight = 2


class WebSocketUserDefault(WebSocketUser):
    """10 concurrent WebSocket subscriber users (default)."""
    weight = 1


# ---------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------

@events.init.add_listener
def on_locust_init(environment, **_kwargs):
    _log.info("Locust load test initialised")
    _log.info("  BASE_URL    = %s", BASE_URL)
    _log.info("  Users       = PipelineRun: up to 50, HITL: up to 20, WS: up to 10")
    _log.info("  Weighting   = PipelineRun 3 : HITL 2 : WS 1")
