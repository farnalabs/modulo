"""Support-bundle format v1 (FAR-2305).

A versioned, allowlist-by-construction archive a self-hoster can inspect
(``--list`` / ``--show``) before sending it to support:

* **Fixed member allowlist** — a member cannot appear unless it is named in
  :data:`FIXED_MEMBERS` (or matches the ``logs/<component>.log.tail`` pattern).
  Adding one requires a golden-list test update.
* **Versioned format** — :data:`FORMAT_ID` (``modulo-support-bundle/v1``) and
  :data:`BUNDLE_FORMAT_VERSION` are stamped in ``report.json`` and the printed
  ref line. A v1 reader ignores unknown members (forward compatibility).
* **Redaction** — three substitution layers from
  :mod:`modulo.support.redaction` (seeded exact, structural ``KEY=value``,
  shared secret patterns) plus a **fail-closed post-assembly gate**: any
  surviving secret-shaped content aborts the build, names the member, writes
  no zip and exits non-zero.
* **Bounded** — every member is size-capped; the archive is capped at
  :data:`MAX_BUNDLE_COMPRESSED_BYTES` compressed, and log members are
  truncated further (recorded in the manifest) rather than emitting an
  oversized bundle.

``modulo doctor --report`` delegates here; the standalone contract is::

    python -m modulo.support.bundle --out bundle.zip [--json]
    python -m modulo.support.bundle --list bundle.zip
    python -m modulo.support.bundle --show MANIFEST.txt bundle.zip

It never requires the launcher, a data dir or ``~/.local/state``.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import sys
import zipfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Final, TextIO

from modulo.core.secret_patterns import SENSITIVE_VALUE_MASK
from modulo.support.collect import (
    LOG_PREFIX,
    LOG_SUFFIX,
    BundleSources,
    collect_sources,
)
from modulo.support.redaction import (
    REDACTED,
    RedactionCounts,
    RedactionSelfCheckError,
    allowed_member_predicate,
    assert_redaction_proven,
    redact_text,
)

FORMAT_ID: Final[str] = "modulo-support-bundle/v1"
BUNDLE_FORMAT_VERSION: Final[int] = 1
MAX_BUNDLE_COMPRESSED_BYTES: Final[int] = 5 * 1024 * 1024
MIN_LOG_BYTES_AFTER_TRUNCATION: Final[int] = 16 * 1024
MAX_TRUNCATION_PASSES: Final[int] = 8

#: The fixed v1 allowlist. A new member REQUIRES a golden-list test update.
FIXED_MEMBERS: Final[tuple[str, ...]] = (
    "MANIFEST.txt",
    "report.json",
    "version.txt",
    "doctor.txt",
    "health.json",
    "settings.txt",
    "deployment.txt",
    "db.txt",
    "errors.json",
    "runs.json",
    "licence.txt",
    "degraded.json",
)

#: What each member carries — rendered verbatim into ``MANIFEST.txt``.
MEMBER_DESCRIPTIONS: Final[dict[str, str]] = {
    "MANIFEST.txt": "this file: format version, every member, byte count and what it carries",
    "report.json": "machine inventory: format version, members with bytes, redaction counts per layer",
    "version.txt": "modulo version, build commit, Python, OS, arch, UTC offset and clock drift",
    "doctor.txt": "the existing doctor output",
    "health.json": "health check names and statuses only (closed vocabulary; no free-text detail)",
    "settings.txt": "full key inventory; values only for the reviewed safe-list, otherwise set/unset",
    "deployment.txt": "install shape, service names, image refs, restart count, proxy variable names, CA path",
    "db.txt": "migration head, expected head, server version, row counts for a fixed table list (no rows)",
    "errors.json": "top error groups: id, fingerprint, level, source, count, timestamps, sanitized message",
    "runs.json": "last failed runs: ids, status, error_code, timings, cost (no inputs/outputs/prompts/stdout)",
    "licence.txt": "tier, state, expiry, opaque id, feature list (the licence key is never included)",
    "degraded.json": "native install only: terminal-degrade reason and redacted crash backtraces",
}

_MEMBER_ALLOWED = allowed_member_predicate(FIXED_MEMBERS, LOG_PREFIX, LOG_SUFFIX)


class BundleBuildError(RuntimeError):
    """The bundle could not be built (never a silent partial write)."""


class BundleSizeExceededError(BundleBuildError):
    """The archive could not be brought under the compressed-size cap."""


@dataclass(frozen=True)
class MemberEntry:
    """One member in an existing bundle (``known`` = on the v1 allowlist)."""

    name: str
    bytes: int
    known: bool


@dataclass(frozen=True)
class BundleResult:
    """The written bundle's ref facts."""

    path: Path
    members: tuple[str, ...]
    compressed_bytes: int
    redaction: RedactionCounts
    missing: dict[str, str]
    truncated: dict[str, str]

    @property
    def ref_line(self) -> str:
        return (
            f"support bundle v1 written: {self.path} "
            f"({FORMAT_ID}, {self.compressed_bytes} compressed bytes, {len(self.members)} members)"
        )


