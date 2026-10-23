"""Shared custom-endpoint (``base_url``) rules for model backends.

FAR-2046 moved a model backend's operator-supplied endpoint out of the
``default_params.base_url`` model-parameter bag and into the encrypted credential
JSON, behind a strict validation floor (scheme, userinfo/query/fragment, cloud
metadata deny, non-canonical IP literals, explicit ``allow_insecure_http`` opt-in)
and a provider capability map.

The REST write path (``api/routes/model_backends.py``) and the one-shot legacy
backfill CLI (``cli/backfill_model_backend_base_url.py``) must apply *the same*
rules. Keeping a private copy in either place would drift silently — the CLI
could bless a value the API rejects, or reject one it accepts. This module is the
single implementation; the route translates :class:`BaseUrlValidationError` into
its typed 422, and the CLI classifies it.

FAR-2067.
"""

from __future__ import annotations

import ipaddress
import logging
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlparse

from modulo.core.ssrf import validate_outbound_url_syntax
from modulo.util import sanitise_log_value

logger = logging.getLogger(__name__)

BASE_URL_MAX_LENGTH = 2048

# Cloud metadata endpoints are a HARD DENY — never reachable by the model
# backend egress path regardless of scheme, hostname or allowlist. 169.254.0.0/16
# is IPv4 link-local (AWS/GCP/Azure IMDS); fe80::/10 is the IPv6 equivalent.
# fd00:ec2::254 is the AWS IMDS IPv6 address, 100.100.100.200 the Aliyun
# endpoint, and ::ffff:169.254.169.254 is the IPv4 address in mapped form (the
# IPv6 network below is checked via ``ipv4_mapped`` too).
METADATA_NETWORKS: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...] = (
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("fe80::/10"),
    ipaddress.ip_network("fd00:ec2::254/128"),
    ipaddress.ip_network("100.100.100.200/32"),
)
METADATA_HOSTNAMES = frozenset(
    {
        "metadata",
        "metadata.google.internal",
        "instance-data",
        "instance-data.ec2.internal",
    }
)

#: Raised by :func:`validate_base_url`. ``code`` carries the machine-readable
#: reason the REST layer publishes in its 422 (``insecure_http_not_allowed`` is
#: the one the backfill treats as "strip, do not bless").
INSECURE_HTTP_NOT_ALLOWED = "insecure_http_not_allowed"


class BaseUrlValidationError(ValueError):
    """A base URL violates the FAR-2046 custom-endpoint rules."""

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        self.code = code


def reject_metadata_endpoint(host: str) -> None:
    """Raise for a cloud metadata host/literal; never allowlisted."""
    normalized = host.strip("[]").rstrip(".").lower()
    if normalized in METADATA_HOSTNAMES:
        raise BaseUrlValidationError("Base URL must not target a cloud metadata endpoint")
    try:
        address: ipaddress.IPv4Address | ipaddress.IPv6Address = ipaddress.ip_address(normalized)
    except ValueError:
        return
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    if any(address in network for network in METADATA_NETWORKS if network.version == address.version):
        raise BaseUrlValidationError("Base URL must not target a cloud metadata endpoint")


def validate_base_url(value: str, *, allow_insecure_http: bool = False) -> str:
    """Validate and normalise an operator-supplied model-backend base URL.

    Rules (FAR-2046):
    * ``http``/``https`` only, no embedded userinfo (credentials belong in
      ``api_key``), no query and no fragment;
    * non-canonical IP-literal encodings (decimal/hex integers, mixed-base
      dotted strings) are rejected by the shared outbound-URL syntax floor;
    * cloud metadata endpoints are refused outright;
    * ``https`` is required unless the caller explicitly opts in with
      ``allow_insecure_http`` (a separate, audited flag — never inferred from
      the hostname shape, because a name's privacy cannot be established
      without resolving it).

    Returns the trimmed URL. Raises :class:`BaseUrlValidationError` for any
    violation.
    """
    candidate = value.strip()
    if not candidate:
        raise BaseUrlValidationError("Base URL must not be empty")
    if len(candidate) > BASE_URL_MAX_LENGTH:
        raise BaseUrlValidationError(f"Base URL must be at most {BASE_URL_MAX_LENGTH} characters")
    try:
        scheme, host = validate_outbound_url_syntax(candidate)
    except ValueError as exc:
        raise BaseUrlValidationError(str(exc)) from None
    parsed = urlparse(candidate)
    if parsed.query or parsed.fragment:
        raise BaseUrlValidationError("Base URL must not contain a query string or fragment")
    reject_metadata_endpoint(host)
    if scheme == "http":
        if not allow_insecure_http:
            raise BaseUrlValidationError(
                "Base URL must use https://; set allow_insecure_http to permit plain HTTP",
                code=INSECURE_HTTP_NOT_ALLOWED,
            )
        logger.warning(
            "model_backends.base_url_http_downgrade host=%s",
            sanitise_log_value(host),
        )
    return candidate


def stored_base_url(payload: Mapping[str, Any] | None) -> str | None:
    """Return the validated endpoint stored in a credential payload, if any."""
    if not isinstance(payload, Mapping):
        return None
    value = payload.get("base_url")
    return value if isinstance(value, str) and value.strip() else None


def legacy_base_url(default_params: Mapping[str, Any] | None) -> str | None:
    """Return a pre-FAR-2046 ``default_params.base_url``, or ``None``.

    The legacy location is still honoured for existing rows but is never
    written again: create/update migrate it into the credential JSON.
    """
    if not isinstance(default_params, Mapping):
        return None
    value = default_params.get("base_url")
    return value if isinstance(value, str) and value.strip() else None


def strip_legacy_base_url(default_params: Mapping[str, Any] | None) -> dict[str, Any]:
    """Return *default_params* without the legacy endpoint key."""
    if not isinstance(default_params, Mapping):
        return {}
    return {key: value for key, value in default_params.items() if key != "base_url"}


def redact_base_url(value: str | None) -> str | None:
    """Reduce a base URL to its host only, never leaking a path segment.

    A URL path can itself carry a credential, so reports and audit facts must
    use the host alone.
    """
    if not value:
        return None
    try:
        host = urlparse(value).hostname
    except ValueError:
        return None
    return host or None
