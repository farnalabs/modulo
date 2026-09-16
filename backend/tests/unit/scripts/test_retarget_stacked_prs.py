"""Unit tests for .github/scripts/retarget-stacked-prs.py (FAR-917 review).

These prove the fail-safe contract of the stacked-PR guard:

  * a successful retarget moves the child PR and protects nothing;
  * a FAILED retarget protects the child's base branch (so the delete sweep
    skips it and the child PR is not silently closed);
  * a FAILED / blank open-PR query protects EVERY slated branch -- the previous
    implementation discarded the `gh` return code, read the failure as "no open
    PRs", and let the sweep delete the base branch (the exact FAR-917 incident);
  * a genuine empty response (rc 0, ``[]``) permits deletion;
  * all pages of a ``gh api --paginate`` response are seen (no 100-item cutoff);
  * step outputs are written on every path, so downstream never reads ''.
"""

from __future__ import annotations

import json
from importlib.machinery import SourceFileLoader
from importlib.util import module_from_spec, spec_from_loader
from pathlib import Path

import pytest


def _load():
    for parent in Path(__file__).resolve().parents:
        script_path = parent / ".github" / "scripts" / "retarget-stacked-prs.py"
        if script_path.exists():
            break
    else:
        raise RuntimeError("Could not find .github/scripts/retarget-stacked-prs.py")
    loader = SourceFileLoader("retarget_stacked_prs", str(script_path))
    mod = module_from_spec(spec_from_loader("retarget_stacked_prs", loader))
    loader.exec_module(mod)
    return mod


mod = _load()


def _pr(num: int, base: str, head: str, title: str = "a PR") -> dict:
    return {"number": num, "title": title, "base": {"ref": base}, "head": {"ref": head}}


class FakeGh:
    """Injectable stand-in for the `gh` runner. Records every call."""

    def __init__(
        self,
        open_prs=None,
        open_prs_rc: int = 0,
        open_prs_out=None,
        open_prs_err: str = "",
        edit_rc: int = 0,
        edit_rc_by_pr=None,
        compare_ahead_by: str = "1",
        compare_rc: int = 0,
        comment_rc: int = 0,
    ):
        self.open_prs = open_prs if open_prs is not None else []
        self.open_prs_rc = open_prs_rc
        self.open_prs_out = open_prs_out
        self.open_prs_err = open_prs_err
        self.edit_rc = edit_rc
        self.edit_rc_by_pr = edit_rc_by_pr or {}
        self.compare_ahead_by = compare_ahead_by
        self.compare_rc = compare_rc
        self.comment_rc = comment_rc
        self.calls: list[tuple] = []

    def __call__(self, *args):
        self.calls.append(args)
        if args[0] == "api" and args[1] == "--paginate":
            if self.open_prs_rc != 0:
                return self.open_prs_rc, "", self.open_prs_err
            out = self.open_prs_out if self.open_prs_out is not None else json.dumps(self.open_prs)
            return 0, out, ""
        if args[0] == "api" and any("compare/" in a for a in args):
            if self.compare_rc != 0:
                return self.compare_rc, "", "compare failed"
            return 0, self.compare_ahead_by, ""
        if args[0] == "pr" and args[1] == "edit":
            rc = self.edit_rc_by_pr.get(int(args[2]), self.edit_rc)
            return rc, "", ("" if rc == 0 else "retarget failed")
        if args[0] == "pr" and args[1] == "comment":
            return self.comment_rc, "", ("" if self.comment_rc == 0 else "comment failed")
        raise AssertionError(f"unexpected gh call: {args}")

    def edits(self) -> list[tuple]:
        return [c for c in self.calls if c[0] == "pr" and c[1] == "edit"]


def _read_outputs(path: Path) -> dict:
    data: dict = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            data[key] = value
    return data


def test_retarget_success_protects_nothing(tmp_path):
    out = tmp_path / "out"
    fake = FakeGh(open_prs=[_pr(602, "feat/base", "feat/child")], edit_rc=0)

    rc = mod.run(["prog", "feat/base", "main"], run_gh=fake, github_output=str(out))

    assert rc == 0
    outputs = _read_outputs(out)
    assert outputs["retargeted_prs"] == "602"
    assert outputs["protected_branches"] == ""
    assert any(c[:2] == ("pr", "comment") and c[2] == "602" for c in fake.calls)


def test_retarget_failure_protects_base_branch(tmp_path):
    out = tmp_path / "out"
    fake = FakeGh(open_prs=[_pr(602, "feat/base", "feat/child")], edit_rc=1)

    rc = mod.run(["prog", "feat/base", "main"], run_gh=fake, github_output=str(out))

    assert rc == 0
    outputs = _read_outputs(out)
    assert outputs["protected_branches"] == "feat/base"
    assert outputs["retargeted_prs"] == ""


