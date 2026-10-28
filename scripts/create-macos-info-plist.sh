#!/bin/sh
# Validate the inputs for the future native macOS bundle metadata generator.
# The native macOS runtime is not available yet, so this helper intentionally
# does not manufacture an executable or an installer artifact.

set -eu

if [ "$#" -ne 2 ]; then
    printf 'usage: %s <version> <output-path>\n' "$0" >&2
    exit 2
fi

VERSION=$1
OUTPUT_PATH=$2

case "$VERSION" in
    ''|*[!0-9.]*|*.*.*.*)
        printf 'invalid macOS bundle version: %s\n' "$VERSION" >&2
        exit 1
        ;;
esac

case "$OUTPUT_PATH" in
    ''|*..*|/*)
        printf 'unsafe macOS metadata output path: %s\n' "$OUTPUT_PATH" >&2
        exit 1
        ;;
esac

printf 'macOS metadata contract validated for version %s (native bundle unavailable; no file written)\n' "$VERSION"
