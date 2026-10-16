# AsyncTaskEngine

This repository currently contains a synchronous incident-triage API, rather than a
background task queue or webhook dispatcher. It accepts incident details, applies
transparent rule-based triage, and stores the result in PostgreSQL with a best-effort
Redis cache.

## Architecture

```text
HTTP client
    └── FastAPI (app/main.py)
          ├── validates request and assigns a request ID
          ├── triage rules (app/triage.py)
          └── storage functions (app/storage.py)
                ├── PostgreSQL: durable incident records
                └── Redis: optional incident cache
```

- `app/main.py` exposes health, readiness, triage, and incident-retrieval endpoints.
- `app/schemas.py` validates incoming incident fields and shapes successful responses.
- `app/config.py` reads and validates environment-based settings.
- `app/storage.py` owns PostgreSQL access, bounded transient retries, and best-effort caching.
- `app/triage.py` contains deterministic severity, duplicate, and runbook rules.
- `app/logging_config.py` emits JSON logs with request IDs and lifecycle fields.

The triage rules are deliberately simple and do not call an external AI service. The API
does not currently provide authentication, asynchronous workers, webhook delivery,
distributed tracing, or a metrics endpoint. Put it behind an authenticated, TLS-enabled
gateway before exposing it to an untrusted network.

## Run locally

Use Python 3.11 or newer and have PostgreSQL and Redis available. Set connection
environment variables (see [Configuration](#configuration)), then run:

```bash
python -m pip install -r requirements.txt
uvicorn app.main:app --reload
```

The API listens on `http://localhost:8000`. Interactive API documentation is available
at `http://localhost:8000/docs`.

## Run with Docker Compose

```bash
docker compose up --build
```

The Compose defaults are for local development only. To change the database password,
set `POSTGRES_PASSWORD` in your shell or a local `.env` file before starting the stack.
Do not use the Compose development password in a deployed environment. The API, database,
and Redis ports are bound to localhost only.

Check service health:

```bash
curl http://localhost:8000/health
curl http://localhost:8000/ready
```

To stop the services, run `docker compose down`. Add `-v` only when you intentionally
want to remove the database volume.

## Configuration

| Variable | Default | Description |
| --- | --- | --- |
| `DATABASE_URL` | unset | Optional PostgreSQL URL; when set, it takes precedence over the `POSTGRES_*` settings. |
| `POSTGRES_HOST` | `localhost` | PostgreSQL host. Compose configures the service hostname `postgres`. |
| `POSTGRES_PORT` | `5432` | PostgreSQL port, from 1 to 65535. |
| `POSTGRES_DB` | `incident_triage` | Database name. |
| `POSTGRES_USER` | `postgres` | Database user. |
| `POSTGRES_PASSWORD` | empty | Database password. Configure a non-empty value for deployments. |
| `REDIS_URL` | `redis://localhost:6379/0` | Redis connection URL. |
| `DATABASE_RETRY_ATTEMPTS` | `5` | Total database attempts for transient connection failures. |
| `RETRY_BASE_DELAY_SECONDS` | `0.1` | Initial retry delay; exponential backoff uses jitter. |
| `RETRY_MAX_DELAY_SECONDS` | `2.0` | Maximum retry delay. Must be at least the base delay. |
| `CACHE_TTL_SECONDS` | `3600` | Lifetime of cached incident records. |
| `LOG_LEVEL` | `INFO` | One of `DEBUG`, `INFO`, `WARNING`, `ERROR`, or `CRITICAL`. |
| `SKIP_STARTUP_INIT` | `false` | Skip schema initialization; intended for tests and controlled migrations. |

Invalid values are rejected during application startup with an actionable configuration
error. `DATABASE_URL` must use PostgreSQL; `REDIS_URL` must use Redis or a Unix socket.

## API

### `GET /health`

Liveness endpoint. It does not query the database or Redis.

```json
{"status":"ok"}
```

### `GET /ready`

Checks PostgreSQL and Redis connectivity. Returns `503` if either dependency is
unavailable.

### `POST /incidents/triage`

Example request:

```bash
curl -X POST http://localhost:8000/incidents/triage \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: incident-2026-001' \
  -d '{
    "title": "Payment API latency spike",
    "description": "Checkout requests time out",
    "service": "payments-api",
    "source": "monitoring"
  }'
```

`title` is required and limited to 300 characters; `description` is required and limited
to 10,000 characters. Optional `service` and `source` values are limited to 100
characters. Unknown fields are rejected.

The optional `Idempotency-Key` header makes repeated submissions safe: the same key and
same incident input return the original stored incident. Reusing the key with different
input returns `409 Conflict`. Keys may contain letters, numbers, `.`, `_`, `:`, and `-`,
up to 255 characters.

### `GET /incidents/{incident_id}`

Returns a stored incident or `404` if it does not exist.

### Errors and retries

Errors use the same shape, with a stable code and a request ID:

```json
{
  "error": {
    "code": "validation_error",
    "message": "Request validation failed",
    "details": []
  },
  "request_id": "..."
}
```

Malformed or invalid input returns `4xx`; unavailable persistence returns `503`.
PostgreSQL connection failures are retried a bounded number of times using exponential
backoff and jitter. Redis is a cache, not the source of truth: cache read/write failures
are logged and database-backed requests continue. The `X-Request-ID` response header
contains the correlation ID; a safe supplied `X-Request-ID` is preserved, otherwise one
is generated.

JSON logs include timestamps, severity, event names, and request IDs. Incident lifecycle
and retry events include incident IDs or attempt/delay fields when available.

## Tests and checks

Install the pinned dependencies, then run the deterministic unit/API tests:

```bash
python -m pip install -r requirements.txt
pytest -q
```

Tests mock PostgreSQL and Redis so no external services are needed. There is no separate
linter or type-checker configured in this repository.

## Troubleshooting

- **Startup reports invalid configuration:** check variable names and values in the
  configuration table. Retry attempts and cache TTL must be positive integers.
- **`/ready` returns `503`:** verify PostgreSQL and Redis are running and that their
  configured hostnames, ports, and credentials match the API environment.
- **Triage returns `503`:** inspect JSON application logs for `database.retry` and
  `database.retry_exhausted` events; confirm the database is reachable.
- **Triage succeeds but a cache warning appears:** the incident is persisted in
  PostgreSQL; restore Redis to re-enable caching.
- **Docker cannot bind a port:** stop the local service already using port 8000, 5432,
  or 6379, or adjust the host-side Compose port mapping.
