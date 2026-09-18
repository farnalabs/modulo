"""Unit tests for modulo.core.worker_memory (FAR-776).

Covers: /proc/meminfo parsing, cgroup v1 counter reads, memory capture with
fake FS paths, alarm evaluation at thresholds, and the cron wrapper. All
tests use monkeypatched paths — no real FS access.
"""

from __future__ import annotations

import textwrap
from typing import Any
from unittest.mock import patch

import pytest

import modulo.core.worker_memory as wm

# ---------------------------------------------------------------------------
# /proc/meminfo parsing
# ---------------------------------------------------------------------------


class TestReadProcMeminfo:
    """Parse /proc/meminfo lines into a dict of name -> kB values."""

    def test_parses_standard_fields(self, tmp_path: Any) -> None:
        meminfo = tmp_path / "meminfo"
        meminfo.write_text(
            textwrap.dedent("""\
                MemTotal:       2048000 kB
                MemFree:         100000 kB
                MemAvailable:    500000 kB
                Buffers:          50000 kB
                Cached:          200000 kB
                Committed_AS:   1300000 kB
                CommitLimit:     960000 kB
            """)
        )
        with patch.object(wm, "_PROC_MEMINFO_PATH", str(meminfo)):
            result = wm._read_proc_meminfo()
        assert result["MemTotal"] == 2048000
        assert result["MemAvailable"] == 500000
        assert result["Committed_AS"] == 1300000
        assert result["CommitLimit"] == 960000

    def test_skips_unrecognised_fields(self, tmp_path: Any) -> None:
        meminfo = tmp_path / "meminfo"
        meminfo.write_text("MemTotal:       2048000 kB\nBuffers:          50000 kB\n")
        with patch.object(wm, "_PROC_MEMINFO_PATH", str(meminfo)):
            result = wm._read_proc_meminfo()
        assert "MemTotal" in result
        assert "Buffers" not in result

    def test_empty_file_returns_empty_dict(self, tmp_path: Any) -> None:
        meminfo = tmp_path / "meminfo"
        meminfo.write_text("")
        with patch.object(wm, "_PROC_MEMINFO_PATH", str(meminfo)):
            result = wm._read_proc_meminfo()
        assert result == {}

    def test_missing_file_returns_empty_dict(self, tmp_path: Any) -> None:
        with patch.object(wm, "_PROC_MEMINFO_PATH", str(tmp_path / "nonexistent")):
            result = wm._read_proc_meminfo()
        assert result == {}


# ---------------------------------------------------------------------------
# cgroup v1 counter reads
# ---------------------------------------------------------------------------


class TestReadCgroupCounter:
    """Read a single integer from a cgroup v1 file."""

    def test_reads_integer(self, tmp_path: Any) -> None:
        f = tmp_path / "counter"
        f.write_text("42\n")
        result = wm._read_cgroup_counter(str(f))
        assert result == 42

    def test_missing_file_returns_none(self, tmp_path: Any) -> None:
        result = wm._read_cgroup_counter(str(tmp_path / "nope"))
        assert result is None


class TestReadCgroupOomControl:
    """Parse memory.oom_control into a dict."""

    def test_parses_oom_kill(self, tmp_path: Any) -> None:
        f = tmp_path / "oom_control"
        f.write_text("oom_kill_disable: 0\nunder_oom: 0\noom_kill: 1\n")
        with patch.object(wm, "_CGROUP_MEMORY_OOM_CONTROL", str(f)):
            result = wm._read_cgroup_oom_control()
        assert result["oom_kill"] == "1"

    def test_missing_file_returns_empty(self, tmp_path: Any) -> None:
        with patch.object(wm, "_CGROUP_MEMORY_OOM_CONTROL", str(tmp_path / "nope")):
            result = wm._read_cgroup_oom_control()
        assert result == {}


# ---------------------------------------------------------------------------
# capture_memory_stats (integration of reads)
# ---------------------------------------------------------------------------


class TestCaptureMemoryStats:
    """Full capture with fake FS paths."""

    def test_captures_all_fields(self, tmp_path: Any) -> None:
        meminfo = tmp_path / "meminfo"
        meminfo.write_text(
            textwrap.dedent("""\
                MemTotal:       2048000 kB
                MemAvailable:    500000 kB
                Committed_AS:   1300000 kB
                CommitLimit:     960000 kB
            """)
        )
        failcnt = tmp_path / "failcnt"
        failcnt.write_text("7\n")
        oom_ctrl = tmp_path / "oom_control"
        oom_ctrl.write_text("oom_kill_disable: 0\noom_kill: 3\n")
        peak = tmp_path / "peak"
        peak.write_text("1800000\n")

        with (
            patch.object(wm, "_PROC_MEMINFO_PATH", str(meminfo)),
            patch.object(wm, "_CGROUP_MEMORY_FAILCNT", str(failcnt)),
            patch.object(wm, "_CGROUP_MEMORY_OOM_CONTROL", str(oom_ctrl)),
            patch.object(wm, "_CGROUP_MEMORY_PEAK", str(peak)),
        ):
            stats = wm.capture_memory_stats()

        assert stats["MemTotal"] == 2048000
        assert stats["MemAvailable"] == 500000
        assert stats["Committed_AS"] == 1300000
        assert stats["CommitLimit"] == 960000
        assert stats["memory_failcnt"] == 7
        assert stats["memory_oom_kill"] == "3"
        assert stats["memory_peak"] == 1800000
        assert "machine_id" in stats

    def test_partial_capture_on_missing_files(self, tmp_path: Any) -> None:
        """When some cgroup files are missing, capture still returns /proc fields."""
        meminfo = tmp_path / "meminfo"
        meminfo.write_text("MemTotal:       1024000 kB\nMemAvailable:    900000 kB\n")
        with (
            patch.object(wm, "_PROC_MEMINFO_PATH", str(meminfo)),
            patch.object(wm, "_CGROUP_MEMORY_FAILCNT", "/nonexistent"),
            patch.object(wm, "_CGROUP_MEMORY_OOM_CONTROL", "/nonexistent"),
            patch.object(wm, "_CGROUP_MEMORY_PEAK", "/nonexistent"),
        ):
            stats = wm.capture_memory_stats()
        assert stats["MemTotal"] == 1024000
        assert stats["MemAvailable"] == 900000
        assert "memory_failcnt" not in stats
        assert "memory_oom_kill" not in stats


