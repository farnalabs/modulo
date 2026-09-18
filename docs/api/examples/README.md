# Modulo API Client Examples

Standalone, copy-paste runnable code examples in **Python** (`httpx`), **curl**, and **JavaScript** (`fetch`) covering all common Modulo API operations.

## Prerequisites

- A running Modulo instance (default: `http://localhost:8000`)
- An active user account (`MODULO_EMAIL` / `MODULO_PASSWORD`)

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
| GET | `/api/v1/pipelines` | `pipelines/` |
| POST | `/api/v1/pipelines` | `pipelines/`, `full-workflow.py` |
| GET | `/api/v1/pipelines/{id}` | `pipelines/` |
| PATCH | `/api/v1/pipelines/{id}` | `pipelines/` |
| DELETE | `/api/v1/pipelines/{id}` | `pipelines/` |
| PATCH | `/api/v1/pipelines/{id}/graph` | `full-workflow.py` |
| POST | `/api/v1/runs` | `runs/`, `full-workflow.py` |
| GET | `/api/v1/runs/{id}` | `runs/`, `full-workflow.py` |
| POST | `/api/v1/runs/{id}/cancel` | `runs/`, `full-workflow.py` |
| GET | `/api/v1/runs/{id}/io` | `runs/`, `full-workflow.py` |
| GET | `/api/v1/runs/{id}/hitl/pending` | `hitl/`, `full-workflow.py` |
| POST | `/api/v1/runs/{id}/hitl/{gate}/claim` | `hitl/`, `full-workflow.py` |
| POST | `/api/v1/runs/{id}/hitl/{gate}/approve` | `hitl/`, `full-workflow.py` |
| POST | `/api/v1/runs/{id}/hitl/{gate}/reject` | `hitl/` |
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
