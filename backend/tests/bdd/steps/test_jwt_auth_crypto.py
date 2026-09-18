"""BDD step definitions: JWT token lifecycle and purpose isolation (feat-auth-jwt-auth).

Drives the REAL ``modulo.auth.jwt`` mint/decode seams — access / WebSocket /
refresh / claim-token minting, ``decode_principal`` signature/expiry/purpose
validation, ``refresh_access_token`` rotation propagation and ``decode_claim_token``
HITL-gate scoping — with no DB and no network (pure crypto), so the product-map
claim that tokens are purpose-isolated, forgery-resistant and tenant-bound is
pinned by executing BDD rather than by an endpoint-mocked assertion. The API-level
flows (login/refresh endpoints, token-family theft detection) stay covered by the
separate ``jwt_security.feature``; this file locks the token-utility contract.
"""

from __future__ import annotations

import base64
import json
import time
from typing import Any

import jwt as pyjwt
from jwt import InvalidTokenError as JWTError
from pytest_bdd import given, parsers, scenarios, then, when

from modulo.auth import jwt as jwt_mod

scenarios("../features/auth/jwt_auth_crypto.feature")

_DEFAULT_SECRET = "a_sufficiently_long_secret_key_32b"
_WRONG_SECRET = "a_different_sufficiently_long_secret_key_32b"
_MISSING_IDENTITY_EXPIRY_SECONDS = 3600


def _state(request) -> dict[str, Any]:
    """Scenario-scoped shared state stored on the request node."""
    state = getattr(request.node, "_jwt_auth_state", None)
    if state is None:
        state = {
            "secret": _DEFAULT_SECRET,
            "org": "00000000-0000-0000-0000-000000000001",
            "account": "11111111-1111-1111-1111-111111111111",
            "token": None,
            "decoded": None,
            "rotated": None,
            "jwt_error": None,
            "claim_run": None,
            "claim_gate": None,
            "claim_payload": None,
        }
        request.node._jwt_auth_state = state
    return state


def _capture(request, call) -> None:
    """Run ``call`` recording the decoded value (or the raised JWT error)."""
    state = _state(request)
    try:
        state["decoded"] = call()
        state["jwt_error"] = None
    except JWTError as exc:
        state["decoded"] = None
        state["jwt_error"] = exc


# -- Given: context ------------------------------------------------------------


@given(parsers.parse('the signing secret is "{secret}"'))
def step_secret(secret: str, request) -> None:
    _state(request)["secret"] = secret


@given(parsers.parse('the tenant org is "{org}"'))
def step_tenant_org(org: str, request) -> None:
    _state(request)["org"] = org


@given(parsers.parse('the user account is "{account}"'))
def step_user_account(account: str, request) -> None:
    _state(request)["account"] = account


# -- When / Given: minting a token ---------------------------------------------


@when(parsers.parse('an access token is minted for user "{name}" with role "{role}" and client kind "{kind}"'))
def step_mint_access(name: str, role: str, kind: str, request) -> None:
    state = _state(request)
    state["token"] = jwt_mod.create_access_token(
        name,
        state["secret"],
        organisation_id=state["org"],
        account_id=state["account"],
        org_role=role,
        client_kind=kind,
    )


@when(parsers.parse('an expired access token is minted for user "{name}" with role "{role}"'))
def step_mint_expired(name: str, role: str, request) -> None:
    state = _state(request)
    state["token"] = jwt_mod.create_access_token(
        name,
        state["secret"],
        organisation_id=state["org"],
        account_id=state["account"],
        org_role=role,
        ttl_minutes=-30,
        client_kind="browser",
    )


@when(parsers.parse('a token is forged with algorithm "none" for user "{name}" with role "{role}"'))
def step_forge_none(name: str, role: str, request) -> None:
    """Hand-craft an unsigned ``alg: none`` JWT, bypassing PyJWT encode-time checks."""
    state = _state(request)
    now = int(time.time())
    header = base64.urlsafe_b64encode(json.dumps({"alg": "none", "typ": "JWT"}).encode()).rstrip(b"=").decode()
    payload = {
        "sub": name,
        "org_id": state["org"],
        "account_id": state["account"],
        "org_role": role,
        "iat": now - 300,
        "exp": now + _MISSING_IDENTITY_EXPIRY_SECONDS,
    }
    payload_b64 = base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=").decode()
    state["token"] = f"{header}.{payload_b64}."