def test_failed_open_pr_query_protects_every_branch_and_deletes_nothing(tmp_path):
    # The core FAR-917 regression: with the old helper the return code was
    # ignored, this looked like "no open PRs", protected_branches came out empty,
    # and the sweep deleted the base branch -> child PR silently closed.
    out = tmp_path / "out"
    fake = FakeGh(open_prs_rc=1, open_prs_err="HTTP 403: rate limit exceeded")

    rc = mod.run(["prog", "feat/a", "feat/b", "main"], run_gh=fake, github_output=str(out))

    assert rc == 0  # fail SAFE, not failed: post-merge CI/deploy dispatch must run
    outputs = _read_outputs(out)
    assert set(outputs["protected_branches"].split()) == {"feat/a", "feat/b"}
    assert outputs["retargeted_prs"] == ""
    # No branch was retargeted/deleted on an unverifiable query.
    assert fake.edits() == []


def test_blank_open_pr_query_output_protects_every_branch(tmp_path):
    out = tmp_path / "out"
    fake = FakeGh(open_prs_rc=0, open_prs_out="")

    rc = mod.run(["prog", "feat/a", "main"], run_gh=fake, github_output=str(out))

    assert rc == 0
    assert _read_outputs(out)["protected_branches"] == "feat/a"


def test_genuine_empty_response_permits_deletion(tmp_path):
    out = tmp_path / "out"
    fake = FakeGh(open_prs=[])  # rc 0, "[]"

    rc = mod.run(["prog", "feat/a", "main"], run_gh=fake, github_output=str(out))

    assert rc == 0
    outputs = _read_outputs(out)
    assert outputs["protected_branches"] == ""
    assert outputs["retargeted_prs"] == ""


def test_unrelated_base_is_ignored(tmp_path):
    out = tmp_path / "out"
    fake = FakeGh(open_prs=[_pr(1, "someone/other", "x")], edit_rc=0)

    rc = mod.run(["prog", "feat/base", "main"], run_gh=fake, github_output=str(out))

    assert rc == 0
    assert _read_outputs(out)["retargeted_prs"] == ""
    assert fake.edits() == []


def test_multi_page_paginated_response_is_fully_seen(tmp_path):
    # gh api --paginate concatenates pages ([p1][p2]); the stacked PR lives only
    # on page 2, so a first-page-only parse would miss it.
    out = tmp_path / "out"
    page1 = [_pr(1, "someone/other", "x")]
    page2 = [_pr(602, "feat/base", "feat/child")]
    fake = FakeGh(open_prs_out=json.dumps(page1) + json.dumps(page2), edit_rc=0)

    rc = mod.run(["prog", "feat/base", "main"], run_gh=fake, github_output=str(out))

    assert rc == 0
    assert _read_outputs(out)["retargeted_prs"] == "602"


def test_mixed_outcomes_protect_only_failed_base(tmp_path):
    out = tmp_path / "out"
    fake = FakeGh(
        open_prs=[_pr(10, "feat/good", "child/good"), _pr(11, "feat/bad", "child/bad")],
        edit_rc_by_pr={10: 0, 11: 1},
    )

    rc = mod.run(["prog", "feat/good", "feat/bad", "main"], run_gh=fake, github_output=str(out))

    assert rc == 0
    outputs = _read_outputs(out)
    assert outputs["protected_branches"] == "feat/bad"
    assert outputs["retargeted_prs"] == "10"


def test_no_branches_still_writes_outputs(tmp_path):
    out = tmp_path / "out"
    fake = FakeGh()

    rc = mod.run(["prog"], run_gh=fake, github_output=str(out))

    assert rc == 0
    outputs = _read_outputs(out)
    assert outputs == {"protected_branches": "", "retargeted_prs": ""}
    assert fake.calls == []  # no query for an empty branch list


def test_gh_checked_propagates_returncode():
    with pytest.raises(mod.GhError):
        mod.gh_checked(lambda *a: (1, "", "boom"), "api", "x")


def test_parse_concatenated_flattens_all_pages():
    items = mod.parse_concatenated('[{"a": 1}][{"b": 2}, {"c": 3}]')
    assert len(items) == 3


def test_empty_diff_note_added_to_retarget_comment(tmp_path):
    out = tmp_path / "out"
    fake = FakeGh(open_prs=[_pr(602, "feat/base", "feat/child")], edit_rc=0, compare_ahead_by="0")

    mod.run(["prog", "feat/base", "main"], run_gh=fake, github_output=str(out))

    bodies = [c[4] for c in fake.calls if c[:2] == ("pr", "comment")]
    assert bodies and "adds no commits" in bodies[0]


def test_compare_failure_does_not_fail_guard(tmp_path):
    out = tmp_path / "out"
    fake = FakeGh(open_prs=[_pr(602, "feat/base", "feat/child")], edit_rc=0, compare_rc=1)

    rc = mod.run(["prog", "feat/base", "main"], run_gh=fake, github_output=str(out))

    assert rc == 0
    assert _read_outputs(out)["retargeted_prs"] == "602"
