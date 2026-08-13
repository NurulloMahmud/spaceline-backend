# Bid-to-Book Automation — Build Plan & Implementation Prompt

> **Audience:** an AI coding model (or engineer) implementing this feature set.
> **Scope:** backend only. Frontend team gets API documentation (deliverable in Phase 6).
> **Goal:** humans (dispatchers) only make decisions; the system does the work.

---

## STATUS: BUILT (2026-07-23)

All phases below are implemented and verified. See `IMPLEMENTATION_NOTES.md` for
what was built, where it deviates from this plan, and what remains before
production. Frontend contract: `FRONTEND_API_GUIDE.md`.

The bid email uses a **fixed template** (§4.11), not AI-generated text.

---

## 1. System context (read this first)

This repo contains three services that already work together. Do not restructure them.

### 1.1 `boxTruck/` — Django TMS (system of record)
- DRF + SimpleJWT. JWT contains `user_id`, `department`, `company`.
- Apps: `users`, `hiring` (drivers, vehicles, companies), `billing` (loads, brokers, ratecon parsing), `dispatchers`, `payroll`, `analytics`, `ai` (SQL analyst — unrelated to this project), `mobile`.
- Key models (`billing/models.py`): `Load` (fields incl. `company`, `driver`, `broker`, `load_number`, `driver_pay`, `carrier_pay`, `pickup_date`, `drop_date`, `status`), `LoadStop`, `LoadFile` (ratecon PDFs), `Broker` (has `email`, `mc`, `ai_type`).
- Key endpoints:
  - `POST /api/billing/rate-con-upload/` — multipart upload `{file, broker}` → returns `{parsed_data}` (stops, dates, price…). Parses with Gemini or GPT depending on `Broker.ai_type`. **This is the existing ratecon-reading endpoint — reuse it, do not build a new parser.** It can throw (returns 400 "Failed to process file") — that failure path must notify dispatchers (see §4.6).
  - `POST /api/billing/loads/` (LoadsViewSet) + `POST /api/billing/load-stops/` + `POST /api/billing/load-files/` — how loads, stops, and files are created.
  - `GET /api/hiring/drivers/?telegram_group_id=<id>` and `GET /api/hiring/drivers-nearby/?zip=&radius=` — already used by the bot.
- Internal service auth: header `X-Internal-Secret` (see `users/permissions.py:49`, `hiring/views.py`). Extend this pattern to whatever billing endpoints the new service needs; today only some hiring views honor it, so **you will need to add internal-secret access to the billing load-creation and rate-con-upload views** (add a permission class, do not weaken existing JWT auth for humans).

### 1.2 `atrek/` — Go/Gin realtime loads + bids service
- Consumes a third-party "Atrek" websocket feed of available loads; persists to Postgres (`loads`, `load_events`); rebroadcasts via SSE.
- Validates the same Django JWTs (shared HS256 secret) — no callback to Django. Also supports `X-Internal-Secret` via `AuthenticateOrInternal()` middleware (`internal/middleware/auth.go:186`, env `INTERNAL_SECRET_KEY`).
- Bids domain already exists (`internal/bids/`): table `load_bids` (`load_id` uuid, `driver_id`, `user_id`, `company_id`, `action` ∈ {`viewed`,`driver_bid`,`dispatcher_bid`}, `bid_amount`, `driver_amount`, `note`), per-company load color resolution (white/grey/red/green), SSE hub at `GET /api/v1/loads/bids/stream`.
- Routes (`internal/server/routes.go`): JWT-only group has `POST /loads/:id/bid`, `GET /loads/:id/bids`, `GET /bids` (company bid list, paginated+filtered). Internal-or-JWT group has `GET /loads/stream` and `GET /loads/:id`.
- `GET /api/v1/loads/:id` proxies to the third-party Atrek HTTP API (`pkg/atrek/atrek.go GetLoad`) and returns the **full load detail JSON — this is where load dimensions (`dims`, `pieces`, `weight`) and the posting contact's email come from.**

