# Box Truck TMS — Feature Reference

A complete description of what this system does: fleet and back-office management
for a box-truck carrier, plus an automated **bid-to-book** pipeline in which AI
offers loads to drivers, negotiates with brokers over email, fact-checks the rate
confirmation, and creates the load in the TMS without anyone re-keying it.

The guiding principle of the automated half: **humans make decisions, the system
does the work.** A dispatcher decides *ignore or bid*, *what price*, and *does this
drafted email go out*. Everything around those three decisions is automated.

---

## 1. System shape

Four services, each owning one thing, sharing one JWT.

```
                    third-party load feed (Atrek)
                              │  websocket
                              ▼
   ┌──────────────────────────────────────────────┐
   │  atrek/        Go · Gin · GORM · Postgres     │  loads, bids, bid board,
   │                                               │  SSE fan-out
   └───────┬───────────────────────────┬───────────┘
           │ SSE load feed             │ bid records
           ▼                           ▲
   ┌────────────────────┐      ┌───────┴────────────────────────┐
   │  agent_bot/        │      │  email-agent/                  │
   │  Python · PTB v20  │      │  Python · FastAPI · SQLAlchemy │
   │  Telegram + OpenAI │      │  Nylas + OpenAI                │
   │                    │      │                                │
   │  matches loads to  │      │  bid emails, AI reply drafts,  │
   │  drivers, reads    │      │  ratecon verification, booking │
   │  their replies     │      │                                │
   └─────────┬──────────┘      └───────────────┬────────────────┘
             │                                 │
             │        internal HTTP (X-Internal-Secret)
             ▼                                 ▼
   ┌──────────────────────────────────────────────────────────┐
   │  boxTruck/     Django · DRF · SimpleJWT · Celery          │
   │  SYSTEM OF RECORD                                        │
   │  users · hiring · billing · payroll · dispatchers ·       │
   │  mobile · analytics · ai                                 │
   └──────────────────────────────────────────────────────────┘
```

| Service | Stack | Owns |
|---|---|---|
| `boxTruck/` | Django 5 + DRF + SimpleJWT + Celery + Postgres | Loads, drivers, vehicles, brokers, payroll, invoicing, factoring, analytics. The system of record. |
| `atrek/` | Go + Gin + GORM + Postgres | The live load feed, `load_bids`, bid-board state, SSE broadcast. |
| `agent_bot/` | Python + python-telegram-bot v20 + OpenAI | Driver-facing Telegram agent: load offers, bid capture, location check-ins, preferences. |
| `email-agent/` | Python 3.12 + FastAPI + SQLAlchemy 2 + Nylas v3 + OpenAI | Broker-facing email agent: bid emails, negotiation, ratecon verification, auto-booking. |

**One identity everywhere.** Django issues the JWT (`user_id`, `department`,
`company`). atrek and email-agent validate it locally against the shared HS256
signing key — no callback to Django. Service-to-service calls use a shared
`X-Internal-Secret` header. Every dispatcher-facing query in every service is
bounded by the JWT's `company` claim; a cross-company read returns `404`, not `403`.

---

## 2. The bid-to-book pipeline

The headline feature. Nine stages, from a load appearing on a feed to a booked
load in the TMS and a driver told about it.

```
 ①  load appears on the feed
 ②  AI matches it to nearby drivers → offer posted to each driver's Telegram group
 ③  driver replies with a rate → AI reads it → driver bid recorded
 ④  BID BOARD — dispatcher decides: Ignore, or Bid at $X          ◄── HUMAN
 ⑤  fixed-template bid email sent to the broker
 ⑥  broker replies → AI drafts a reply → dispatcher approves      ◄── HUMAN
 ⑦  broker sends the rate confirmation → AI parses and fact-checks it
 ⑧  verified → load, stops and PDF created in the TMS; driver notified
 ⑨  every step streams to the dispatcher UI over SSE
```

---

### Stage ① — Load ingestion (`atrek/`)

- Consumes a third-party load-board websocket feed and persists every event to
  Postgres (`loads`, `load_events`).
- Rebroadcasts to authenticated clients over SSE: `GET /api/v1/loads/stream`.
- `GET /api/v1/loads/:id` proxies the upstream detail API for the full payload —
  freight dimensions (`dims`), `pieces`, `weight`, and the posting contact's
  email address. atrek merges its own stored geography into the response wherever
  upstream omits coordinates, so deadhead is always computable.

---

### Stage ② — AI offers the load to matching drivers (`agent_bot/`)

The bot subscribes to atrek's SSE load stream. For each new load:

1. **Find candidates.** `GET {boxTruck}/api/hiring/drivers-nearby/?zip=<pickup>&radius=50`
   — drivers within 50 miles of the pickup zip.
