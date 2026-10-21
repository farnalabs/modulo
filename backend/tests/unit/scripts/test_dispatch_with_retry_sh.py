"""Unit tests for scripts/dispatch_with_retry.sh — the shared webhook-dispatch
retry policy used by every post-MW-migration webhook dispatch step in the
repo's GitHub Actions workflows.

The script is executed for real in a subprocess, with a stubbed `curl` placed
first on PATH. The stub returns canned HTTP codes and bodies, appends one log
line per invocation (recording the URL and every -H header pair) to a log
file, and supports an exit-code failure mode for transport-failure coverage.

Delays are made fast by environment overrides (base/max delay 1s, jitter 0),
never by mocking sleep. Runs stay well under a second per retry.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[4]
SCRIPT = REPO_ROOT / "scripts" / "dispatch_with_retry.sh"

CURL_STUB = """\
#!/usr/bin/env bash
# Test stub for dispatch_with_retry.sh tests. Consumes:
#   CANNED_CODES     comma-separated HTTP codes; the last one repeats
#   CANNED_BODY      body written to the -o file (plain string)
#   CANNED_HEADERS   raw header text written to the -D file
#   CANNED_LOG       file receiving one invocation log line per call
#   CANNED_STATE_DIR directory holding the invocation counter file
#   CANNED_TRANSPORT_EXIT  when set, exit with that code and print no code
set -u

