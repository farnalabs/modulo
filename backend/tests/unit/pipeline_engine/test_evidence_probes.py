"""Unit tests for the FAR-152 evidence & no-op detection machinery.

Covers the §15.3/§15.14 surface implemented in
``modulo.core.pipeline_engine.evidence``: tri-state ``EvidenceResult`` and the
no-op gate input, the concrete ``SandboxEvidenceProvider`` probed against tiny
real git repos and real directories, the bounded ``run_evidence_probe`` runner,
the §15.14 metric fires-when contracts, and the bounded reconciliation sweep.
"""

import asyncio
import datetime
import os
import subprocess
import uuid
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import modulo.core.pipeline_engine.evidence as evidence
from modulo.core.pipeline_engine.evidence import (
    EVIDENCE_PROBE_TIMEOUT_SECONDS,
    CommandResult,
    EvidenceResult,
    FileInfo,
    SandboxEvidenceProvider,
    combine_probe_results,
    compute_work_intact,
    evidence_enabled,
    extract_output_json,
    extract_stored_output_json,
    node_declared_success,
    output_json_has_content,
    reconcile_noop_evidence,
    run_evidence_probe,
    write_evidence_row,
)
from modulo.db.models.base import Base
from modulo.db.models.run import Run
from modulo.db.models.run_evidence import RunEvidence

# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


class TestEvidenceResult:
    def test_tri_state_values(self) -> None:
        assert EvidenceResult.has_work.value == "has_work"
        assert EvidenceResult.verified_empty.value == "verified_empty"
        assert EvidenceResult.unverifiable.value == "unverifiable"


class TestNodeDeclaredSuccess:
    def test_completed_success_declared(self) -> None:
        out = {"output": {"status": "completed", "agent_status": "completed", "agent_outcome": "success"}}
        assert node_declared_success(out) is True

    def test_missing_agent_status_not_declared(self) -> None:
        # §7.2.3: status must be present — a missing status never qualifies.
        assert node_declared_success({"output": {"agent_outcome": "success"}}) is False

    def test_non_success_outcome_not_declared(self) -> None:
        out = {"output": {"agent_status": "completed", "agent_outcome": "partial"}}
        assert node_declared_success(out) is False
        assert node_declared_success({"output": {"agent_status": "completed"}}) is False

    def test_garbage_not_declared(self) -> None:
        assert node_declared_success(None) is False
        assert node_declared_success("nope") is False
        assert node_declared_success({}) is False


class TestOutputJsonHasContent:
    def test_empty_and_metadata_only(self) -> None:
        assert output_json_has_content(None) is False
        assert output_json_has_content({}) is False
        assert output_json_has_content({"a": None}) is False
        assert output_json_has_content({"a": ""}) is False
        assert output_json_has_content({"a": []}) is False
        assert output_json_has_content({"a": {}}) is False

    def test_any_non_empty_value_counts(self) -> None:
        assert output_json_has_content({"a": "x"}) is True
        assert output_json_has_content({"a": 0}) is True
        assert output_json_has_content({"a": [1]}) is True
        assert output_json_has_content({"a": {"b": 1}}) is True


class TestExtractOutputJson:
    def test_artifact_envelope(self) -> None:
        node = {"artifacts": [{"output": {"output_json": {"changed": True}}}], "output": {"status": "completed"}}
        assert extract_output_json(node) == {"changed": True}

    def test_direct_output_envelope(self) -> None:
        assert extract_output_json({"output": {"output_json": {"a": 1}}}) == {"a": 1}

    def test_garbage(self) -> None:
        assert extract_output_json(None) is None
        assert extract_output_json({"output": {}}) is None

    def test_artifacts_empty_or_non_dict_ignored(self) -> None:
        # An empty artifacts list must fall through to the direct-output check.
        assert extract_output_json({"artifacts": [], "output": {"output_json": {"b": 2}}}) == {"b": 2}
        assert extract_output_json({"artifacts": [{"output": {}}]}) is None


class TestExtractStoredOutputJson:
    def test_legacy_envelope(self) -> None:
        outputs = {
            "node-a": {
                "artifacts": [{"output": {"output_json": {"changed": True}, "status": "completed"}}],
                "status": "completed",
            }
        }
        assert extract_stored_output_json(outputs, {}, "node-a") == {"changed": True}

    def test_pure_return_used_when_no_envelope(self) -> None:
        # A P1 split row: node_return returns the pure output_json dict.
        outputs = {"node-a": {"pr_url": "https://github.com/farnalabs/modulo/pull/1"}}
        assert extract_stored_output_json(outputs, {}, "node-a") == {
            "pr_url": "https://github.com/farnalabs/modulo/pull/1"
        }

    def test_non_dict_return_is_none(self) -> None:
        assert extract_stored_output_json(None, {}, "node-a") is None
        assert extract_stored_output_json({"node-a": "just-a-string"}, {}, "node-a") is None

    def test_envelope_without_output_json_is_none(self) -> None:
        outputs = {"node-a": {"output": {"status": "completed"}}}
        assert extract_stored_output_json(outputs, {}, "node-a") is None