2. **Enrich the load.** The SSE event omits dimensions, so the bot fetches
   `GET {atrek}/api/v1/loads/{uuid}` **once per load** (not per driver) and merges
   `dims` / `pieces` / `weight` / `notes` in.
3. **Match each driver** (`services/matching.py`) — all of these must pass:

   | Check | Rule |
   |---|---|
   | Vehicle type | Load's `suggested_truck` must equal the driver's `vehicle_type`, normalized (`CARGO VAN` → `Cargo Van`, `SMALL STRAIGHT`, `LARGE STRAIGHT`) |
   | Deadhead | Haversine from the driver's last known lat/lng to the pickup ≤ **50 mi** |
   | Length | `dims[0] × pieces` ≤ vehicle `length` |
   | Width / Height | `dims[1]` ≤ vehicle `width`, `dims[2]` ≤ vehicle `height` |
   | Weight | load `weight` ≤ vehicle `payload` |
   | Availability | driver preference `available` must not be false |
   | Local-only | if `local_only`, load miles ≤ 100 |
   | Max miles | load miles ≤ the driver's stated `max_miles` |

   **AI fallback on dimensions:** when the feed has no `dims` (or `[0,0,0]`) but
   carries broker notes, `ai.extract_dims_from_notes()` reads the freight
   dimensions, piece count and weight out of the free text and the fit check runs
   on those. If the vehicle itself has no recorded dimensions, the dimension check
   is skipped rather than guessed at.

4. **Post the offer** to the driver's own Telegram group (`Driver.telegram_group_id`):

```
👤 John Doe

🚛 New Load Match

📍 Pick-up: Chicago, IL
📍 Delivery: Detroit, MI
📅 Pick-up: 2026-08-01 09:00 (Chicago, IL)
📅 Delivery: 2026-08-02 14:00 (Detroit, MI)
🚗 Vehicle: Large Straight
📏 Miles: 283
📐 Dimensions: 96" × 48" × 60"
📦 Pieces: 4 | Weight: 2,400 lbs
🏁 Dead head: 12.4 miles

💰 Interested? Reply to this message with your rate (e.g. $1200)
```

   Stop times are shown as local wall-clock time with the place they apply to —
   never converted — because the driver is reading them.

5. **Record a pending offer** locally with a **30-minute expiry** and the Telegram
   message id, so the reply can be tied back to the exact offer. A driver is never
   sent the same active load twice.

---

### Stage ③ — AI reads the driver's reply and records the bid

Driver groups are working chat rooms, so the bot is deliberately narrow. It acts on
exactly two kinds of message:

- **A Telegram *reply* to one of its own load offers** → treated as a bid.
- **A standalone message containing a zip code** → a location check-in.

Everything else is ignored in silence. The bid classifier only ever runs on a reply
to an offer, so ordinary chatter can never be read as a bid or a decline.

**Classification** (`ai.classify_offer_reply`, GPT-4o-mini, `temperature=0`,
strict JSON schema):

| Intent | Meaning | Bot response |
|---|---|---|
| `bid` | Driver states a price. `amount` is that number. | Records the bid |
| `interested` | Accepts but names no price ("yes", "I'll take it") | Asks "What rate do you want for this load?" |
| `decline` | Clearly turns it down | Silence |
| `other` | Questions, chatter, greetings, tagging a colleague | Silence |

Guardrails: *a bare number is a price; never invent an amount; when in doubt use
`other`.* A `bid` whose amount is missing or non-positive is downgraded to
`interested`. If the model call fails outright, the bot does nothing rather than
guessing.

On a valid bid the bot calls `POST {atrek}/api/v1/loads/{uuid}/bid` with the
driver's company id, recording `action=driver_bid` with `driver_amount`, and
confirms to the driver: *"✅ Got your rate: $1,200 — your dispatcher will review."*
If atrek can't be reached, the driver is told to contact dispatch directly rather
than being left believing the bid went through.

**Two more driver-facing agents** run in the same bot:

- **Location agent** — a bare 5-digit zip is handled without an AI call; anything
  else goes through `is_location_message` → `extract_location` (zip/city/state) →
  `PATCH` the driver's location in the TMS. This is what keeps deadhead accurate.
- **Preference agent** — reads free-text availability from the driver
  ("local only today max 100 miles", "off today", "available again") into
  structured preferences with a scope-based expiry: `today` → midnight Chicago
  time, `week` → Sunday midnight, `until_further_notice` → no expiry.

---

### Stage ④ — The Bid Board (dispatcher decision #1 and #2)