n_file="${CANNED_STATE_DIR}/n"
n=$(cat "${n_file}" 2>/dev/null || printf '0')
IFS=, read -ra _codes <<< "${CANNED_CODES:-000}"
idx=$(( n < ${#_codes[@]} ? n : ${#_codes[@]} - 1 ))
code="${_codes[$idx]}"
echo "$(( n + 1 ))" > "${n_file}"

printf 'inv %s\\n' "$code" >> "${CANNED_LOG}"

# Log every -H pair verbatim so tests can assert the signed headers.
for (( a=1; a <= $#; a++ )); do
  if [ "${!a}" = "-H" ]; then
    b=$(( a + 1 ))
    printf 'header %s\\n' "${!b}" >> "${CANNED_LOG}"
  fi
done

out_file=""
hdr_file=""
for (( a=1; a <= $#; a++ )); do
  case "${!a}" in
    -o) b=$(( a + 1 )); out_file="${!b}" ;;
    -D) b=$(( a + 1 )); hdr_file="${!b}" ;;
    -d) b=$(( a + 1 ));
        printf 'body %s\\n' "${!b}" >> "${CANNED_LOG}" ;;
    *) ;;
  esac
done

if [ -n "${CANNED_TRANSPORT_EXIT:-}" ]; then
  exit "${CANNED_TRANSPORT_EXIT}"
fi

if [ -n "${out_file}" ]; then
  printf '%s\\n' "${CANNED_BODY:-}" > "${out_file}"
fi
if [ -n "${hdr_file}" ]; then
  printf '%s\\n' "${CANNED_HEADERS:-}" > "${hdr_file}"
fi
printf '%s' "$code"
exit 0
"""


def make_env(
    tmp_path: Path,
    codes: str,
    *,
    body: str,
    headers: str = "",
    transport_exit: str | None = None,
    summary: Path | None = None,
    result_file: Path | None = None,
    extra_env: dict[str, str] | None = None,
) -> dict[str, str]:
    stub_dir = tmp_path / "stubs"
    stub_dir.mkdir(exist_ok=True)
    stub = stub_dir / "curl"
    stub.write_text(CURL_STUB)
    stub.chmod(0o755)
    state_dir = tmp_path / "state"
    state_dir.mkdir(exist_ok=True)
    log = tmp_path / "curl.log"

    env = {
        "PATH": f"{stub_dir}:{os.environ.get('PATH', '')}",
        "HOME": str(tmp_path / "home"),
        "CANNED_CODES": codes,
        "CANNED_BODY": body,
        "CANNED_HEADERS": headers,
        "CANNED_LOG": str(log),
        "CANNED_STATE_DIR": str(state_dir),
        # Bounded, deterministic delays: 1s exponential backoff, no jitter.
        "DISPATCH_URL": "https://modulo.test/app/api/v1/webhooks/branch-fixer",
        "DISPATCH_BODY": '{"repository":{"full_name":"farnalabs/modulo"},"prNumber":7}',
        "DISPATCH_SECRET": "test-hmac-secret",
        "DISPATCH_LABEL": "Modulo Branch Fixer",
        "DISPATCH_BASE_DELAY": "1",
        "DISPATCH_MAX_DELAY": "1",
        "DISPATCH_JITTER_MAX": "0",
    }
    if transport_exit is not None:
        env["CANNED_TRANSPORT_EXIT"] = transport_exit
    if summary is not None:
        env["GITHUB_STEP_SUMMARY"] = str(summary)
    if result_file is not None:
        env["DISPATCH_RESULT_FILE"] = str(result_file)
    env.update(extra_env or {})
    return env


def run_dispatch(tmp_path: Path, **kwargs) -> subprocess.CompletedProcess[str]:
    env = make_env(tmp_path, **kwargs)
    return subprocess.run(  # noqa: S603 — fixed argv, repo script under test
        ["bash", str(SCRIPT)],  # noqa: S607
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def invocation_count(tmp_path: Path) -> int:
    return sum(1 for line in read_log(tmp_path) if line.startswith("inv "))


def read_log(tmp_path: Path) -> list[str]:
    log = tmp_path / "curl.log"
    return log.read_text(encoding="utf-8").splitlines() if log.exists() else []


def invocation_headers(log_lines: list[str]) -> list[list[str]]:
    """Group the `header ...` lines by invocation (inv marker lines)."""
    groups: list[list[str]] = []
    current: list[str] | None = None
    for line in log_lines:
        if line.startswith("inv "):
            if current is not None:
                groups.append(current)
            current = []
        elif line.startswith("header ") and current is not None:
            current.append(line[len("header ") :])
    if current is not None:
        groups.append(current)
    return groups


def header_values(all_headers: list[str], name: str) -> list[str]:
    prefix = f"{name}: "
    return [h[len(prefix) :] for h in all_headers if h.startswith(prefix)]


class TestAcceptance:
    def test_202_accepted_on_first_attempt(self, tmp_path: Path) -> None:
        proc = run_dispatch(tmp_path, codes="202", body='{"run_id":"r1"}')
        assert proc.returncode == 0
        assert "Modulo Branch Fixer accepted (run: r1)" in proc.stdout
        assert invocation_count(tmp_path) == 1

    def test_coalesced_status_is_a_notice_not_a_failure(self, tmp_path: Path) -> None:
        proc = run_dispatch(tmp_path, codes="202", body='{"status":"coalesced"}')
        assert proc.returncode == 0
        assert "::notice::" in proc.stdout
        assert "coalesced" in proc.stdout

    def test_any_2xx_accepted_only_when_flag_is_set(self, tmp_path: Path) -> None:
        proc = run_dispatch(tmp_path, codes="200", body='{"run_id":"r2"}', extra_env={"DISPATCH_ACCEPT_ANY_2XX": "1"})
        assert proc.returncode == 0
        assert "accepted" in proc.stdout

    def test_non_202_2xx_is_terminal_under_202_only_contract(self, tmp_path: Path) -> None:
        proc = run_dispatch(tmp_path, codes="200", body='{"run_id":"r2"}')
        assert proc.returncode == 1
        # Exactly one attempt: a bare 200 means something other than the app
        # answered, so retrying is pointless.
        assert invocation_count(tmp_path) == 1
        assert "::error::" in proc.stdout
        assert "non-retryable rejection" in proc.stdout

    def test_signed_headers_present_on_every_invocation(self, tmp_path: Path) -> None:
        proc = run_dispatch(tmp_path, codes="429,202", body='{"run_id":"r3"}')
        assert proc.returncode == 0
        log_lines = read_log(tmp_path)
        assert len(log_lines) >= 2
        groups = invocation_headers(log_lines)
        assert len(groups) == 2
        for headers in groups:
            ts = header_values(headers, "X-Modulo-Timestamp")
            hmac_val = header_values(headers, "X-Modulo-Webhook-Secret")
            content_type = header_values(headers, "Content-Type")
            assert len(ts) == 1
            assert len(hmac_val) == 1
            assert len(content_type) == 1
            assert ts[0].isdigit()
            assert hmac_val[0].startswith("sha256=")
            assert content_type[0] == "application/json"


class TestRetryClassification:
    def test_capacity_429_retries_then_succeeds(self, tmp_path: Path) -> None:
        proc = run_dispatch(tmp_path, codes="429,202", body='{"run_id":"r4"}')
        assert proc.returncode == 0
        assert proc.stdout.count("capacity") == 1  # the retry warning, not a failure class
        assert invocation_count(tmp_path) == 2

    def test_transient_503_retries_then_succeeds(self, tmp_path: Path) -> None:
        proc = run_dispatch(tmp_path, codes="503,502,202", body='{"run_id":"r5"}')
        assert proc.returncode == 0
        assert invocation_count(tmp_path) == 3

    def test_transport_failure_000_retries_then_succeeds(self, tmp_path: Path) -> None:
        # The stub always exits 7, so curl prints no code: the script must
        # classify 000 as transient and keep retrying until the cap, then
        # fail loudly with the transient class. MAX_ATTEMPTS=2 keeps it fast.
        proc = run_dispatch(
            tmp_path,
            codes="202",
            body='{"run_id":"r6"}',
            transport_exit="7",
            extra_env={"DISPATCH_MAX_ATTEMPTS": "2"},
        )
        assert proc.returncode == 1
        assert "webhook unreachable" in proc.stdout
        assert invocation_count(tmp_path) == 2

    def test_transient_504_retries_then_succeeds(self, tmp_path: Path) -> None:
        proc = run_dispatch(tmp_path, codes="504,202", body='{"run_id":"r8"}')
        assert proc.returncode == 0
        assert invocation_count(tmp_path) == 2

    def test_terminal_401_fails_immediately(self, tmp_path: Path) -> None:
        proc = run_dispatch(tmp_path, codes="401", body='{"detail":"bad secret"}')
        assert proc.returncode == 1
        assert invocation_count(tmp_path) == 1
        assert "::error::" in proc.stdout
        assert "non-retryable rejection" in proc.stdout

    @pytest.mark.parametrize("code", ["400", "403", "404", "409", "500", "501"])
    def test_other_4xx_and_non_retryable_5xx_are_terminal(self, tmp_path: Path, code: str) -> None:
        # 400 without the duplicate marker, every other 4xx, and 5xx outside
        # 502/503/504 are terminal: retrying cannot help, so one attempt only.
        proc = run_dispatch(tmp_path, codes=code, body='{"detail":"nope"}')
        assert proc.returncode == 1
        assert invocation_count(tmp_path) == 1
        assert "non-retryable rejection" in proc.stdout


class TestCaps:
    def test_attempt_cap_bounds_retries(self, tmp_path: Path) -> None:
        proc = run_dispatch(tmp_path, codes="429", body='{"detail":"busy"}', extra_env={"DISPATCH_MAX_ATTEMPTS": "2"})
        assert proc.returncode == 1
        assert invocation_count(tmp_path) == 2
        assert "attempt cap reached" in proc.stdout
        assert "capacity saturated" in proc.stdout

    def test_budget_exhaustion_stops_retries(self, tmp_path: Path) -> None:
        proc = run_dispatch(
            tmp_path,
            codes="429",
            body='{"detail":"busy"}',
            extra_env={"DISPATCH_BUDGET_SECONDS": "0", "DISPATCH_MAX_ATTEMPTS": "8"},
        )
        assert proc.returncode == 1
        assert invocation_count(tmp_path) == 1
        assert "budget exhausted" in proc.stdout

    def test_retry_after_is_honoured_verbatim(self, tmp_path: Path) -> None:
        headers = "retry-after: 1\r\nHTTP/1.1 429 Too Many Requests\r\n"
        proc = run_dispatch(tmp_path, codes="429,202", body='{"run_id":"r7"}', headers=headers)
        assert proc.returncode == 0
        assert "retrying in 1s" in proc.stdout


class TestDup400:
    def test_dup_400_is_a_handled_noop(self, tmp_path: Path) -> None:
        proc = run_dispatch(tmp_path, codes="400", body='{"detail":"Duplicate webhook payload"}')
        assert proc.returncode == 0
        assert "::notice::" in proc.stdout
        assert invocation_count(tmp_path) == 1

    def test_dup_400_guard_can_be_disabled(self, tmp_path: Path) -> None:
        proc = run_dispatch(
            tmp_path,
            codes="400",
            body='{"detail":"Duplicate webhook payload"}',
            extra_env={"DISPATCH_DUP_400_IS_OK": "0"},
        )
        assert proc.returncode == 1
        assert invocation_count(tmp_path) == 1


class TestFailureReporting:
    def test_failure_writes_github_step_summary(self, tmp_path: Path) -> None:
        summary = tmp_path / "summary.md"
        proc = run_dispatch(
            tmp_path, codes="429", body='{"detail":"busy"}', summary=summary, extra_env={"DISPATCH_MAX_ATTEMPTS": "1"}
        )
        assert proc.returncode == 1
        text = summary.read_text(encoding="utf-8")
        assert "### Modulo Branch Fixer dispatch failed: capacity saturated" in text
        assert "Last HTTP:" in text
        assert "Next step:" in text

    def test_summary_reports_transient_and_terminal_shapes(self, tmp_path: Path) -> None:
        summary = tmp_path / "summary.md"
        proc = run_dispatch(
            tmp_path,
            codes="502",
            body='{"detail":"bad upstream"}',
            summary=summary,
            extra_env={"DISPATCH_MAX_ATTEMPTS": "1"},
        )
        assert proc.returncode == 1
        assert "webhook unreachable" in summary.read_text(encoding="utf-8")

    def test_failure_result_file_carries_machine_readable_values(self, tmp_path: Path) -> None:
        result_file = tmp_path / "result.env"
        proc = run_dispatch(
            tmp_path,
            codes="500",
            body='{"detail":"boom"}',
            result_file=result_file,
            extra_env={"DISPATCH_MAX_ATTEMPTS": "1"},
        )
        assert proc.returncode == 1
        content = result_file.read_text(encoding="utf-8")
        assert "DISPATCH_LAST_HTTP=500" in content
        assert "DISPATCH_DELIVERED=false" in content
        assert "DISPATCH_ATTEMPTS=1" in content
        assert "DISPATCH_LAST_CLASS=terminal" in content


class TestRequiredInputs:
    def test_missing_url_fails_loudly(self, tmp_path: Path) -> None:
        env = make_env(tmp_path, codes="202", body="{}")
        del env["DISPATCH_URL"]
        proc = subprocess.run(  # noqa: S603 — fixed argv, repo script under test
            ["bash", str(SCRIPT)],  # noqa: S607
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        assert proc.returncode != 0
        assert "DISPATCH_URL is required" in proc.stderr

    def test_missing_body_fails_loudly(self, tmp_path: Path) -> None:
        env = make_env(tmp_path, codes="202", body="{}")
        del env["DISPATCH_BODY"]
        proc = subprocess.run(  # noqa: S603 — fixed argv, repo script under test
            ["bash", str(SCRIPT)],  # noqa: S607
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        assert proc.returncode != 0
        assert "DISPATCH_BODY is required" in proc.stderr
