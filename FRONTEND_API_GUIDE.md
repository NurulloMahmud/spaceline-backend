# Frontend API Guide — Bid Board & Broker Negotiations

Everything the frontend needs to build the two new screens: the **Bid Board**
(driver bids, ignore/bid actions) and the **Negotiations inbox** (broker email
threads, AI reply suggestions, ratecon results).

Two backends are involved. Both accept the same Django JWT.

| Service | Base URL | Owns |
|---|---|---|
| **atrek** | `ATREK_BASE_URL` | loads, driver bids, dispatcher bids, ignore state |
| **email-agent** | `EMAIL_AGENT_BASE_URL` | negotiations, broker emails, suggestions, ratecon results |

## Authentication

Send the Django access token on every request:

```
Authorization: Bearer <access token from POST /api/token/>
```

Both services validate the token locally against Django's signing key. A `401`
means the token is invalid or expired — refresh via `POST /api/token/refresh/`.
A `403` from email-agent means the user's account has no company attached.

**SSE endpoints cannot use headers** (`EventSource` does not support them), so
they accept the token as a query parameter instead: `?token=<access token>`.

Every response is scoped to the caller's company. There is no cross-company
read; a negotiation from another company returns `404`, not `403`.

---

# 1. Bid Board (atrek)

The board shows driver bids that came in from the Telegram bot. Each bid has two
actions: **Ignore** (moves it out of focus) and **Bid** (opens the price dialog
and starts a broker negotiation).

## 1.1 List driver bids

```
GET {atrek}/api/v1/bids?action=driver_bid&ignored=false&page=1&limit=20
```

Query parameters:

| Param | Values | Notes |
|---|---|---|
| `action` | `driver_bid`, `dispatcher_bid`, `viewed` | defaults to `dispatcher_bid` for backwards compatibility — **the bid board must pass `driver_bid` explicitly** |
| `ignored` | `true`, `false` | omit to get both; `false` is the main focus list, `true` is the ignored panel |
| `page`, `limit` | integers | `limit` defaults to 20 |
| `pick_up_state`, `deliver_state`, `brokerage`, `dispatcher`, `driver` | free text | partial, case-insensitive matches |

```json
{
  "success": true,
  "data": [
    {
      "id": "9f1c...",
      "action": "driver_bid",
      "created_at": "2026-07-23T14:02:11Z",
      "bid_amount": 2400,
      "driver_amount": 2400,
      "note": "I can do 2400",
      "dispatcher": null,
      "driver": { "id": 42, "full_name": "John Doe" },
      "driver_bid": 2400,
      "ignored": false,
      "ignored_at": null,
      "load": {
        "id": "load-uuid-1",
        "load_id": 55012,
        "pick_up_at": "Chicago, IL",
        "deliver_to": "Detroit, MI",
        "vehicle_type": "Large Straight",
        "miles": 283,
        "source_name": "Atrek",
        "contact_name": "Acme Logistics",
        "pick_up_date": "2026-08-01T09:00:00Z",
        "delivery_date": "2026-08-02T14:00:00Z"
      }
    }
  ],
  "metadata": { "current_page": 1, "page_size": 20, "total_records": 37, "last_page": 2 }
}
```

`load.id` is the **load UUID** you pass to email-agent as `load_uuid`.
The row `id` is the **bid id** used for ignore/unignore and as `driver_bid_id`.

## 1.2 Ignore / un-ignore a bid

```
POST {atrek}/api/v1/bids/{bid_id}/ignore
POST {atrek}/api/v1/bids/{bid_id}/unignore
```

No body. Returns `{"success": true, "data": {"id": "...", "load_id": "...", "ignored": true}}`.
`403` means the bid belongs to another company; `404` means it does not exist.

Both actions broadcast on the bid stream, so other open boards update themselves.

## 1.3 Bid stream (SSE)

```
GET {atrek}/api/v1/loads/bids/stream?token=<access token>
```

Events arrive under the event name `bid`:

```json
{
  "type": "bid",
  "load_id": "load-uuid-1",
  "company_id": 1,
  "action": "driver_bid",
  "color": "red",
  "amount": 2400,
  "bid_id": "9f1c..."
}
```

`type` is one of `bid`, `bid_ignored`, `bid_unignored`. `action` distinguishes
`driver_bid` from `dispatcher_bid`. On `bid` + `driver_bid`, prepend the row to
the focus list. On `bid_ignored`, move that row to the ignored panel.