Driver bids land live on a dispatcher board backed by atrek.

```
GET  {atrek}/api/v1/bids?action=driver_bid&ignored=false   focus list
GET  {atrek}/api/v1/bids?action=driver_bid&ignored=true    ignored panel
POST {atrek}/api/v1/bids/{bid_id}/ignore | /unignore
GET  {atrek}/api/v1/loads/bids/stream?token=<jwt>          live SSE
```

Each row carries the driver, the amount they want, the note they wrote, and a
joined load summary (origin, destination, vehicle type, miles, dates, broker).
Filters: pickup state, delivery state, brokerage, dispatcher, driver.

- **Ignore** → the bid moves out of focus into the ignored panel. Reversible.
- **Bid** → a modal asks the company's price → starts a negotiation (Stage ⑤).

Per-company load colouring (white / grey / red / green) tracks whether anyone at
the company has viewed the load, a driver has bid, or a dispatcher has bid.
Every ignore, driver bid and dispatcher bid broadcasts on the bid SSE hub, so
every open board updates itself.

---

### Stage ⑤ — The bid email (the only auto-sent email)

`POST {email-agent}/api/v1/negotiations` with `{load_uuid, bid_amount, driver_bid_id,
driver_id, driver_amount, broker_email?}`.

What it does, in order:

1. Confirms the company has a connected dispatch mailbox — else `409 mailbox_not_connected`.
2. Refuses a duplicate — `409 already_open` if a live negotiation exists on this load.
3. Fetches the live load detail from atrek and snapshots it onto the negotiation
   (so later verification compares against the load *as it was at bid time*).
4. Resolves the broker's email from the load's `contact_email` (several field
   shapes are tried, including a nested `contact` object). If nothing is found,
   `422 broker_email_required` and the UI re-asks the dispatcher.
5. Pulls the company letterhead, the dispatcher's name, and the driver's vehicle
   from the TMS over internal endpoints.
6. Renders the **fixed template** and sends it via Nylas from the company's shared
   dispatch mailbox; stores the Nylas thread id.
7. Records `action=dispatcher_bid` in atrek — the load turns green on the board.

**The template is fixed on purpose. No model writes it**, because the numbers in
it *are* the offer:

```
RATE: $3,200

DIMENSIONS: 288" x 96" x 96" / 12,000 lbs

MILES OUT: 4

MC: 846834

VEHICLE: Large Straight

Truck equipment: Liftgate, Pallet Jack, Ramps: Yes

ALL BIDS ARE VALID 15 MINUTES!

[company logo]

SHIPLUXE LLC
MC 846834
Address: 10921 Reed Hartman Highway STE 323, Cincinnati, OH 45242
Phone: 630-426-3362
operation@shipluxellc.com

Jane Dispatcher
✉: operation@shipluxellc.com
☎: 630-426-3362
```

| Placeholder | Source |
|---|---|
| `RATE` | the dispatcher's `bid_amount` |
| `DIMENSIONS` | the **driver's truck cargo box** `L" x W" x H" / payload lbs` — what we can carry, not the freight's dims |
| `MILES OUT` | deadhead, haversine from the driver's current position to the pickup |
| `VEHICLE`, `Truck equipment` | the driver's vehicle type + its `VehicleEquipment` rows, plus ramps |
| `ALL BIDS ARE VALID N MINUTES` | `Company.bid_validity_minutes` (default 15) |
| letterhead | `Company` — name, MC, address, phone, email, logo |
| signature | the dispatcher who clicked Bid |

Both HTML (with logo) and plain-text bodies are rendered; the plain text is what
the AI later reads as thread history.

---

### Stage ⑥ — AI negotiation over email (dispatcher decision #3)

A Nylas webhook (`message.created`) fires on every broker reply.

1. Match `grant_id` → company, `thread_id` → negotiation. Mail on unrelated threads
   is ignored — the mailbox is a real shared inbox. Duplicate webhook deliveries are
   dropped by `nylas_message_id` uniqueness and a `processed_webhooks` table.
2. Store the message, stripping quoted history (`-----Original Message-----`,
   `From:`, `>` lines, HTML tags) so the model reads only what is new.
3. **Classify** (`ai.classify_inbound`): `intent` ∈ `counter_offer`, `accept`,
   `question`, `ratecon_promise`, `ratecon_attached`, `rejection`, `other`; plus
   `contains_ratecon`, the attachment name, and any `quoted_amount`.
4. If it's a rate confirmation → Stage ⑦. Otherwise → **draft a reply**.

**Drafting** (`ai.draft_reply`) receives the negotiation context, the full thread,
and the new message, and returns `{intent, draft_subject, draft_body, reasoning}`.
The system prompt holds the broker-facing rules:

