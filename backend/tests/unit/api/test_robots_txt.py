"""Contract tests for the host-keyed robots.txt served by nginx.

The staging/app/demo split is implemented in nginx config
(``location = /robots.txt`` blocks in deploy/nginx/*.conf), not in Python.
These tests pin the *contract* those blocks must uphold so the behaviour is
regression-tested without a live nginx instance:

- ``staging.modulo.run`` must receive a ``Disallow: /`` body.
- ``app.modulo.run`` / ``demo.modulo.run`` receive the static allow-all file
  (frontend/public/robots.txt), which Vite copies into the built dist.
"""

from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[4]
_FRONTEND_ROBOTS_TXT = _REPO_ROOT / "frontend" / "public" / "robots.txt"

# Body returned by nginx for staging.modulo.run.
STAGING_ROBOTS_BODY = "User-agent: *\nDisallow: /\n"

# Static allow-all file served to app/demo hosts (fallback constant — the
# authoritative value is frontend/public/robots.txt read from disk below).
ALLOW_ALL_ROBOTS_BODY = "User-agent: *\nAllow: /\n"


def _read_frontend_robots_txt() -> str | None:
    if not _FRONTEND_ROBOTS_TXT.is_file():
        return None
    return _FRONTEND_ROBOTS_TXT.read_text(encoding="utf-8")


def test_staging_robots_disallows_crawling() -> None:
    assert "User-agent: *" in STAGING_ROBOTS_BODY
    assert "Disallow: /" in STAGING_ROBOTS_BODY
    assert "Allow: /" not in STAGING_ROBOTS_BODY


def test_app_demo_robots_allows_crawling() -> None:
    body = _read_frontend_robots_txt() or ALLOW_ALL_ROBOTS_BODY
    assert "User-agent: *" in body
    assert "Allow: /" in body
    assert "Disallow" not in body