@when("a token is minted with no subject and no account identity")
def step_mint_no_identity(request) -> None:
    """A signed token that declares a tenant but no subject / account identity."""
    state = _state(request)
    now = int(time.time())
    claims = {
        "org_id": state["org"],
        "org_role": "admin",
        "iat": now - 300,
        "exp": now + _MISSING_IDENTITY_EXPIRY_SECONDS,
    }
    state["token"] = str(pyjwt.encode(claims, state["secret"], algorithm="HS256"))


@when(parsers.parse('a websocket token is minted for user "{name}" with role "{role}"'))
def step_mint_ws(name: str, role: str, request) -> None:
    state = _state(request)
    state["token"] = jwt_mod.create_ws_token(
        name,
        state["secret"],
        organisation_id=state["org"],
        account_id=state["account"],
        org_role=role,
    )


@when(
    parsers.parse(
        'a refresh token is minted for user "{name}" with role "{role}" and client kind "{kind}" '
        'in family "{family}" at sequence {seq:d}'
    )
)
def step_mint_refresh(name: str, role: str, kind: str, family: str, seq: int, request) -> None:
    state = _state(request)
    state["token"] = jwt_mod.create_refresh_token(
        name,
        state["secret"],
        organisation_id=state["org"],
        account_id=state["account"],
        org_role=role,
        token_family=family,
        token_sequence=seq,
        client_kind=kind,
    )


@given(parsers.parse('a "{kind}" token is minted for user "{name}"'))
def step_mint_kind(kind: str, name: str, request) -> None:
    if kind == "access":
        step_mint_access(name, "admin", "browser", request)
    elif kind == "websocket":
        step_mint_ws(name, "admin", request)
    elif kind == "refresh":
        step_mint_refresh(name, "admin", "browser", "family-a", 1, request)
    else:
        raise AssertionError(f"unknown token kind: {kind!r}")


@when(parsers.parse('a legacy token is minted for user "{name}" with role "{role}"'))
def step_mint_legacy(name: str, role: str, request) -> None:
    """A token minted BEFORE the ``client_kind`` claim existed (no such claim)."""
    state = _state(request)
    now = int(time.time())
    claims = {
        "sub": name,
        "org_id": state["org"],
        "account_id": state["account"],
        "org_role": role,
        "iat": now - 300,
        "exp": now + _MISSING_IDENTITY_EXPIRY_SECONDS,
    }
    state["token"] = str(pyjwt.encode(claims, state["secret"], algorithm="HS256"))


@when(parsers.parse('a claim token is minted for user "{name}" for run "{run_id}" and gate "{gate_id}"'))
def step_mint_claim(name: str, run_id: str, gate_id: str, request) -> None:
    state = _state(request)
    state["token"] = jwt_mod.create_claim_token(
        name,
        state["secret"],
        run_id=run_id,
        gate_id=gate_id,
        client_id=state["account"],
    )
    state["claim_run"] = run_id
    state["claim_gate"] = gate_id


# -- When: decoding / rotating --------------------------------------------------


@when("I decode the token as a principal")
def step_decode_principal(request) -> None:
    state = _state(request)
    _capture(request, lambda: jwt_mod.decode_principal(state["token"], state["secret"]))


@when(parsers.parse('I decode the token as a principal for the "{purpose}" purpose'))
def step_decode_principal_purpose(purpose: str, request) -> None:
    state = _state(request)
    _capture(request, lambda: jwt_mod.decode_principal(state["token"], state["secret"], allowed_purposes=[purpose]))


@when("the token is decoded with a different secret")
def step_decode_wrong_secret(request) -> None:
    state = _state(request)
    _capture(request, lambda: jwt_mod.decode_principal(state["token"], _WRONG_SECRET))


@when("the token signature is tampered with")
def step_tamper_signature(request) -> None:
    state = _state(request)
    parts = state["token"].split(".")
    parts[2] = "tampered"
    state["token"] = ".".join(parts)


@when("I rotate the refresh token")
def step_rotate_refresh(request) -> None:
    state = _state(request)
    _capture(request, lambda: jwt_mod.refresh_access_token(state["token"], state["secret"]))
    if state["jwt_error"] is None and state["decoded"] is not None:
        state["rotated"] = state["decoded"]