> Never mention the driver's pay, the driver's own bid, or the carrier's margin.
> Never agree to a rate, cancel, or commit to anything the dispatcher has not stated.
> Short, professional, plain — no marketing language, no emoji.
> Sign off as the dispatch team; do not invent names, phone numbers or addresses.
> If the broker countered below our rate, hold our number and justify it briefly
> using the load facts. If the broker accepted, ask them to send the rate confirmation.

`reasoning` is written for the dispatcher, not the broker — it explains why the
draft says what it says.

The draft becomes a **suggestion** in the dispatcher's inbox with three actions:

```
POST /api/v1/suggestions/{id}/ignore          → status: ignored
POST /api/v1/suggestions/{id}/send   {}       → status: sent        (draft unchanged)
POST /api/v1/suggestions/{id}/send   {body}   → status: edited_sent (dispatcher's text)
```

Sending replies on the same Nylas thread and records the outbound message.
If the model fails to produce a draft, an empty suggestion is still created with
*"The assistant could not draft a reply — please write one"* — the dispatcher is
never left unaware that a broker wrote in.

> **The hard rule:** the initial bid email is the only message this system ever
> sends to a broker on its own. Every other outbound email requires a dispatcher's
> user id, enforced in `services/suggestions.send()` — it is the only other code
> path that touches `nylas.send_message`.

---

### Stage ⑦ — Fact-checking the rate confirmation

The most safety-critical part of the system. A rate confirmation is a contract;
the agent treats a mismatched one as a *hard stop*, not a warning.

```
PDF attachment
   │
   ├─ 1. download from Nylas ─────────────► fail → parse_failed + alert
   │
   ├─ 2. resolve/create the TMS Broker (by MC, then name, then email)
   │
   ├─ 3. parse via the TMS's existing ratecon parser ──► fail → parse_failed + alert
   │       Gemini or GPT per Broker.ai_type, strict schema:
   │       {broker_name, load_number, total_rate_usd,
   │        pickup_addresses[], delivery_locations[], special_instructions}
   │
   ├─ 4. establish the AGREED RATE
   │       ai.extract_agreed_rate(thread) → {agreed_amount, confident, evidence}
   │       confident?  → that number is the contract
   │       not confident? → fall back to the opening bid_amount
   │
   ├─ 5. verify (ai.verify_ratecon, strict JSON verdict)
   │       {price_ok, ratecon_amount, locations_ok, dates_ok, discrepancies[]}
   │
   ├─ 6. RE-DECIDE THE PRICE ARITHMETICALLY — the model's boolean is not trusted
   │       |round(ratecon × 100) − round(agreed × 100)| ≤ PRICE_TOLERANCE_CENTS (= 0)
   │
   └─ 7. price_ok AND locations_ok AND dates_ok ?
           yes → BOOK (Stage ⑧)
           no  → mismatch: no load created, drafted correction reply, alert
```

**Why the arithmetic re-check matters.** The model reads the number off the
document; the *comparison* is done in code with a zero-cent tolerance. An agreed
$3,200 against a $3,000 ratecon can never become a booked load, regardless of what
the model concluded. If the verification call itself fails, that is treated as a
**failure, not a pass**.

**Strict about money, lenient about formatting.** Locations and dates are matched
semantically:

| Difference | Verdict |
|---|---|
| "Chicago, IL" vs "Chicago IL 60609" vs zip-only | fine |
| State abbreviated vs spelled out | fine |
| Added facility name | fine |
| Appointment window vs single time | fine |
| A different city or state | **discrepancy** |
| A stop that appears or disappears | **discrepancy** |
| A different calendar day | **discrepancy** |
| Any cent of price difference | **discrepancy** |

The thread is fed into verification too, so **terms the broker and dispatcher agreed
to change over email** — a moved pickup date, an added stop — are honoured rather
than flagged.

Every check is persisted as a `ratecon_checks` row: the parsed data, the agreed
amount, the ratecon amount, the three booleans, the discrepancy list, and the
outcome (`passed` / `mismatch` / `parse_failed`). Discrepancies are written for a
dispatcher to read verbatim:

> *"Rate confirmation shows $3,000.00 but we agreed $3,200.00."*

**On mismatch:** negotiation goes to `mismatch`, no load is created, and
`ai.draft_mismatch_reply` produces a polite correction request naming every
discrepancy and asking for a corrected ratecon — still requiring the dispatcher to
send it. A corrected ratecon arriving later on the same thread re-runs the whole
pipeline and can still book.

**On parse failure:** a `parse_failure` suggestion (no draft body — the dispatcher
handles it by hand) plus a `ratecon_parse_failed` alert with the filename and the
error. **A rate confirmation is never silently dropped.**

