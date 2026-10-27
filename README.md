# API Sherlock

Find exactly what changed when an API integration breaks.

API Sherlock imports an OpenAPI specification, runs real requests against a live
API, validates every response against the contract, and reports the exact field
that broke — before your users find out.

> Status: **Phase 1** — project, database and spec import are implemented.
> Running requests, baselines, diffing, reports and the CLI arrive in later phases.

## What it does (Phase 1)

- Create projects and list them.
- Import an OpenAPI 3.0 / 3.1 document as a JSON/YAML **file upload** or from a
  **URL**, validated on import with clear JSON-path errors.
- `$ref` resolution (local `#/...` references) and extraction of the operation
  list: method, path, parameters, response schemas per status code.
- Persistent schema for later phases: runs, run results and baselines already exist.
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
- SSRF protection for user-supplied URLs arrives with Phase 2 (the run runner).

## Limitations (yet)

- No request execution, baselines, diffing, reports or CLI yet (Phases 2–4).
- External `$ref` targets are rejected — inline them or import one
  self-contained document.
- Local `$ref` cycles are left as references in the stored document.