@when("I decode the rotated token as a principal")
def step_decode_rotated(request) -> None:
    state = _state(request)
    _capture(request, lambda: jwt_mod.decode_principal(state["rotated"], state["secret"]))


@when("I rotate the token")
def step_rotate_token(request) -> None:
    """Rotation over a non-refresh token — the same seam as the refresh route."""
    state = _state(request)
    _capture(request, lambda: jwt_mod.refresh_access_token(state["token"], state["secret"]))


@when(parsers.parse('I decode the claim token against run "{run_id}" and gate "{gate_id}"'))
def step_decode_claim(run_id: str, gate_id: str, request) -> None:
    state = _state(request)
    _capture(
        request,
        lambda: jwt_mod.decode_claim_token(state["token"], state["secret"], run_id=run_id, gate_id=gate_id),
    )
    if state["jwt_error"] is None and isinstance(state["decoded"], dict):
        state["claim_payload"] = state["decoded"]


# -- Then: verdicts -------------------------------------------------------------


@then("decoding succeeds")
def step_decode_succeeds(request) -> None:
    state = _state(request)
    assert state["jwt_error"] is None, f"expected decoding to succeed, got: {state['jwt_error']!r}"
    assert state["decoded"] is not None, "no decoded value was produced"


@then("decoding is rejected")
def step_decode_rejected(request) -> None:
    assert _state(request)["jwt_error"] is not None, "expected decoding to be rejected but it succeeded"


@then(parsers.parse("decoding is {outcome}"))
def step_decode_outcome(outcome: str, request) -> None:
    if outcome == "accepted":
        step_decode_succeeds(request)
    elif outcome == "rejected":
        step_decode_rejected(request)
    else:
        raise AssertionError(f"unknown outcome: {outcome!r}")


@then(parsers.parse('the principal has username "{name}"'))
def step_principal_username(name: str, request) -> None:
    principal = _state(request)["decoded"]
    assert principal is not None, "no principal was decoded"
    assert principal.username == name, f"expected username {name!r}, got {principal.username!r}"


@then(parsers.parse('the principal has role "{role}"'))
def step_principal_role(role: str, request) -> None:
    principal = _state(request)["decoded"]
    assert principal is not None, "no principal was decoded"
    assert principal.org_role == role, f"expected role {role!r}, got {principal.org_role!r}"


@then("the principal belongs to the minted org")
def step_principal_org(request) -> None:
    state = _state(request)
    principal = state["decoded"]
    assert principal is not None, "no principal was decoded"
    assert str(principal.organisation_id) == state["org"], (
        f"expected org {state['org']!r}, got {principal.organisation_id!r}"
    )


@then(parsers.parse('the principal has client kind "{kind}"'))
def step_principal_client_kind(kind: str, request) -> None:
    principal = _state(request)["decoded"]
    assert principal is not None, "no principal was decoded"
    assert principal.client_kind == kind, f"expected client kind {kind!r}, got {principal.client_kind!r}"


@then("rotation is rejected")
def step_rotation_rejected(request) -> None:
    assert _state(request)["jwt_error"] is not None, "expected rotation to be rejected but it succeeded"


@then("the claim token is accepted")
def step_claim_accepted(request) -> None:
    state = _state(request)
    assert state["jwt_error"] is None, f"expected the claim token to be accepted, got: {state['jwt_error']!r}"
    assert state["claim_payload"] is not None, "no claim payload was decoded"


@then(parsers.parse('the claim token carries run "{run_id}" and gate "{gate_id}"'))
def step_claim_carries(run_id: str, gate_id: str, request) -> None:
    payload = _state(request)["claim_payload"]
    assert payload is not None, "no claim payload was decoded"
    assert payload.get("run_id") == run_id, f"expected run {run_id!r}, got {payload.get('run_id')!r}"
    assert payload.get("gate_id") == gate_id, f"expected gate {gate_id!r}, got {payload.get('gate_id')!r}"
    assert payload.get("purpose") == "claim_token"


@then(parsers.parse('decoding the claim token against the wrong gate "{gate_id}" is rejected'))
def step_claim_wrong_gate(gate_id: str, request) -> None:
    state = _state(request)
    try:
        jwt_mod.decode_claim_token(state["token"], state["secret"], run_id=state["claim_run"], gate_id=gate_id)
    except JWTError:
        return
    raise AssertionError("expected a gate-id mismatch to be rejected but the claim token decoded")
