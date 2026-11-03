"""FAR-2333: a hashed build-artefact URL is not a secret, and one bad item
withholds itself instead of killing the whole support bundle.

Two defects found by manual testing on a real instance:

(a) a browser error message carrying a hashed Vite asset path
    (``.../assets/index-<hash>.js``) tripped the layer-5 entropy heuristic, so
    the bundle could not be built at all;
(b) a single tripping member aborted the whole build instead of losing just
    the offending item.

The exemption in (a) is deliberately narrow: a credential that lives in a URL
path (a Slack webhook token) must still be caught, which the webhook tests
below pin.
"""

from __future__ import annotations

import json
import zipfile
from pathlib import Path
from typing import Any, ClassVar

import pytest

import modulo.support.bundle as bundle
from modulo.support.bundle import BundleSources, RedactionGateError, build_bundle, inspect_bundle
from modulo.support.redaction import Redactor

# The two real message shapes, with a plausible hashed build-artefact token.
VITE_CSS_URL = "Unable to preload CSS for https://staging.modulo.run/assets/index-aB3dE5fG7hI9jK1lM3nO5pQ.css"
VITE_JS_URL = (
    "Failed to fetch dynamically imported module: https://staging.modulo.run/assets/index-Xq7Lp2Rt9Vw4Bn6Mc8Kd3Fg.js"
)
UNCLASSIFIED_SECRET = "aB3dE5fG7hI9jK1lM3nO5pQ7"
# A real Slack incoming-webhook SHAPE, assembled so the test file does not
# itself contain a literal webhook URL (push protection scans for that shape).
# The last path segment is the secret, mixed case + digits, i.e. exactly what
# the entropy heuristic exists to catch.
SLACK_WEBHOOK = "/".join(("https://hooks.slack.com/services", "T0AAAAAAA", "B0BBBBBBB", UNCLASSIFIED_SECRET))


def _seed_log(tmp_path: Path, text: str) -> None:
    (tmp_path / "logs").mkdir(exist_ok=True)
    (tmp_path / "launcher.log").write_text(text, encoding="utf-8")


def _build(tmp_path: Path, *, settings: object | None = None) -> tuple[bundle.BundleResult, Path]:
    out = tmp_path / "bundle.zip"
    result = build_bundle(
        out,
        sources=BundleSources(
            data_dir=tmp_path,
            doctor_output="modulo doctor: healthy",
            settings=settings,
            environ={},
            enable_db=False,
        ),
    )
    return result, out


def _build_from_members(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    members: dict[str, str],
) -> tuple[bundle.BundleResult, Path]:
    async def _fixed(*_args: Any, **_kwargs: Any) -> dict[str, str]:
        return dict(members)

    monkeypatch.setattr(bundle, "_collect_raw_members", _fixed)
    out = tmp_path / "bundle.zip"
    result = build_bundle(out, sources=BundleSources(environ={}, enable_db=False))
    return result, out


def _member_text(path: Path, member: str) -> str:
    with zipfile.ZipFile(path) as archive:
        return archive.read(member).decode("utf-8")


def _manifest_and_report(path: Path) -> tuple[str, dict[str, Any]]:
    with zipfile.ZipFile(path) as archive:
        return archive.read("MANIFEST.txt").decode("utf-8"), json.loads(archive.read("report.json"))


# ---------------------------------------------------------------------------
# (a) the hashed build-artefact URL is not a secret
# ---------------------------------------------------------------------------


def test_hashed_vite_asset_urls_produce_a_bundle_instead_of_refusing(tmp_path: Path) -> None:
    """BEFORE FAR-2333 both of these real messages refused the whole build."""
    _seed_log(tmp_path, f"{VITE_CSS_URL}\n{VITE_JS_URL}")

    result, out = _build(tmp_path)

    assert out.is_file()
    log_text = _member_text(out, "logs/app.log.tail")
    assert VITE_CSS_URL in log_text
    assert VITE_JS_URL in log_text
    assert not result.withheld


def test_redactor_accepts_a_hashed_asset_path() -> None:
    redactor = Redactor({})

    assert not redactor.gate_reasons(VITE_CSS_URL)
    assert not redactor.gate_reasons(VITE_JS_URL)


# ---------------------------------------------------------------------------
# ... but the exemption does NOT weaken the URL-path guarantee
# ---------------------------------------------------------------------------


def test_slack_webhook_in_a_url_path_is_still_caught() -> None:
    with pytest.raises(RedactionGateError) as excinfo:
        Redactor({}).gate("logs/app.log.tail", SLACK_WEBHOOK)

    assert "unclassified high-entropy token" in "; ".join(excinfo.value.reasons)