# ---------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------


def build_bundle(
    out_path: Path,
    *,
    sources: BundleSources,
    generated_by: str = "modulo.support.bundle",
) -> BundleResult:
    """Assemble, redact, gate and atomically write a v1 bundle.

    Raises :class:`RedactionSelfCheckError` (naming the member) when the gate
    finds surviving secret-shaped content, and :class:`BundleBuildError` for a
    non-converging inventory or an over-cap archive. In every failure case no
    zip is written.
    """
    raw_members = dict(sources.members)
    for name in raw_members:
        if not _MEMBER_ALLOWED(name):
            raise BundleBuildError(f"member {name!r} is not on the support-bundle v1 allowlist")
        if name in ("MANIFEST.txt", "report.json"):
            raise BundleBuildError(f"member {name!r} is reserved for the bundle inventory")

    truncated = dict(sources.truncated)
    while True:
        redacted, counts = _redact_members(raw_members, sources.secret_values)
        manifest, report = _render_inventory(
            redacted,
            generated_by=generated_by,
            install_shape=sources.install_shape,
            counts=counts,
            missing=sources.missing,
            truncated=truncated,
        )
        members = dict(redacted)
        members["MANIFEST.txt"] = manifest
        members["report.json"] = report

        archive_bytes = _zip_bytes(members)
        if len(archive_bytes) <= MAX_BUNDLE_COMPRESSED_BYTES:
            break
        log_to_shrink = _shrinkable_log(raw_members)
        if log_to_shrink is None:
            raise BundleSizeExceededError(
                f"bundle is {len(archive_bytes)} compressed bytes, over the "
                f"{MAX_BUNDLE_COMPRESSED_BYTES}-byte cap, and no log member can be truncated further"
            )
        previous = raw_members[log_to_shrink]
        raw_members[log_to_shrink] = previous[: max(MIN_LOG_BYTES_AFTER_TRUNCATION, len(previous) // 2)]
        truncated[log_to_shrink] = (
            f"truncated from {len(previous.encode('utf-8'))} to "
            f"{len(raw_members[log_to_shrink].encode('utf-8'))} bytes for the bundle cap"
        )

    assert_redaction_proven(members, sources.secret_values, member_allowed=_MEMBER_ALLOWED)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = out_path.with_name(f"{out_path.name}.tmp-{os.getpid()}")
    try:
        temp_path.write_bytes(archive_bytes)
        temp_path.replace(out_path)
    finally:
        if temp_path.exists():
            temp_path.unlink()

    return BundleResult(
        path=out_path,
        members=tuple(sorted(members)),
        compressed_bytes=len(archive_bytes),
        redaction=counts,
        missing=dict(sources.missing),
        truncated=dict(truncated),
    )


def _redact_members(
    raw_members: Mapping[str, str],
    secret_values: Mapping[str, str],
) -> tuple[dict[str, str], RedactionCounts]:
    redacted: dict[str, str] = {}
    seed_exact = structural = patterns = 0
    for name, text in raw_members.items():
        result = redact_text(text, secret_values)
        redacted[name] = result.text
        seed_exact += result.counts.seed_exact
        structural += result.counts.structural
        patterns += result.counts.patterns
    return redacted, RedactionCounts(seed_exact=seed_exact, structural=structural, patterns=patterns)


def _shrinkable_log(raw_members: Mapping[str, str]) -> str | None:
    candidates = [
        (len(text.encode("utf-8")), name)
        for name, text in raw_members.items()
        if _MEMBER_ALLOWED(name)
        and name.startswith(LOG_PREFIX)
        and len(text.encode("utf-8")) > MIN_LOG_BYTES_AFTER_TRUNCATION
    ]
    if not candidates:
        return None
    return max(candidates)[1]


def _zip_bytes(members: Mapping[str, str]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name in sorted(members):
            archive.writestr(name, members[name].encode("utf-8"))
    return buffer.getvalue()


# ---------------------------------------------------------------------------
# Inventory (MANIFEST.txt + report.json) — a fixed point, because each file
# records the other's (and its own) byte size.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _InventoryEntry:
    name: str
    bytes: int
    description: str


def _render_inventory(
    content: Mapping[str, str],
    *,
    generated_by: str,
    install_shape: str,
    counts: RedactionCounts,
    missing: Mapping[str, str],
    truncated: Mapping[str, str],
) -> tuple[str, str]:
    generated_at = datetime.now(UTC).replace(microsecond=0).isoformat()
    manifest = ""
    report = ""
    for _ in range(6):
        entries = _inventory_entries(content, manifest, report)
        new_report = _report_text(
            entries,
            generated_by=generated_by,
            install_shape=install_shape,
            generated_at=generated_at,
            counts=counts,
            missing=missing,
            truncated=truncated,
        )
        new_manifest = _manifest_text(
            entries,
            generated_by=generated_by,
            install_shape=install_shape,
            generated_at=generated_at,
            counts=counts,
            missing=missing,
            truncated=truncated,
        )
        if new_manifest == manifest and new_report == report:
            return manifest, report
        manifest, report = new_manifest, new_report
    raise BundleBuildError("support-bundle inventory did not reach a stable size")


def _inventory_entries(content: Mapping[str, str], manifest: str, report: str) -> list[_InventoryEntry]:
    entries = [
        _InventoryEntry(
            name=name,
            bytes=len(text.encode("utf-8")),
            description=_description_for(name),
        )
        for name, text in content.items()
    ]
    entries.append(
        _InventoryEntry(
            name="MANIFEST.txt",
            bytes=len(manifest.encode("utf-8")),
            description=MEMBER_DESCRIPTIONS["MANIFEST.txt"],
        )
    )
    entries.append(
        _InventoryEntry(
            name="report.json",
            bytes=len(report.encode("utf-8")),
            description=MEMBER_DESCRIPTIONS["report.json"],
        )
    )
    return sorted(entries, key=lambda entry: entry.name)


def _description_for(name: str) -> str:
    if name in MEMBER_DESCRIPTIONS:
        return MEMBER_DESCRIPTIONS[name]
    if name.startswith(LOG_PREFIX) and name.endswith(LOG_SUFFIX):
        component = name[len(LOG_PREFIX) : -len(LOG_SUFFIX)]
        return f"capped tail of the {component} log (repeated lines collapsed)"
    return "support-bundle member"


def _redaction_payload(counts: RedactionCounts) -> dict[str, object]:
    return {
        "redacted_marker": REDACTED,
        "pattern_mask": SENSITIVE_VALUE_MASK,
        "total_substitutions": counts.total,
        "layers": counts.as_dict(),
        "layers_applied": counts.applied_layers(),
        "member_allowlist": {
            "applied": True,
            "fixed_members": list(FIXED_MEMBERS),
            "log_pattern": f"{LOG_PREFIX}<component>{LOG_SUFFIX}",
        },
    }


def _report_text(
    entries: Sequence[_InventoryEntry],
    *,
    generated_by: str,
    install_shape: str,
    generated_at: str,
    counts: RedactionCounts,
    missing: Mapping[str, str],
    truncated: Mapping[str, str],
) -> str:
    payload: dict[str, object] = {
        "format": FORMAT_ID,
        "bundle_format_version": BUNDLE_FORMAT_VERSION,
        "generated_by": generated_by,
        "generated_at": generated_at,
        "install_shape": install_shape,
        "members": [{"name": entry.name, "bytes": entry.bytes, "description": entry.description} for entry in entries],
        "redaction": _redaction_payload(counts),
        "missing": dict(sorted(missing.items())),
        "truncated": dict(sorted(truncated.items())),
        "bundle_cap_bytes": MAX_BUNDLE_COMPRESSED_BYTES,
        "reader_contract": "v1 readers must ignore members they do not know (forward compatible)",
    }
    return _json_text(payload)


def _manifest_text(
    entries: Sequence[_InventoryEntry],
    *,
    generated_by: str,
    install_shape: str,
    generated_at: str,
    counts: RedactionCounts,
    missing: Mapping[str, str],
    truncated: Mapping[str, str],
) -> str:
    lines = [
        "modulo support bundle",
        f"format: {FORMAT_ID} (bundle_format_version {BUNDLE_FORMAT_VERSION})",
        f"generated: {generated_at}",
        f"generated by: {generated_by}",
        f"install shape: {install_shape}",
        f"bundle cap: {MAX_BUNDLE_COMPRESSED_BYTES} bytes compressed",
        "",
        "Members (fixed v1 allowlist — a new member requires a golden-list update):",
    ]
    width = max(len(entry.name) for entry in entries)
    lines.extend(f"  {entry.name.ljust(width)}  {entry.bytes:>9} bytes  {entry.description}" for entry in entries)

    lines.extend(
        [
            "",
            "Redaction: allowlist by construction — seeded exact values, structural key/value lines,",
            "then the shared secret-value patterns, then a fail-closed post-assembly gate.",
            f"  marker: {REDACTED} (secret-value patterns: {SENSITIVE_VALUE_MASK})",
            (
                "  substitutions: "
                f"seed-exact={counts.seed_exact} structural={counts.structural} "
                f"secret-pattern={counts.patterns} (total {counts.total})"
            ),
            f"  layers applied: {', '.join(counts.applied_layers()) or 'none'}",
        ]
    )

    lines.append("")
    if missing:
        lines.append(f"missing: [{', '.join(sorted(missing))}]")
        lines.extend(f"  {name}: {missing[name]}" for name in sorted(missing))
    else:
        lines.append("missing: []")

    if truncated:
        lines.append(f"truncated: [{', '.join(sorted(truncated))}]")
        lines.extend(f"  {name}: {truncated[name]}" for name in sorted(truncated))
    else:
        lines.append("truncated: []")
    return "\n".join(lines) + "\n"


def _json_text(payload: object) -> str:
    return json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n"


# ---------------------------------------------------------------------------
# Reader (never prints an unredacted member)
# ---------------------------------------------------------------------------


def list_bundle(bundle_path: Path) -> list[MemberEntry]:
    """List members without validating them (unknown members are tolerated)."""
    with zipfile.ZipFile(bundle_path) as archive:
        return [
            MemberEntry(name=info.filename, bytes=info.file_size, known=_MEMBER_ALLOWED(info.filename))
            for info in archive.infolist()
        ]


def show_member(bundle_path: Path, member: str) -> str:
    """Return one member's stored text, refusing secret-shaped content.

    The bundle is already redacted; re-running the content gate on read means a
    tampered/hand-edited bundle cannot make this tool print a token.
    """
    with zipfile.ZipFile(bundle_path) as archive:
        names = set(archive.namelist())
        if member not in names:
            raise BundleBuildError(f"member {member!r} is not in {bundle_path}")
        text = archive.read(member).decode("utf-8", errors="replace")
    assert_redaction_proven({member: text}, {}, member_allowed=lambda _name: True)
    return text


# ---------------------------------------------------------------------------
# CLI (``python -m modulo.support.bundle``)
# ---------------------------------------------------------------------------


def _emit_json(payload: object, *, stream: TextIO) -> None:
    stream.write(json.dumps(payload, sort_keys=True, default=str) + "\n")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m modulo.support.bundle",
        description="Build or inspect a versioned, redacted modulo support bundle (v1).",
    )
    parser.add_argument("bundle", nargs="?", type=Path, help="existing bundle to read (--list / --show)")
    parser.add_argument("--out", type=Path, help="write a new bundle to this path")
    parser.add_argument("--json", dest="as_json", action="store_true", help="emit one JSON object")
    parser.add_argument("--list", dest="list_members", action="store_true", help="list members and sizes")
    parser.add_argument("--show", metavar="MEMBER", help="print one redacted member")
    parser.add_argument("--data-dir", type=Path, help="native launcher data dir (optional)")
    parser.add_argument("--doctor-output", type=Path, help="file containing the native doctor output (optional)")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    read_mode = args.list_members or args.show is not None
    if args.out is not None and read_mode:
        parser.error("--out cannot be combined with --list / --show")
    if args.out is None and not read_mode:
        parser.error("one of --out, --list or --show is required")
    if read_mode and args.bundle is None:
        parser.error("--list / --show require the bundle path")

    if read_mode:
        return _read_command(args)
    return _build_command(args)


def _read_command(args: argparse.Namespace) -> int:
    bundle_path: Path = args.bundle
    try:
        if args.list_members:
            entries = list_bundle(bundle_path)
            if args.as_json:
                _emit_json(
                    [{"name": entry.name, "bytes": entry.bytes, "known": entry.known} for entry in entries],
                    stream=sys.stdout,
                )
            else:
                for entry in entries:
                    suffix = "" if entry.known else "  (unknown member — ignored by v1 readers)"
                    sys.stdout.write(f"{entry.name}\t{entry.bytes} bytes{suffix}\n")
            return 0
        text = show_member(bundle_path, args.show)
        sys.stdout.write(text if text.endswith("\n") else f"{text}\n")
        return 0
    except (BundleBuildError, RedactionSelfCheckError, OSError, zipfile.BadZipFile) as exc:
        if args.as_json:
            _emit_json({"ok": False, "error": type(exc).__name__, "detail": str(exc)}, stream=sys.stderr)
        else:
            sys.stderr.write(f"{exc}\n")
        return 1


def _build_command(args: argparse.Namespace) -> int:
    doctor_output: str | None = None
    if args.doctor_output is not None:
        doctor_output = args.doctor_output.read_text(encoding="utf-8", errors="replace")
    install_shape = "native" if args.data_dir is not None else "container"
    try:
        sources = collect_sources(
            install_shape=install_shape,
            data_dir=args.data_dir,
            doctor_output=doctor_output,
        )
        result = build_bundle(args.out, sources=sources)
    except (BundleBuildError, RedactionSelfCheckError, OSError) as exc:
        member = getattr(exc, "member", None)
        if args.as_json:
            _emit_json(
                {"ok": False, "error": type(exc).__name__, "member": member, "detail": str(exc)},
                stream=sys.stderr,
            )
        else:
            sys.stderr.write(f"{exc}\n")
        return 1

    if args.as_json:
        _emit_json(
            {
                "ok": True,
                "format": FORMAT_ID,
                "bundle_format_version": BUNDLE_FORMAT_VERSION,
                "path": str(result.path),
                "ref_line": result.ref_line,
                "compressed_bytes": result.compressed_bytes,
                "members": list(result.members),
                "missing": result.missing,
            },
            stream=sys.stdout,
        )
    else:
        sys.stdout.write(f"{result.ref_line}\n")
        if result.missing:
            sys.stdout.write(f"missing: [{', '.join(sorted(result.missing))}]\n")
    return 0


if __name__ == "__main__":  # pragma: no cover - module entry point
    raise SystemExit(main())
