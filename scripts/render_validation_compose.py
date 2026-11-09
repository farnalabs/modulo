#!/usr/bin/env python3
"""Render the phase-selected image in a release validation compose file."""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path


def _print_stdout(msg: str) -> None:
    sys.stdout.write(msg + "\n")


_IMAGE_LINE = re.compile(r"(?m)^(\s*image:\s*ghcr\.io/farnalabs/modulo:)[^\r\n]+$")
_REPLACEMENT = "${TAG:?TAG is required - set the image tag for this matrix phase}"


def render_matrix_compose(compose_file: Path, tag: str | None = None) -> None:
    """Replace the packaged immutable image tag with a phase image selector."""
    # ``compose_file`` is a CLI argument fixed by the calling workflow
    # (.github/workflows/rc-validate.yml renders
    # ``${{ runner.temp }}/compose-matrix/docker-compose.prod.yml``); it is never
    # user- or model-supplied, so this S8707 taint flow is a false positive.
    text = compose_file.read_text(encoding="utf-8")  # NOSONAR pythonsecurity:S8707
    replacement = tag if tag is not None else _REPLACEMENT
    rendered, count = _IMAGE_LINE.subn(lambda match: match.group(1) + replacement, text, count=0)
    if count != 1:
        raise ValueError(f"expected exactly one modulo image to render, found {count}")
    compose_file.write_text(rendered, encoding="utf-8")  # NOSONAR pythonsecurity:S8707
    _print_stdout(f"Rendered matrix compose image selector in {compose_file}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--compose-file", type=Path, required=True)
    parser.add_argument("--tag", help="Render a concrete image tag instead of the compose variable")
    args = parser.parse_args()
    render_matrix_compose(args.compose_file, args.tag)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