def test_slack_webhook_line_is_withheld_from_the_bundle(tmp_path: Path) -> None:
    _seed_log(tmp_path, f"clean line\nnotify {SLACK_WEBHOOK}\n")

    result, out = _build(tmp_path)

    log_text = _member_text(out, "logs/app.log.tail")
    _, report = _manifest_and_report(out)
    assert "clean line" in log_text
    assert "aB3dE5fG7hI9jK1lM3nO5pQ7" not in log_text
    assert result.withheld[0]["member"] == "logs/app.log.tail"
    assert report["withheld"][0]["items"] == 1


def test_synthetic_unclassified_secret_is_still_caught() -> None:
    with pytest.raises(RedactionGateError):
        Redactor({}).gate("logs/app.log.tail", f"GREETING={UNCLASSIFIED_SECRET}")


def test_synthetic_unclassified_secret_withholds_only_its_line(tmp_path: Path) -> None:
    _seed_log(tmp_path, f"harmless line\nGREETING={UNCLASSIFIED_SECRET}\n")

    _result, out = _build(tmp_path)

    log_text = _member_text(out, "logs/app.log.tail")
    assert "harmless line" in log_text
    assert UNCLASSIFIED_SECRET not in log_text


# ---------------------------------------------------------------------------
# (b) one bad item withholds itself
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("member", "key", "good_items", "bad_item"),
    [
        (
            "errors.json",
            "error_groups",
            [{"id": "good-1", "message": "first"}, {"id": "good-2", "message": "second"}],
            {"id": "bad", "message": f"boom {UNCLASSIFIED_SECRET}"},
        ),
        (
            "runs.json",
            "runs",
            [{"run_id": "good-1"}, {"run_id": "good-2"}],
            {"run_id": "bad", "pipeline_name": f"dogfood {UNCLASSIFIED_SECRET}"},
        ),
    ],
)
def test_one_bad_record_is_withheld_and_the_rest_kept(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    member: str,
    key: str,
    good_items: list[dict[str, Any]],
    bad_item: dict[str, Any],
) -> None:
    payload = {"bundle_format_version": 1, key: [good_items[0], bad_item, good_items[1]]}
    members = {member: json.dumps(payload, indent=2, sort_keys=True)}

    result, out = _build_from_members(tmp_path, monkeypatch, members)

    survivor_payload = json.loads(_member_text(out, member))
    manifest, report = _manifest_and_report(out)
    survivors = survivor_payload[key]
    assert len(survivors) == 2
    assert {item.get("id") or item.get("run_id") for item in survivors} == {"good-1", "good-2"}
    assert report["withheld"] == [
        {
            "member": member,
            "items": 1,
            "reason": "unclassified high-entropy token present",
            "scope": "item",
        }
    ]
    assert f"withheld: {member}" in manifest
    assert "1 item(s) removed" in manifest
    assert result.withheld[0]["items"] == 1