---

### Stage ⑧ — Auto-booking into the TMS

Reachable *only* from the verified path. One call, one transaction, so a partially
created load is impossible.

`POST {boxTruck}/api/billing/internal/book-load/` creates, atomically:

1. The **Load** —
   `carrier_pay` = the verified ratecon total (the agreed rate),
   `driver_pay` = the driver's own Telegram bid,
   `load_number`, `pickup_date` (first pickup stop), `drop_date` (last delivery stop),
   `broker`, `driver`, `company`, `booked_by` = the dispatcher who bid,
   `note` = the ratecon's special instructions,
   `dispatcher_note` = "Booked automatically from a verified rate confirmation".
2. Every **LoadStop** in document order, flagged `load_pickup` / `load_drop`, with
   address, city, state, zip, facility name and per-stop driver instructions.
3. The **ratecon PDF** itself, attached as a `LoadFile`.

Then:

4. **The driver is told, in the Telegram group they already use** — origin →
   destination, pickup date/time, their rate, load number. Sent via the Bot API
   with the same bot token. *A Telegram failure never undoes a booked load*; it
   logs and moves on.
5. Negotiation → `booked`, `tms_load_id` stored, `load_booked` event published.

**Idempotent:** a negotiation that already has a `tms_load_id` is skipped, and the
TMS endpoint reports `already_existed` rather than duplicating.

**If the TMS write fails** after verification passed → negotiation goes to `failed`
with the error stored, and a `booking_failed` alert tells dispatch to create the
load by hand. Nothing is swallowed.

---

### Stage ⑨ — Realtime to the dispatcher

`GET {email-agent}/api/v1/events/stream?token=<jwt>` — company-scoped SSE,
`connected` on open, keep-alive comment every 25 s.

| Event | Fires when | Key payload | Intended UX |
|---|---|---|---|
| `suggestion_created` | a reply was drafted | `suggestion_id`, `kind`, `intent` | badge + prepend inbox card |
| `ratecon_mismatch` | ratecon rejected, **no load created** | `discrepancies[]`, `agreed_amount`, `ratecon_amount` | persistent alert; move to "needs attention" |
| `ratecon_parse_failed` | ratecon unreadable | `filename`, `error` | alert — handle manually |
| `load_booked` | load created in the TMS | `tms_load_id`, `load_number`, `shipment`, `carrier_pay`, `driver_pay` | success toast, link to the load |
| `booking_failed` | verified but the TMS write failed | `error` | error toast — book by hand |
| `negotiation_updated` | status or suggestion state changed | `status` | patch the row in place |

Negotiation statuses the UI renders: `bid_sent` → `negotiating` →
`ratecon_received` → **`booked`** | **`mismatch`** | `failed` | `closed`.

**What the dispatcher never has to do:** write the bid email, chase the broker for
a ratecon, re-key a load into the TMS, or tell the driver they got the load.

---

## 3. Every AI call in the system, and its guardrail

| Where | Call | Model | Output | Guardrail |
|---|---|---|---|---|
| agent_bot | `extract_dims_from_notes` | gpt-4o-mini | dims / pieces / weight | only used when the feed has none; failure → no dimension filter |
| agent_bot | `classify_offer_reply` | gpt-4o-mini, T=0 | strict schema `{intent, amount}` | bid without a positive amount → downgraded; failure → do nothing |
| agent_bot | `is_location_message` / `extract_location` | gpt-4o-mini | zip / city / state | bare zips bypass the model entirely |
| agent_bot | `is_preference_message` / `extract_preferences` | gpt-4o-mini | availability, local_only, max_miles, scope | unparseable → no change |
| email-agent | `classify_inbound` | strict JSON schema, T=0 | intent, `contains_ratecon`, quoted amount | failure → falls back to drafting a reply, never to booking |
| email-agent | `draft_reply` | strict JSON schema | subject, body, reasoning | never auto-sent; driver's amount excluded from context |
| email-agent | `extract_agreed_rate` | strict JSON schema | amount + `confident` + evidence | not confident → falls back to the opening bid |
| email-agent | `verify_ratecon` | strict JSON schema | price/locations/dates + discrepancies | **price re-decided in code**; verdict failure = failure |
| email-agent | `draft_mismatch_reply` | strict JSON schema | correction request | never auto-sent |
| boxTruck | rate confirmation parser | Gemini 3 Flash or GPT per `Broker.ai_type` | Pydantic `RateConfirmationData` | parse error → dispatcher alert, never a silent drop |
| boxTruck | AI data analyst | GPT-4 | SQL + narrative + chart config | SELECT-only regex allowlist, DDL/DML keyword blocklist, company-scoped |