class TestCombineProbeResults:
    def test_any_positive_wins(self) -> None:
        state, _detail = combine_probe_results(EvidenceResult.verified_empty, EvidenceResult.has_work)
        assert state == EvidenceResult.has_work

    def test_any_unverifiable_wins_over_empty(self) -> None:
        state, _ = combine_probe_results(EvidenceResult.verified_empty, EvidenceResult.unverifiable)
        assert state == EvidenceResult.unverifiable
        state, _ = combine_probe_results(EvidenceResult.unverifiable, EvidenceResult.has_work)
        assert state == EvidenceResult.has_work

    def test_both_empty_is_verified_empty(self) -> None:
        state, _ = combine_probe_results(EvidenceResult.verified_empty, EvidenceResult.verified_empty)
        assert state == EvidenceResult.verified_empty


class TestComputeWorkIntact:
    def test_full_dag_with_valid_artifacts(self) -> None:
        completed = {"a": {"output": {"status": "completed"}}, "b": {"output": {"status": "completed"}}}
        assert compute_work_intact(completed, {"a", "b"}) is True

    def test_truncated_dag_not_intact(self) -> None:
        completed = {"a": {"output": {"status": "completed"}}}
        assert compute_work_intact(completed, {"a", "b"}) is False

    def test_invalid_artifact_breaks_intact(self) -> None:
        completed = {"a": {}, "b": {"output": {"status": "completed"}}}
        assert compute_work_intact(completed, {"a", "b"}) is False

    def test_empty_completed_not_intact(self) -> None:
        assert compute_work_intact({}, {"a"}) is False
        assert compute_work_intact(None, {"a"}) is False

    def test_garbage_outputs_not_intact(self) -> None:
        completed = {"a": "not-a-dict", "b": None}
        assert compute_work_intact(completed, {"a", "b"}) is False


# ---------------------------------------------------------------------------
# Concrete SandboxEvidenceProvider — tiny real git repos + real directories
# ---------------------------------------------------------------------------


def _clean_git_env() -> dict[str, str]:
    """The test subprocess env with every inherited GIT_* variable stripped.

    `git commit` sets GIT_INDEX_FILE to a temporary commit index and propagates
    it to the pre-commit hooks. Without this, the fixture's `git add`/`git
    commit` in temp repos write into that shared temp index and corrupt the
    very commit being made (observed: a commit full of spurious deletions).
    """
    return {k: v for k, v in os.environ.items() if not k.startswith("GIT")}


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    # GIT_CEILING_DIRECTORIES makes git discovery hermetic: it never walks above
    # the test's temp dir, so ambient ancestor repos under %TEMP% can never leak
    # into these fixtures.
    return subprocess.run(  # noqa: S603
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],  # noqa: S607
        cwd=str(repo),
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
        env={**_clean_git_env(), "GIT_CEILING_DIRECTORIES": str(repo.parent)},
    )


