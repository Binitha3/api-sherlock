# API Sherlock

Find exactly what changed in your API.

API Sherlock imports an OpenAPI spec, runs real requests against a live API, compares each response to the contract, and reports breaking changes with the exact field that changed.

## Status

In development.

- Landing page: done (`index.html`)
- Backend (Python, FastAPI): in progress

## Planned

- Import and validate OpenAPI 3.0 and 3.1 specs
- Run test requests and validate responses against the contract
- Save a baseline and compare later runs against it
- Classify changes as breaking, warning or safe
- CLI for CI that fails a build on breaking changes

## Run the landing page locally

    python -m http.server 4187

Then open http://127.0.0.1:4187/index.html

Built by Binitha.