Every email-agent model call goes through one helper that enforces
`response_format: json_schema` with `strict: true`, `temperature=0`, **one retry**,
then failure. The agent never guesses at a number that decides whether a load
gets booked.

---

## 4. Safety rules, and where they live

1. **Only the initial bid email is auto-sent.** Everything else needs a dispatcher's
   user id — `services/suggestions.send()` is the sole other caller of
   `nylas.send_message`.
2. **A mismatched ratecon never becomes a load.** Price is compared arithmetically
   at zero-cent tolerance; `booking.book_verified_load` is unreachable unless
   price, locations and dates all pass.
3. **The agreed rate can move during the thread** — a later agreement, read with
   confidence, becomes the number the ratecon must match. Both the opening bid and
   the agreed amount are stored.
4. **No failure is silent.** Parse failure, verification failure, mismatch and
   booking failure each write a durable row *and* emit an SSE alert.
5. **The driver's amount never reaches the broker.** Enforced in the template
   (which never receives it), in the AI system prompt, and asserted by tests.
6. **Company scoping on every query, in every service.** The JWT's `company` claim
   bounds all reads; cross-company access returns `404`.
7. **Only management can connect a mailbox.** `POST /accounts/connect` requires
   department `Management`; a regular dispatcher gets `403` even for their own
   company. The mailbox sends every bid and receives every broker reply for the
   whole company, so only management can point it.
8. **The bot stays quiet.** The bid classifier runs only on a Telegram reply to an
   actual offer — group chatter can never be read as a bid or a decline.

---

## 5. The rest of the TMS (`boxTruck/`)

The bid-to-book pipeline sits on top of a full carrier back office.

### 5.1 Users, companies, access control

- Multi-company. `Company` carries name, MC, address, phone, email, website,
  `shipment_number` (the load-numbering seed), plus `logo` and
  `bid_validity_minutes` for the bid email letterhead.
- `CustomUser` extends Django auth with `company`, `department`, hire date.
- **Department-based permissions**: `Management`, `Dispatch`, `Dispatch Manager`,
  `Billing`, `Payroll`, `Updater` — plus `IsInternalService` for the
  `X-Internal-Secret` server-to-server calls.
- Invite-based onboarding (`UserInvite` with UUID token + expiry), password reset
  flow, bulk user create/update, JWT issue/refresh with `department` and `company`
  baked into the token.

### 5.2 Hiring — drivers, vehicles, documents

- **Driver**: identity, SSN, DOB, contact + emergency contact, address, status,
  hire/termination dates, driver type and contract, FEIN, manager and referrer,
  medical expiry, full CDL block (number, state, class, endorsements, issue and
  expiration), `telegram_group_id`, and live position (`current_lat/lng`, city,
  state, zip, address).
- **Vehicle**: type, make/model/year, VIN, plate + registration state and expiry,
  insurance company/policy/expiry, `payload`, `gvw`, cargo box `length`/`width`/
  `height`, door-opening dimensions, ramps. Plus `VehicleEquipment` rows (liftgate,
  pallet jack, …) — these feed the bid email.
- **DriverCompany** — the owner-operator's own entity (MC, EIN, DBA, address).
- **Document vaults** for drivers, vehicles and driver companies (`DriverFile`,
  `VehicleFile`, `CompanyFile`).
- **Change history** on drivers, vehicles and driver companies — who changed what,
  when.
- **Onboarding**: HR hiring requests, invite links (UUID token, expiry,
  revocable), bulk driver create/update, **Excel driver import**.
- **Geo**: `drivers-nearby/?zip=&radius=` (Google Maps geocoding + haversine) — the
  query that drives load matching — and per-driver location endpoints.

### 5.3 Billing — loads, brokers, factoring

- **Load**: company, driver, broker, `load_number`, auto-incrementing per-company
  `shipment` number, `driver_pay`, `carrier_pay`, pickup/drop dates, `delivered_at`,
  `loaded_miles`, `empty_miles`, status, payment type, recovery flag, parent load
  (for recovery/relay), notes and dispatcher notes, `booked_by` / `created_by` /
  `updated_by`.
- **LoadStop**: ordered stops with address/city/state/zip, pickup/drop flags,
  trailer pickup/drop, partial, last-location, trailer info, per-stop requirements.
- **LoadFile**: rate confirmations, BOLs, PODs, generated invoices.
- **LoadHistory**: a per-load audit trail.
- **Tags** (`Tag` / `LoadTag`) with colours, for board organisation.
- **Broker**: name, MC (unique), address, phone, email, billing email, notes, and
  `ai_type` — which model parses that broker's rate confirmations. Bulk broker
  import.
