# Frontend Updates — August 2026

Everything that changed since [`FRONTEND_API_GUIDE.md`](FRONTEND_API_GUIDE.md) was
handed over. That guide is still correct for the Bid Board and Negotiations
screens; this document is the **delta on top of it**.

Two parts:

1. **[Dispatch teams](#part-1--dispatch-teams-new)** — new feature, needs new UI.
2. **[Changes to existing screens](#part-2--changes-to-screens-you-already-built)** — some fix things
   that were quietly broken, some add fields, one removes a card you're
   currently rendering.

> **Read §2.1 first if you build nothing else.** The Bid Board's live updates
> have never worked, and the cause was a backend bug that is now fixed. Your
> `EventSource` code was correct all along.

Auth is unchanged: `Authorization: Bearer <access token>` on every request.

| Service | Base URL | Owns |
|---|---|---|
| **TMS** (Django) | `/api` | users, teams, drivers, loads |
| **atrek** | `ATREK_BASE_URL` | loads, bids, bid board |
| **email-agent** | `EMAIL_AGENT_BASE_URL` | negotiations, broker email, suggestions |

---

# Part 1 — Dispatch teams (new)

A **team** groups dispatchers and the drivers they run. Every dispatch user
should end up on a team; every driver should end up with a team **and** a
dispatcher.

### Important: assignment is not enforced yet

The columns are nullable. There are **1,019 drivers and 87 users currently
unassigned** in production, so the UI cannot assume a driver has a team. Two
endpoints exist specifically to work through that backlog — see
[§1.6](#16-who-is-still-unassigned).

If you want assignment to become mandatory later (rejecting a driver save with
no team), say so and we'll add the validation server-side. Right now nothing
blocks an unassigned record.

## 1.1 The team object

```json
{
  "id": 1,
  "name": "Team Alpha",
  "description": "East coast reefer",
  "company": 1,
  "company_name": "Shipluxe LLC",
  "lead": 7,
  "lead_name": "Jane Dispatcher",
  "is_active": true,
  "member_count": 4,
  "driver_count": 23,
  "created_at": "2026-08-12T11:40:00Z",
  "last_updated": "2026-08-12T11:40:00Z"
}
```

`member_count` is dispatchers on the team; `driver_count` is drivers assigned to
it. Both are computed server-side — don't count them client-side.

## 1.2 List and read teams

```
GET /api/users/teams/
GET /api/users/teams/{id}/
```

**Returns a plain array, not a paginated envelope.** Teams are few.

| Query param | Effect |
|---|---|
| `company` | filter by company id (management only sees more than their own) |
| `is_active` | `true` / `false` |
| `search` | partial, case-insensitive name match |

Any authenticated user can read. Non-management users only ever see their own
company's teams.

## 1.3 Create, update, delete

```
POST   /api/users/teams/          { "name": "Team Alpha", "company": 1, "lead": 7, "description": "..." }
PATCH  /api/users/teams/{id}/     { "name": "Team Bravo", "is_active": false }
DELETE /api/users/teams/{id}/
```

**Management or Dispatch Manager only** — everyone else gets `403`. Hide the
buttons accordingly.

- `company` is ignored for non-management users; the team is created for their
  own company whatever you send.
- `lead` must be a user in the same company, else `400`:
  `{"lead": ["The team lead must belong to the same company as the team."]}`
- Team names are unique per company; a duplicate returns `400`.

**Delete does not cascade.** Members and drivers are unassigned, not deleted:

```json
{ "detail": "Team deleted.", "users_unassigned": 4, "drivers_unassigned": 23 }
```

Worth surfacing those numbers in the confirmation dialog — "this will unassign
23 drivers" is the thing the user needs to know.

## 1.4 Team members (dispatchers)

```
GET  /api/users/teams/{id}/members/
POST /api/users/teams/{id}/assign-users/   { "user_ids": [12, 13] }
POST /api/users/teams/{id}/remove-users/   { "user_ids": [12] }
```

`members` returns an array:

```json
[{ "id": 12, "username": "jdoe", "first_name": "Jane", "last_name": "Doe",
   "full_name": "Jane Doe", "department": "Dispatch", "is_active": true }]
```

A user belongs to **one team at a time** — assigning someone who is already on
another team moves them. Users from a different company are skipped, not
assigned:

```json
{ "assigned": 1, "rejected_user_ids": [99],
  "detail": "Users not in this team's company were skipped." }
```

Show `rejected_user_ids` if non-empty; the operation partially succeeded.

## 1.5 Assigning drivers

Two fields on the driver: `team` and `dispatcher`.

**Single driver** — the existing driver endpoint, unchanged in shape:

```
PATCH /api/hiring/drivers/{id}/    { "team": 1, "dispatcher": 12 }
```

**Bulk** — the one to use for the assignment screen:

```
POST /api/hiring/drivers-assign/
{ "driver_ids": [101, 102, 103], "team": 1, "dispatcher": 12 }
```

- Send **either or both** of `team` / `dispatcher`. Omitting a field leaves it
  alone; sending `null` **clears** it. Omitting both is a `400`.
- Drivers outside your company are skipped and named back to you.

```json
{ "updated": 3, "rejected_driver_ids": [], "detail": "All drivers updated." }
```

Errors: `400` if the team or user doesn't exist, `403` if it belongs to another
company.

**Filtering drivers** — `GET /api/hiring/drivers/` now accepts `team` and
`dispatcher` alongside the existing filters:

```
GET /api/hiring/drivers/?team=1
GET /api/hiring/drivers/?dispatcher=12
```

## 1.6 Who is still unassigned

The backlog worklists.

```
GET /api/users/unassigned/          dispatch users with no team      → ARRAY
GET /api/hiring/drivers-unassigned/ drivers missing team/dispatcher  → PAGINATED
```

`/users/unassigned/` defaults to dispatch-type departments (Dispatch, Dispatch
Manager, Updater). Pass `?dispatch_only=false` for everyone.

`/hiring/drivers-unassigned/` takes `?missing=`:

| Value | Returns |
|---|---|
| `team` | drivers with no team |
| `dispatcher` | drivers with no dispatcher |
| `either` *(default)* | drivers missing at least one |

Paginated (`count` / `next` / `previous` / `results`, 50 per page,
`?page_size=` up to 500). Each row:

```json
{ "id": 101, "full_name": "John Doe", "phone_number": "555-0100",
  "unit_number": "204", "company": 1, "company_name": "Shipluxe LLC",
  "status_name": "Active", "team": null, "dispatcher": null }
```

## 1.7 New fields on payloads you already read

**Driver** (`GET /api/hiring/drivers/`, `/drivers/{id}/`) — nested objects, or `null`:

```json
"team":       { "id": 1, "name": "Team Alpha" },
"dispatcher": { "id": 12, "first_name": "Jane", "last_name": "Doe", "username": "jdoe" }
```

**User** (`GET /api/users/list/`, `/user-profile/`):

```json
"team": { "id": 1, "name": "Team Alpha" }
```

`Driver.manager` is a **different, pre-existing field** and is unchanged. Don't
conflate it with `dispatcher`.

---

# Part 2 — Changes to screens you already built

## 2.1 Bid Board live updates now actually work — action required

**This was a backend bug, not yours.** The SSE endpoint only ever read the
`Authorization` header, and `EventSource` cannot send headers, so every
connection was rejected with `401` before it opened. The board never received a
single live event, which is why bids only appeared on refresh.

`FRONTEND_API_GUIDE.md` §1.3 documented `?token=` as the contract. The server
now honours it:

```
GET {atrek}/api/v1/loads/bids/stream?token=<access token>
```

**What to check on your side:** the failure was silent — `EventSource` retries
forever without surfacing anything — so if you added polling as a workaround,
you can remove it now. If you never connected the stream, connect it.

There is also now a **`ping` event every 30 seconds** to stop nginx closing an
idle connection. Ignore it; it exists only to keep the socket alive.

## 2.2 Driver and dispatcher names on the bid board

Bid rows used to come back with `driver: null` / `dispatcher: null`, so
everything rendered as "unknown driver". The backend was calling two URLs that
didn't exist and discarding the 404s. Fixed — `driver` and `dispatcher` are now
populated.

Consequence: the `?driver=` and `?dispatcher=` filters on `GET /bids` work now.
They were matching against a null object and always returned nothing.

## 2.3 Driver bids disappear once dispatch bids the load

`GET {atrek}/api/v1/bids?action=driver_bid` **no longer returns bids for a load
this company has already bid to a broker.** That load is in negotiations and a
second bid on it is refused, so leaving the row on the board only invited
someone to price a load they couldn't take.

- Each row now carries **`bid_placed`** (boolean).
- Pass **`?bid_placed=true`** to review the hidden ones.
- Rows are filtered, never deleted.

**Live behaviour:** when a `bid` event arrives with `action: "dispatcher_bid"`,
**remove every row matching that `load_id`** from the focus list. Otherwise the
row lingers until the next refresh.

Note this is per **load**, not per bid: if drivers A and B both bid the same
load and dispatch bids for A, *both* rows leave the board. That's intended —
one load, one negotiation.

## 2.4 Negotiations: filter by who sent the bid

```
GET {email-agent}/api/v1/negotiations?mine=true               only my bids
GET {email-agent}/api/v1/negotiations?mine=false              only other dispatchers'
GET {email-agent}/api/v1/negotiations?dispatcher_user_id=99   a named dispatcher
```

Combines freely with `status`, `page`, `limit`. Company scoping is applied
first and is unaffected.

## 2.5 Sent suggestions no longer appear in the thread — delete that branch

`GET /negotiations/{id}` **no longer returns suggestions with status `sent` or
`edited_sent`.**

Sending a draft already writes the email into `messages[]`. Returning the
resolved suggestion as well meant the same reply rendered twice — once as the
email, once as a card saying you sent it.

- **`pending`** — still returned; needs a decision.
- **`ignored`** — still returned; "we deliberately didn't reply" is real history.
- **`sent` / `edited_sent`** — gone from the thread. The email is in `messages[]`.

If you special-cased `sent` in the thread view, that code is now unreachable.
The full record is still available at `GET /suggestions?status=sent` if you ever
want an audit view.

## 2.6 Replies sent from outside the app now appear in the thread

If someone answers the broker straight from Gmail or Outlook instead of through
the app, that message is now recorded on the negotiation. It used to be dropped
entirely, leaving the thread showing half a conversation.

Messages gain **`sent_outside_app`** (boolean):

```json
{ "direction": "outbound", "sent_by_user_id": null, "sent_outside_app": true, "...": "..." }
```

Worth a small badge — "sent from mail client" — so a dispatcher can see a
colleague already replied.

**Caution worth building for:** a pending AI draft is *not* auto-dismissed when
someone replies out of band. If a colleague answered from Gmail and you then
click Send on the pending suggestion, the broker gets two replies. The
`negotiation_updated` SSE event carries `sent_outside_app: true` and the live
`pending_suggestions` count so you can warn. Tell us if you'd rather the backend
auto-ignore those drafts.

## 2.7 Unanswered bids close themselves after 30 minutes

A negotiation left in `bid_sent` with no broker reply for 30 minutes is now
closed automatically. Previously it sat open forever, blocking the load from
being bid again.

The `negotiation_updated` SSE event carries:

```json
{ "status": "closed", "auto_closed": true,
  "reason": "Closed automatically: the broker did not reply within 30 minutes." }
```

Distinguish it from a manual close in the UI — "no response" reads very
differently from "a dispatcher gave up on this". Only `bid_sent` is swept; once
a broker replies, only a human closes it.

## 2.8 Replies stay on one email chain (no frontend change)

Replies were arriving in the broker's inbox as a **new email chain**. Nylas was
threading correctly — the AI was renaming the subject on every draft, and mail
clients split a conversation when the subject changes.

Every reply now carries the thread's original subject. `draft_subject` on a
suggestion is now always `Re: <original subject>`, so what you display is what
actually goes out.

The `subject` field in `POST /suggestions/{id}/send` is **ignored** — the thread
subject always wins. Drop the field from the request if you send it; keeping the
chain intact matters more than letting someone rename it.

## 2.9 Quoted history stripped from broker replies

`body_text` on inbound messages used to include the entire quoted chain of our
own previous email. It's now trimmed to just what the broker wrote, so you can
render `body_text` directly without your own quote-stripping.

Messages received before 12 August still contain the old text.

---

# Summary of what needs frontend work

| # | Change | Work needed |
|---|---|---|
| 1.1–1.7 | Dispatch teams | **New screens**: team CRUD, member assignment, driver assignment, two unassigned worklists |
| 2.1 | SSE `?token=` now works | **Connect the stream**; remove any polling workaround |
| 2.3 | Bids hidden once dispatched | Handle `dispatcher_bid` event by removing rows for that `load_id`; optional `bid_placed=true` tab |
| 2.5 | Sent suggestions gone from thread | **Delete dead branch** |
| 2.6 | `sent_outside_app` | Add a badge; consider a double-reply warning |
| 2.7 | `auto_closed` | Distinguish auto-close from manual close |
| 2.2, 2.4, 2.8, 2.9 | Names, filters, threading, quoting | Nothing required — filters are optional to expose |