## 1.4 Load detail

```
GET {atrek}/api/v1/loads/{load_uuid}
```

The full third-party payload, including `dims`, `pieces`, `weight` and the
posting contact's email — useful for the bid dialog preview.

---

# 2. Starting a bid (email-agent)

This is what the **Bid** button does after the dispatcher enters a price.

```
POST {email-agent}/api/v1/negotiations
```

```json
{
  "load_uuid": "load-uuid-1",
  "bid_amount": 3200,
  "driver_bid_id": "9f1c...",
  "driver_id": 42,
  "driver_amount": 2400,
  "broker_email": "broker@acme.com",
  "note": ""
}
```

Only `load_uuid` and `bid_amount` are required, but pass `driver_id`,
`driver_amount` and `driver_bid_id` from the bid row whenever you have them —
they populate the truck's dimensions and equipment in the email, and the
driver's pay when the load is eventually booked.

**`201`** returns the negotiation (see §3.1). The bid email is already sent.

**Error handling** — the `detail` object carries a stable `code`:

| Status | `code` | What the UI should do |
|---|---|---|
| 422 | `broker_email_required` | The load has no contact email. Re-open the dialog with an email field and resubmit with `broker_email`. |
| 409 | `mailbox_not_connected` | This company has no dispatch mailbox. Link to the mailbox settings page (§5). |
| 409 | `already_open` | A negotiation is already running on this load. Navigate to it instead. |
| 502 | `load_unavailable` / `send_failed` | Transient — show the message and let them retry. |

```json
{ "detail": { "error": "This load has no broker email address...", "code": "broker_email_required" } }
```

## The email that gets sent

Fixed template, not AI-written. Rendered from the company profile, the
dispatcher, the driver's vehicle, and the price the dispatcher typed:

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

The letterhead, logo and bid validity come from the **Company** record in the
TMS (`logo`, `bid_validity_minutes`, `name`, `mc`, `address`, `phone_number`,
`email`). If a company has no logo or MC set, those lines render empty — worth
surfacing in company settings.

---

# 3. Negotiations inbox (email-agent)

## 3.1 List

```
GET {email-agent}/api/v1/negotiations?status=bid_sent,negotiating&page=1&limit=20
```

```json
{
  "items": [
    {
      "id": "6b2e...",
      "company_id": 1,
      "load_uuid": "load-uuid-1",
      "driver_id": 42,
      "driver_amount": 2400,
      "dispatcher_user_id": 7,
      "bid_amount": 3200,
      "agreed_amount": null,
      "broker_email": "broker@acme.com",
      "broker_name": "Acme Logistics",
      "subject": "Bid — Chicago, IL to Detroit, MI (Ref 55012)",
      "status": "negotiating",
      "tms_load_id": null,
      "failure_reason": null,
      "created_at": "2026-07-23T14:05:00Z",
      "updated_at": "2026-07-23T14:41:00Z",
      "pending_suggestions": 1
    }
  ],
  "page": 1, "limit": 20, "total": 6
}
```

`status` values and what they mean to the user:

| Status | Meaning | Suggested UI |
|---|---|---|
| `bid_sent` | Bid emailed, no reply yet | neutral |
| `negotiating` | Broker replied; may have a pending suggestion | neutral, badge if `pending_suggestions > 0` |
| `ratecon_received` | A ratecon is being verified right now | spinner / "checking" |
| `mismatch` | Ratecon did **not** match — no load created | **warning**, needs attention |
| `booked` | Load created in the TMS | success, link to `tms_load_id` |
| `failed` | Booking failed after verification passed | **error**, needs manual booking |
| `closed` | Closed by a dispatcher | muted |

`pending_suggestions` drives the "needs you" badge.

## 3.2 Detail (thread view)

```
GET {email-agent}/api/v1/negotiations/{id}
```

Returns everything in §3.1 plus:

- `load_snapshot` — the load as it was when the bid went out
- `messages[]` — the thread, oldest first: `{id, direction: "inbound"|"outbound", from_email, to_email, subject, body_text, has_attachments, attachments, sent_by_user_id, created_at}`
- `suggestions[]` — see §4
- `ratecon_checks[]` — see §3.3

Render `messages` as a conversation and attach each pending suggestion under the
message it replies to (`suggestion.in_reply_to_message_id` matches `message.id`).

## 3.3 Ratecon check results

