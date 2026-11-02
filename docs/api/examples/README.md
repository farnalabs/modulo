# Modulo API Client Examples

Standalone, copy-paste runnable code examples in **Python** (`httpx`), **curl**, and **JavaScript** (`fetch`) covering all common Modulo API operations, including the cookie-based login, refresh, and logout flow.

## Prerequisites

- A running Modulo instance (default: `http://localhost:8000`)
- An active user account (`MODULO_EMAIL` / `MODULO_PASSWORD`)
- Python 3 with `httpx` for the Python examples, Node.js for the JavaScript
  examples, and `bash` plus `jq` for the curl examples (the shell scripts use
  `jq` to parse responses)

## Authentication model

`POST /api/v1/auth/login` returns the access token in the JSON body
(`access_token`) and sets the refresh token as an httpOnly `modulo_refresh`
cookie. The refresh token never appears in a response body (FAR-1197).
`POST /api/v1/auth/refresh` and `POST /api/v1/auth/logout` read that cookie,
not a request body, and are additionally guarded by a double-submit CSRF
check: the `XSRF-TOKEN` cookie must be echoed in an `X-CSRF-Token` header,
or the request fails with 403 (`modulo_csrf_enabled`, default on). Every
authenticated call carries `Authorization: Bearer <access_token>`.

Login and refresh both set fresh `modulo_session`, `modulo_refresh`, and
`XSRF-TOKEN` cookies on the response, so a client with a cookie jar needs no
extra step beyond echoing the current `XSRF-TOKEN`. The Python examples use
the `httpx` cookie jar; the curl examples use `-c`/`-b` with a jar file; the
JavaScript examples use the `CookieJar` helper in
[`_shared/client.js`](./_shared/client.js) because Node's `fetch` does not
store cookies (a browser does this itself, sending `credentials: "include"`).
The login body also carries `requires_bootstrap` and `must_change_password`,
which a client should surface to the user.

## Quick Start

```bash
export MODULO_URL=http://localhost:8000
export MODULO_EMAIL=admin@example.com
export MODULO_PASSWORD=changeme

# Python examples
pip install httpx
python auth-login/python.py
python pipelines/python.py
python runs/python.py
python hitl/python.py
python library/python.py
python full-workflow.py

# JavaScript examples
node auth-login/js.js
node pipelines/js.js
node runs/js.js
node hitl/js.js
node library/js.js

# curl examples
bash auth-login/curl.sh
bash pipelines/curl.sh
bash runs/curl.sh
bash hitl/curl.sh
bash library/curl.sh
```

## Example Index

### [`auth-login/`](./auth-login/)

| File | Language | Description |
|---|---|---|
| [`python.py`](./auth-login/python.py) | Python (httpx) | Login with email/password, get JWT, refresh, logout |
| [`curl.sh`](./auth-login/curl.sh) | bash + curl | Login, token refresh, logout via curl |
| [`js.js`](./auth-login/js.js) | JavaScript (fetch) | Login, token refresh, logout |

### [`pipelines/`](./pipelines/)

| File | Language | Description |
|---|---|---|
| [`python.py`](./pipelines/python.py) | Python (httpx) | List, create, get, update, delete pipelines |
| [`curl.sh`](./pipelines/curl.sh) | bash + curl | Pipeline CRUD via curl |
| [`js.js`](./pipelines/js.js) | JavaScript (fetch) | Pipeline CRUD via fetch |

### [`runs/`](./runs/)

| File | Language | Description |
|---|---|---|
| [`python.py`](./runs/python.py) | Python (httpx) | Trigger run, poll status, get IO, cancel, WS token |
| [`curl.sh`](./runs/curl.sh) | bash + curl | Run lifecycle via curl |
| [`js.js`](./runs/js.js) | JavaScript (fetch) | Run lifecycle via fetch |

### [`hitl/`](./hitl/)

| File | Language | Description |
|---|---|---|
| [`python.py`](./hitl/python.py) | Python (httpx) | List pending gates, claim, approve/reject |
| [`curl.sh`](./hitl/curl.sh) | bash + curl | HITL gate management via curl |
| [`js.js`](./hitl/js.js) | JavaScript (fetch) | HITL gate management via fetch |

