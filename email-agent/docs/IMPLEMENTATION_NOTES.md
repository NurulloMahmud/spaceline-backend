# Implementation Notes — Bid-to-Book Automation

Built 2026-07-23. Companion to `BID_TO_BOOK_BUILD_PLAN.md` (the design) and
`FRONTEND_API_GUIDE.md` (the frontend contract).

## What changed, by service

### `agent_bot/` — two fixes

- **Driver bids now actually reach dispatch.** `agents/loads.py` called nothing
  when a driver replied with a rate; it only flipped a local `pending_offers`
  row. It now calls `agent.place_bid()`, so a `driver_bid` lands in atrek's
  `load_bids` and appears on the bid board. If the call fails the driver is told
  to contact dispatch rather than being left thinking the bid went through.
- **Load dimensions in the group message.** The SSE feed omits `dims`, so
  `LoadAgent.enrich_load()` fetches `GET {atrek}/api/v1/loads/{id}` once per load
  and merges `dims` / `pieces` / `weight` / `notes` before the offer is formatted.
  The message gains a `📐 Dimensions: 96" × 48" × 60"` line when available.
- `services/agent.py` gained `get_load()`, and `place_bid()` now takes
  `company_id` (internal callers carry no JWT) and logs failures instead of
  silently returning `False`.

### `atrek/` — bid board

- `LoadBid` gained `ignored_at` / `ignored_by` (GORM automigrate adds the columns).
- `POST /api/v1/bids/:bid_id/ignore` and `/unignore`, company-checked, broadcasting
  `bid_ignored` / `bid_unignored` on the existing bid SSE hub.
- `GET /api/v1/bids` now takes `?action=` and `?ignored=`. **It still defaults to
  `action=dispatcher_bid`**, so existing consumers are unaffected; the bid board
  passes `action=driver_bid` explicitly.
- `POST /loads/:id/bid` moved from the JWT-only group into `AuthenticateOrInternal`.
  Internal callers supply `company_id` plus `driver_id` or `user_id` in the body.
  Without this the bot's bid call would have 401'd.
- `RecordBid` now broadcasts dispatcher bids too, not only driver bids, so the
  board turns green when the email-agent records a bid.

### `boxTruck/` — company fields + internal API

- `Company.logo` (ImageField) and `Company.bid_validity_minutes` (default 15),
  migration `users/0008`. These feed the bid email letterhead.
- `NearbyDriverSerializer` now returns `company: {id, name}` — the bot needs the
  company id to record a bid.
- New `billing/internal_views.py`, all behind `IsInternalService`:
  - `GET  /api/billing/internal/company/<company_id>/`
  - `GET  /api/billing/internal/dispatcher/<user_id>/`
  - `POST /api/billing/internal/brokers/resolve/` — find by MC then name, create if absent
  - `POST /api/billing/internal/parse-ratecon/` — the existing Gemini/GPT parser, no JWT
  - `POST /api/billing/internal/book-load/` — load + stops + ratecon file in one transaction

  **Deviation from the plan:** the plan said to add internal-secret permission to
  the existing `LoadsViewSet` / `LoadStopsViewSet` / `LoadFilesAPIView`. Those
  views scope every query to `request.user`, which internal callers do not have,
  and opening them up would have meant touching auth on dispatcher-facing
  endpoints. A separate internal module is safer and makes booking atomic —
  one call, one transaction, no partially-created loads.

### `email-agent/` — new service

FastAPI + SQLAlchemy + Postgres. See its `README.md`. Roughly:

| Concern | File |
|---|---|
| Django JWT validation, company scoping | `services/auth.py` |
| Starting a bid (the only auto-sent email) | `services/negotiations.py` |
| The fixed bid template | `services/bid_email.py` |
| Webhook → classify → suggest or verify | `services/inbound.py` |
| Verified ratecon → TMS load → driver notice | `services/booking.py` |
| Ignore / send / edit & send | `services/suggestions.py` |
| Every model call, schema-constrained JSON | `services/ai.py` |
| SSE hub | `services/events.py` |

## The safety rules, and where they are enforced

1. **Only the initial bid email is auto-sent.** Every other outbound message goes
   through `services/suggestions.send()`, which requires a dispatcher's user id.
   Nothing else calls `nylas.send_message`.
2. **A mismatched ratecon never becomes a load.** `inbound.handle_ratecon` computes
   `price_ok` **arithmetically** (`abs(ratecon_cents - agreed_cents) <= PRICE_TOLERANCE_CENTS`,
   tolerance `0`) rather than trusting the model's boolean, and `booking.book_verified_load`
   is only reachable when price, locations and dates all pass.
3. **The agreed rate can move during the thread.** If the AI reads a later agreement
   with confidence, that becomes the number the ratecon must match; otherwise it
   falls back to the opening bid. Both are stored on the negotiation.
4. **No failure is silent.** Parse failure, verification failure, mismatch and TMS
   booking failure each write a row and emit an SSE event.
5. **The driver's amount never reaches the broker.** Enforced in the template
   (which never receives it), in the AI system prompt, and asserted by tests.
6. **Company scoping on every query.** The JWT's `company` claim bounds all reads;
   cross-company access returns 404.
7. **Only management can connect a mailbox.** `POST /accounts/connect` requires
   `Principal.is_management` (department `Management`, read straight off the same
   Django JWT — not a separate role system). A regular dispatcher gets 403 even
   for their own company's mailbox, not just another company's. `GET /accounts`
   (read-only status) is unrestricted. Added 2026-07-29, the same day a test
   connection got filed under the wrong company by hand — a management-only
   gate doesn't stop a mistaken click, but it does restrict who can make one.