- **Rate confirmation upload & parse** — `POST /api/billing/rate-con-upload/`
  returns structured `{load_number, total_rate_usd, pickup_addresses[],
  delivery_locations[], special_instructions}` from a PDF. This is the same parser
  the email-agent calls internally; there is exactly one ratecon parser in the system.
- **Mileage** — loaded and empty miles computed via the PC*MILER API from the
  stop addresses.
- **Batches** (`Batch` / `BatchLoad`) — group delivered loads for billing runs.
- **RTS factoring integration** — generate the factoring CSV, generate per-load
  **invoice PDFs** (ReportLab, per-carrier letterhead), merge the ratecon and POD
  pages, and upload the whole batch to RTS over **FTPS**, with live Telegram
  progress and failure reporting to the billing group.
- **Reporting**: loads by status/process, daily dispatcher report, load pay
  summary, most profitable loads for a date (`carrier_pay − driver_pay`).
- Async work runs on Celery + threads (invoice generation, factoring upload,
  mileage calculation).

### 5.4 Payroll

- **Statement** per driver per week: date range, week number, gross amount, status,
  final flag, generated PDF, and timestamps for when it was sent by Telegram and
  by email.
- **StatementLoad** — which loads make up the statement.
- **Deduction** with typed categories (`DeductionType`), amount, fee, paid flag,
  soft delete, and a change history; attached to statements via
  `StatementDeduction`.
- Active/inactive driver lists, deduction statistics, dropdowns for statement
  building.

### 5.5 Dispatcher compensation

- **DispatchSalary** — commission tiers: percentage of profit between a min and max
  amount, per company.
- **DispatchBonus** — bonus amount for hitting a daily profit band.
- Endpoints for a manager to see all dispatcher salaries, for a dispatcher to see
  their own, and for dispatcher targets.

### 5.6 Driver mobile app API

- **Passwordless auth**: check phone → OTP by SMS or email → verify → issue a
  driver token (UUID, expiring, refreshable). A driver working for more than one
  company picks which one to sign in as.
- **Location reporting** — the app posts GPS fixes (`DriverLocation` with device),
  which keeps `Driver.current_latitude/longitude` fresh. This is the same position
  used for load matching and the bid email's `MILES OUT`.
- **Active load** view with stops and documents.
- **Earnings dashboard** — total earnings, loads completed, miles driven and rate
  per mile for the week / month / year, with a chart series for each period.

### 5.7 Analytics

- Load summary and revenue analytics, load-status breakdowns, weekly driver
  performance, aggregate load summaries — all company-scoped.

### 5.8 AI data analyst (natural-language reporting)

Separate from the bid pipeline: an in-app chat that answers questions about the
company's own data.

- The full database schema is described to GPT-4, which writes SQL, runs it
  read-only, and returns a narrative answer plus a chart config.
- **Safety**: statements must match `^SELECT`; `INSERT|UPDATE|DELETE|DROP|ALTER|
  TRUNCATE|CREATE|REPLACE|EXEC|GRANT|REVOKE` are rejected outright.
- **Scoping**: Management, Billing and Payroll see all companies; everyone else's
  queries are forced to their own `company_id`.
- Conversations and messages persist with the SQL used, the result set and the
  chart config, so a follow-up question keeps context.

---

## 6. Data model reference

**boxTruck (Postgres)** — `companies`, `departments`, `users`, `user_invites`,
`reset_passwords` · `drivers`, `driver_statuses`, `driver_files`, `driver_companies`,
`company_files`, `vehicles`, `vehicle_equipments`, `vehicle_files`,
`driver_invite_links`, `driver_histories`, `vehicle_histories`, `company_histories` ·
`loads`, `load_stops`, `load_files`, `load_statuses`, `load_history`, `brokers`,
`batches`, `batch_loads`, `payment_types`, `tags`, `load_tags` · `statements`,
`statement_loads`, `statement_deductions`, `statement_statuses`, `deductions`,
`deduction_types`, `deduction_histories` · `dispatch_salaries`, `dispatch_bonuses` ·
`driver_otps`, `driver_auth_tokens`, `driver_locations` · `ai_conversations`,
`ai_messages`.

**atrek (Postgres)** — `loads`, `load_events`, `load_bids`
(`load_id`, `driver_id`, `user_id`, `company_id`, `action` ∈ `viewed` |
`driver_bid` | `dispatcher_bid`, `bid_amount`, `driver_amount`, `note`,
`ignored_at`, `ignored_by`).