@pytest.fixture
def repo_dir(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / "base.txt").write_text("base\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "base")
    return repo


def _local_runner(repo: Path):
    async def _run(_sandbox_id: str, command: str) -> CommandResult:
        proc = await asyncio.create_subprocess_shell(
            command,
            cwd=str(repo),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={**_clean_git_env(), "GIT_CEILING_DIRECTORIES": str(repo.parent)},
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
        return CommandResult(
            exit_code=proc.returncode if proc.returncode is not None else 1,
            stdout=stdout.decode(errors="replace"),
            stderr=stderr.decode(errors="replace"),
        )

    return _run


def _local_lister(root: Path):
    async def _list(_sandbox_id: str) -> list[FileInfo]:
        return _scan_files(root)

    return _list


def _scan_files(root: Path) -> list[FileInfo]:
    files: list[FileInfo] = []
    for dirpath, _dirnames, filenames in os.walk(str(root)):
        for fname in filenames:
            full = os.path.join(dirpath, fname)
            files.append(FileInfo(name=os.path.relpath(full, root), size=os.path.getsize(full)))
    return files


def _provider(repo: Path, output_json: dict[str, Any] | None = None, root: Path | None = None):
    # Generous probe bound for the local-git fixture tests: the ≤3s bound is a
    # PRODUCTION contract (E2B SDK calls). These doubles spawn real git
    # subprocesses which can be slow under a loaded CI/pre-commit environment —
    # the bound must never cancel them.
    return SandboxEvidenceProvider(
        sandbox_id_resolver=lambda _rid, _nid: "sandbox-1",
        run_command=_local_runner(repo),
        list_files=_local_lister(root or repo),
        output_json_loader=lambda _rid, _nid: output_json,
        timeout_seconds=30.0,
    )


class TestGitDiffEmptyProbe:
    async def test_clean_repo_with_empty_output_json_is_verified_empty(self, repo_dir: Path) -> None:
        provider = _provider(repo_dir, output_json={})
        assert await provider.git_diff_empty(uuid.uuid4(), "node-a") == EvidenceResult.verified_empty

    async def test_substantive_change_is_has_work(self, repo_dir: Path) -> None:
        (repo_dir / "base.txt").write_text("changed\n")
        provider = _provider(repo_dir, output_json={})
        assert await provider.git_diff_empty(uuid.uuid4(), "node-a") == EvidenceResult.has_work

    async def test_whitespace_only_change_ignored_is_verified_empty(self, repo_dir: Path) -> None:
        # A trailing-space-only change is whitespace-ignorable for `git diff -w`.
        (repo_dir / "base.txt").write_text("base   \n")
        provider = _provider(repo_dir, output_json={})
        assert await provider.git_diff_empty(uuid.uuid4(), "node-a") == EvidenceResult.verified_empty

    async def test_untracked_file_is_has_work(self, repo_dir: Path) -> None:
        (repo_dir / "new.txt").write_text("new\n")
        provider = _provider(repo_dir, output_json={})
        assert await provider.git_diff_empty(uuid.uuid4(), "node-a") == EvidenceResult.has_work

    async def test_output_json_content_counts_as_has_work(self, repo_dir: Path) -> None:
        provider = _provider(repo_dir, output_json={"tickets_groomed": 3})
        assert await provider.git_diff_empty(uuid.uuid4(), "node-a") == EvidenceResult.has_work

    async def test_no_repo_is_unverifiable(self, tmp_path: Path) -> None:
        empty = tmp_path / "no-repo"
        empty.mkdir()
        provider = _provider(empty, output_json={})
        assert await provider.git_diff_empty(uuid.uuid4(), "node-a") == EvidenceResult.unverifiable

    async def test_no_sandbox_is_unverifiable(self, repo_dir: Path) -> None:
        provider = SandboxEvidenceProvider(
            sandbox_id_resolver=lambda _rid, _nid: None,
            run_command=_local_runner(repo_dir),
            output_json_loader=lambda _rid, _nid: {},
            timeout_seconds=30.0,
        )
        assert await provider.git_diff_empty(uuid.uuid4(), "node-a") == EvidenceResult.unverifiable

    async def test_timeout_propagates(self, repo_dir: Path) -> None:
        async def _slow(_sandbox_id: str, _command: str) -> CommandResult:
            await asyncio.sleep(10)
            return CommandResult(0, "", "")

        provider = SandboxEvidenceProvider(
            sandbox_id_resolver=lambda _rid, _nid: "sandbox-1",
            run_command=_slow,
            output_json_loader=lambda _rid, _nid: {},
            timeout_seconds=0.1,
        )
        with pytest.raises(asyncio.TimeoutError):
            await provider.git_diff_empty(uuid.uuid4(), "node-a")

    async def test_no_output_json_loader_is_unverifiable_on_clean_repo(self, repo_dir: Path) -> None:
        provider = SandboxEvidenceProvider(
            sandbox_id_resolver=lambda _rid, _nid: "sandbox-1",
            run_command=_local_runner(repo_dir),
            output_json_loader=None,
            timeout_seconds=30.0,
        )
        assert await provider.git_diff_empty(uuid.uuid4(), "node-a") == EvidenceResult.unverifiable


class TestSandboxFilesystemProbe:
    async def test_fs_with_content_is_has_work(self, repo_dir: Path) -> None:
        provider = _provider(repo_dir)
        assert await provider.sandbox_filesystem_probe(uuid.uuid4(), "node-a") == EvidenceResult.has_work

    async def test_empty_fs_is_verified_empty(self, tmp_path: Path) -> None:
        empty = tmp_path / "empty"
        empty.mkdir()
        provider = _provider(empty, root=empty)
        assert await provider.sandbox_filesystem_probe(uuid.uuid4(), "node-a") == EvidenceResult.verified_empty

    async def test_no_sandbox_is_unverifiable(self) -> None:
        provider = SandboxEvidenceProvider(sandbox_id_resolver=lambda _rid, _nid: None)
        assert await provider.sandbox_filesystem_probe(uuid.uuid4(), "node-a") == EvidenceResult.unverifiable

    async def test_no_list_files_is_unverifiable(self) -> None:
        provider = SandboxEvidenceProvider(sandbox_id_resolver=lambda _rid, _nid: "sandbox-1", list_files=None)
        assert await provider.sandbox_filesystem_probe(uuid.uuid4(), "node-a") == EvidenceResult.unverifiable

    async def test_directory_only_fs_is_verified_empty(self, tmp_path: Path) -> None:
        root = tmp_path / "dirs"
        root.mkdir()
        (root / "sub").mkdir()

        async def _list(_sid: str) -> list[FileInfo]:
            return [FileInfo(name="sub", size=0, is_dir=True)]

        provider = SandboxEvidenceProvider(
            sandbox_id_resolver=lambda _rid, _nid: "sandbox-1",
            list_files=_list,
            timeout_seconds=30.0,
        )
        assert await provider.sandbox_filesystem_probe(uuid.uuid4(), "node-a") == EvidenceResult.verified_empty


class TestProviderResolverConvenience:
    async def test_awaitable_resolver_is_awaited(self) -> None:
        async def _resolve(_rid, _nid) -> str | None:
            return "sandbox-1"

        provider = SandboxEvidenceProvider(
            sandbox_id_resolver=_resolve,
            run_command=lambda _sid, _cmd: _cmd_result(),
            output_json_loader=lambda _rid, _nid: {},
            timeout_seconds=30.0,
        )
        # Reaching the command runner with a resolved sandbox id proves the
        # awaitable resolver path was awaited (non-awaitable resolver covered
        # implicitly by _provider() above).
        result = await provider._resolve_sandbox_id(uuid.uuid4(), "node-a")
        assert result == "sandbox-1"

    async def test_awaitable_output_json_loader_is_awaited(self) -> None:
        async def _load(_rid, _nid) -> dict:
            return {"a": 1}

        provider = SandboxEvidenceProvider(output_json_loader=_load)
        result = await provider._load_output_json(uuid.uuid4(), "node-a")
        assert result == {"a": 1}


def _cmd_result():
    return CommandResult(exit_code=0, stdout="", stderr="")


# ---------------------------------------------------------------------------
# run_evidence_probe — bounded runner + run_evidence persistence
# ---------------------------------------------------------------------------


class FakeEvidenceProvider:
    """The injectable fake — canned git/fs results, records calls."""

    def __init__(self, git_result: EvidenceResult, fs_result: EvidenceResult) -> None:
        self.git_result = git_result
        self.fs_result = fs_result
        self.git_calls: list[tuple[Any, Any]] = []
        self.fs_calls: list[tuple[Any, Any]] = []

    async def git_diff_empty(self, run_id: uuid.UUID, node_id: str) -> EvidenceResult:
        self.git_calls.append((run_id, node_id))
        return self.git_result

    async def sandbox_filesystem_probe(self, run_id: uuid.UUID, node_id: str) -> EvidenceResult:
        self.fs_calls.append((run_id, node_id))
        return self.fs_result


@pytest.fixture
async def sqlite_factory():
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(
            lambda sync_conn: Base.metadata.create_all(sync_conn, tables=[RunEvidence.__table__, Run.__table__])
        )
    factory = async_sessionmaker(engine, expire_on_commit=False, autobegin=False)
    yield factory
    await engine.dispose()


class TestRunEvidenceProbe:
    async def test_verified_empty_writes_row(self, sqlite_factory) -> None:
        provider = FakeEvidenceProvider(EvidenceResult.verified_empty, EvidenceResult.verified_empty)
        run_id = uuid.uuid4()
        result = await run_evidence_probe(
            provider=provider, session_factory=sqlite_factory, run_id=run_id, node_id="node-a"
        )
        assert result == EvidenceResult.verified_empty
        assert provider.git_calls == [(run_id, "node-a")]

        async with sqlite_factory() as session, session.begin():
            rows = (await session.execute(select(RunEvidence))).scalars().all()
        assert len(rows) == 1
        assert rows[0].run_id == run_id
        assert rows[0].node_id == "node-a"
        assert rows[0].evidence_state == "verified_empty"

    async def test_has_work_any_positive_wins(self, sqlite_factory) -> None:
        provider = FakeEvidenceProvider(EvidenceResult.verified_empty, EvidenceResult.has_work)
        result = await run_evidence_probe(
            provider=provider, session_factory=sqlite_factory, run_id=uuid.uuid4(), node_id="node-a"
        )
        assert result == EvidenceResult.has_work

    async def test_unverifiable_never_flags(self, sqlite_factory) -> None:
        provider = FakeEvidenceProvider(EvidenceResult.unverifiable, EvidenceResult.unverifiable)
        result = await run_evidence_probe(
            provider=provider, session_factory=sqlite_factory, run_id=uuid.uuid4(), node_id="node-a"
        )
        assert result == EvidenceResult.unverifiable

    async def test_race_on_conflict_do_nothing(self, sqlite_factory) -> None:
        run_id = uuid.uuid4()
        provider = FakeEvidenceProvider(EvidenceResult.verified_empty, EvidenceResult.verified_empty)
        await run_evidence_probe(provider=provider, session_factory=sqlite_factory, run_id=run_id, node_id="node-a")
        # A second write (the reconciliation sweep racing the async probe) must
        # not raise on the UNIQUE(run_id, node_id) constraint.
        await run_evidence_probe(provider=provider, session_factory=sqlite_factory, run_id=run_id, node_id="node-a")
        async with sqlite_factory() as session, session.begin():
            rows = (await session.execute(select(RunEvidence))).scalars().all()
        assert len(rows) == 1

    async def test_write_evidence_row_persists_detail(self, sqlite_factory) -> None:
        run_id = uuid.uuid4()
        async with sqlite_factory() as session, session.begin():
            await write_evidence_row(
                session, run_id=run_id, node_id="node-a", evidence_state="unverifiable", evidence_detail="timeout"
            )
        async with sqlite_factory() as session, session.begin():
            row = (await session.execute(select(RunEvidence))).scalars().one()
        assert row.evidence_state == "unverifiable"
        assert row.evidence_detail == "timeout"
        assert row.evidence_written_at is not None


# ---------------------------------------------------------------------------
# §15.14 metric fires-when (fake meter pattern from test_cost_metrics.py)
# ---------------------------------------------------------------------------


class _FakeCounter:
    def __init__(self, name: str) -> None:
        self.name = name
        self.calls: list[dict] = []

    def add(self, value: int, attributes: dict | None = None) -> None:
        self.calls.append({"value": value, "attributes": attributes})


class _FakeHistogram:
    def __init__(self, name: str) -> None:
        self.name = name
        self.records: list[float] = []

    def record(self, value: float, attributes: dict | None = None) -> None:
        self.records.append(value)


class _FakeGauge:
    def __init__(self, name: str) -> None:
        self.name = name
        self.values: list[float] = []

    def set(self, value: float) -> None:
        self.values.append(value)


class _FakeMeter:
    def __init__(self) -> None:
        self.counters: list[_FakeCounter] = []
        self.histograms: list[_FakeHistogram] = []
        self.gauges: list[_FakeGauge] = []

    def create_counter(self, *, name: str, description: str, unit: str) -> _FakeCounter:
        c = _FakeCounter(name)
        self.counters.append(c)
        return c

    def counter(self, name: str) -> _FakeCounter | None:
        return next((c for c in self.counters if c.name == name), None)

    def create_histogram(self, *, name: str, description: str, unit: str) -> _FakeHistogram:
        h = _FakeHistogram(name)
        self.histograms.append(h)
        return h

    def histogram(self, name: str) -> _FakeHistogram | None:
        return next((h for h in self.histograms if h.name == name), None)

    def create_gauge(self, *, name: str, description: str, unit: str) -> _FakeGauge:
        g = _FakeGauge(name)
        self.gauges.append(g)
        return g

    def gauge(self, name: str) -> _FakeGauge | None:
        return next((g for g in self.gauges if g.name == name), None)


_ALL_HANDLES = (
    "_heuristic_errors_total",
    "_heuristic_unverifiable_total",
    "_heuristic_probe_latency",
    "_heuristic_probe_cost",
)


@pytest.fixture(autouse=True)
def _reset_metric_handles(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in _ALL_HANDLES:
        monkeypatch.setattr(evidence, name, None)


@pytest.fixture
def fake_meter() -> _FakeMeter:
    return _FakeMeter()


def _stub_meter(monkeypatch: pytest.MonkeyPatch, meter: _FakeMeter | None) -> None:
    monkeypatch.setattr(evidence, "_get_meter", lambda: meter)


class TestHeuristicMetrics:
    def test_errors_total_fires_when_probe_raises(
        self, monkeypatch: pytest.MonkeyPatch, fake_meter: _FakeMeter
    ) -> None:
        _stub_meter(monkeypatch, fake_meter)
        evidence.record_heuristic_error("probe_raised")
        counter = fake_meter.counter("modulo_heuristic_errors_total")
        assert counter is not None
        assert counter.calls
        assert counter.calls[0]["attributes"] == {"reason": "probe_raised"}

    def test_unverifiable_total_fires_when_row_unverifiable(
        self, monkeypatch: pytest.MonkeyPatch, fake_meter: _FakeMeter
    ) -> None:
        _stub_meter(monkeypatch, fake_meter)
        evidence.record_heuristic_unverifiable("probe exceeded the bounded window")
        counter = fake_meter.counter("modulo_heuristic_unverifiable_total")
        assert counter is not None
        assert counter.calls[0]["attributes"] == {"reason": "probe exceeded the bounded window"}

    async def test_probe_latency_and_cost_fire_on_success_path(
        self, sqlite_factory, monkeypatch: pytest.MonkeyPatch, fake_meter: _FakeMeter
    ) -> None:
        _stub_meter(monkeypatch, fake_meter)
        provider = FakeEvidenceProvider(EvidenceResult.verified_empty, EvidenceResult.verified_empty)
        result = await run_evidence_probe(
            provider=provider, session_factory=sqlite_factory, run_id=uuid.uuid4(), node_id="node-a"
        )
        assert result == EvidenceResult.verified_empty
        hist = fake_meter.histogram("modulo_heuristic_probe_latency")
        assert hist is not None
        assert hist.records
        assert hist.records[0] >= 0
        gauge = fake_meter.gauge("modulo_heuristic_probe_cost")
        assert gauge is not None
        assert gauge.values
        assert gauge.values[0] >= 0

    async def test_probe_latency_bounded_at_3s_and_unverifiable_on_timeout(
        self, sqlite_factory, monkeypatch: pytest.MonkeyPatch, fake_meter: _FakeMeter
    ) -> None:
        _stub_meter(monkeypatch, fake_meter)

        class _SlowProvider:
            async def git_diff_empty(self, run_id, node_id):
                await asyncio.sleep(30)
                return EvidenceResult.verified_empty

            async def sandbox_filesystem_probe(self, run_id, node_id):
                return EvidenceResult.verified_empty

        result = await run_evidence_probe(
            provider=_SlowProvider(), session_factory=sqlite_factory, run_id=uuid.uuid4(), node_id="node-a"
        )
        assert result == EvidenceResult.unverifiable
        hist = fake_meter.histogram("modulo_heuristic_probe_latency")
        assert hist is not None
        assert hist.records
        assert hist.records[0] <= EVIDENCE_PROBE_TIMEOUT_SECONDS + 1.0
        unv = fake_meter.counter("modulo_heuristic_unverifiable_total")
        assert unv is not None
        assert unv.calls
        assert "bounded window" in unv.calls[0]["attributes"]["reason"]

    def test_metrics_noop_without_meter_provider(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _stub_meter(monkeypatch, None)
        for attr in (
            "_heuristic_errors_total",
            "_heuristic_unverifiable_total",
            "_heuristic_probe_latency",
            "_heuristic_probe_cost",
        ):
            setattr(evidence, attr, None)
        evidence.record_heuristic_error("x")
        evidence.record_heuristic_unverifiable("y")
        evidence.record_heuristic_probe_latency(0.5)
        evidence.record_heuristic_probe_cost(0.0001)
        assert evidence._heuristic_errors_total is None
        assert evidence._heuristic_unverifiable_total is None
        assert evidence._heuristic_probe_latency is None
        assert evidence._heuristic_probe_cost is None

    def test_get_meter_returns_none_when_provider_missing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import opentelemetry.metrics as otel_metrics

        provider = otel_metrics.get_meter_provider()
        monkeypatch.setattr(otel_metrics, "get_meter_provider", lambda: None)
        assert evidence._get_meter() is None
        assert provider is not None  # ensure we actually toggled the provider

    def test_get_meter_returns_none_on_lookup_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _boom() -> None:
            raise RuntimeError("otel unavailable")

        monkeypatch.setattr(
            "opentelemetry.metrics.get_meter_provider",
            _boom,
        )
        assert evidence._get_meter() is None

    def test_ensure_early_returns_when_handles_set(self) -> None:
        evidence._heuristic_errors_total = object()
        evidence._ensure()
        assert evidence._heuristic_errors_total is not None


class TestMetadataKeySkip:
    def test_output_json_has_content_skips_metadata_key(self) -> None:
        # _METADATA_OUTPUT_JSON_KEYS is empty today, so the branch is a no-op —
        # a normal key still counts as content.
        assert output_json_has_content({"status": "completed", "summary": "done"}) is True
        assert output_json_has_content({"status": ""}) is False


class TestSandboxProviderUnavailablePaths:
    def test_resolve_sandbox_id_none_resolver(self) -> None:
        provider = SandboxEvidenceProvider()
        assert asyncio.run(provider._resolve_sandbox_id(uuid.uuid4(), "node-a")) is None

    def test_resolve_sandbox_id_none_resolver_reconcile(self, sqlite_factory) -> None:
        provider = SandboxEvidenceProvider()
        result = asyncio.run(
            run_evidence_probe(
                provider=provider,
                session_factory=sqlite_factory,
                run_id=uuid.uuid4(),
                node_id="node-a",
            )
        )
        assert result == EvidenceResult.unverifiable

    def test_git_diff_empty_no_run_command(self) -> None:
        async def _no_sandbox(run_id, node_id):
            return None

        provider = SandboxEvidenceProvider(sandbox_id_resolver=_no_sandbox)
        result = asyncio.run(provider.git_diff_empty(uuid.uuid4(), "node-a"))
        assert result == EvidenceResult.unverifiable

    def test_run_evidence_probe_cancelled_error_propagates(self, sqlite_factory) -> None:
        class _CancellingProvider:
            async def git_diff_empty(self, run_id, node_id):
                raise asyncio.CancelledError

        with pytest.raises(asyncio.CancelledError):
            asyncio.run(
                run_evidence_probe(
                    provider=_CancellingProvider(),
                    session_factory=sqlite_factory,
                    run_id=uuid.uuid4(),
                    node_id="node-a",
                )
            )

    def test_reconcile_disabled_returns_empty_summary(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MODULO_HEURISTIC_ENABLED", "0")
        summary = asyncio.run(
            reconcile_noop_evidence(
                session_factory=lambda: None,  # type: ignore[arg-type]  # never called when disabled
                provider=object(),  # type: ignore[arg-type]
            )
        )
        assert summary == {
            "scanned": 0,
            "probed": 0,
            "has_work": 0,
            "verified_empty": 0,
            "unverifiable": 0,
            "errors": 0,
        }


# ---------------------------------------------------------------------------
# reconcile_noop_evidence — bounded reconciliation sweep
# ---------------------------------------------------------------------------


def _complete_run(run_id: uuid.UUID, org_id: uuid.UUID, outputs_json: dict[str, Any], telemetry: dict[str, Any]) -> Run:
    return Run(
        id=run_id,
        organisation_id=org_id,
        pipeline_id=uuid.uuid4(),
        snapshot_id=uuid.uuid4(),
        trigger_type="manual",
        status="complete",
        run_number=1,
        input_hash="a" * 64,
        langgraph_thread_id=f"thread-{run_id}",
        completed_at=datetime.datetime.now(datetime.UTC),
        outputs_json=outputs_json,
        node_telemetry_json=telemetry,
    )


class TestReconcileNoopEvidence:
    async def test_backfills_missing_evidence_rows(self, sqlite_factory) -> None:
        run_id = uuid.uuid4()
        org_id = uuid.uuid4()
        async with sqlite_factory() as session, session.begin():
            session.add(
                _complete_run(
                    run_id,
                    org_id,
                    outputs_json={
                        "node-a": {
                            "artifacts": [
                                {"output": {"output_json": {}, "agent_status": "completed", "agent_outcome": "success"}}
                            ],
                            "output": {"agent_status": "completed", "agent_outcome": "success"},
                        }
                    },
                    telemetry={"node-a": {"agent_status": "completed", "agent_outcome": "success"}},
                )
            )
            await session.flush()

        provider = FakeEvidenceProvider(EvidenceResult.verified_empty, EvidenceResult.verified_empty)
        summary = await reconcile_noop_evidence(sqlite_factory, provider=provider, max_runs=10)

        assert summary["scanned"] == 1
        assert summary["probed"] == 1
        assert summary["verified_empty"] == 1
        async with sqlite_factory() as session, session.begin():
            rows = (await session.execute(select(RunEvidence))).scalars().all()
        assert len(rows) == 1
        assert rows[0].run_id == run_id
        assert rows[0].node_id == "node-a"

    async def test_skips_runs_that_already_have_evidence(self, sqlite_factory) -> None:
        run_id = uuid.uuid4()
        async with sqlite_factory() as session, session.begin():
            session.add(
                _complete_run(
                    run_id,
                    uuid.uuid4(),
                    outputs_json={"node-a": {"output": {"agent_status": "completed", "agent_outcome": "success"}}},
                    telemetry={"node-a": {"agent_status": "completed", "agent_outcome": "success"}},
                )
            )
            await session.flush()
            await write_evidence_row(
                session, run_id=run_id, node_id="node-a", evidence_state="verified_empty", evidence_detail=""
            )

        provider = FakeEvidenceProvider(EvidenceResult.has_work, EvidenceResult.has_work)
        summary = await reconcile_noop_evidence(sqlite_factory, provider=provider, max_runs=10)

        assert summary["scanned"] == 1
        assert summary["probed"] == 0

    async def test_ignores_non_declared_success_nodes(self, sqlite_factory) -> None:
        run_id = uuid.uuid4()
        async with sqlite_factory() as session, session.begin():
            session.add(
                _complete_run(
                    run_id,
                    uuid.uuid4(),
                    outputs_json={"node-a": {"output": {"agent_status": "completed"}}},
                    telemetry={"node-a": {"agent_status": "completed"}},
                )
            )
            await session.flush()

        provider = FakeEvidenceProvider(EvidenceResult.verified_empty, EvidenceResult.verified_empty)
        summary = await reconcile_noop_evidence(sqlite_factory, provider=provider, max_runs=10)

        assert summary["scanned"] == 1
        assert summary["probed"] == 0

    async def test_probe_failure_counts_as_error_and_continues(self, sqlite_factory) -> None:
        run_id = uuid.uuid4()
        async with sqlite_factory() as session, session.begin():
            session.add(
                _complete_run(
                    run_id,
                    uuid.uuid4(),
                    outputs_json={"node-a": {"output": {"agent_status": "completed", "agent_outcome": "success"}}},
                    telemetry={"node-a": {"agent_status": "completed", "agent_outcome": "success"}},
                )
            )
            await session.flush()

        class _RaisingProvider:
            async def git_diff_empty(self, run_id, node_id):
                raise RuntimeError("sandbox gone")

            async def sandbox_filesystem_probe(self, run_id, node_id):
                return EvidenceResult.verified_empty

        # run_evidence_probe fails open (never raises for ordinary errors), so
        # the sweep's errors bucket is only reachable by an exception escaping
        # run_evidence_probe itself — patch it to raise and assert the sweep
        # still completes without aborting.
        async def _exploding_probe(**kwargs):
            raise RuntimeError("probe layer failure")

        monkeypatch_probe = pytest.MonkeyPatch()
        monkeypatch_probe.setattr("modulo.core.pipeline_engine.evidence.run_evidence_probe", _exploding_probe)
        try:
            summary = await reconcile_noop_evidence(sqlite_factory, provider=_RaisingProvider(), max_runs=10)
        finally:
            monkeypatch_probe.undo()

        assert summary["scanned"] == 1
        assert summary["probed"] == 1
        assert summary["errors"] == 1

    async def test_budget_break_stops_sweep(self, sqlite_factory) -> None:
        run_id = uuid.uuid4()
        async with sqlite_factory() as session, session.begin():
            session.add(
                _complete_run(
                    run_id,
                    uuid.uuid4(),
                    outputs_json={"node-a": {"output": {"agent_status": "completed", "agent_outcome": "success"}}},
                    telemetry={"node-a": {"agent_status": "completed", "agent_outcome": "success"}},
                )
            )
            await session.flush()

        provider = FakeEvidenceProvider(EvidenceResult.verified_empty, EvidenceResult.verified_empty)
        # A negative budget puts the deadline in the past — the sweep scans the
        # run then stops before probing any node.
        summary = await reconcile_noop_evidence(sqlite_factory, provider=provider, max_runs=10, budget_seconds=-1.0)
        assert summary["scanned"] == 1
        assert summary["probed"] == 0


class TestEvidenceEnabled:
    def test_default_enabled_without_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("MODULO_HEURISTIC_ENABLED", raising=False)
        assert evidence_enabled() is True

    @pytest.mark.parametrize(
        "value",
        ["1", "true", "TRUE", "yes", "on"],
    )
    def test_env_truthy_values(self, monkeypatch: pytest.MonkeyPatch, value: str) -> None:
        monkeypatch.setenv("MODULO_HEURISTIC_ENABLED", value)
        assert evidence_enabled() is True

    @pytest.mark.parametrize(
        "value",
        ["0", "false", "no", "off", "banana"],
    )
    def test_env_falsy_values(self, monkeypatch: pytest.MonkeyPatch, value: str) -> None:
        monkeypatch.setenv("MODULO_HEURISTIC_ENABLED", value)
        assert evidence_enabled() is False

    def test_settings_fallback_enabled(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("MODULO_HEURISTIC_ENABLED", raising=False)

        class _Settings:
            modulo_heuristic_enabled = True

        monkeypatch.setattr("modulo.settings.get_settings", lambda: _Settings())
        assert evidence_enabled() is True

    def test_settings_fallback_disabled(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("MODULO_HEURISTIC_ENABLED", raising=False)

        class _Settings:
            modulo_heuristic_enabled = False

        monkeypatch.setattr("modulo.settings.get_settings", lambda: _Settings())
        assert evidence_enabled() is False

    def test_settings_lookup_failure_defaults_enabled(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("MODULO_HEURISTIC_ENABLED", raising=False)

        def _boom() -> None:
            raise ImportError("no settings module")

        monkeypatch.setattr("modulo.settings.get_settings", _boom)
        assert evidence_enabled() is True

    async def test_run_probe_disabled_is_unverifiable_without_provider(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MODULO_HEURISTIC_ENABLED", "0")
        result = await run_evidence_probe(
            provider=object(),  # type: ignore[arg-type]  # never touched when disabled
            session_factory=object(),  # type: ignore[arg-type]
            run_id=uuid.uuid4(),
            node_id="node-a",
        )
        assert result == EvidenceResult.unverifiable


class TestRunEvidenceProbeErrorPaths:
    async def test_probe_error_records_and_fails_open(self, sqlite_factory) -> None:
        class _BrokenProvider:
            async def git_diff_empty(self, run_id, node_id):
                raise RuntimeError("boom")

            async def sandbox_filesystem_probe(self, run_id, node_id):
                return EvidenceResult.verified_empty

        result = await run_evidence_probe(
            provider=_BrokenProvider(), session_factory=sqlite_factory, run_id=uuid.uuid4(), node_id="node-a"
        )
        assert result == EvidenceResult.unverifiable
        async with sqlite_factory() as session, session.begin():
            rows = (await session.execute(select(RunEvidence))).scalars().all()
        assert len(rows) == 1
        assert rows[0].evidence_state == "unverifiable"

    async def test_evidence_write_failure_fails_open(self, sqlite_factory) -> None:
        provider = FakeEvidenceProvider(EvidenceResult.verified_empty, EvidenceResult.verified_empty)

        class _BrokenFactory:
            def __call__(self):
                raise RuntimeError("db down")

        result = await run_evidence_probe(
            provider=provider, session_factory=_BrokenFactory(), run_id=uuid.uuid4(), node_id="node-a"
        )
        assert result == EvidenceResult.verified_empty

    async def test_cancelled_error_propagates(self, sqlite_factory) -> None:
        class _CancelProvider:
            async def git_diff_empty(self, run_id, node_id):
                raise asyncio.CancelledError()

            async def sandbox_filesystem_probe(self, run_id, node_id):
                return EvidenceResult.verified_empty

        with pytest.raises(asyncio.CancelledError):
            await run_evidence_probe(
                provider=_CancelProvider(), session_factory=sqlite_factory, run_id=uuid.uuid4(), node_id="node-a"
            )