Each entry in `ratecon_checks[]`:

```json
{
  "id": "c1...",
  "attachment_filename": "ratecon.pdf",
  "agreed_amount": 3200,
  "ratecon_amount": 3000,
  "price_ok": false,
  "locations_ok": true,
  "dates_ok": true,
  "discrepancies": ["Rate confirmation shows $3,000.00 but we agreed $3,200.00."],
  "outcome": "mismatch",
  "error": null,
  "parsed_data": { "load_number": "ACME-99871", "total_rate_usd": 3000, "pickup_addresses": [], "delivery_locations": [] },
  "created_at": "2026-07-23T15:10:00Z"
}
```

`outcome` is `passed`, `mismatch`, or `parse_failed`. Show `discrepancies` as a
prominent list — it is written for a dispatcher to read verbatim. On
`parse_failed`, `error` explains what went wrong and the dispatcher must handle
the ratecon manually.

## 3.4 Close

```
POST {email-agent}/api/v1/negotiations/{id}/close
```

`409` if the negotiation is already `booked`.

---

# 4. Suggestions (email-agent)

A suggestion is a drafted reply or an alert awaiting a decision. **Nothing is
ever sent to a broker unless a dispatcher sends it here.**

## 4.1 The inbox

```
GET {email-agent}/api/v1/suggestions?status=pending&page=1&limit=20
```

```json
{
  "items": [
    {
      "id": "a7...",
      "negotiation_id": "6b2e...",
      "kind": "reply",
      "intent": "counter_offer",
      "draft_subject": "Re: Bid — Chicago, IL to Detroit, MI",
      "draft_body": "Thanks for coming back to us. We're holding at $3,200 on this one...",
      "ai_reasoning": "The broker countered at $2,900. Holding the rate.",
      "status": "pending",
      "final_body": null,
      "resolved_by_user_id": null,
      "created_at": "2026-07-23T14:41:00Z",
      "negotiation": { "...": "the parent negotiation, as in §3.1" }
    }
  ],
  "page": 1, "limit": 20, "total": 3
}
```

`kind` decides how the card looks:

| `kind` | Meaning | UI |
|---|---|---|
| `reply` | Normal drafted reply to a broker message | editable draft + Send / Edit & Send / Ignore |
| `mismatch_reply` | Ratecon didn't match; draft asks for a corrected one | **warning** styling, show the discrepancy list from the negotiation's latest ratecon check |
| `parse_failure` | Ratecon could not be read at all | **alert**, no draft body — `ai_reasoning` explains it. The dispatcher opens the email and books manually. |

`intent` (on `reply`) is one of `counter_offer`, `accept`, `question`,
`ratecon_promise`, `rejection`, `other` — useful as a chip on the card.

`status` is `pending`, `ignored`, `sent`, or `edited_sent`.

## 4.2 Ignore

```
POST {email-agent}/api/v1/suggestions/{id}/ignore
```

`409` if it was already resolved.

## 4.3 Send / Edit & send

```
POST {email-agent}/api/v1/suggestions/{id}/send
```

```json
{ "body": "optional edited text", "subject": "optional" }
```

Send `{}` to send the draft unchanged (results in `status: "sent"`). Include
`body` to send edited text (results in `status: "edited_sent"`). The reply goes
out on the same email thread automatically.

| Status | `code` | Meaning |
|---|---|---|
| 409 | `already_resolved` | Someone else handled it — refresh |
| 409 | `mailbox_not_connected` | Mailbox was disconnected |
| 422 | `empty_body` | Nothing to send (typical for `parse_failure` cards — hide the Send button on those) |
| 502 | `send_failed` | Nylas rejected it; show the message and let them retry |

---

# 5. Mailbox connection (email-agent)

One shared dispatch mailbox per company. Management-only screen.

```
GET  {email-agent}/api/v1/accounts          → {"connected": true, "email_address": "...", "status": "active", "connected_at": "..."}
POST {email-agent}/api/v1/accounts/connect  → {"auth_url": "https://api.us.nylas.com/v3/connect/auth?..."}
```

`GET /accounts` needs no special role — any authenticated user from the
company can check whether a mailbox is connected.