### [`library/`](./library/)

| File | Language | Description |
|---|---|---|
| [`python.py`](./library/python.py) | Python (httpx) | Browse, search, preview, copy-to-adapt, rate primitives |
| [`curl.sh`](./library/curl.sh) | bash + curl | Library operations via curl |
| [`js.js`](./library/js.js) | JavaScript (fetch) | Library operations via fetch |

### [`full-workflow.py`](./full-workflow.py)

| Language | Description |
|---|---|
| Python (httpx) | End-to-end: login → create pipeline → add agent → configure connector → trigger run → monitor via WebSocket → handle HITL gate → view results |

## Environment Variables

| Variable | Default | Description |
|---|---|---|
| `MODULO_URL` | `http://localhost:8000` | Base URL of the Modulo API server |
| `MODULO_EMAIL` | _(required)_ | Email for authentication |
| `MODULO_PASSWORD` | _(required)_ | Password for authentication |
| `MODULO_POLL_INTERVAL` | `2` | Seconds between run status polls (`full-workflow.py`) |
| `MODULO_MAX_POLLS` | `30` | Maximum poll attempts (`full-workflow.py`) |

## API Endpoints Used

| Method | Endpoint | Example |
|---|---|---|
| POST | `/api/v1/auth/login` | `auth-login/` |
| POST | `/api/v1/auth/refresh` | `auth-login/` |
| POST | `/api/v1/auth/logout` | `auth-login/` |
| GET | `/api/v1/auth/me` | `auth-login/` |
| POST | `/api/v1/auth/ws-token` | `runs/`, `full-workflow.py` |
| WS | `/api/v1/runs/{id}/ws?token=<ws-token>` | `runs/`, `full-workflow.py` |
| GET | `/api/v1/pipelines` | `pipelines/` |
| POST | `/api/v1/pipelines` | `pipelines/`, `full-workflow.py` |
| GET | `/api/v1/pipelines/{id}` | `pipelines/` |
| PATCH | `/api/v1/pipelines/{id}` | `pipelines/` |
| DELETE | `/api/v1/pipelines/{id}` | `pipelines/` |
| PUT | `/api/v1/pipelines/{id}/graph` | `full-workflow.py` |
| POST | `/api/v1/runs` | `runs/`, `full-workflow.py` |
| GET | `/api/v1/runs/{id}` | `runs/`, `full-workflow.py` |
| POST | `/api/v1/runs/{id}/cancel` | `runs/`, `full-workflow.py` |
| GET | `/api/v1/runs/{id}/io` | `runs/`, `full-workflow.py` |
| GET | `/api/v1/runs/{id}/hitl/pending` | `hitl/`, `full-workflow.py` |
| POST | `/api/v1/runs/{id}/hitl/{review_id}/claim` | `hitl/`, `full-workflow.py` |
| POST | `/api/v1/runs/{id}/hitl/{review_id}/approve` | `hitl/`, `full-workflow.py` |
| POST | `/api/v1/runs/{id}/hitl/{review_id}/reject` | `hitl/` |
| GET | `/api/v1/hitl/pending` | `hitl/` |
| GET | `/api/v1/libraries` | `library/` |
| GET | `/api/v1/libraries/{id}` | `library/` |
| POST | `/api/v1/libraries` | `library/` |
| POST | `/api/v1/libraries/{id}/adapt` | `library/` |
| POST | `/api/v1/libraries/{id}/ratings` | `library/` |
| GET | `/api/v1/agents` | `full-workflow.py` |
| POST | `/api/v1/agents` | `full-workflow.py` |
| POST | `/api/v1/schemas` | `full-workflow.py` |
| POST | `/api/v1/schemas/{id}/versions` | `full-workflow.py` |
| GET | `/api/v1/connectors` | `full-workflow.py` |
| POST | `/api/v1/connectors` | `full-workflow.py` |
| GET | `/api/v1/model-backends` | `full-workflow.py` |
