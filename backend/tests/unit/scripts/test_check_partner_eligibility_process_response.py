"""Unit tests for the new ``_process_response`` helper in check_partner_eligibility.py."""

from __future__ import annotations

import json
import subprocess
from unittest.mock import MagicMock

import check_partner_eligibility as mod
import pytest
from check_partner_eligibility import GithubApiError


def _resp(status_code: int, *, headers: dict | None = None, json_body=None):
    resp = MagicMock()
    resp.status_code = status_code
    resp.headers = headers or {}
    if json_body is not None:
        resp.json.return_value = json_body
    else:
        resp.json.side_effect = json.JSONDecodeError("not JSON", "", 0)
    return resp


def test_process_response_ok_returns_json_and_headers():
    resp = _resp(200, headers={"X-A": "1"}, json_body={"login": "acme"})
    result = mod._process_response(resp, 0, ())
    assert result == ({"login": "acme"}, {"X-A": "1"})


def test_process_response_non_json_body_raises():
    resp = _resp(200, json_body=None)
    with pytest.raises(GithubApiError, match="non-JSON"):
        mod._process_response(resp, 0, ())


def test_process_response_rate_limit_raises():
    resp = _resp(403, headers={"X-RateLimit-Remaining": "0"})
    with pytest.raises(GithubApiError, match="rate limit"):
        mod._process_response(resp, 0, ())


def test_process_response_401_raises_auth_error():
    resp = _resp(401)
    with pytest.raises(GithubApiError, match="auth/permission"):
        mod._process_response(resp, 0, ())


def test_process_response_403_raises_auth_error():
    resp = _resp(403)
    with pytest.raises(GithubApiError, match="auth/permission"):
        mod._process_response(resp, 0, ())


def test_process_response_allowed_code_returns_empty_dict(monkeypatch):
    monkeypatch.setattr(mod, "_backoff", lambda *a, **k: None)
    resp = _resp(404)
    result = mod._process_response(resp, 0, (404,))
    assert result == ({}, resp.headers)


def test_process_response_disallowed_4xx_raises():
    resp = _resp(404)
    with pytest.raises(GithubApiError, match="HTTP 404"):
        mod._process_response(resp, 0, ())


def test_process_response_429_retryable_returns_none(monkeypatch):
    monkeypatch.setattr(mod, "_backoff", lambda *a, **k: None)
    resp = _resp(429, headers={"Retry-After": "2"})
    assert mod._process_response(resp, 0, ()) is None


def test_process_response_500_retryable_returns_none(monkeypatch):
    monkeypatch.setattr(mod, "_backoff", lambda *a, **k: None)
    resp = _resp(500)
    assert mod._process_response(resp, 0, ()) is None


def test_process_response_429_last_attempt_raises(monkeypatch):
    monkeypatch.setattr(mod, "_backoff", lambda *a, **k: None)
    resp = _resp(429)
    with pytest.raises(GithubApiError, match="HTTP 429"):
        # attempt == _MAX_RETRIES - 1 means no further retry
        mod._process_response(resp, mod._MAX_RETRIES - 1, ())


def test_run_gh_once_returns_completed_process(monkeypatch):
    fake = subprocess.CompletedProcess(["gh"], 0, stdout="HTTP/1.1 200\n\n{}", stderr="")
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: fake)
    result = mod._run_gh_once("orgs/acme")
    assert result.returncode == 0


def test_run_gh_once_oserror_returns_transient(monkeypatch):
    def _boom(*a, **k):
        raise OSError("no gh")

    monkeypatch.setattr(subprocess, "run", _boom)
    assert mod._run_gh_once("orgs/acme") is mod._TRANSIENT_GH


def test_gh_fetch_with_retries_parses_success(monkeypatch):
    stdout = 'HTTP/1.1 200\nx-ratelimit-remaining: 5\n\n{"login": "acme"}'
    fake = subprocess.CompletedProcess(["gh"], 0, stdout=stdout, stderr="")
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: fake)
    monkeypatch.setattr(mod, "_backoff", lambda *a, **k: None)
    result = mod._gh_fetch_with_retries("orgs/acme")
    assert result == ({"login": "acme"}, {"x-ratelimit-remaining": "5"})


def test_gh_fetch_with_retries_transient_falls_back_to_none(monkeypatch):
    # gh reports a transient server error; after retries it falls back to None.
    fake = subprocess.CompletedProcess(["gh"], 1, stdout="", stderr="503 Service Unavailable")
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: fake)
    monkeypatch.setattr(mod, "_backoff", lambda *a, **k: None)

    # Force the transient branch so all retries are consumed.
    monkeypatch.setattr(mod, "_gh_is_transient", lambda result: True)
    assert mod._gh_fetch_with_retries("orgs/acme") is None