`POST /accounts/connect` is **management only**. Every other department gets
`403` — including for their own company, not just someone else's. This is the
endpoint the "Connect mailbox" / "Reconnect" button calls; the mailbox sends
every bid and receives every broker reply for the whole company, so only
management can (re)point it. There is no separate department flag in the
response — a `403` here means "not management," full stop. Hide or disable
the connect button for non-management users (`MailboxConnectionCard` already
does this via `isManagementUser()`), and treat a `403` as confirmation, not a
surprise.

Management users may still pass `{"company_id": N}` to connect a **different**
company's mailbox (unchanged from before) — that ability was never
department-gated separately, it's covered by the same management check now.

## The redirect back from Nylas

Open `auth_url` as a full-page navigation (`window.location.href = auth_url`,
not a fetch). Nylas eventually sends the browser to
`/api/v1/accounts/callback`, and **that endpoint always redirects the browser
onward — it never returns JSON.** It lands on the frontend's mailbox settings
page (`/settings`) with a query string:

| Query string | Meaning |
|---|---|
| `?mailbox=connected` | Success. Refetch `GET /accounts` to show the new state — the query string alone doesn't carry the email address. |
| `?mailbox=error&reason=declined` | The user closed/declined at Nylas. |
| `?mailbox=error&reason=missing_params` | Malformed redirect (shouldn't happen organically). |
| `?mailbox=error&reason=invalid_state` | The one-time state token was tampered with or expired. |
| `?mailbox=error&reason=exchange_failed` | Nylas rejected the authorization code. |
| `?mailbox=error&reason=no_grant` | Nylas responded but returned no grant id. |

Reading `mailbox`/`reason` off `location.search` on the settings page is
optional — enough to show a toast — but the connect card itself doesn't need
it, since it already refetches `GET /accounts` on mount and will show the
correct state either way.

---

# 6. Realtime events (email-agent)

```
GET {email-agent}/api/v1/events/stream?token=<access token>
```

Standard SSE. Named events, company-scoped, with a `: keep-alive` comment every
25 seconds. On connect you get `event: connected`.

Every event's payload:

```json
{
  "type": "suggestion_created",
  "negotiation_id": "6b2e...",
  "load_uuid": "load-uuid-1",
  "payload": { "...": "event-specific, see below" },
  "created_at": "2026-07-23T14:41:00Z"
}
```

| Event | When | Payload | Suggested UX |
|---|---|---|---|
| `suggestion_created` | A reply was drafted | `suggestion_id`, `kind`, `intent`, `broker_email` | Increment the inbox badge, prepend the card |
| `ratecon_mismatch` | Ratecon rejected — **no load created** | `suggestion_id`, `ratecon_check_id`, `discrepancies[]`, `agreed_amount`, `ratecon_amount` | **Alert-level** toast that persists; move the negotiation to "needs attention" |
| `ratecon_parse_failed` | Ratecon unreadable | `suggestion_id`, `filename`, `error` | **Alert-level** toast; tell them to handle it manually |
| `load_booked` | Load created in the TMS | `tms_load_id`, `load_number`, `shipment`, `carrier_pay`, `driver_pay` | Success toast, move the card to Booked, link to the load |
| `booking_failed` | Verification passed but the TMS write failed | `error` | **Error** toast; the dispatcher must create the load by hand |
| `negotiation_updated` | Status or suggestion state changed | `status`, `suggestion_id`, `suggestion_status` | Patch the row in place |

Reconnect with backoff on disconnect; `EventSource` does this natively. Because
the stream is in-memory, events emitted while disconnected are not replayed —
refetch the lists on reconnect.

---

# 7. Putting the screens together

**Bid Board.** Two tabs backed by `GET {atrek}/api/v1/bids?action=driver_bid`
with `ignored=false` / `ignored=true`. Live updates from the atrek bid stream.
Row actions: *Ignore* → `POST /bids/{id}/ignore`; *Bid* → price dialog →
`POST {email-agent}/api/v1/negotiations` (handle `broker_email_required` by
asking for an address). After a successful bid, the row's load turns green on the
board and a negotiation appears in the inbox.

**Negotiations inbox.** List from `GET /negotiations`, ordered by `updated_at`.
Badge rows where `pending_suggestions > 0` or `status` is `mismatch` / `failed`.
Detail view renders the thread with suggestion cards inline and a ratecon panel
showing the latest check. Live updates from the email-agent event stream.

**What the dispatcher never has to do:** write the bid email, chase the broker
for a ratecon, re-key a load into the TMS, or tell the driver they got the load.
They decide: ignore or bid, what price, and whether each drafted reply goes out.