### 1.3 `agent_bot/` — Python Telegram bot
- python-telegram-bot v20 + OpenAI. Listens to atrek `GET /loads/stream` (SSE, internal secret), matches loads to nearby drivers (vehicle type, deadhead ≤ 50 mi, dimension fit, driver prefs), sends offers to each driver's **Telegram group chat** (`driver.telegram_group_id`), interprets replies (`accept`/`counter`/`reject` + amount) via `ai.extract_bid_intent`.
- Local Postgres for `driver_preferences` and `pending_offers` (offers expire after 30 min).
- `services/agent.py` has `place_bid()` → `POST {atrek}/loads/{id}/bid` — **currently never called** (known bug, fixed in Phase 1).
- Env config in `config/settings.py`: `TELEGRAM_BOT_TOKEN`, `BOXTRUCK_BASE_URL`, `BOXTRUCK_INTERNAL_SECRET`, `AGENT_BASE_URL` (atrek), `AGENT_INTERNAL_SECRET`, `OPENAI_API_KEY`.

### 1.4 Current end-to-end flow
Atrek feed → atrek service (persist + SSE) → agent_bot matches & posts offer to driver group → driver replies with a rate → bot records it locally (bug: not forwarded to atrek) → dispatchers were supposed to see it on the bid stream.

---

## 2. What we are building

New flow, end to end:

1. Bot posts load offers to driver groups **including load dimensions** (fetched from atrek `GET /loads/:id`).
2. Driver replies with a rate → bot records a `driver_bid` in atrek → dispatchers see it live on a **Bid Board**.
3. On the Bid Board each driver bid has two actions:
   - **Ignore** → bid moves out of main focus into an "ignored" list.
   - **Bid** → dispatcher enters a company price → system **emails the broker** the bid (via Nylas) and opens a *negotiation*.
4. A new **email-agent service** monitors the broker email thread (Nylas webhooks):
   - For **every inbound broker email**, the AI drafts a suggested reply. Dispatcher can *ignore*, *send as-is*, or *edit & send*. Nothing is ever auto-sent to a broker except the initial bid email the dispatcher explicitly triggered.
   - When the broker sends a **ratecon PDF**, the agent parses it via boxTruck `rate-con-upload`, then **verifies it against the negotiation** (price exact to the cent; locations & dates fuzzy/AI-matched).
   - **Verification passes** → auto-create the load in the TMS (`carrier_pay` = ratecon price, `driver_pay` = driver's bid), attach the ratecon PDF, create stops, notify the driver's Telegram group, notify dispatchers ("booked").
   - **Verification fails** (e.g. agreed $3,200 but ratecon says $3,000) → do **NOT** create the load. Create a suggested reply pointing out the discrepancy and notify dispatchers.
   - **Parsing fails** (unreadable PDF) → notify dispatchers with the raw attachment so a human takes over.
5. Dispatchers receive all realtime events (new suggestion, ratecon result, booked, parse failure) over an SSE stream from the email-agent, mirroring the atrek pattern.

### Architecture decisions (already made — do not revisit)
| Decision | Choice |
|---|---|
| Bid Board (list/ignore driver bids) | **Extend atrek** (owns `load_bids`, SSE, auth) |
| Email + AI negotiation workflow | **New Python service `email-agent/`** (FastAPI + SQLAlchemy + Postgres) |
| Email provider | **Nylas v3** (one grant = one shared dispatch mailbox **per company**) |
| AI provider | **OpenAI** (same key pattern as agent_bot) |
| Ratecon verified OK | **Auto-create load + notify** (no human confirm step) |
| Load money fields | `carrier_pay` = ratecon total, `driver_pay` = driver's Telegram bid |
| Verification strictness | Price **exact**; locations/dates **semantic match** via AI (formatting differences OK) |
| Broker email source | From the Atrek load detail (`GET /loads/:id` contact email); if absent, the dispatcher supplies it in the bid request |
| Realtime to frontend | Email-agent exposes its **own SSE stream** (Django JWT validated locally, same as atrek) |
| Telegram notifications from email-agent | Call the **Telegram Bot API directly** with the same `TELEGRAM_BOT_TOKEN` (agent_bot has no HTTP server; do not add one) |

---

## 3. Phase 1 — agent_bot fixes (small, do first)

1. **Forward driver bids to atrek.** In `agent_bot/agents/loads.py handle_driver_response`, when intent is `accept`/`counter` with an amount, call `agent.place_bid(load_id=offer.load_id, driver_id=driver_id, amount=<amount>, note=<original message>)` in addition to updating the local offer status. `offer.load_id` is the atrek load **UUID** (it comes from the SSE event `data.id`) — confirm and use the UUID, not the third-party integer id.
2. **Fix atrek route access.** `POST /loads/:id/bid` currently sits in the JWT-only group, so the bot's `X-Internal-Secret` call would 401. Move it (and `GET /loads/:id/bids` if needed) into the `AuthenticateOrInternal()` group in `internal/server/routes.go`. For internal calls there is no JWT user: accept `driver_id` + `company_id` from the request body when the caller is internal (the bot knows the driver's company from the boxTruck driver payload — pass it through).
3. **Include dimensions in the Telegram offer.** In `LoadAgent.on_new_load`, before formatting, fetch `GET {atrek}/api/v1/loads/{id}` (internal secret — already allowed) and merge `dims` / `pieces` / `weight` into the load dict. Update `format_load_offer` to show `📐 Dims: L×W×H in` when available (fall back to the existing AI notes extraction if the detail call has no dims). Cache the detail response per load id for the send loop (one fetch per load, not per driver).