def test_all_items_withheld_drops_the_member_and_records_it(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    payload = {"bundle_format_version": 1, "error_groups": [{"id": "bad", "message": UNCLASSIFIED_SECRET}]}
    members = {"errors.json": json.dumps(payload, indent=2, sort_keys=True)}

    _result, out = _build_from_members(tmp_path, monkeypatch, members)

    with zipfile.ZipFile(out) as archive:
        names = set(archive.namelist())
    _, report = _manifest_and_report(out)
    assert "errors.json" not in names
    assert report["withheld"] == [
        {
            "member": "errors.json",
            "items": 1,
            "reason": "unclassified high-entropy token present",
            "scope": "item",
        }
    ]


def test_withholdings_are_visible_through_the_reader(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    payload = {"bundle_format_version": 1, "error_groups": [{"id": "bad", "message": UNCLASSIFIED_SECRET}]}
    members = {"errors.json": json.dumps(payload, indent=2, sort_keys=True)}

    _result, out = _build_from_members(tmp_path, monkeypatch, members)

    assert inspect_bundle(out).withheld[0]["member"] == "errors.json"


# ---------------------------------------------------------------------------
# (b) a small invariant member is withheld whole
# ---------------------------------------------------------------------------


class _HostSettings:
    """A settings object whose safe-listed URL carries an unclassified token."""

    model_fields: ClassVar[dict[str, Any]] = {"modulo_public_url": None, "secret_key": None}
    modulo_public_url = f"https://example.test/{UNCLASSIFIED_SECRET}"
    secret_key = "seeded-value-that-never-appears"


def test_a_tripping_invariant_member_is_withheld_and_the_bundle_still_builds(tmp_path: Path) -> None:
    _seed_log(tmp_path, "clean line")

    result, out = _build(tmp_path, settings=_HostSettings())

    with zipfile.ZipFile(out) as archive:
        names = set(archive.namelist())
    manifest, report = _manifest_and_report(out)
    # settings.txt is emitted verbatim for the reviewed safe-list, so the URL
    # lands in the member and trips the gate: the member is withheld whole.
    assert "settings.txt" not in names
    assert "version.txt" in names
    assert report["withheld"] == [
        {
            "member": "settings.txt",
            "items": 1,
            "reason": "unclassified high-entropy token present",
            "scope": "member",
        }
    ]
    assert "withheld: settings.txt" in manifest
    assert "member removed" in manifest
    assert result.withheld[0]["scope"] == "member"


# ---------------------------------------------------------------------------
# an unattributable trip is still a hard refusal
# ---------------------------------------------------------------------------


def test_an_unknown_member_shape_is_still_refused() -> None:
    members = {"mystery.txt": f"token {UNCLASSIFIED_SECRET}"}

    with pytest.raises(RedactionGateError) as excinfo:
        bundle._withhold_tripping_items(members, Redactor({}), frozenset())

    assert excinfo.value.member == "mystery.txt"


def test_a_malformed_collection_member_is_still_refused() -> None:
    payload = {"bundle_format_version": 1, "error_groups": f"not a list {UNCLASSIFIED_SECRET}"}
    members = {"errors.json": json.dumps(payload)}

    with pytest.raises(RedactionGateError) as excinfo:
        bundle._withhold_tripping_items(members, Redactor({}), frozenset())

    assert excinfo.value.member == "errors.json"


class _WholeTextOnlyRedactor:
    """Trips on the whole member but on no single item — unattributable."""

    def gate_reasons(self, text: str, *, allowed_tokens: frozenset[str] = frozenset()) -> list[str]:
        return ["unclassified high-entropy token present"] if "MARKER" in text else []


def test_a_trip_no_single_item_explains_is_refused() -> None:
    payload = {"bundle_format_version": 1, "error_groups": [{"id": "ok"}], "marker": "MARKER"}
    members = {"errors.json": json.dumps(payload)}

    with pytest.raises(RedactionGateError) as excinfo:
        bundle._withhold_tripping_items(members, _WholeTextOnlyRedactor(), frozenset())  # type: ignore[arg-type]

    assert excinfo.value.member == "errors.json"


@pytest.mark.parametrize(
    "text",
    [
        pytest.param(f"not json at all {UNCLASSIFIED_SECRET}", id="invalid-json"),
        pytest.param(json.dumps([UNCLASSIFIED_SECRET]), id="not-an-object"),
    ],
)
def test_a_collection_member_that_cannot_be_decomposed_is_refused(text: str) -> None:
    """Malformed or non-object JSON has no per-item shape to withhold from."""
    members = {"errors.json": text}

    with pytest.raises(RedactionGateError) as excinfo:
        bundle._withhold_tripping_items(members, Redactor({}), frozenset())

    assert excinfo.value.member == "errors.json"


def test_a_tripping_field_outside_the_item_list_still_refuses() -> None:
    """Dropping the bad record cannot help when the member itself still trips."""
    payload = {
        "bundle_format_version": 1,
        "note": UNCLASSIFIED_SECRET,
        "error_groups": [{"id": "good"}, {"id": "bad", "message": UNCLASSIFIED_SECRET}],
    }
    members = {"errors.json": json.dumps(payload, indent=2, sort_keys=True)}

    with pytest.raises(RedactionGateError) as excinfo:
        bundle._withhold_tripping_items(members, Redactor({}), frozenset())

    assert excinfo.value.member == "errors.json"


class _SplitTokenRedactor:
    """Trips only when the whole text carries both markers, never one alone.

    Models a gate finding that spans lines, so no single log line can be held
    responsible and the withholding helper must refuse the member.
    """

    def gate_reasons(self, text: str, *, allowed_tokens: frozenset[str] = frozenset()) -> list[str]:
        tokens = set(text.split())
        return ["unclassified high-entropy token present"] if {"PART_A", "PART_B"} <= tokens else []


def test_log_member_with_no_attributable_line_is_refused() -> None:
    text = "PART_A\nPART_B\n"

    assert bundle._without_tripping_log_lines(text, _SplitTokenRedactor(), frozenset()) is None  # type: ignore[arg-type]


def test_log_member_whose_survivors_still_trip_is_refused() -> None:
    text = "x PART_A PART_B\nPART_A\nPART_B\n"

    assert bundle._without_tripping_log_lines(text, _SplitTokenRedactor(), frozenset()) is None  # type: ignore[arg-type]
