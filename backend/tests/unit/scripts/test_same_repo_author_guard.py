"""Unit tests for .github/scripts/same-repo-author-guard.py (FAR-264).

These prove the same-repo author gate:
  * allows only PRs whose head lives on the base repo AND whose author
    association is in the trusted set, and
  * fails CLOSED (verdict ``external``) for forks, deleted-fork heads, missing
    or untrusted author associations, mismatched payloads, and bad input -
    never ``ok``.
"""

from __future__ import annotations

import contextlib
import io
import json
from importlib.machinery import SourceFileLoader
from importlib.util import module_from_spec, spec_from_loader
from pathlib import Path

for parent in Path(__file__).resolve().parents:
    script_path = parent / ".github" / "scripts" / "same-repo-author-guard.py"
    if script_path.exists():
        break
else:
    raise RuntimeError("Could not find .github/scripts/same-repo-author-guard.py")

_loader = SourceFileLoader("same_repo_author_guard", str(script_path))
guard = module_from_spec(spec_from_loader("same_repo_author_guard", _loader))
_loader.exec_module(guard)

BASE = "farnalabs/modulo"


def _pr(
    *,
    head_full: str | None = "farnalabs/modulo",
    base_full: str = "farnalabs/modulo",
    association: str | None = "MEMBER",
    author: str = "farnalabs",
    author_type: str = "User",
) -> dict:
    pr: dict = {
        "user": {"login": author, "type": author_type},
        "base": {"repo": {"full_name": base_full}},
        "head": {"repo": {"full_name": head_full}},
    }
    if association is not None:
        pr["author_association"] = association
    return pr


def test_same_repo_member_is_ok() -> None:
    verdict, reason = guard.decide(_pr(), BASE)
    assert verdict == "ok"
    assert "farnalabs" in reason


def test_every_trusted_association_is_ok() -> None:
    for assoc in ("OWNER", "MEMBER", "COLLABORATOR", "CONTRIBUTOR"):
        verdict, _ = guard.decide(_pr(association=assoc), BASE)
        assert verdict == "ok", f"expected ok for {assoc}, got {verdict}"


def test_same_repo_dependabot_with_none_association_is_ok() -> None:
    verdict, reason = guard.decide(_pr(association="NONE", author="dependabot[bot]", author_type="Bot"), BASE)
    assert verdict == "ok"
    assert "dependabot" in reason


def test_dependabot_fork_head_is_external() -> None:
    pr = _pr(head_full="someone/fork", association="NONE", author="dependabot[bot]", author_type="Bot")
    verdict, _ = guard.decide(pr, BASE)
    assert verdict == "external"


def test_dependabot_lookalike_user_is_external() -> None:
    verdict, _ = guard.decide(_pr(association="NONE", author="dependabot[bot]", author_type="User"), BASE)
    assert verdict == "external"


def test_same_repo_renovate_with_none_association_is_ok() -> None:
    verdict, reason = guard.decide(_pr(association="NONE", author="renovate[bot]", author_type="Bot"), BASE)
    assert verdict == "ok"
    assert "renovate[bot]" in reason


def test_renovate_fork_head_is_external() -> None:
    pr = _pr(head_full="someone/fork", association="NONE", author="renovate[bot]", author_type="Bot")
    verdict, _ = guard.decide(pr, BASE)
    assert verdict == "external"


def test_unlisted_bot_with_none_association_is_external() -> None:
    verdict, _ = guard.decide(_pr(association="NONE", author="some-other[bot]", author_type="Bot"), BASE)
    assert verdict == "external"


def test_fork_head_is_external() -> None:
    verdict, reason = guard.decide(_pr(head_full="someone/evil-fork"), BASE)
    assert verdict == "external"
    assert "someone/evil-fork" in reason


def test_deleted_fork_head_is_external() -> None:
    verdict, reason = guard.decide(_pr(head_full=None), BASE)
    assert verdict == "external"
    assert "head repository" in reason


def test_missing_association_is_external() -> None:
    verdict, reason = guard.decide(_pr(association=None), BASE)
    assert verdict == "external"
    assert "author_association" in reason


def test_untrusted_associations_are_external() -> None:
    for assoc in ("NONE", "FIRST_TIMER", "FIRST_TIME_CONTRIBUTOR", "MANNEQUIN"):
        verdict, _ = guard.decide(_pr(association=assoc), BASE)
        assert verdict == "external", f"expected external for {assoc}, got {verdict}"


def test_payload_base_mismatch_is_external() -> None:
    verdict, reason = guard.decide(_pr(base_full="other/repo"), BASE)
    assert verdict == "external"
    assert "does not match" in reason


def test_head_comparison_is_case_insensitive() -> None:
    verdict, _ = guard.decide(_pr(head_full="FarnaLabs/Modulo"), "farnalabs/modulo")
    assert verdict == "ok"


def test_missing_base_repo_arg_is_external() -> None:
    verdict, _ = guard.decide(_pr(), "")
    assert verdict == "external"


def test_non_dict_payload_is_external() -> None:
    verdict, _ = guard.decide(["not", "a", "dict"], BASE)  # type: ignore[arg-type]
    assert verdict == "external"


def test_main_ok_returns_zero_and_ok_line(tmp_path) -> None:
    pr_file = tmp_path / "pr.json"
    pr_file.write_text(json.dumps(_pr()), encoding="utf-8")
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = guard.main(["same-repo-author-guard.py", str(pr_file), BASE])
    assert rc == 0
    assert buf.getvalue().startswith("ok|")


def test_main_external_returns_nonzero(tmp_path) -> None:
    pr_file = tmp_path / "pr.json"
    pr_file.write_text(json.dumps(_pr(head_full="someone/evil-fork")), encoding="utf-8")
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = guard.main(["same-repo-author-guard.py", str(pr_file), BASE])
    assert rc != 0
    assert buf.getvalue().startswith("external|")


def test_main_unreadable_file_fails_closed(tmp_path) -> None:
    missing = tmp_path / "no-such.json"
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = guard.main(["same-repo-author-guard.py", str(missing), BASE])
    assert rc != 0
    assert buf.getvalue().startswith("external|")
    assert "fail closed" in buf.getvalue()


def test_main_usage_missing_args_returns_two() -> None:
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = guard.main(["same-repo-author-guard.py"])
    assert rc == 2
    assert buf.getvalue().startswith("external|")