**Acceptance:** a driver reply "$1200" produces a `load_bids` row (`action=driver_bid`, `driver_amount=1200`) visible on `GET /api/v1/bids` and broadcast on the bid SSE stream; group messages show dims when the feed provides them.

---

## 4. Phase 2–4 — the `email-agent/` service (new, biggest piece)

Create top-level directory `email-agent/` (Python 3.12, FastAPI, SQLAlchemy 2, Alembic, Postgres, httpx, `nylas` SDK, `openai`). Layout mirrors agent_bot conventions: `config/settings.py` (env-driven), `database/`, `services/`, `routers/`, `main.py`. Single deployable process; use FastAPI `BackgroundTasks`/asyncio workers — no Celery.

### 4.1 Auth
- **Dispatcher endpoints:** validate Django SimpleJWT access tokens locally (HS256, shared `JWT_SECRET`, claims `user_id`, `company`, `department`) — port the logic from `atrek/internal/middleware/auth.go`. Company scoping on every query: a dispatcher only sees their company's data.
- **SSE:** accept `?token=` query param fallback (EventSource can't set headers) — same as atrek.
- **Internal endpoints & outbound calls:** `X-Internal-Secret` header, env `INTERNAL_SECRET_KEY`.
- **Nylas webhooks:** verify the Nylas webhook signature; respond to the challenge request.

### 4.2 Data model (own Postgres DB, e.g. `email_agent`)
- `email_accounts` — `id`, `company_id` (Django company id, unique), `nylas_grant_id`, `email_address`, `status`, timestamps. One shared dispatch mailbox per company.
- `negotiations` — `id` (uuid), `company_id`, `load_uuid` (atrek), `load_snapshot` (jsonb: the full Atrek detail at bid time), `driver_bid_id` (atrek `load_bids.id`), `driver_id`, `driver_amount`, `dispatcher_user_id`, `bid_amount` (what we emailed), `broker_email`, `broker_name`, `nylas_thread_id` (nullable until first send), `status` ∈ {`bid_sent`, `negotiating`, `ratecon_received`, `booked`, `mismatch`, `failed`, `closed`}, `tms_load_id` (nullable), timestamps.
- `email_messages` — `id`, `negotiation_id`, `nylas_message_id` (unique), `direction` ∈ {`inbound`,`outbound`}, `from_email`, `subject`, `body_text` (stripped), `has_attachments`, `sent_by_user_id` (nullable), `created_at`.
- `suggestions` — `id`, `negotiation_id`, `in_reply_to_message_id`, `kind` ∈ {`reply`, `mismatch_reply`, `parse_failure`}, `draft_subject`, `draft_body`, `ai_reasoning`, `status` ∈ {`pending`, `ignored`, `sent`, `edited_sent`}, `final_body` (what was actually sent, nullable), `resolved_by_user_id`, timestamps.
- `ratecon_checks` — `id`, `negotiation_id`, `email_message_id`, `attachment_filename`, `parsed_data` (jsonb), `price_ok` bool, `locations_ok` bool, `dates_ok` bool, `discrepancies` (jsonb list of human-readable strings), `outcome` ∈ {`passed`, `mismatch`, `parse_failed`}, `created_at`.

### 4.3 Dispatcher-facing REST API (all JWT + company-scoped)
```
POST /api/v1/negotiations                      start a bid (the Bid Board "Bid" action)
  body: { load_uuid, driver_bid_id, bid_amount, broker_email? , note? }
  → resolves broker email from stored load snapshot / atrek GET /loads/:id if not given
  → 422 if no broker email can be resolved (frontend then asks the dispatcher)
  → composes the bid email with OpenAI (professional, includes load ref, origin→destination,
    pickup date, vehicle type, offered rate = bid_amount; never mentions the driver's amount)
  → sends via Nylas from the company mailbox, stores thread id, status=bid_sent
  → records action=dispatcher_bid in atrek (internal POST /loads/:id/bid)
GET  /api/v1/negotiations?status=&page=        list (Bid Board "in progress" panel)
GET  /api/v1/negotiations/{id}                 detail: messages, suggestions, ratecon checks
POST /api/v1/negotiations/{id}/close           manual close
GET  /api/v1/suggestions?status=pending        suggestion inbox
POST /api/v1/suggestions/{id}/ignore
POST /api/v1/suggestions/{id}/send             body: { body?, subject? } — if body present ⇒ edited_sent
GET  /api/v1/events/stream                     SSE (see §4.7)
POST /api/v1/accounts/connect                  begin Nylas hosted-auth OAuth for the company mailbox
GET  /api/v1/accounts/callback                 OAuth redirect handler → stores grant
POST /internal/v1/webhooks/nylas               Nylas webhook receiver (signature-verified, not JWT)
```

### 4.4 Inbound email pipeline (Nylas webhook `message.created`)
1. Match `grant_id` → company. Match `thread_id` → negotiation. Unmatched threads: ignore (log only) — this mailbox may receive unrelated mail.
2. Store the `email_messages` row (strip quoted history to plain text for AI use).
3. If the message has PDF attachments **and** OpenAI classifies the email as containing a rate confirmation → run the ratecon pipeline (§4.5). A ratecon email may also get a suggested reply if verification fails; on success no suggestion is needed.
4. Otherwise → generate a **suggested reply** (§4.6) and set negotiation `status=negotiating`.

### 4.5 Ratecon pipeline
1. Download the PDF from Nylas.
2. `POST {boxTruck}/api/billing/rate-con-upload/` (multipart, internal secret) with the company's broker id when a TMS `Broker` matches (match by MC or name from the load snapshot; if no match, create the Broker via `POST /api/billing/brokers/` with name/email from the negotiation — flag `ai_type` default). If the parse endpoint errors → outcome `parse_failed` → suggestion of kind `parse_failure` (no draft body needed; message = "Ratecon received but could not be read — manual review required", attach filename) + SSE event `ratecon_parse_failed`. **Never silently drop a ratecon.**
3. **Verification** against the negotiation:
   - **Price — exact.** The agreed price is `negotiations.bid_amount` *unless a later email in the thread agreed on a different number*: run OpenAI over the stored thread messages to extract the final agreed rate (with confidence). If the model is not confident, fall back to `bid_amount`. Ratecon total must equal the agreed rate to the cent.
   - **Locations & dates — semantic.** Compare parsed pickup/delivery stops (city/state/zip) and date/times against the load snapshot and any changes agreed in the thread. Use OpenAI with a strict JSON verdict schema: `{price_ok, locations_ok, dates_ok, discrepancies: []}`. Formatting differences (abbreviations, "Chicago, IL" vs zip-only) are OK; different city/day/appointment window is a discrepancy.
4. **All OK →** booking (§4.8). **Any discrepancy →** `status=mismatch`, store `ratecon_checks` row, generate a `mismatch_reply` suggestion (draft politely points out the discrepancy, e.g. "the ratecon shows $3,000 but we agreed at $3,200 — please send a corrected ratecon"), SSE event `ratecon_mismatch`. Do **not** create the load. If a corrected ratecon later arrives on the same thread, the pipeline runs again and can still book.

### 4.6 Suggested replies
- OpenAI prompt gets: negotiation context (load facts, our bid, driver amount **excluded**), full thread history, the new inbound message. Output JSON: `{intent, draft_subject, draft_body, reasoning}`. Intents worth distinguishing: `counter_offer`, `accept`, `question`, `ratecon_promise`, `rejection`, `other`.
- Store as `suggestions` row (`kind=reply`), push SSE `suggestion_created`.
- Sending a suggestion replies **on the same Nylas thread**. Record outbound message. Nothing is ever auto-sent.

### 4.7 SSE event stream
`GET /api/v1/events/stream` — company-scoped. Event JSON: `{type, negotiation_id, load_uuid, payload, created_at}`. Types: `suggestion_created`, `ratecon_mismatch`, `ratecon_parse_failed`, `load_booked`, `negotiation_updated`. In-memory hub (single instance) modeled on `atrek/internal/bids/hub.go`; heartbeat comment every 25s.

### 4.8 Booking (verification passed)
All calls to boxTruck use internal secret; make the sequence idempotent (if the negotiation already has `tms_load_id`, skip):
1. `POST /api/billing/loads/` — `company`, `driver` (from negotiation), `broker` (matched/created above), `load_number` (from ratecon parsed data), `carrier_pay` = ratecon total, `driver_pay` = `driver_amount`, `pickup_date` / `drop_date` from parsed stops, `note` = "Booked automatically by email-agent from ratecon".
2. `POST /api/billing/load-stops/` for each parsed stop in order (`load_pickup` / `load_drop` flags, address/city/state/zip, requirements = driver_instructions).
3. Upload the ratecon PDF via `POST /api/billing/load-files/` (`name="RateCon"`).
4. Telegram: `POST https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage` to the driver's `telegram_group_id` (fetch driver via boxTruck `GET /api/hiring/drivers/?...` or store the group id on the negotiation at creation time — prefer storing it): "✅ Load booked! …origin → destination, pickup date/time, your rate $X. Ratecon received — dispatch will follow up with details."
5. Set `status=booked`, store `tms_load_id`, SSE `load_booked`.
6. If any TMS call fails mid-sequence → `status=failed`, SSE `ratecon_parse_failed`-style alert with the error (dispatch finishes manually). Log everything.

### 4.9 boxTruck changes required (keep minimal)
- Add an `InternalSecretOrAuthenticated`-style permission (pattern already in `users/permissions.py`) to: `LoadsViewSet` create, `LoadStopsViewSet` create, `LoadFilesAPIView` post, `RateConfirmationUploadView`, `BrokersViewSet` create/list, `DriverViewSet` list. When called internally, `created_by`/`booked_by` stay null; require an explicit `company` id in the payload.

### 4.11 The bid email template (fixed — do not let the model write this)

Rendered verbatim by `services/bid_email.py`. The numbers are the offer, so they
are never paraphrased by an AI.

```
RATE: $[broker_price]

DIMENSIONS: [dimension]

MILES OUT: [miles]

MC: [company_mc]

VEHICLE: [vehicle_type]

Truck equipment: [equipment]

ALL BIDS ARE VALID [bid_validity_minutes] MINUTES!

[company logo]

[COMPANY NAME]
MC [company_mc]
Address: [company_address]
Phone: [company_phone]
[company_email]

[dispatcher_name]
✉: [company_email]
☎: [company_phone]
```

Placeholder sources:

| Placeholder | Source |
|---|---|
| `broker_price` | the dispatcher's `bid_amount` on the negotiation |
| `dimension` | the **driver's vehicle** cargo box `L" x W" x H" / payload lbs` (what we can carry), not the freight's dims |
| `miles` | deadhead: haversine from the driver's current lat/lng to the load's pickup |
| `vehicle_type`, `equipment` | the driver's vehicle and its `VehicleEquipment` rows (plus `ramps`) |
| company block | `GET /api/billing/internal/company/<id>/` — `name`, `mc`, `address`, `phone_number`, `email`, `logo`, `bid_validity_minutes` |
| `dispatcher_name` | `GET /api/billing/internal/dispatcher/<user_id>/` |

`Company.logo` (ImageField) and `Company.bid_validity_minutes` (default 15) were
added to boxTruck for this; migration `users/0008`.

### 4.10 Env (`email-agent/.env.example`)
```
DATABASE_URL=postgresql+psycopg2://…/email_agent
JWT_SECRET=            # = Django SIMPLE_JWT signing key
INTERNAL_SECRET_KEY=   # shared with atrek/boxTruck callers
BOXTRUCK_BASE_URL=     BOXTRUCK_INTERNAL_SECRET=
ATREK_BASE_URL=        ATREK_INTERNAL_SECRET=
NYLAS_API_KEY=  NYLAS_API_URI=  NYLAS_WEBHOOK_SECRET=  NYLAS_CALLBACK_URI=
OPENAI_API_KEY=
TELEGRAM_BOT_TOKEN=    # same bot as agent_bot
```

---

## 5. Phase 5 — atrek Bid Board extensions

The Bid Board frontend reads driver bids from atrek and negotiation state from email-agent. atrek needs:
1. **Ignore action.** New `action` value `ignored` on `load_bids` referencing the driver bid (or an `ignored_at`/`ignored_by` pair of columns on the bid row — pick columns, simpler). Endpoints:
   - `POST /api/v1/bids/:bid_id/ignore` (JWT; company-checked)
   - `POST /api/v1/bids/:bid_id/unignore`
2. **Bid Board list.** Extend `GET /api/v1/bids` filtering: `?action=driver_bid&ignored=false` (main focus), `?ignored=true` (ignored panel). Include on each row the joined load summary (already available via `GetLoadsByUUIDs`) and `driver_amount`.
3. **Internal bid recording** (from Phase 1 fix + email-agent's `dispatcher_bid` recording): body may carry `company_id`/`user_id` when internal.
4. Broadcast `bid_ignored` / `dispatcher_bid` events on the existing bid SSE hub so the board updates live.

The "Bid" button flow for frontend: click → modal asks price (and broker email only if email-agent returned 422) → `POST {email-agent}/api/v1/negotiations`. Everything after that is email-agent territory.

---

## 6. Phase 6 — Frontend documentation (deliverable)

Write `FRONTEND_API_GUIDE.md` at repo root covering, with request/response JSON examples and auth notes (Django JWT everywhere, `?token=` for SSE):
1. **Bid Board page:** atrek `GET /bids` (focused + ignored tabs), ignore/unignore, bid SSE stream; the Bid modal → email-agent `POST /negotiations` (incl. the 422 broker-email flow).
2. **Negotiations / inbox:** list + detail (thread view: messages, suggestions inline, ratecon checks with discrepancy list), suggestion actions (ignore / send / edit & send).
3. **Realtime:** the email-agent SSE event catalog with payload examples and the UX expected for each (`suggestion_created` → badge + inbox row; `ratecon_mismatch` / `ratecon_parse_failed` → alert-level notification; `load_booked` → success toast, move card to Booked).
4. **Mailbox connect:** admin-only page hitting `POST /accounts/connect` → redirect to Nylas hosted auth.

---

## 7. Non-negotiable rules for the implementer

1. **Never auto-send email to a broker** except the initial bid email explicitly triggered by a dispatcher. Every other outbound email goes through the suggestion approve/edit flow.
2. **Never create a TMS load when verification fails** — price mismatch to the cent blocks booking, no exceptions, no tolerance.
3. **Never silently swallow a failure** in the ratecon or booking pipeline — every failure ends in a dispatcher-visible SSE event + stored record.
4. **Company scoping on every dispatcher-facing query** in every service. A JWT's `company` claim bounds everything.
5. Don't leak the driver's amount into broker-facing emails or AI drafts.
6. All AI calls use structured JSON outputs with schemas; on malformed AI output, retry once, then treat as low-confidence/failure — never guess.
7. Follow each codebase's existing conventions (agent_bot's service/repo layout for email-agent; atrek's dto/handler/repo/service split for Go changes; existing DRF patterns in boxTruck). No framework swaps, no dependency churn beyond what §4 lists.
8. Migrations: Alembic for email-agent; GORM automigrate for atrek columns; Django migrations for permission changes only (no boxTruck schema changes expected).
9. Write tests for: bid → negotiation creation, webhook → suggestion, ratecon verify pass/mismatch/parse-fail, booking idempotency, and JWT/company scoping.

## 8. Suggested build order

1. Phase 1 (agent_bot + atrek route fix) — small, unblocks real driver bids.
2. Phase 5 (atrek ignore/list) — Bid Board data ready.
3. Phase 2 (email-agent skeleton: auth, models, Nylas connect, negotiation create + bid email send).
4. Phase 3 (webhook pipeline + suggestions + SSE).
5. Phase 4 (ratecon verify + booking + Telegram notify + boxTruck internal permissions).
6. Phase 6 (frontend docs).