# ---------------------------------------------------------------------------
# evaluate_memory_alarm
# ---------------------------------------------------------------------------


class TestEvaluateMemoryAlarm:
    """Threshold evaluation — returns alarm reason or None."""

    def test_no_alarm_when_healthy(self) -> None:
        stats = {"MemTotal": 2048000, "MemAvailable": 1500000, "Committed_AS": 800000, "CommitLimit": 960000}
        assert wm.evaluate_memory_alarm(stats) is None

    def test_alarm_on_low_available(self) -> None:
        """MemAvailable < 20% of MemTotal triggers the alarm."""
        stats = {"MemTotal": 2048000, "MemAvailable": 100000, "Committed_AS": 800000, "CommitLimit": 960000}
        alarm = wm.evaluate_memory_alarm(stats)
        assert alarm is not None
        assert "MemAvailable=100000kB" in alarm
        assert "20% of MemTotal" in alarm

    def test_alarm_on_overcommit(self) -> None:
        """Committed_AS > CommitLimit triggers the alarm."""
        stats = {"MemTotal": 2048000, "MemAvailable": 500000, "Committed_AS": 1300000, "CommitLimit": 960000}
        alarm = wm.evaluate_memory_alarm(stats)
        assert alarm is not None
        assert "Committed_AS=1300000kB" in alarm
        assert "CommitLimit=960000kB" in alarm

    def test_low_available_takes_priority_over_overcommit(self) -> None:
        """When both are breached, MemAvailable fires first."""
        stats = {"MemTotal": 2048000, "MemAvailable": 50000, "Committed_AS": 1500000, "CommitLimit": 960000}
        alarm = wm.evaluate_memory_alarm(stats)
        assert alarm is not None
        assert "MemAvailable" in alarm

    def test_boundary_at_exactly_20_percent(self) -> None:
        """Exactly 20% available is NOT a breach (strict <)."""
        stats = {"MemTotal": 1000000, "MemAvailable": 200000, "Committed_AS": 500000, "CommitLimit": 960000}
        assert wm.evaluate_memory_alarm(stats) is None

    def test_boundary_just_below_20_percent(self) -> None:
        """19.9% available IS a breach."""
        stats = {"MemTotal": 1000000, "MemAvailable": 199000, "Committed_AS": 500000, "CommitLimit": 960000}
        alarm = wm.evaluate_memory_alarm(stats)
        assert alarm is not None

    def test_boundary_committed_equals_limit(self) -> None:
        """Committed_AS == CommitLimit is NOT a breach (strict >)."""
        stats = {"MemTotal": 2048000, "MemAvailable": 500000, "Committed_AS": 960000, "CommitLimit": 960000}
        assert wm.evaluate_memory_alarm(stats) is None

    def test_boundary_committed_just_over_limit(self) -> None:
        """Committed_AS == CommitLimit + 1 IS a breach."""
        stats = {"MemTotal": 2048000, "MemAvailable": 500000, "Committed_AS": 960001, "CommitLimit": 960000}
        alarm = wm.evaluate_memory_alarm(stats)
        assert alarm is not None

    def test_missing_fields_no_alarm(self) -> None:
        """Missing /proc fields should not trigger a false alarm."""
        assert wm.evaluate_memory_alarm({}) is None
        assert wm.evaluate_memory_alarm({"MemTotal": 1000}) is None

    def test_zero_memtotal_no_division_error(self) -> None:
        """MemTotal=0 should not cause ZeroDivisionError."""
        stats = {"MemTotal": 0, "MemAvailable": 0, "Committed_AS": 0, "CommitLimit": 0}
        assert wm.evaluate_memory_alarm(stats) is None


# ---------------------------------------------------------------------------
# memory_monitor_cron (async wrapper)
# ---------------------------------------------------------------------------


class TestMemoryMonitorCron:
    """The SAQ cron wrapper — captures + logs + alarm."""

    @pytest.mark.asyncio
    async def test_returns_ok_with_alarm_false(self) -> None:
        fake_stats = {
            "MemTotal": 2048000,
            "MemAvailable": 1500000,
            "Committed_AS": 800000,
            "CommitLimit": 960000,
        }
        with patch.object(wm, "capture_memory_stats", return_value=fake_stats):
            result = await wm.memory_monitor_cron({})
        assert result["status"] == "ok"
        assert result["alarm"] is False

    @pytest.mark.asyncio
    async def test_returns_alarm_true_when_breach(self) -> None:
        fake_stats = {
            "MemTotal": 2048000,
            "MemAvailable": 100000,  # < 20% — alarm fires
            "Committed_AS": 800000,
            "CommitLimit": 960000,
        }
        with patch.object(wm, "capture_memory_stats", return_value=fake_stats):
            result = await wm.memory_monitor_cron({})
        assert result["alarm"] is True

    @pytest.mark.asyncio
    async def test_never_raises_on_capture_failure(self) -> None:
        with patch.object(wm, "capture_memory_stats", return_value={}):
            result = await wm.memory_monitor_cron({})
        assert result["status"] == "ok"
