#!/usr/bin/env python3
"""Validate published bundle releases and resolve their semantic predecessor.

The native release smoke workflow uses this helper for every release-consuming
path.  GitHub API JSON is deliberately passed in from the workflow so the
predicate and history selection are executable and unit-testable in one place.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

_TAG_RE = re.compile(
    r"bundle-v(?P<major>0|[1-9]\d*)\.(?P<minor>0|[1-9]\d*)\.(?P<patch>0|[1-9]\d*)",
    re.ASCII,
)


class ReleaseValidationError(ValueError):
    """The supplied release JSON or tag is not a published bundle release."""


def _version(tag: str) -> tuple[int, int, int]:
    match = _TAG_RE.fullmatch(tag)
    if match is None:
        raise ReleaseValidationError(f"tag must match bundle-vX.Y.Z exactly: {tag}")
    return tuple(int(match.group(part)) for part in ("major", "minor", "patch"))


def _load_json(path: Path) -> Any:
    # ``path`` is a CLI argument supplied by the calling workflow
    # (.github/workflows/native-release-smoke.yml writes the ``gh api``
    # response to fixed paths such as release.json / release-pages.json); it is
    # never user- or model-supplied, so this S8707 taint flow is a false
    # positive.
    try:
        return json.loads(path.read_text(encoding="utf-8"))  # NOSONAR pythonsecurity:S8707
    except (OSError, json.JSONDecodeError) as exc:
        raise ReleaseValidationError(f"cannot read GitHub API JSON from {path}: {exc}") from exc


def validate_published_release(tag: str, release: Any) -> str:
    """Return *tag* only when it is an exact, existing published release."""
    _version(tag)
    if not isinstance(release, dict):
        raise ReleaseValidationError(f"release response for {tag} is not an object")
    if release.get("tag_name") != tag:
        raise ReleaseValidationError(f"release response does not identify requested tag: {tag}")
    if release.get("draft") is not False or release.get("prerelease") is not False:
        raise ReleaseValidationError(f"release is not published: {tag}")
    return tag


def _history_pages(payload: Any) -> list[list[dict[str, Any]]]:
    if not isinstance(payload, list):
        raise ReleaseValidationError("release history response is not a list of pages")
    pages: list[list[dict[str, Any]]] = []
    for page in payload:
        if not isinstance(page, list) or not all(isinstance(item, dict) for item in page):
            raise ReleaseValidationError("release history contains an invalid page")
        pages.append(page)
    return pages


def published_tags(release_pages: Any) -> list[str]:
    """Return all published bundle tags, validating every bundle history entry."""
    pages = _history_pages(release_pages)
    candidates: dict[str, tuple[int, int, int]] = {}
    for page in pages:
        for release in page:
            tag = release.get("tag_name")
            if not isinstance(tag, str) or not tag.startswith("bundle-v"):
                continue
            version = _version(tag)
            if release.get("draft") is not False or release.get("prerelease") is not False:
                continue
            candidates[tag] = version
    return sorted(candidates, key=lambda tag: candidates[tag])


def resolve_n_minus_1(release_pages: Any, current: str) -> str | None:
    """Resolve the published semantic predecessor of *current* across all pages."""
    _version(current)
    ordered = published_tags(release_pages)
    if current not in ordered:
        raise ReleaseValidationError(f"current release is not published: {current}")
    index = ordered.index(current)
    return ordered[index - 1] if index else None


_DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}")


def _attested_digests(payload: Any) -> set[str]:
    """Collect sha256 subject digests from a GitHub attestation response."""
    digests: set[str] = set()
    if isinstance(payload, dict):
        for key, value in payload.items():
            if key == "sha256" and isinstance(value, str) and _DIGEST_RE.fullmatch(f"sha256:{value}"):
                digests.add(f"sha256:{value}")
            else:
                digests.update(_attested_digests(value))
    elif isinstance(payload, list):
        for item in payload:
            digests.update(_attested_digests(item))
    return digests


def verify_image_digest(image: str, resolved_digest: str, attestation: Any) -> str:
    """Return the digest only when it is covered by published provenance."""
    if not image.startswith("ghcr.io/"):
        raise ReleaseValidationError(f"image must be a GHCR reference: {image}")
    if _DIGEST_RE.fullmatch(resolved_digest) is None:
        raise ReleaseValidationError(f"resolved image digest is invalid: {resolved_digest}")
    attested = _attested_digests(attestation)
    if resolved_digest not in attested:
        raise ReleaseValidationError(
            f"published provenance for {image} does not contain resolved digest {resolved_digest}"
        )
    return resolved_digest


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    validate = subparsers.add_parser("validate", help="validate one release API response")
    validate.add_argument("--tag", required=True)
    validate.add_argument("--release-json", type=Path, required=True)

    published = subparsers.add_parser("published-tags", help="list published bundle tags")
    published.add_argument("--release-json", type=Path, required=True)

    predecessor = subparsers.add_parser("resolve-n-minus-1", help="resolve a release predecessor")
    predecessor.add_argument("--current", required=True)
    predecessor.add_argument("--release-json", type=Path, required=True)

    image = subparsers.add_parser("verify-image-digest", help="verify an image digest against provenance")
    image.add_argument("--image", required=True)
    image.add_argument("--digest", required=True)
    image.add_argument("--attestation-json", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "verify-image-digest":
            payload = _load_json(args.attestation_json)
            print(verify_image_digest(args.image, args.digest, payload))
        else:
            payload = _load_json(args.release_json)
            if args.command == "validate":
                print(validate_published_release(args.tag, payload))
            elif args.command == "published-tags":
                print("\n".join(published_tags(payload)))
            else:
                predecessor = resolve_n_minus_1(payload, args.current)
                if predecessor is not None:
                    print(predecessor)
        return 0
    except ReleaseValidationError as exc:
        print(f"release validation failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
