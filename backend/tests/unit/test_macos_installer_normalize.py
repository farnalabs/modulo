"""FAR-2327: the macOS installer normalize step must honour reusable-workflow call inputs
even when the caller (release-unified) was itself started by workflow_dispatch."""

import hashlib
import http.server
import io
import os
import re
import shutil
import subprocess
import tarfile
import threading
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]


def _run_bash(script: str, *, cwd: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    bash = r"C:\Program Files\Git\bin\bash.exe" if Path(r"C:\Program Files\Git\bin\bash.exe").is_file() else shutil.which("bash")
    if bash is None:
        pytest.skip("bash is required to execute workflow scripts")
    return subprocess.run(  # noqa: S603 - fixed bash executable and repository-owned workflow text
        [bash, "--noprofile", "--norc", "-c", script],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def _macos_normalize_script() -> str:
    workflow = yaml.safe_load((REPO_ROOT / ".github" / "workflows" / "macos-installer.yml").read_text(encoding="utf-8"))
    steps = workflow["jobs"]["normalize-inputs"]["steps"]
    run = next(step["run"] for step in steps if step.get("id") == "normalize")
    assert isinstance(run, str)
    return run


def _run_macos_normalize(tmp_path: Path, **overrides: str) -> tuple[subprocess.CompletedProcess[str], dict[str, str]]:
    out = tmp_path / "github_output"
    out.write_text("", encoding="utf-8")
    env = {key: value for key, value in os.environ.items() if not key.startswith(("CALL_", "DISPATCH_", "GITHUB_"))}
    # Actions always defines every mapped env var (empty when unset), so default them all to "".
    for name in ("VERSION", "RC_TAG", "REF", "MODE"):
        env[f"CALL_{name}"] = ""
        env[f"DISPATCH_{name}"] = ""
    env["DISPATCH_BUILD_NATIVE"] = ""
    env.update({"GITHUB_OUTPUT": str(out), "GITHUB_REF": "refs/heads/main"})
    env.update(overrides)
    env["WSLENV"] = ":".join(name for name in env if name.startswith(("CALL_", "DISPATCH_", "GITHUB_")))
    result = _run_bash(_macos_normalize_script(), cwd=tmp_path, env=env)

    outputs = dict(line.split("=", 1) for line in out.read_text(encoding="utf-8").splitlines() if "=" in line)
    return result, outputs


def test_macos_normalize_uses_call_inputs_when_caller_was_dispatched(tmp_path: Path) -> None:
    result, outputs = _run_macos_normalize(
        tmp_path,
        GITHUB_EVENT_NAME="workflow_dispatch",
        CALL_VERSION="0.3.324",
        CALL_RC_TAG="v0.3.324-rc5",
        CALL_REF="v0.3.324-rc5",
        CALL_MODE="recut",
        DISPATCH_VERSION="",
        DISPATCH_RC_TAG="",
        DISPATCH_REF="",
        DISPATCH_MODE="",
        DISPATCH_BUILD_NATIVE="",
    )
    assert result.returncode == 0, result.stderr
    assert outputs["rc_tag"] == "v0.3.324-rc5"
    assert outputs["mode"] == "recut"
    assert outputs["run_native_build"] == "true"


def test_macos_normalize_direct_dispatch_dry_run_still_works(tmp_path: Path) -> None:
    result, outputs = _run_macos_normalize(
        tmp_path,
        GITHUB_EVENT_NAME="workflow_dispatch",
        CALL_VERSION="0.3.324",
        CALL_RC_TAG="manual",
        CALL_MODE="dry-run",
        DISPATCH_VERSION="0.3.324",
        DISPATCH_RC_TAG="manual",
        DISPATCH_REF="",
        DISPATCH_MODE="dry-run",
        DISPATCH_BUILD_NATIVE="true",
    )
    assert result.returncode == 0, result.stderr
    assert outputs["ref"] == "refs/heads/main"
    assert outputs["run_native_build"] == "true"


def _native_installer_step() -> tuple[dict[str, object], str]:
    workflow_path = REPO_ROOT / ".github" / "workflows" / "macos-installer.yml"
    workflow = yaml.safe_load(workflow_path.read_text(encoding="utf-8"))
    steps = workflow["jobs"]["build"]["steps"]
    step = next(step for step in steps if step.get("name") == "Sign, notarize and smoke-test native installer")
    run = step.get("run")
    assert isinstance(run, str)
    return step, run


def _line_numbers(script: str, pattern: str) -> list[int]:
    return [index for index, line in enumerate(script.splitlines()) if re.search(pattern, line)]


def test_macos_finder_heredoc_terminator_is_at_column_zero_after_yaml_parsing() -> None:
    """Keep YAML parsing from indenting bash's AppleScript heredoc terminator."""
    _step, build_run = _native_installer_step()
    lines = build_run.splitlines()
    opener_index = next(index for index, line in enumerate(lines) if "<<'APPLESCRIPT'" in line)
    terminator_indexes = [index for index, line in enumerate(lines) if line.strip() == "APPLESCRIPT"]

    assert len(terminator_indexes) == 1
    terminator_index = terminator_indexes[0]
    assert terminator_index > opener_index
    assert lines[terminator_index] == "APPLESCRIPT"


def test_macos_dmg_contract_structurally_verifies_staging_and_post_mount_gates() -> None:
    """Verify the DMG workflow contract without attempting a macOS DMG build on Windows."""
    step, build_run = _native_installer_step()
    plist_script = (REPO_ROOT / "scripts" / "create-macos-info-plist.sh").read_text(encoding="utf-8")
    bundle_script = (REPO_ROOT / "scripts" / "macos" / "build-native-bundle.sh").read_text(encoding="utf-8")
    icon_path = REPO_ROOT / "scripts" / "macos" / "assets" / "Modulo.svg"
    lines = build_run.splitlines()

    assert step["if"] == "${{ env.RUN_NATIVE_BUILD == 'true' }}"
    assert icon_path.is_file()
    assert icon_path.stat().st_size > 0
    assert "CFBundleIconFile</key><string>Modulo.icns" in plist_script
    assert "iconutil --convert icns" in bundle_script

    staging_start = next(index for index, line in enumerate(lines) if 'mkdir -p "$DMG_STAGING"' in line)
    dmg_create = next(index for index, line in enumerate(lines) if "run_pre_smoke dmg_create" in line)
    hard_staging_checks = {
        "app": 'test -d "$DMG_STAGING/Modulo-${VERSION_NUM}.app"',
        "alias": 'test -L "$DMG_STAGING/Applications"',
    }
    for check in hard_staging_checks.values():
        check_index = next(index for index, line in enumerate(lines) if line.strip() == check)
        assert staging_start < check_index < dmg_create
    alias_create = next(index for index, line in enumerate(lines) if line.strip() == 'ln -s /Applications "$DMG_STAGING/Applications"')
    alias_check = next(index for index, line in enumerate(lines) if line.strip() == hard_staging_checks["alias"])
    assert alias_create < alias_check

    osascript_index = next(index for index, line in enumerate(lines) if 'osascript - "$DMG_STAGING"' in line)
    assert osascript_index > staging_start
    assert any("finder_layout_exit=$?" in line for line in lines[osascript_index:])
    assert any('if [[ "$finder_layout_exit" -eq 0' in line for line in lines[osascript_index:])
    assert any('echo "::warning::Finder automation unavailable' in line for line in lines[osascript_index:])
    assert any("Finder automation completed but produced no .DS_Store" in line for line in lines[osascript_index:])
    assert not any("exit 1" in line for line in lines[osascript_index:dmg_create])

    mounted_checks = {
        "app": r"run_pre_smoke mounted_dmg_app bash -c",
        "alias": r"run_pre_smoke mounted_dmg_applications_alias bash -c",
        "icon": r"run_pre_smoke mounted_dmg_icon bash -c",
        "plist": r"run_pre_smoke mounted_dmg_plist_icon bash -c",
    }
    attach_index = next(index for index, line in enumerate(lines) if "run_pre_smoke dmg_attach" in line)
    for name, pattern in mounted_checks.items():
        matches = _line_numbers(build_run, pattern)
        assert len(matches) == 1, name
        assert attach_index < matches[0]
    assert '[[ -L "$1" && "$(readlink "$1")" == "/Applications" ]]' in build_run
    assert 'plutil -extract CFBundleIconFile raw -o - "$1" | grep -Fxq Modulo.icns' in build_run


def test_macos_dmg_contract_structurally_preserves_signing_notarization_and_gatekeeper_order() -> None:
    """Verify command order structurally; execution remains macOS-only."""
    _step, build_run = _native_installer_step()
    order = [
        "run_pre_smoke app_codesign codesign",
        "run_pre_smoke app_codesign_verify codesign",
        "run_pre_smoke dmg_create bash -c",
        "run_pre_smoke dmg_codesign codesign",
        "run_pre_smoke dmg_notary_submit xcrun notarytool submit",
        "run_pre_smoke dmg_codesign_verify codesign",
        "run_pre_smoke dmg_verify hdiutil verify",
        "run_pre_smoke dmg_attach hdiutil attach",
        "run_pre_smoke mounted_app_codesign codesign",
        "run_pre_smoke mounted_app_spctl spctl --assess --type execute",
    ]
    positions = [build_run.index(command) for command in order]
    assert positions == sorted(positions)
    assert "run_pre_smoke dmg_staple_validate xcrun stapler validate" in build_run
    assert "run_pre_smoke dmg_spctl spctl --assess --type open" in build_run
    assert "dmg_staple_ticket_unavailable=1" in build_run
    assert "DMG stapling failed for a non-propagation/non-network reason; refusing to defer" in build_run
    assert "Mounted-app codesign and Gatekeeper checks remain hard gates" in build_run


def test_macos_normalize_direct_dispatch_rejects_non_dry_run(tmp_path: Path) -> None:
    result, _outputs = _run_macos_normalize(
        tmp_path,
        GITHUB_EVENT_NAME="workflow_dispatch",
        DISPATCH_VERSION="0.3.324",
        DISPATCH_RC_TAG="manual",
        DISPATCH_REF="",
        DISPATCH_MODE="recut",
        DISPATCH_BUILD_NATIVE="false",
    )
    assert result.returncode != 0
    assert "unsupported manual dispatch mode" in result.stderr


def _install_sh_script() -> str:
    """Read the install.sh script content."""
    script_path = REPO_ROOT / "scripts" / "macos" / "install.sh"
    return script_path.read_text(encoding="utf-8")


def _run_install_sh(bogus_dmg_path: Path) -> subprocess.CompletedProcess[str]:
    """Run install.sh with a bogus DMG path and return the result."""
    script = _install_sh_script()

    bash = r"C:\Program Files\Git\bin\bash.exe" if Path(r"C:\Program Files\Git\bin\bash.exe").is_file() else shutil.which("bash")
    if bash is None:
        pytest.skip("bash is required to execute install.sh")

    return subprocess.run(  # noqa: S603 - fixed bash executable and repository-owned install script
        [bash, "--noprofile", "--norc", "-c", script + '\ninstall.sh "$1"', "install.sh", bogus_dmg_path.as_posix()],
        cwd=bogus_dmg_path.parent,
        env={**os.environ, "PATH": os.environ.get("PATH", "")},
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def test_install_sh_normalizes_duplicate_dmg_extension(tmp_path: Path) -> None:
    """Test that install.sh normalizes .dmg.dmg to .dmg and shows the normalized path in the error."""
    bogus_dmg = tmp_path / "foo.dmg.dmg"

    result = _run_install_sh(bogus_dmg)

    assert result.returncode != 0
    assert "install.sh: normalized duplicate .dmg extension -> " in result.stderr
    assert "foo.dmg" in result.stderr
    assert "ERROR: DMG not found" in result.stderr


# ---------------------------------------------------------------------------
# FAR-2478: the native smoke teardown must stop the Postgres/Redis child tree
# the installed launcher started, before asserting that none remain.
# ---------------------------------------------------------------------------

#: Marks a bash block that a test extracts verbatim from the workflow run step.
#: Both markers live in the workflow itself, so the executable regression test
#: runs the exact teardown/poll code the macOS job runs.
_MARKER_TEMPLATE = "# {edge} {marker}"

#: A stand-in for a launcher child that ignores SIGTERM. Postgres answers
#: SIGTERM with a "smart" shutdown that waits for connected clients forever, so
#: a TERM-only teardown leaves it alive; only the SIGKILL escalation stops it.
_TERM_IGNORING_HOLDER = '$SIG{TERM}="IGNORE"; sleep 300;\n'


def _bash_executable() -> str:
    """Resolve bash without silently skipping the regression when it is absent."""
    bash = r"C:\Program Files\Git\bin\bash.exe"
    if Path(bash).is_file():
        return bash
    resolved = shutil.which("bash")
    if resolved is None:
        pytest.skip("bash is required to execute workflow scripts")
    return resolved


def _extract_marked_bash(script: str, marker: str) -> str:
    """Return the marked region (markers included) from a workflow run block."""
    begin = _MARKER_TEMPLATE.format(edge="BEGIN", marker=marker)
    end = _MARKER_TEMPLATE.format(edge="END", marker=marker)
    start = script.index(begin)
    finish = script.index(end, start) + len(end)
    return script[start:finish]


def _run_bash_file(
    script_path: Path,
    args: list[str],
    *,
    cwd: Path,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run a repository-owned bash script (written under tmp_path) to completion."""
    return subprocess.run(  # noqa: S603 - fixed bash executable and repository-owned workflow text
        [_bash_executable(), "--noprofile", "--norc", script_path.as_posix(), *args],
        cwd=cwd,
        env={**os.environ, **(env or {})},
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def test_macos_teardown_stops_owned_term_ignoring_processes(tmp_path: Path) -> None:
    """FAR-2478: the teardown fully stops owned Postgres/Redis children.

    Both owned holders ignore SIGTERM (as Postgres' smart shutdown effectively
    does when a client is connected), so this only passes when the teardown
    escalates to SIGKILL. The unowned control process must survive.
    """
    _step, build_run = _native_installer_step()
    teardown = _extract_marked_bash(build_run, "stop_owned_processes")

    data_dir = tmp_path / "modulo-native-smoke-data"
    (data_dir / "pgdata").mkdir(parents=True)
    (data_dir / "redis").mkdir()
    holder = tmp_path / "term_ignoring_holder.pl"
    holder.write_text(_TERM_IGNORING_HOLDER, encoding="utf-8")

    driver = tmp_path / "teardown_driver.sh"
    driver.write_text(
        "#!/usr/bin/env bash\nset -uo pipefail\n"
        + teardown
        + """
data_dir="$MODULO_TEST_DATA_DIR"
pid_file="$MODULO_TEST_PIDFILE"
holder="$MODULO_TEST_HOLDER"
perl "$holder" "$data_dir/pgdata" &
pg_pid=$!
perl "$holder" "$data_dir/redis/dump.rdb" &
redis_pid=$!
printf '%s\\n%s\\n' "$pg_pid" "$redis_pid" > "$pid_file"
perl "$holder" "unrelated-control" &
control_pid=$!
status=0
stop_owned_processes "$pid_file" "$data_dir" 0 || status=$?
for pid in "$pg_pid" "$redis_pid"; do
  if owned_pid_is_alive "$pid"; then
    printf 'owned process survived teardown: pid=%s\\n' "$pid" >&2
    status=1
  fi
done
if owned_pid_is_alive "$control_pid"; then
  :
else
  printf 'unowned control process was killed: pid=%s\\n' "$control_pid" >&2
  status=1
fi
kill -KILL "$pg_pid" "$redis_pid" "$control_pid" 2>/dev/null || true
wait "$pg_pid" 2>/dev/null || true
wait "$redis_pid" 2>/dev/null || true
wait "$control_pid" 2>/dev/null || true
exit "$status"
""",
        encoding="utf-8",
    )

    result = _run_bash_file(
        driver,
        [],
        cwd=tmp_path,
        env={
            "MODULO_TEST_DATA_DIR": data_dir.as_posix(),
            "MODULO_TEST_PIDFILE": (tmp_path / "owned-pids").as_posix(),
            "MODULO_TEST_HOLDER": holder.as_posix(),
        },
    )

    assert result.returncode == 0, f"stdout={result.stdout}\nstderr={result.stderr}"
    assert "owned process survived teardown" not in result.stderr
    assert "unowned control process was killed" not in result.stderr


def test_macos_teardown_runs_before_the_no_owned_postgres_redis_gate() -> None:
    """FAR-2478: teardown must execute BEFORE the assertion it makes passable."""
    _step, build_run = _native_installer_step()
    teardown_index = build_run.index('run_assertion "stop_owned_processes"')
    assertion_index = build_run.index('run_assertion "no_owned_postgres_redis"')
    assert teardown_index < assertion_index
    assert 'stop_owned_processes "$OWNED_PIDS" "$DATA" 5 || exit 1' in build_run
    # The end-state assertion stays a hard gate.
    assert 'run_assertion "no_owned_postgres_redis"' in build_run


# ---------------------------------------------------------------------------
# FAR-2479: spa_root_html must wait for the SPA entry document, never accept
# the API root, and the Intel Homebrew withdrawal must be explicit.
# ---------------------------------------------------------------------------


def _serve_body(body: str, content_type: str) -> tuple[http.server.HTTPServer, threading.Thread, str]:
    """Serve one fixed response on 127.0.0.1 (real HTTP, no mocking)."""
    payload = body.encode("utf-8")

    class _Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *args: object) -> None:
            return

    server = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address[:2]
    return server, thread, f"http://{host}:{port}/"


def _stop_server(server: http.server.HTTPServer, thread: threading.Thread) -> None:
    server.shutdown()
    thread.join(timeout=10)
    server.server_close()


def _spa_poller_script(tmp_path: Path) -> Path:
    _step, build_run = _native_installer_step()
    poller = _extract_marked_bash(build_run, "spa_root_html_ready")
    script = tmp_path / "spa_root_html_ready.sh"
    script.write_text(poller + '\nspa_root_html_ready "$@"\n', encoding="utf-8")
    return script


def test_macos_spa_root_html_rejects_the_api_root(tmp_path: Path) -> None:
    """FAR-2479: the API root body (`{"status":"ok"}`) must never satisfy the gate."""
    script = _spa_poller_script(tmp_path)
    server, thread, url = _serve_body('{"status":"ok"}', "application/json")
    try:
        result = _run_bash_file(script, [url, "5", "0"], cwd=tmp_path)
    finally:
        _stop_server(server, thread)

    assert result.returncode != 0
    assert "is not the SPA entry document" in result.stderr
    assert "SPA entry document not served" in result.stderr


def test_macos_spa_root_html_accepts_a_served_spa_entry(tmp_path: Path) -> None:
    """FAR-2479: the gate passes once the SPA entry document is actually served."""
    script = _spa_poller_script(tmp_path)
    server, thread, url = _serve_body('<!doctype html><html><body><div id="app"></div></body></html>', "text/html")
    try:
        result = _run_bash_file(script, [url, "5", "0"], cwd=tmp_path)
    finally:
        _stop_server(server, thread)

    assert result.returncode == 0, result.stderr
    assert "served after" in result.stdout


def test_macos_spa_root_gate_uses_the_readiness_poller() -> None:
    """FAR-2479: the workflow gate calls the bounded poller, not a single curl."""
    _step, build_run = _native_installer_step()
    assert 'spa_root_html_ready "http://127.0.0.1:${API_PORT}/" 60 1 || exit 1' in build_run
    assert "# BEGIN spa_root_html_ready" in build_run


def _build_tools_step() -> str:
    workflow = yaml.safe_load((REPO_ROOT / ".github" / "workflows" / "macos-installer.yml").read_text(encoding="utf-8"))
    steps = workflow["jobs"]["build"]["steps"]
    step = next(step for step in steps if step.get("name") == "Install build tools")
    run = step.get("run")
    assert isinstance(run, str)
    return run


def test_macos_intel_homebrew_withdrawal_is_explicit_and_actionable() -> None:
    """FAR-2479: the Intel build must state why it no longer uses Homebrew."""
    run = _build_tools_step()
    assert "Intel (x86_64) macOS is a Homebrew Tier-3 configuration with no bottles" in run
    assert "FAR-2486" in run
    assert 'test "$(uname -m)" =' in run


# ---------------------------------------------------------------------------
# FAR-2486: the Intel macOS installer must not depend on Homebrew at all.
# The only Homebrew-installed build tool was shellcheck, so the Intel branch
# installs a pinned, SHA-256-verified upstream release instead. arm64 keeps the
# existing Homebrew path unchanged.
# ---------------------------------------------------------------------------

_X86_64_SHELLCHECK_SHA256 = "c2c15e08df0e8fbc374c335b230a7ee958c313fa5714817a59aa59f1aa594f51"
_ARM64_SHELLCHECK_SHA256 = "339b930feb1ea764467013cc1f72d09cd6b869ebf1013296ba9055ab2ffbd26f"


def test_macos_intel_build_tools_use_pinned_download_and_never_homebrew() -> None:
    """FAR-2486: the x86_64 branch installs a pinned release, not a brew formula."""
    run = _build_tools_step()
    lines = [line.strip() for line in run.splitlines()]

    x86_branch = next(index for index, line in enumerate(lines) if 'if [[ "$MACOS_ARCH" == x86_64 ]]; then' in line)
    arm64_else = next(index for index, line in enumerate(lines) if line == "else" and index > x86_branch)
    intel_branch = "\n".join(lines[x86_branch:arm64_else])

    assert "bash scripts/macos/install-pinned-shellcheck.sh" in intel_branch
    assert '"$MACOS_ARCH" "$SHELLCHECK_BIN_DIR"' in intel_branch
    assert "$GITHUB_PATH" in intel_branch
    assert "brew install" not in intel_branch
    assert "brew update" not in intel_branch


def test_macos_arm64_build_tools_keep_the_homebrew_path() -> None:
    """FAR-2486: arm64 behaviour is unchanged - Homebrew still installs shellcheck."""
    run = _build_tools_step()
    arm64_section = run.split("else\n", 2)[1]
    assert "brew update" in arm64_section
    assert "brew install shellcheck" in arm64_section


def _pinned_shellcheck_script() -> str:
    script_path = REPO_ROOT / "scripts" / "macos" / "install-pinned-shellcheck.sh"
    assert script_path.is_file()
    return script_path.read_text(encoding="utf-8")


def _pinned_shellcheck_case_arm(script: str, arch: str) -> str:
    """Return the body of the `<arch>)` arm of the script's architecture case."""
    lines = script.splitlines()
    start = next(index for index, line in enumerate(lines) if line.strip() == f"{arch})")
    end = next(index for index in range(start + 1, len(lines)) if lines[index].strip() == ";;")
    return "\n".join(lines[start:end])


def _sha256_literal(text: str) -> str:
    """Return the single SHA256="..." literal inside a case arm."""
    match = re.search(r'SHA256="([0-9a-f]{64})"', text)
    assert match is not None, f"no SHA256 literal in arm:\n{text}"
    return match.group(1)


def test_pinned_shellcheck_manifest_pins_a_verified_asset_per_architecture() -> None:
    """FAR-2486: each architecture resolves a pinned tarball and a pinned SHA-256."""
    script = _pinned_shellcheck_script()
    assert "https://github.com/koalaman/shellcheck/releases/download" in script
    assert "shellcheck-${SHELLCHECK_VERSION}.darwin.x86_64.tar.gz" in script
    assert "shellcheck-${SHELLCHECK_VERSION}.darwin.aarch64.tar.gz" in script
    assert _X86_64_SHELLCHECK_SHA256 in script
    assert _ARM64_SHELLCHECK_SHA256 in script
    assert _X86_64_SHELLCHECK_SHA256 != _ARM64_SHELLCHECK_SHA256
    assert "set -euo pipefail" in script
    # The transport stays HTTPS-only by default; nothing here may downgrade it.
    assert "${SHELLCHECK_DOWNLOAD_PROTOCOLS:-https}" in script


def test_pinned_shellcheck_binds_each_digest_to_its_architecture() -> None:
    """FAR-2486: each case arm must carry ITS OWN digest and tarball.

    The manifest test above only proves both digests appear somewhere in the
    file. Swapping the two values between the arms still satisfies that, yet in
    production it downloads the genuine x86_64 tarball, compares it against the
    aarch64 digest, fails verification, and makes the Intel job red on every run
    - exactly the failure FAR-2486 exists to remove. Bind the digest to the arm
    that selects the tarball, not merely to the file.
    """
    script = _pinned_shellcheck_script()
    x86_arm = _pinned_shellcheck_case_arm(script, "x86_64")
    arm_arm = _pinned_shellcheck_case_arm(script, "arm64")

    assert _sha256_literal(x86_arm) == _X86_64_SHELLCHECK_SHA256
    assert _sha256_literal(arm_arm) == _ARM64_SHELLCHECK_SHA256
    assert "shellcheck-${SHELLCHECK_VERSION}.darwin.x86_64.tar.gz" in x86_arm
    assert "shellcheck-${SHELLCHECK_VERSION}.darwin.aarch64.tar.gz" in arm_arm


def _pinned_shellcheck_driver(tmp_path: Path) -> Path:
    """Write a driver that runs the workflow script's verify+install block verbatim."""
    script = _pinned_shellcheck_script()
    marked = _extract_marked_bash(script, "shellcheck_verify_install")
    driver = tmp_path / "shellcheck_verify_driver.sh"
    driver.write_text(
        "#!/usr/bin/env bash\nset -uo pipefail\n"
        + marked
        + """
status=0
verify_and_install_shellcheck "$1" "$2" "$3" "v0.11.0" "$4" || status=$?
exit "$status"
""",
        encoding="utf-8",
    )
    return driver


def _write_shellcheck_tarball(path: Path, payload: str) -> str:
    """Create a release-shaped tar.gz containing shellcheck-v0.11.0/shellcheck."""
    data = payload.encode("utf-8")
    with tarfile.open(path, "w:gz") as archive:
        info = tarfile.TarInfo("shellcheck-v0.11.0/shellcheck")
        info.size = len(data)
        info.mode = 0o755
        archive.addfile(info, io.BytesIO(data))
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_pinned_shellcheck_installs_only_a_checksum_verified_archive(tmp_path: Path) -> None:
    """FAR-2486: a matching digest installs the binary and runs shellcheck --version."""
    driver = _pinned_shellcheck_driver(tmp_path)
    tarball = tmp_path / "shellcheck-v0.11.0.darwin.x86_64.tar.gz"
    digest = _write_shellcheck_tarball(
        tarball,
        "#!/usr/bin/env bash\nprintf 'ShellCheck - shell script analysis tool\\nversion: 0.11.0\\n'\n",
    )
    prefix = tmp_path / "install"

    # Relative paths: GNU tar treats "C:/..." in `-C` as a remote host spec.
    result = _run_bash_file(
        driver,
        [tarball.name, digest, "install", "work"],
        cwd=tmp_path,
    )

    assert result.returncode == 0, f"stdout={result.stdout}\nstderr={result.stderr}"
    assert (prefix / "shellcheck").is_file()
    assert "verified sha256 of" in result.stderr
    assert "version: 0.11.0" in result.stderr


def test_pinned_shellcheck_refuses_a_tampered_archive(tmp_path: Path) -> None:
    """FAR-2486: a digest mismatch fails closed and installs nothing."""
    driver = _pinned_shellcheck_driver(tmp_path)
    tarball = tmp_path / "shellcheck-v0.11.0.darwin.x86_64.tar.gz"
    digest = _write_shellcheck_tarball(tarball, "#!/usr/bin/env bash\nexit 0\n")
    tarball.write_bytes(tarball.read_bytes() + b"tampered")
    prefix = tmp_path / "install"

    result = _run_bash_file(
        driver,
        [tarball.as_posix(), digest, prefix.as_posix(), (tmp_path / "work").as_posix()],
        cwd=tmp_path,
    )

    assert result.returncode != 0
    assert "checksum verification failed" in result.stderr
    assert not (prefix / "shellcheck").exists()


def test_pinned_shellcheck_refuses_a_missing_archive(tmp_path: Path) -> None:
    """FAR-2486: an absent download is a hard failure, never a skipped check."""
    driver = _pinned_shellcheck_driver(tmp_path)
    prefix = tmp_path / "install"

    result = _run_bash_file(
        driver,
        [
            (tmp_path / "absent.tar.gz").as_posix(),
            _X86_64_SHELLCHECK_SHA256,
            prefix.as_posix(),
            (tmp_path / "work").as_posix(),
        ],
        cwd=tmp_path,
    )

    assert result.returncode != 0
    assert "missing or empty" in result.stderr
    assert not prefix.exists()


def _pinned_shellcheck_download_driver(tmp_path: Path) -> Path:
    """Write a driver that runs the workflow script's download_verified block verbatim."""
    script = _pinned_shellcheck_script()
    marked = _extract_marked_bash(script, "download_verified")
    driver = tmp_path / "shellcheck_download_driver.sh"
    driver.write_text(
        "#!/usr/bin/env bash\nset -uo pipefail\n"
        # The loopback fixture is plain HTTP; production keeps the HTTPS default.
        + "SHELLCHECK_DOWNLOAD_PROTOCOLS=http\n"
        + "SHELLCHECK_MAX_ATTEMPTS=5\n"
        + marked
        + """
status=0
download_verified "$1" "$2" "$3" || status=$?
exit "$status"
""",
        encoding="utf-8",
    )
    return driver


def _serve_status_sequence(statuses: list[int], body: bytes = b"verified-asset") -> tuple[http.server.HTTPServer, threading.Thread, str, dict[str, int]]:
    """Serve the given status codes in order (the last repeats) and count requests."""
    lock = threading.Lock()
    counter = {"requests": 0}

    class _Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            with lock:
                index = counter["requests"]
                counter["requests"] += 1
            status = statuses[index] if index < len(statuses) else statuses[-1]
            if status == 200:
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            else:
                self.send_response(status)
                self.send_header("Content-Length", "0")
                self.end_headers()

        def log_message(self, *args: object) -> None:
            return

    server = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address[:2]
    return server, thread, f"http://{host}:{port}/shellcheck.tar.gz", counter


def test_pinned_shellcheck_download_retries_transient_http_then_succeeds(tmp_path: Path) -> None:
    """FAR-2486: 503/503/200 is retried, then the asset is saved.

    A regression that classified 503 as permanent would fail this job on the
    first network blip; a regression that never retried would fail on request
    two. Both are invisible to the verify/install tests.
    """
    driver = _pinned_shellcheck_download_driver(tmp_path)
    server, thread, url, counter = _serve_status_sequence([503, 503, 200], b"retried-body")
    destination = tmp_path / "pinned.tar.gz"
    try:
        result = _run_bash_file(driver, [url, destination.as_posix(), "fixture"], cwd=tmp_path)
    finally:
        _stop_server(server, thread)

    assert result.returncode == 0, f"stdout={result.stdout}\nstderr={result.stderr}"
    assert counter["requests"] == 3
    assert destination.read_bytes() == b"retried-body"
    assert "transient HTTP 503" in result.stderr


@pytest.mark.parametrize("status", [404, 403])
def test_pinned_shellcheck_download_fails_fast_on_permanent_http_status(tmp_path: Path, status: int) -> None:
    """FAR-2486: 404/403 is a single attempt, non-zero, and installs nothing."""
    driver = _pinned_shellcheck_download_driver(tmp_path)
    # The second entry is 200 so a wrongly-retried permanent failure would
    # "succeed" and fail the assertions below, instead of hiding behind the
    # 5-attempt exhaustion path.
    server, thread, url, counter = _serve_status_sequence([status, 200], b"must-not-be-saved")
    destination = tmp_path / "pinned.tar.gz"
    try:
        result = _run_bash_file(driver, [url, destination.as_posix(), "fixture"], cwd=tmp_path)
    finally:
        _stop_server(server, thread)

    assert result.returncode != 0
    assert counter["requests"] == 1
    assert not destination.exists()
    assert not Path(f"{destination.as_posix()}.part").exists()
    assert f"permanent HTTP {status}" in result.stderr


def test_pinned_shellcheck_download_leaves_no_temporary_files_on_success(tmp_path: Path) -> None:
    """FAR-2486: a clean download leaves no .part/.status scratch files behind."""
    driver = _pinned_shellcheck_download_driver(tmp_path)
    server, thread, url, counter = _serve_status_sequence([200], b"clean-body")
    destination = tmp_path / "pinned.tar.gz"
    try:
        result = _run_bash_file(driver, [url, destination.as_posix(), "fixture"], cwd=tmp_path)
    finally:
        _stop_server(server, thread)

    assert result.returncode == 0, f"stdout={result.stdout}\nstderr={result.stderr}"
    assert counter["requests"] == 1
    assert destination.read_bytes() == b"clean-body"
    leftovers = sorted(path.name for path in tmp_path.iterdir() if path.name.startswith("pinned.tar.gz."))
    assert not leftovers
