"""``modulo evidence`` — offline verification of evidence packs (FAR-1697).

Two commands:

* ``modulo evidence verify <pack.json> --public-key <key.pem>`` — verify a pack
  using ONLY the artefact and a pre-published Ed25519 public key: re-verifies
  the signature (authorship), re-derives every file digest, and re-verifies the
  embedded audit chain. Exits 0 when the pack is valid, 1 otherwise.
* ``modulo evidence public-key`` — print the instance's verification key (the
  same key published by the admin verification-key endpoint and embedded in
  every exported pack).

IMPORT HYGIENE: module scope is click + stdlib only; the core evidence module
is imported lazily inside the commands, so ``modulo evidence verify`` never
needs a database, Redis or a Settings instance — it is genuinely offline.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

import click

if TYPE_CHECKING:
    from modulo.core.evidence_pack import OfflineVerificationResult


def _format_result(result: OfflineVerificationResult) -> str:
    """Render an offline verification result for a human (non-JSON output)."""
    status = {True: "ok", False: "FAILED"}
    lines = [
        "VALID" if result.valid else "INVALID",
        f"  authorship:  {status[result.authorship_ok]}",
        f"  files:       {status[result.files_ok]}",
        f"  audit chain: {status[result.chain_ok]}",
        f"  manifest:    {status[result.manifest_ok]}",
    ]
    lines.extend(f"  error: {message}" for message in result.errors)
    return "\n".join(lines)


@click.group("evidence")
def evidence() -> None:
    """Verify offline-verifiable evidence packs and publish the verification key."""


@evidence.command("verify")
@click.argument("pack", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option(
    "--public-key",
    "public_key_path",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    required=True,
    help="PEM file with the pre-published Ed25519 public key.",
)
@click.option("--json", "as_json", is_flag=True, default=False, help="Emit the full result as JSON.")
@click.pass_context
def verify(ctx: click.Context, pack: Path, public_key_path: Path, as_json: bool) -> None:
    """Verify PACK against the pre-published public key (artefact + key only)."""
    from modulo.core.evidence_pack import verify_evidence_pack

    try:
        document = json.loads(pack.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise click.ClickException(f"could not read the evidence pack {pack}: {exc}") from exc
    if not isinstance(document, dict):
        raise click.ClickException("the evidence pack root must be a JSON object")
    try:
        public_key_pem = public_key_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise click.ClickException(f"could not read the public key {public_key_path}: {exc}") from exc

    result = verify_evidence_pack(document, public_key_pem=public_key_pem)
    if as_json:
        click.echo(json.dumps(result.to_dict(), indent=2, sort_keys=True))
    else:
        click.echo(_format_result(result))
    ctx.exit(0 if result.valid else 1)


@evidence.command("public-key")
@click.option("--json", "as_json", is_flag=True, default=False, help="Emit as JSON.")
def public_key(as_json: bool) -> None:
    """Print the instance's Ed25519 evidence verification key (PEM)."""
    from modulo.core.evidence_pack import (
        EvidenceSigningError,
        configured_public_key_pem,
        key_fingerprint,
    )

    try:
        public_key_pem = configured_public_key_pem()
    except EvidenceSigningError as exc:
        raise click.ClickException(str(exc)) from exc
    if as_json:
        click.echo(
            json.dumps(
                {"algorithm": "ed25519", "public_key_pem": public_key_pem, "key_id": key_fingerprint(public_key_pem)},
                sort_keys=True,
            )
        )
        return
    click.echo(public_key_pem, nl=False)
