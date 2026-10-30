# API Sherlock

Find exactly what changed when an API integration breaks.

API Sherlock imports an OpenAPI specification, runs real requests against a live
API, validates every response against the contract, and reports the exact field
that broke — before your users find out.

> Status: **Phases 1–4** — project/spec import, environment management, safe
> request execution, response-contract validation, saved response baselines,
> spec/live schema diffs, JSON/Markdown/HTML reports and a CI-ready CLI are
> implemented. The expanded demo API and end-to-end/SSRF coverage finish in Phase 5.

## What it does (Phases 1–4)

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
- Save a run's inferred response shapes as a named baseline; raw response bodies
  and auth credentials are not stored in baseline data.
- Compare two imported specs or a new run against a saved baseline, with exact
  paths and breaking, warning or non-breaking severity classifications.
- Report a run as JSON, Markdown or HTML: severity counts, a change table,
  failed operations with auth hints and violation paths, and latency statistics.
- Check a live API from CI with `sherlock check`, without the server or database,
  and fail the build on changes at or above a chosen severity.
- `GET /health`.

## Stack

FastAPI · Pydantic v2 · SQLAlchemy 2 + Alembic · PostgreSQL · httpx ·
jsonschema (Draft 2020-12) · openapi-spec-validator · Typer · pytest + respx ·
Docker

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
app/cli.py     Typer CLI (local/CI mode, no server or database)
examples/      GitHub Actions workflow for the CLI
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

## Phase 3: baselines and diffs

Save response shapes from a completed run with `POST /runs/{run_id}/baseline`
(optionally pass `{"label":"v1"}`). List a project's baseline metadata with
`GET /projects/{project_id}/baselines`. Compare a later run with
`GET /runs/{run_id}/diff?baseline_id=<baseline_id>`, or compare two imported
specifications with `POST /projects/{project_id}/diff/specs` and JSON fields
`old_spec_id` and `new_spec_id`. Both diff responses include counts by severity
and changes with exact paths. Inferred baseline schemas retain field names,
types, nullability and array shape, not response values.

## Phase 4: reports and CI

### Reports

`GET /runs/{run_id}/report?format=json|markdown|html`

A report contains operation pass/fail/skip counts, change counts by severity, a
table of contract changes, failed operations with violation paths and auth
hints, skipped operations and latency statistics (min, mean, p95, max). Response
bodies and credentials are never part of a report.

If `baseline_id` is omitted, the newest baseline saved **before** the run is
used, so a report never compares against a baseline created later. `markdown`
and `html` responses are documents with an inline `Content-Disposition`
filename.

```bash
curl -s "http://localhost:8000/runs/<run_id>/report?format=markdown" -o report.md
curl -s "http://localhost:8000/runs/<run_id>/report?baseline_id=<baseline_id>"
```

### CLI (local mode, no server or database)

```bash
pip install .
sherlock baseline save --spec openapi.yaml --base-url https://api.example.com   --output baseline.json
sherlock check --spec openapi.yaml --base-url https://api.example.com   --baseline baseline.json --fail-on breaking --format markdown --output report.md
```

Exit codes: `0` clean, `1` changes at or above `--fail-on`, `2` tool or
configuration error. `--fail-on` accepts `breaking` (default), `warning` or
`non_breaking`; `--format` accepts `markdown` (default), `json` or `html`.
Without `--output` the report is printed to stdout; a one-line summary and the
result always go to stderr.

Secrets come from the environment only — never from flags, reports or logs:

| Variable | Used for |
|---|---|
| `SHERLOCK_AUTH_TYPE` | `none` (default), `bearer`, `api_key_header`, `basic` |
| `SHERLOCK_TOKEN` | Bearer token |
| `SHERLOCK_API_KEY` | API key value |
| `SHERLOCK_AUTH_HEADER` | API key header name (default `X-API-Key`) |
| `SHERLOCK_USERNAME`, `SHERLOCK_PASSWORD` | Basic auth |

`--allow-private-hosts` permits loopback/private targets for local development
and is equivalent to `ALLOW_PRIVATE_HOSTS=true`. Baselines store inferred
response shapes (field names, types, nullability), never response values.

### Try it against the demo API

```bash
# Terminal 1 — v1 matches the spec
uvicorn demo_api.main:app --port 8001

# Terminal 2 — save the baseline from v1 (auth is only needed for /account)
SHERLOCK_TOKEN=demo-token sherlock baseline save   --spec demo_api/openapi.yaml --base-url http://127.0.0.1:8001   --auth-type bearer --allow-private-hosts --output /tmp/baseline.json

# Terminal 3 — start the breaking version elsewhere, then check it
DEMO_API_VERSION=v2-breaking uvicorn demo_api.main:app --port 8002

SHERLOCK_TOKEN=demo-token sherlock check   --spec demo_api/openapi.yaml --base-url http://127.0.0.1:8002   --auth-type bearer --allow-private-hosts   --baseline /tmp/baseline.json --format markdown --output /tmp/report.md
# exit code 1, and the report names the removed `name` field and changed
# `active` type, plus the auth-header switch on /account
```

### GitHub Actions

[examples/github-actions.yml](examples/github-actions.yml) is ready to copy to
`.github/workflows/api-contract.yml`. It installs the package, runs
`sherlock check --fail-on breaking` on pull requests, writes the Markdown report
to `$GITHUB_STEP_SUMMARY`, uploads it as an artifact and fails the job when a
breaking change is found. Set the `API_BASE_URL` repository variable and the
credential as a secret (`API_TOKEN` for bearer).

## Limitations (yet)

- API-key protection (`X-API-Key`) and CORS wiring are Phase 5; today the API
  runs unauthenticated locally.
- The CLI reports changes relative to a baseline file; it does not schedule
  monitoring runs or post comments on pull requests by itself (the workflow
  posts the report to the job summary).
- External `$ref` targets are rejected — inline them or import one
  self-contained document.
- Local `$ref` cycles are left as references in the stored document.
