"""Product analytics — opt-in aggregate usage & error metrics.

Daily cron builds a cross-org aggregate payload and POSTs it to the
vendor endpoint.  Consent gating, watermark tracking, and HMAC
signing are handled here; the vendor service (FAR-352) is external.
"""

from __future__ import annotations

__all__: list[str] = []
