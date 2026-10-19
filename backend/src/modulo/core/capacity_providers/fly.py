"""Fly.io volume capacity probe (FAR-1992).

A concrete :class:`modulo.db.capacity.PlatformCapacityProbe` implementation for
Fly.io deployments where the API/MCP app and the Postgres machine are SEPARATE
Fly apps, so the app cannot ``statvfs`` the database volume (the reason
FAR-1882's filesystem probe cannot auto-detect capacity there). The probe reads
the database app's volumes from the Fly Machines API and returns the volume's
configured capacity in bytes.

WHY A SECONDARY SIGNAL STILL NEEDS THE DATABASE SIZE
---------------------------------------------------
The Fly volumes listing exposes a volume's *capacity* (``size_gb``) but not its
consumption. The capacity numerator is therefore the platform value, and the
usage numerator stays ``pg_database_size(current_database())`` - the same
honest pairing the ``env`` source uses. It is a LATE signal for the volume's
true fill level (WAL, temp files and other databases on the volume are not
counted), which is documented in ``docs/configuration-reference.md``; the
filesystem source remains the accurate one wherever the volume IS mountable.

FAIL-OPEN
---------
Every failure - no credentials, app/volume ambiguous, HTTP error, malformed
payload - returns ``None`` (with a warning) so the DB layer's source chain falls
through to ``none`` and the 98% hard-stop stays INERT. The probe never raises and
never guesses a volume.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from modulo.core.ssrf import pinned_async_client

_log = logging.getLogger(__name__)

# The Fly Machines API origin (also the SSRF-pinned egress target). Stable
# public host; overridable for tests via the constructor.
FLY_API_BASE_URL = "https://api.machines.dev"

_BYTES_PER_GIB = 1024**3

# A factory that builds an httpx-compatible async client for ``url``. Defaults to
# the SSRF-pinned client (``modulo.core.ssrf``); injectable so tests can supply a
# fake without touching the network or the pinned transport.
ClientFactory = Callable[..., Awaitable[Any]]


def capacity_bytes_from_volumes(payload: Any, selector: str = "") -> int | None:
    """Pick the DB volume from a Fly ``/volumes`` payload and return its capacity.

    ``payload`` is the parsed JSON array from ``GET /v1/apps/{app}/volumes``.
    ``selector`` matches a volume's ``name`` or ``id``; empty selects the app's
    SINGLE volume.

    Returns ``None`` - never guesses - when the payload is not a list, no volume
    matches, the selection is ambiguous (multiple volumes with no/duplicate
    selector), or ``size_gb`` is missing/non-positive/non-numeric.
    """
    if not isinstance(payload, list):
        return None
    volumes = [entry for entry in payload if isinstance(entry, dict)]
    if selector:
        volumes = [entry for entry in volumes if entry.get("name") == selector or entry.get("id") == selector]
    if len(volumes) != 1:
        # 0 matches, or >1 matches (ambiguous selection): resolve nothing.
        return None
    size_gb = volumes[0].get("size_gb")
    if isinstance(size_gb, bool) or not isinstance(size_gb, (int, float)):
        return None
    if size_gb <= 0:
        return None
    return int(size_gb) * _BYTES_PER_GIB


class FlyVolumeCapacityProbe:
    """Platform capacity probe backed by the Fly Machines API volumes endpoint.

    Implements the structural :class:`modulo.db.capacity.PlatformCapacityProbe`
    port (``name`` + awaitable ``probe_capacity_bytes``). It is deliberately NOT a
    subclass of the port - the DB layer is kept free of any import of this module,
    so the ``db → core`` import-linter contract stays intact.
    """

    name = "fly"

    def __init__(
        self,
        *,
        app: str,
        token: str,
        volume: str = "",
        api_base_url: str = FLY_API_BASE_URL,
        timeout_seconds: float = 10.0,
        client_factory: ClientFactory | None = None,
    ) -> None:
        self._app = app
        self._token = token
        self._volume = volume
        self._api_base_url = api_base_url.rstrip("/")
        self._timeout_seconds = timeout_seconds
        self._client_factory: ClientFactory = client_factory or pinned_async_client

    def _volumes_url(self) -> str:
        return f"{self._api_base_url}/v1/apps/{self._app}/volumes"

    async def _request_volumes(self, url: str) -> Any | None:
        """GET ``url`` and return the raw response, or ``None`` on any failure.

        Narrowly scoped to the request itself (client construction, connect and
        status); decoding is done by the caller so a JSON error is handled as a
        JSON error, never swallowed by a blanket ``except`` around the
        ``.json()`` call.
        """
        try:
            client = await self._client_factory(
                url,
                headers={"Authorization": f"Bearer {self._token}", "Accept": "application/json"},
                timeout=self._timeout_seconds,
            )
            async with client:
                response = await client.get(url)
                response.raise_for_status()
        except Exception:
            _log.warning(
                "capacity.platform.fly_probe_failed",
                extra={"app": self._app, "url": url},
                exc_info=True,
            )
            return None
        return response

    async def probe_capacity_bytes(self) -> int | None:
        """Return the DB volume capacity in bytes, or ``None`` (fail-open).

        Any failure - connect error, non-2xx, malformed body - is logged and
        resolves to ``None`` so the caller's source chain falls through.
        """
        url = self._volumes_url()
        response = await self._request_volumes(url)
        if response is None:
            return None
        try:
            payload = response.json()
        except ValueError:
            _log.warning(
                "capacity.platform.fly_response_not_json",
                extra={"app": self._app, "url": url},
                exc_info=True,
            )
            return None

        capacity = capacity_bytes_from_volumes(payload, self._volume)
        if capacity is None:
            _log.warning(
                "capacity.platform.fly_volume_unresolved",
                extra={"app": self._app, "volume": self._volume or "<single-volume>"},
            )
        return capacity
