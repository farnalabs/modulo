"""Unit tests for the OSS Partner eligibility checker.

The GitHub network layer (``gather_repo_evidence``) is patched so no network
calls are made -- every scenario is driven from a synthetic evidence dict.
"""

from __future__ import annotations

import datetime as _dt
import json
import subprocess

import check_partner_eligibility as mod
import pytest
from check_partner_eligibility import GithubApiError


def _base_evidence(now: _dt.datetime) -> dict:
    """A fully-qualifying repo as of *now*."""
    return {
        "repo_id": 123456,
        "full_name": "acme/widgets",
        "private": False,
        "fork": False,
        "archived": False,
        "stargazers_count": 1500,
        "created_at": (now - _dt.timedelta(days=400)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "spdx_id": "MIT",
        "license_file_present": True,
        "tags": [{"name": "v1.0.0"}],
        "commits": [
            {"author": {"login": "alice"}},
            {"author": {"login": "bob"}},
            {"author": {"login": "dependabot[bot]"}},
        ],
        "now": now,
    }


@pytest.fixture
def now() -> _dt.datetime:
    return _dt.datetime(2026, 1, 15, 12, 0, 0, tzinfo=_dt.UTC)


def test_fully_qualifying_repo_is_eligible(now: _dt.datetime) -> None:
    result = mod.evaluate_evidence(_base_evidence(now))
    assert result["verdict"] == mod.VERDICT_ELIGIBLE
    assert result["repo_id"] == 123456
    assert all(c["status"] == mod.STATUS_PASS for c in result["criteria"].values())


def test_fork_is_ineligible(now: _dt.datetime) -> None:
    ev = _base_evidence(now)
    ev["fork"] = True
    result = mod.evaluate_evidence(ev)
    assert result["verdict"] == mod.VERDICT_INELIGIBLE
    assert result["criteria"]["public"]["reason"] == mod.RC_IS_FORK


def test_archived_is_ineligible(now: _dt.datetime) -> None:
    ev = _base_evidence(now)
    ev["archived"] = True
    result = mod.evaluate_evidence(ev)
    assert result["verdict"] == mod.VERDICT_INELIGIBLE
    assert result["criteria"]["public"]["reason"] == mod.RC_IS_ARCHIVED


def test_private_is_ineligible(now: _dt.datetime) -> None:
    ev = _base_evidence(now)
    ev["private"] = True
    result = mod.evaluate_evidence(ev)
    assert result["verdict"] == mod.VERDICT_INELIGIBLE
    assert result["criteria"]["public"]["reason"] == mod.RC_NOT_PUBLIC


def test_below_stars_is_ineligible(now: _dt.datetime) -> None:
    ev = _base_evidence(now)
    ev["stargazers_count"] = 42
    result = mod.evaluate_evidence(ev)
    assert result["verdict"] == mod.VERDICT_INELIGIBLE
    assert result["criteria"]["stars"]["reason"] == mod.RC_BELOW_STARS


def test_too_young_is_ineligible(now: _dt.datetime) -> None:
    ev = _base_evidence(now)
    ev["created_at"] = (now - _dt.timedelta(days=10)).strftime("%Y-%m-%dT%H:%M:%SZ")
    result = mod.evaluate_evidence(ev)
    assert result["verdict"] == mod.VERDICT_INELIGIBLE
    assert result["criteria"]["age"]["reason"] == mod.RC_TOO_YOUNG


def test_no_tagged_release_is_ineligible(now: _dt.datetime) -> None:
    ev = _base_evidence(now)
    ev["tags"] = []
    result = mod.evaluate_evidence(ev)
    assert result["verdict"] == mod.VERDICT_INELIGIBLE
    assert result["criteria"]["release"]["reason"] == mod.RC_NO_TAGGED_RELEASE


def test_insufficient_committers_is_ineligible(now: _dt.datetime) -> None:
    ev = _base_evidence(now)
    # Only one human committer (the other entries are bots).
    ev["commits"] = [
        {"author": {"login": "alice"}},
        {"author": {"login": "dependabot[bot]"}},
        {"committer": {"login": "renovate[bot]"}},
    ]
    result = mod.evaluate_evidence(ev)
    assert result["verdict"] == mod.VERDICT_INELIGIBLE
    assert result["criteria"]["committers"]["reason"] == mod.RC_INSUFFICIENT_COMMITTERS


def test_license_file_fallback_passes(now: _dt.datetime) -> None:
    ev = _base_evidence(now)
    ev["spdx_id"] = "NOASSERTION"
    # licence file present -> fallback pass.
    result = mod.evaluate_evidence(ev)
    assert result["criteria"]["license"]["status"] == mod.STATUS_PASS
    assert result["criteria"]["license"]["reason"] == mod.RC_LICENSE_FILE_FALLBACK


def test_no_osi_license_fails(now: _dt.datetime) -> None:
    ev = _base_evidence(now)
    ev["spdx_id"] = "NOASSERTION"
    ev["license_file_present"] = False
    result = mod.evaluate_evidence(ev)
    assert result["criteria"]["license"]["status"] == mod.STATUS_FAIL
    assert result["criteria"]["license"]["reason"] == mod.RC_NO_OSI_LICENSE


def test_no_osi_license_with_unknown_spdx_id(now: _dt.datetime) -> None:
    # A non-OSI spdx id (e.g. LicenseRef-Unknown) with no LICENSE file -> fail.
    ev = _base_evidence(now)
    ev["spdx_id"] = "LicenseRef-Unknown"
    ev["license_file_present"] = False
    result = mod.evaluate_evidence(ev)
    assert result["criteria"]["license"]["status"] == mod.STATUS_FAIL
    assert result["criteria"]["license"]["reason"] == mod.RC_NO_OSI_LICENSE


def test_license_file_fallback_with_null_spdx(now: _dt.datetime) -> None:
    # spdx_id null (GitHub returned no license object) but a LICENSE file is
    # detected -> the fallback path still passes the licence criterion.
    ev = _base_evidence(now)
    ev["spdx_id"] = None
    ev["license_file_present"] = True
    result = mod.evaluate_evidence(ev)
    assert result["criteria"]["license"]["status"] == mod.STATUS_PASS
    assert result["criteria"]["license"]["reason"] == mod.RC_LICENSE_FILE_FALLBACK


def test_lowercase_osi_spdx_is_eligible(now: _dt.datetime) -> None:
    # Licence match must be case-insensitive: "mit" (lowercase) is accepted.
    ev = _base_evidence(now)
    ev["spdx_id"] = "mit"
    result = mod.evaluate_evidence(ev)
    assert result["verdict"] == mod.VERDICT_ELIGIBLE
    assert result["criteria"]["license"]["status"] == mod.STATUS_PASS
    assert result["criteria"]["license"]["reason"] is None


def test_known_non_osi_spdx_with_license_file_is_ineligible(now: _dt.datetime) -> None:
    # A KNOWN non-OSI spdx id (GPL-1.0) must be rejected even when a LICENSE
    # file is present -- it must not be false-accepted via the fallback.
    ev = _base_evidence(now)
    ev["spdx_id"] = "GPL-1.0"
    ev["license_file_present"] = True
    result = mod.evaluate_evidence(ev)
    assert result["verdict"] == mod.VERDICT_INELIGIBLE
    assert result["criteria"]["license"]["status"] == mod.STATUS_FAIL
    assert result["criteria"]["license"]["reason"] == mod.RC_NO_OSI_LICENSE


def test_exit_code_mapping() -> None:
    # The CLI must distinguish verdicts via distinct exit codes.
    assert mod._exit_code(mod.VERDICT_ELIGIBLE) == 0
    assert mod._exit_code(mod.VERDICT_INELIGIBLE) == 1
    assert mod._exit_code(mod.VERDICT_INCONCLUSIVE) == 2


def test_age_boundary_exactly_eligible(now: _dt.datetime) -> None:
    # A repo exactly MIN_AGE_DAYS old is old enough to pass.
    ev = _base_evidence(now)
    ev["created_at"] = (now - _dt.timedelta(days=mod.MIN_AGE_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ")
    result = mod.evaluate_evidence(ev)
    assert result["criteria"]["age"]["status"] == mod.STATUS_PASS
    assert result["verdict"] == mod.VERDICT_ELIGIBLE


def test_age_boundary_just_below_ineligible(now: _dt.datetime) -> None:
    # One day under the boundary -> TOO_YOUNG (covers the ~5 months 29 days case).
    ev = _base_evidence(now)
    ev["created_at"] = (now - _dt.timedelta(days=mod.MIN_AGE_DAYS - 1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    result = mod.evaluate_evidence(ev)
    assert result["criteria"]["age"]["status"] == mod.STATUS_FAIL
    assert result["criteria"]["age"]["reason"] == mod.RC_TOO_YOUNG


def test_reason_code_surfaced_in_human_output(
    now: _dt.datetime, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    ev = _base_evidence(now)
    ev["private"] = True

    def _fake(target: str) -> dict:
        return ev

    monkeypatch.setattr(mod, "gather_repo_evidence", _fake)
    mod.main(["acme/widgets"])
    out = capsys.readouterr().out
    assert mod.RC_NOT_PUBLIC in out


def test_reason_code_surfaced_in_json_output(
    now: _dt.datetime, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    ev = _base_evidence(now)
    ev["private"] = True

    def _fake(target: str) -> dict:
        return ev

    monkeypatch.setattr(mod, "gather_repo_evidence", _fake)
    mod.main(["acme/widgets", "--json"])
    parsed = json.loads(capsys.readouterr().out)
    assert parsed["criteria"]["public"]["reason"] == mod.RC_NOT_PUBLIC


def _patch_api_responses(monkeypatch, status_code: int, headers: dict) -> None:
    """Force ``gather_repo_evidence`` through the requests fallback with a fixed HTTP response."""

    class _Resp:
        def __init__(self) -> None:
            self.status_code = status_code
            self.headers = headers

        def json(self) -> dict:
            return {}

        def raise_for_status(self) -> None:
            return None

    def _run(*_args, **_kwargs):
        # gh CLI unavailable -> fall through to the requests path.
        raise FileNotFoundError()

    def _get(*_args, **_kwargs):
        return _Resp()

    monkeypatch.setattr(mod.subprocess, "run", _run)
    monkeypatch.setattr(mod.requests, "get", _get)


def test_403_rate_limit_is_inconclusive(now: _dt.datetime, monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_api_responses(
        monkeypatch,
        status_code=403,
        headers={"X-RateLimit-Remaining": "0"},
    )
    result = mod.check_eligibility("acme/widgets", fetcher=mod.gather_repo_evidence)
    assert result["verdict"] == mod.VERDICT_INCONCLUSIVE
    assert result["criteria"]["api"]["reason"] == mod.RC_API_FAILURE
    # Never a false reject.
    assert result["verdict"] != mod.VERDICT_INELIGIBLE


def test_401_token_expiry_is_inconclusive(now: _dt.datetime, monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_api_responses(monkeypatch, status_code=401, headers={})
    result = mod.check_eligibility("acme/widgets", fetcher=mod.gather_repo_evidence)
    assert result["verdict"] == mod.VERDICT_INCONCLUSIVE
    assert result["criteria"]["api"]["reason"] == mod.RC_API_FAILURE
    assert result["verdict"] != mod.VERDICT_INELIGIBLE


def test_api_failure_is_inconclusive(monkeypatch, now: _dt.datetime) -> None:
    def _raise(target: str) -> dict:
        raise GithubApiError("rate limit")

    monkeypatch.setattr(mod, "gather_repo_evidence", _raise)
    result = mod.check_eligibility("acme/widgets")
    assert result["verdict"] == mod.VERDICT_INCONCLUSIVE
    assert result["criteria"]["api"]["reason"] == mod.RC_API_FAILURE
    assert "escalation" in result


def test_json_output_shape(
    now: _dt.datetime, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    ev = _base_evidence(now)

    def _fake(target: str) -> dict:
        return ev

    monkeypatch.setattr(mod, "gather_repo_evidence", _fake)
    mod.main(["acme/widgets", "--json"])

    captured = capsys.readouterr().out
    parsed = json.loads(captured)
    assert parsed["verdict"] == mod.VERDICT_ELIGIBLE
    for key in ("repo_id", "full_name", "verdict", "criteria", "evidence"):
        assert key in parsed
    assert "public" in parsed["criteria"]
    assert "license" in parsed["criteria"]


def test_parse_target_variants() -> None:
    assert mod.parse_target("https://github.com/foo/bar") == "foo/bar"
    assert mod.parse_target("http://github.com/foo/bar/") == "foo/bar"
    assert mod.parse_target("github.com/foo/bar") == "foo/bar"
    assert mod.parse_target("foo/bar") == "foo/bar"
    assert mod.parse_target("foo/bar.git") == "foo/bar"
    with pytest.raises(ValueError, match="Could not parse"):
        mod.parse_target("not-a-repo")


def test_gh_nonjson_stdout_is_inconclusive(now: _dt.datetime, monkeypatch: pytest.MonkeyPatch) -> None:
    # gh returns 0 but emits non-JSON stdout -> must degrade to INCONCLUSIVE.
    cp = subprocess.CompletedProcess(args=["gh"], returncode=0, stdout="not json", stderr="")
    monkeypatch.setattr(mod.subprocess, "run", lambda *a, **k: cp)
    result = mod.check_eligibility("acme/widgets", fetcher=mod.gather_repo_evidence)
    assert result["verdict"] == mod.VERDICT_INCONCLUSIVE
    assert result["criteria"]["api"]["reason"] == mod.RC_API_FAILURE


def test_gh_oserror_falls_through_to_requests(now: _dt.datetime, monkeypatch: pytest.MonkeyPatch) -> None:
    # gh cannot be launched (OSError) -> the requests path must be used.
    calls = {"gh": 0, "requests": 0}

    class _Resp:
        status_code = 200

        def __init__(self, payload: dict | list) -> None:
            self.headers: dict = {}
            self._payload = payload

        def json(self) -> dict | list:
            return self._payload

        def raise_for_status(self) -> None:
            return None

    def _run(*_args, **_kwargs):
        calls["gh"] += 1
        raise PermissionError("denied")

    def _get(url, *_args, **_kwargs):
        calls["requests"] += 1
        if "/tags" in url:
            return _Resp([{"name": "v1"}])
        if "/commits" in url:
            return _Resp([{"author": {"login": "alice"}}, {"author": {"login": "bob"}}])
        if "/license" in url:
            return _Resp({})
        return _Resp(
            {
                "id": 1,
                "full_name": "acme/widgets",
                "private": False,
                "fork": False,
                "archived": False,
                "stargazers_count": 1500,
                "created_at": "2020-01-01T00:00:00Z",
                "license": {"spdx_id": "MIT"},
            }
        )

    monkeypatch.setattr(mod.subprocess, "run", _run)
    monkeypatch.setattr(mod.requests, "get", _get)
    result = mod.check_eligibility("acme/widgets", fetcher=mod.gather_repo_evidence)
    assert calls["gh"] >= 1
    assert calls["requests"] >= 1
    assert result["verdict"] == mod.VERDICT_ELIGIBLE


def _fake_api_get_factory(now: _dt.datetime):
    """Build an ``_api_get_with_headers`` fake that returns per-path fixtures."""

    def _fake(path: str, *, allow_codes: tuple[int, ...] = ()):
        if path.startswith("repos/") and not any(p in path for p in ("/tags", "/commits", "/license")):
            return _fake.repos_payload, {}  # type: ignore[attr-defined]
        if "/license" in path:
            return {}, {}
        if "/tags" in path:
            return [{"name": "v1"}], {}
        if "/commits" in path:
            return [{"author": {"login": "alice"}}, {"author": {"login": "bob"}}], {}
        return {}, {}

    return _fake


def test_partial_repos_response_is_inconclusive(now: _dt.datetime, monkeypatch: pytest.MonkeyPatch) -> None:
    # A /repos response missing stargazers_count must be INCONCLUSIVE, not a
    # definitive reject.
    fake = _fake_api_get_factory(now)
    fake.repos_payload = {  # type: ignore[attr-defined]
        "id": 1,
        "full_name": "acme/widgets",
        "private": False,
        "fork": False,
        "archived": False,
        "created_at": "2020-01-01T00:00:00Z",
        "license": {"spdx_id": "MIT"},
    }
    monkeypatch.setattr(mod, "_api_get_with_headers", fake)
    result = mod.check_eligibility("acme/widgets", fetcher=mod.gather_repo_evidence)
    assert result["verdict"] == mod.VERDICT_INCONCLUSIVE
    assert result["criteria"]["api"]["reason"] == mod.RC_API_FAILURE


def test_unparseable_created_at_is_inconclusive(now: _dt.datetime, monkeypatch: pytest.MonkeyPatch) -> None:
    # An unparseable created_at from the API must be INCONCLUSIVE, never a
    # hard TOO_YOUNG reject.
    fake = _fake_api_get_factory(now)
    fake.repos_payload = {  # type: ignore[attr-defined]
        "id": 1,
        "full_name": "acme/widgets",
        "private": False,
        "fork": False,
        "archived": False,
        "stargazers_count": 1500,
        "created_at": "definitely-not-a-date",
        "license": {"spdx_id": "MIT"},
    }
    monkeypatch.setattr(mod, "_api_get_with_headers", fake)
    result = mod.check_eligibility("acme/widgets", fetcher=mod.gather_repo_evidence)
    assert result["verdict"] == mod.VERDICT_INCONCLUSIVE
    assert result["criteria"]["api"]["reason"] == mod.RC_API_FAILURE


def test_gh_header_pagination_follows_next(monkeypatch: pytest.MonkeyPatch) -> None:
    # G1: the gh CLI path must surface the Link header so pagination works.
    # Simulate `gh api -i` output (headers + blank line + JSON body) where the
    # first page of tags carries rel="next"; the second page has no Link. The
    # paginator must follow to page 2 and aggregate both pages.
    page1 = (
        "HTTP/2 200\n"
        "content-type: application/json\n"
        'link: <https://api.github.com/repos/acme/widgets/tags?page=2&per_page=100>; rel="next"\n'
        "\n" + json.dumps([{"name": "v1.0.0"}, {"name": "v1.1.0"}])
    )
    page2 = "HTTP/2 200\ncontent-type: application/json\n\n" + json.dumps([{"name": "v2.0.0"}])

    def _run(*args, **_kwargs):
        cmd = list(args[0])
        path = cmd[3] if len(cmd) > 3 else ""
        if "/tags" in path and "page=2" not in path:
            stdout = page1
        elif "/tags" in path and "page=2" in path:
            stdout = page2
        elif "/commits" in path:
            stdout = "HTTP/2 200\n\n" + json.dumps([{"author": {"login": "alice"}}, {"author": {"login": "bob"}}])
        elif "/license" in path:
            stdout = "HTTP/2 200\n\n" + json.dumps({})
        else:
            stdout = "HTTP/2 200\n\n" + json.dumps(
                {
                    "id": 1,
                    "full_name": "acme/widgets",
                    "private": False,
                    "fork": False,
                    "archived": False,
                    "stargazers_count": 1500,
                    "created_at": "2020-01-01T00:00:00Z",
                    "license": {"spdx_id": "MIT"},
                }
            )
        return subprocess.CompletedProcess(args=cmd, returncode=0, stdout=stdout, stderr="")

    monkeypatch.setattr(mod.subprocess, "run", _run)
    result = mod.check_eligibility("acme/widgets", fetcher=mod.gather_repo_evidence)
    assert result["verdict"] == mod.VERDICT_ELIGIBLE
    # 2 tags from page 1 + 1 from page 2 must be aggregated.
    assert result["evidence"]["tag_count"] == 3


def test_gh_link_header_unparseable_next_is_inconclusive(monkeypatch: pytest.MonkeyPatch) -> None:
    # G2: a Link header with rel="next" but a truncated/unparseable URL must
    # be INCONCLUSIVE, never a silent "no more pages" undercount.
    tags_block = 'HTTP/2 200\nlink: <https://api.github.com/repos/acme/widgets/tags?page=2 rel="next"\n\n' + json.dumps(
        [{"name": "v1.0.0"}]
    )

    def _run(*args, **_kwargs):
        cmd = list(args[0])
        path = cmd[3] if len(cmd) > 3 else ""
        if "/tags" in path:
            stdout = tags_block
        elif "/commits" in path:
            stdout = "HTTP/2 200\n\n" + json.dumps([{"author": {"login": "alice"}}])
        elif "/license" in path:
            stdout = "HTTP/2 200\n\n" + json.dumps({})
        else:
            stdout = "HTTP/2 200\n\n" + json.dumps(
                {
                    "id": 1,
                    "full_name": "acme/widgets",
                    "private": False,
                    "fork": False,
                    "archived": False,
                    "stargazers_count": 1500,
                    "created_at": "2020-01-01T00:00:00Z",
                    "license": {"spdx_id": "MIT"},
                }
            )
        return subprocess.CompletedProcess(args=cmd, returncode=0, stdout=stdout, stderr="")

    monkeypatch.setattr(mod.subprocess, "run", _run)
    result = mod.check_eligibility("acme/widgets", fetcher=mod.gather_repo_evidence)
    assert result["verdict"] == mod.VERDICT_INCONCLUSIVE
    assert result["criteria"]["api"]["reason"] == mod.RC_API_FAILURE


def test_non_numeric_stargazers_count_is_inconclusive(now: _dt.datetime, monkeypatch: pytest.MonkeyPatch) -> None:
    # G3: a non-numeric stargazers_count from the API must be INCONCLUSIVE, not
    # a crash (TypeError/ValueError escaping the narrow GithubApiError catch).
    fake = _fake_api_get_factory(now)
    fake.repos_payload = {  # type: ignore[attr-defined]
        "id": 1,
        "full_name": "acme/widgets",
        "private": False,
        "fork": False,
        "archived": False,
        "stargazers_count": "lots",
        "created_at": "2020-01-01T00:00:00Z",
        "license": {"spdx_id": "MIT"},
    }
    monkeypatch.setattr(mod, "_api_get_with_headers", fake)
    result = mod.check_eligibility("acme/widgets", fetcher=mod.gather_repo_evidence)
    assert result["verdict"] == mod.VERDICT_INCONCLUSIVE
    assert result["criteria"]["api"]["reason"] == mod.RC_API_FAILURE
