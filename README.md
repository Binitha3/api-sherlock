# API Sherlock

Find exactly what changed when an API integration breaks.

API Sherlock imports an OpenAPI specification, runs real requests against a live
API, validates every response against the contract, and reports the exact field
that broke — before your users find out.

> Status: **Phases 1–2** — project/spec import, environment management, request
> execution and response-contract validation are implemented. Baselines, schema
> diffs, reports and the CI CLI are later phases.

## What it does (Phases 1–2)

- Create projects and list them.
- Import an OpenAPI 3.0 / 3.1 document as a JSON/YAML **file upload** or from a
  **URL**, validate it with clear JSON-path errors, resolve local `$ref`s and list
  operations with request/response contracts.
- Create, list, inspect, update and delete project environments. Environment
  records contain only the base URL and auth configuration, never auth secrets.
- Run selected operations from a spec using parameter examples/defaults and
  per-run bearer, API-key-header or basic credentials. Operations that need an
  uninferable parameter or request body are marked skipped with a reason.
- Validate response status, content type and JSON body schema; record exact
  violation paths, status, latency and concise authentication hints.
- `GET /health`.

## Stack

FastAPI · Pydantic v2 · SQLAlchemy 2 + Alembic · PostgreSQL · httpx ·
jsonschema (Draft 2020-12) · openapi-spec-validator · Typer (CLI, Phase 4) ·
pytest + respx · Docker

## Quick start (Docker)

```bash
docker compose up --build
# API on http://localhost:8000 — docs at /docs
curl http://localhost:8000/health
```

The API container runs `alembic upgrade head` on boot, so the database schema
is always current.

### Import a spec

```bash
# create a project
curl -s -X POST http://localhost:8000/projects \
  -H 'content-type: application/json' \
  -d '{"name": "Payments"}'
# -> {"id": "<project_id>", ...}

# upload a file
curl -s -X POST "http://localhost:8000/projects/<project_id>/specs" \
  -F file=@openapi.yaml

# or import from a URL
curl -s -X POST "http://localhost:8000/projects/<project_id>/specs" \
  -H 'content-type: application/json' \
  -d '{"url": "https://example.com/openapi.json", "version_label": "v1"}'

# list specs and their operations
curl -s "http://localhost:8000/projects/<project_id>/specs"
```

## Phase 2 local demo

In two terminals, after installing the project dependencies:

```bash
# Terminal 1: start the target API
uvicorn demo_api.main:app --host 127.0.0.1 --port 8001

# Terminal 2: allow loopback targets for local development only, then start Sherlock
ALLOW_PRIVATE_HOSTS=true uvicorn app.main:app --reload
```

Upload `demo_api/openapi.yaml` to a project, create an environment with base URL
`http://127.0.0.1:8001`, then `POST /runs` with the resulting `spec_id` and
`environment_id`. Use `include_operations` to select a smaller run. The demo
switches via `DEMO_API_VERSION=v1`, `v2-safe`, or `v2-breaking`; restart the demo
server after changing the variable. `v2-safe` adds an optional response field;
`v2-breaking` removes/changes response fields and switches `/account` from
Bearer to an API-key header (`DEMO_API_KEY_HEADER`, default `X-Demo-Key`; value
from `DEMO_API_KEY`, default `demo-key`). These demo credentials are only for
local development.

## Local development (no Docker)

```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
cp .env.example .env             # point DATABASE_URL at your Postgres
alembic upgrade head
uvicorn app.main:app --reload
```

## Tests and lint

```bash
pytest            # unit + integration tests (SQLite, local only)
ruff check .      # lint
ruff format .     # formatting (check with ruff format --check .)
```

## Configuration

All configuration comes from environment variables (see `.env.example`):

| Variable | Default | Purpose |
|---|---|---|
| `DATABASE_URL` | Postgres on localhost | SQLAlchemy URL (SQLite only for tests) |
| `API_KEY` | `change-me` | `X-API-Key` for API protection (Phase 5) |
| `CORS_ORIGINS` | localhost:5173 | Allowed browser origins (Phase 5) |
| `ALLOW_PRIVATE_HOSTS` | `false` | SSRF escape hatch for local dev (Phase 2) |
| `REQUEST_TIMEOUT_SECONDS` | `10` | Outgoing request timeout (Phase 2) |
| `MAX_RESPONSE_BYTES` | `2097152` | 2 MiB response cap (Phase 2) |

## Repository layout

```
app/api        routers
app/core       config, security, errors
app/db         models, session, Alembic base
app/services   spec_import, runner, validator, differ, reporter
app/cli.py     Typer CLI (Phase 4)
tests/         pytest suite
demo_api/      sample target API (Phase 5)
alembic/       migrations
Dockerfile, docker-compose.yml
```

## Security notes (growing)

- **Secrets are never stored.** Auth tokens/API keys are passed per run and
  kept only in memory.
- SSRF checks resolve target DNS and block non-public addresses before spec URL
  imports and each API request; redirects are rechecked and cross-origin redirects
  are refused. `ALLOW_PRIVATE_HOSTS=true` is only for trusted local development.

## Limitations (yet)

- Baselines, schema diffing, reports, CLI and GitHub Actions integration remain
  for Phases 3–4; the demo API and expanded end-to-end suite are Phase 5.
- External `$ref` targets are rejected — inline them or import one
  self-contained document.
- Local `$ref` cycles are left as references in the stored document.