**agent_bot (Postgres)** — `driver_preferences` (available, local_only, max_miles,
note, expiry), `pending_offers` (driver, load uuid, load snapshot, telegram message
id, status, 30-minute expiry).

**email-agent (Postgres)** — `email_accounts` (one Nylas grant per company) ·
`negotiations` (load uuid + snapshot, driver bid id, driver id/amount/telegram
group, dispatcher, `bid_amount`, `agreed_amount`, broker email/name/MC, Nylas
thread id, status, `tms_load_id`, failure reason) · `email_messages` ·
`suggestions` (kind, intent, draft subject/body, AI reasoning, status, final body,
resolver) · `ratecon_checks` (parsed data, agreed vs ratecon amount, three
booleans, discrepancies, outcome, error) · `processed_webhooks`.

---

## 7. Integrations

| Integration | Used for |
|---|---|
| Atrek load board (websocket + HTTP) | live available loads and full load detail |
| Telegram Bot API | driver load offers, bid capture, booking notices, billing/ops alerts |
| Nylas v3 | the shared per-company dispatch mailbox — send, receive webhooks, download attachments |
| OpenAI | driver reply classification, email classification and drafting, ratecon verification, the SQL analyst |
| Google Gemini | rate confirmation PDF parsing (per broker, via `Broker.ai_type`) |
| Google Maps | geocoding for driver locations and the nearby-driver search |
| PC*MILER | loaded and empty mileage from stop addresses |
| RTS Financial (FTPS) | factoring — invoice + ratecon + POD packets and the batch CSV |
| Twilio / SMTP | driver OTP delivery, statement email |

---

## 8. Deployment

All four services run on one host behind nginx at `boxmanage.smartfleetllc.com`,
under supervisor.

| Service | Path | Process | Route |
|---|---|---|---|
| boxTruck | `/home/api/boxTruck` | `box`, `celery` | `/api/` |
| atrek | `/home/api/agent` | `agent` (:8080) | `/api/v1/` |
| agent_bot | `/home/api/agent_bot` | `agent_bot` | — (outbound only) |
| email-agent | `/home/api/email-agent` | `email_agent` (:8100) | `/email-agent/` |

The email-agent nginx location disables buffering and allows 3600 s reads so SSE
works. `EMAIL_AGENT_BASE_URL` for the frontend is
`https://boxmanage.smartfleetllc.com/email-agent`.

---

## 9. Status and known limits

**Built and verified** (2026-07-23, hardened through 2026-07-29): all nine
pipeline stages, 80 email-agent tests against a real Postgres with Nylas, OpenAI,
the TMS and Telegram stubbed — including the canonical case (agreed $3,200, ratecon
says $3,000 → no load, status `mismatch`, drafted correction, driver not notified),
location and date mismatches, unreadable PDFs, a failed AI verdict treated as a
failure, booking idempotency, and TMS failure. atrek builds and vets clean;
boxTruck's migrations are consistent.

**Configuration still required before the email half runs in production:**

1. **Nylas credentials** (`NYLAS_API_KEY`, `NYLAS_CLIENT_ID`, `NYLAS_WEBHOOK_SECRET`)
   — the blocker. Until they are set, bidding returns `mailbox_not_connected`.
2. **Company letterhead** — MC, phone, email and logo, or those lines render blank
   in the bid email.
3. **`Company.shipment_number`** must be set for any company that books loads —
   `Load.save()` derives the shipment number from it.
4. **Vehicle equipment rows** — otherwise `Truck equipment` renders `N/A`.

**Known limitations:**

- **Single instance.** Both SSE hubs are in-process. Replicas need Redis pub/sub
  behind `services/events.py`; the publish/subscribe surface is already isolated
  for that swap.
- **No event replay.** Events emitted while a dispatcher is disconnected are lost —
  the frontend refetches lists on reconnect.
- **First PDF wins** on a ratecon email. A broker sending the ratecon plus other
  PDFs in one message has the first one parsed.
- **`agent_bot` has no test suite**; its behaviour is covered by review only.

---

## 10. Related documents

- [`BID_TO_BOOK_BUILD_PLAN.md`](BID_TO_BOOK_BUILD_PLAN.md) — the original design and
  the non-negotiable implementation rules.
- [`IMPLEMENTATION_NOTES.md`](IMPLEMENTATION_NOTES.md) — what was built, deviations
  from the plan, where each safety rule is enforced, deployment record.
- [`FRONTEND_API_GUIDE.md`](FRONTEND_API_GUIDE.md) — the frontend contract: every
  endpoint, the error codes that change UI behaviour, and the SSE event catalog.
- [`email-agent/README.md`](email-agent/README.md) · [`atrek/README.md`](atrek/README.md)