## Verification performed

- **email-agent: 80 tests pass** (grown from 54 as fixes landed) against a real Postgres, with Nylas, OpenAI, the
  TMS and Telegram stubbed. `tests/test_ratecon_verification.py` covers the
  scenario you specified: agreed $3,200, ratecon says $3,000 → no load created,
  status `mismatch`, a drafted correction reply, no driver notification. Also
  covered: location mismatch, date mismatch, unreadable PDF, a failed AI verdict
  (treated as failure, not as a pass), booking idempotency, and TMS failure.
- **boxTruck:** `manage.py check` passes and `makemigrations --check` reports no
  missing migrations, so the hand-written migration matches the model.
- **atrek:** `go build ./...` and `go vet ./...` are clean.
- **agent_bot:** syntax-checked; it has no test suite to extend.

## Deployment (live as of 2026-07-23)

Server `95.169.205.181`, all processes under supervisor, all behind nginx on
`boxmanage.smartfleetllc.com`.

| Service | Path | Process | Route |
|---|---|---|---|
| boxTruck | `/home/api/boxTruck` | `box`, `celery` | `/api/` |
| atrek | `/home/api/agent` | `agent` (:8080) | `/api/v1/` |
| agent_bot | `/home/api/agent_bot` | `agent_bot` | — |
| **email-agent** | `/home/api/email-agent` | `email_agent` (:8100) | `/email-agent/` |

`EMAIL_AGENT_BASE_URL` for the frontend is
`https://boxmanage.smartfleetllc.com/email-agent`, which maps 1:1 onto the paths
in `FRONTEND_API_GUIDE.md` (`/email-agent/api/v1/negotiations`, and so on).
The nginx location disables buffering and allows 3600s reads so SSE works.

Pre-deploy backups are in `/root/predeploy_backups/` (boxTruck database dump and
the previous nginx config).

### Three bugs the deployment surfaced

Integration testing against the real services caught problems the unit tests
could not, because each involved another service's actual response shape:

1. **Peer base URLs were doubled.** `agent_bot` stores `BOXTRUCK_BASE_URL` with
   an `/api` suffix because its clients append bare paths; this service builds
   full paths, so a copied value produced `/api/api/...` and 404'd every
   cross-service call. Base URLs are now reduced to the origin with a warning
   when a suffix is stripped (`config/settings.py`, covered by `test_settings.py`).
2. **The driver detail endpoint returned no truck dimensions.** `DriverViewSerializer`
   only returned identity fields, so every bid email would have read
   `DIMENSIONS: N/A` regardless of the truck. It now returns length, width,
   height, payload, ramps and equipment, matching the nearby-drivers endpoint.
3. **The load detail carried no coordinates.** The upstream payload has
   dimensions but no geography; the stored feed event has geography but no
   dimensions. Deadhead was therefore uncomputable and `MILES OUT` was always
   `N/A`. atrek's detail response now merges its stored geography in wherever
   upstream omits it.

### Verified on the live server

- All four services healthy; existing routes unaffected (`/api/v1/health`,
  `/api/swagger/` both 200).
- A token signed with the production Django key is accepted by email-agent;
  unauthenticated requests get 401.
- Cross-service calls succeed: company profile, dispatcher, broker resolve,
  and load detail.
- **The broker email field is `contact_email`** — previously a guess, now
  confirmed against a live payload, so dispatchers will not be prompted for it.
- A bid email rendered end-to-end from live data: real broker address, real
  truck dimensions (`190" x 70" x 76" / 5,000 lbs`) and a real deadhead (471 mi).

## Before this runs in production

Items 1–3 and 6 from the original list are **done** (see Deployment above). What
remains is configuration only I cannot supply:

1. **Nylas credentials — the blocker.** `NYLAS_API_KEY`, `NYLAS_CLIENT_ID` and
   `NYLAS_WEBHOOK_SECRET` are blank in `/home/api/email-agent/.env`. Until they
   are filled in and the service restarted, bidding returns
   `mailbox_not_connected` and no email can be sent or received. Everything else
   is wired and verified.
2. **Company letterhead.** Company 1 (Shipluxe LLC) has no `mc`, `phone_number`,
   `email` or `logo`, so those lines render blank in the bid email. From the
   agreed template these should be MC `846834`, phone `630-426-3362`, email
   `operation@shipluxellc.com`, plus a logo upload via Django admin.
3. **`Company.shipment_number` must be set** for any company that books loads —
   `Load.save()` derives the shipment number from it and will raise without it.
   The booking endpoint returns a clear error, surfaced as `booking_failed`.
4. **Vehicle equipment is mostly empty** in the TMS, so `Truck equipment` renders
   `N/A`. Populate `VehicleEquipment` rows for the trucks you bid with.

## Known limitations

- **Single instance only.** The SSE hub is in-process. Running replicas requires
  Redis pub/sub behind `services/events.py`; the publish/subscribe surface is
  already isolated for that swap.
- **No event replay.** Events emitted while a dispatcher is disconnected are lost;
  the frontend refetches lists on reconnect (documented in the frontend guide).
- **Attachment handling takes the first PDF** on a ratecon email. A broker sending
  the ratecon plus other PDFs in one message will have the first one parsed.
- **`agent_bot` still has no tests**, so its two changes are covered by review and
  syntax checking only.
