# loads-service

Go service that:
1. Validates JWTs issued by your Django backend (`CustomTokenObtainPairSerializer`) — no network call to Django needed, just shared-secret signature verification.
2. Connects to Atrek's third-party websocket feed, persists every event to Postgres via GORM.
3. Broadcasts every event to all authenticated clients connected via Server-Sent Events (SSE).

## Project structure

```
cmd/api/main.go            entry point — loads config, builds App, runs it
internal/
  loads/                   domain: dto.go, repo.go, service.go, handler.go
  middleware/auth.go       JWT validation (Django HS256 tokens)
  server/                  app.go (wiring), routes.go (route registration)
  utils/config.go          .env loading into a typed Config struct
pkg/
  atrek/atrek.go           Atrek websocket client (3rd-party integration)
  database/                models.go (GORM models), postgres.go (connection)
```

This mirrors the structure you described: each new domain under `internal/`
(e.g. `users`, `fleet`, `analytics`) gets its own `dto.go` / `handler.go` /
`repo.go` / `service.go`, following the pattern in `internal/loads/`.

## Setup

```bash
cp .env.example .env
# edit .env: set DB_* to your real Postgres, JWT_SECRET to match Django's
# SIMPLE_JWT signing key exactly, and ATREK_WS_URL / ATREK_AUTH_TOKEN to
# your real Atrek credentials.

go mod tidy
go run ./cmd/api
```

The server auto-migrates `load_events` and `loads` tables on startup —
no separate migration step needed for now.

## JWT integration with Django

Your `CustomTokenObtainPairSerializer` signs an `access` token (SimpleJWT,
HS256) containing `user_id`, `department`, `company`, plus the standard
`token_type: "access"` claim. `internal/middleware/auth.go` validates that
exact shape — it does **not** call back into Django; it only checks the
signature against `JWT_SECRET`, which **must** be the same value as
Django's `SIMPLE_JWT['SIGNING_KEY']` (defaults to `SECRET_KEY` if unset
in Django settings).

**Important:** if Django rotates `SECRET_KEY`/`SIGNING_KEY`, update
`JWT_SECRET` here at the same time, or all tokens will fail validation.

## Endpoints

| Method | Path                | Auth | Description |
|---|---|---|---|
| GET | `/health` | none | liveness check |
| GET | `/api/v1/loads/stream` | JWT | SSE stream of all load events, live |
| GET | `/api/v1/loads` | JWT | paginated history (`?page=`, `?page_size=`) |

Auth accepts either `Authorization: Bearer <token>` or `?token=<token>`
as a query param — the query param exists because browser `EventSource`
(used to consume SSE) cannot set custom headers.

### Example: connecting to the stream from JS

```js
const token = "<jwt from Django login>";
const es = new EventSource(`http://localhost:8080/api/v1/loads/stream?token=${token}`);

es.addEventListener("load", (e) => {
  const msg = JSON.parse(e.data);
  console.log(msg.type, msg.label, msg.data);
});
```

## Data model

Every Atrek message is stored twice:
- **`load_events`** — raw, generic (`type`, `label`, `raw_data` jsonb).
  Every event type is captured here, even ones not yet structurally handled.
- **`loads`** — structured columns, populated only for `type: "LOAD_CREATED"`.
  Add more `case` branches in `internal/loads/service.go`'s `handleEvent`
  as you learn about other Atrek event types (e.g. `LOAD_UPDATED`).

## Notes / things to verify against the real Atrek feed

- `pkg/atrek/atrek.go` assumes auth via an `Authorization: Bearer <token>`
  header on the WS handshake — confirm this matches Atrek's actual auth
  scheme (could be a query param or a post-connect auth message instead).
- SSE broadcasts to **every** connected client (per your spec) — `users_uuid`
  in the load payload is stored but not used for filtering.
