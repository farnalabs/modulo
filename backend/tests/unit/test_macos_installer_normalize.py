"""FAR-2327: the macOS installer normalize step must honour reusable-workflow call inputs
even when the caller (release-unified) was itself started by workflow_dispatch."""

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]


def _run_bash(script: str, *, cwd: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    bash = (
        r"C:\Program Files\Git\bin\bash.exe"
        if Path(r"C:\Program Files\Git\bin\bash.exe").is_file()
        else shutil.which("bash")
    )
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
    alias_create = next(
        index for index, line in enumerate(lines) if line.strip() == 'ln -s /Applications "$DMG_STAGING/Applications"'
    )
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


def test_install_sh_normalizes_duplicate_dmg_extension(tmp_path: Path) -> None:
    """A double `.dmg.dmg` argument is stripped before the existence check.

    The user-supplied path is the only boundary where a double extension can arrive,
    so the guard lives in ``scripts/macos/install.sh``. Feeding a bogus
    ``foo.dmg.dmg`` must normalize to ``foo.dmg`` in the not-found error - on the
    old code the error names ``foo.dmg.dmg`` verbatim.
    """
    install_sh = REPO_ROOT / "scripts" / "macos" / "install.sh"
    result = _run_bash(
        f'bash "{install_sh}" "foo.dmg.dmg"',
        cwd=tmp_path,
        env=os.environ.copy(),
    )

    assert result.returncode != 0
    assert "normalized duplicate .dmg extension -> foo.dmg" in result.stderr
    assert "ERROR: DMG not found: foo.dmg" in result.stderr
    assert "foo.dmg.dmg" not in result.stderr
