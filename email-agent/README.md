# email-agent

Broker email negotiation for the box-truck TMS. It sends dispatcher bids to
brokers, drafts every reply for a human to approve, verifies rate confirmations
against what was agreed, and books verified loads into the TMS automatically.

Dispatchers make decisions; this service does the work around them.

## Documentation

- [`docs/FRONTEND_API_GUIDE.md`](docs/FRONTEND_API_GUIDE.md) — the frontend contract:
  every endpoint, error codes that change UI behaviour, and the SSE event catalog.
- [`docs/IMPLEMENTATION_NOTES.md`](docs/IMPLEMENTATION_NOTES.md) — what changed across
  all four services, where the safety rules are enforced, and the deployment checklist.

## Where it sits

```
atrek (Go)          load feed + bid board          driver bids, dispatcher bids
   │
   ├── agent_bot    telegram                       posts loads to driver groups
   │
   └── email-agent  THIS SERVICE                   broker email + AI + booking
          │
          └── boxTruck (Django)                    system of record: loads, brokers, drivers
```

## Flow

1. A dispatcher clicks **Bid** on the bid board and names a price.
   `POST /api/v1/negotiations` renders the fixed bid-email template and sends it
   to the broker from the company's shared dispatch mailbox (Nylas).
2. The broker replies. The Nylas webhook fires, the agent classifies the message
   and drafts a reply. The dispatcher can **ignore**, **send**, or **edit & send**.
   *Nothing is ever auto-sent to a broker except that first bid email.*
3. The broker sends a rate confirmation. The agent parses it through the TMS's
   existing parser and verifies it:
   - **price** must equal the agreed rate exactly (to the cent),
   - **locations and dates** must match semantically (formatting differences are fine).
4. **Verified** → the load, its stops and the ratecon PDF are created in the TMS,
   the driver's Telegram group is notified, dispatch sees a `load_booked` event.
   **Not verified** → nothing is created; dispatch gets the discrepancy list and a
   drafted reply asking for a corrected ratecon.
   **Unreadable** → dispatch is alerted to handle it by hand.

## Setup

```bash
python3.12 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env      # then fill it in
createdb email_agent
.venv/bin/python main.py  # tables are created on boot
```

### Environment

`JWT_SECRET` must equal Django's `SIMPLE_JWT` signing key (which defaults to
`SECRET_KEY`) or every dispatcher token will be rejected. `INTERNAL_SECRET_KEY`
must match atrek's `INTERNAL_SECRET_KEY` and boxTruck's `INTERNAL_SERVICE_SECRET`.
`TELEGRAM_BOT_TOKEN` is the same bot agent_bot runs, so booking notices arrive in
the group the driver already talks to.

`PRICE_TOLERANCE_CENTS` is `0` and should stay there: it is the guard that stops
a $3,000 ratecon from booking a $3,200 agreement.

### Nylas

One grant per company — a shared dispatch mailbox, not per-dispatcher inboxes.
The grant id and address live on `email_accounts`, keyed by company, so each
customer's mailbox is its own row; nothing about a connection comes from env.

`POST /api/v1/accounts/connect` builds the hosted-auth URL (management only)
and takes an optional `email_address`. That address is sent as `login_hint`
*and* signed into `state`, and the callback refuses a grant for any other
mailbox — handing the unwanted grant back to Nylas rather than keeping live
access to an inbox it just rejected. Without it, whichever mailbox is
authorised is accepted, which is what accounts connected before this existed
still do (their `expected_email_address` is null).

The authorization code is single-use: a failed exchange redirects with
`reason=exchange_failed` and the flow has to be restarted from `connect`.
Retrying the same code cannot succeed, so nothing here retries it.

Broker mail arrives here **directly from Nylas**. This service owns the Nylas
application (client id `4ee94420-d0d6-46f3-b789-999263f0e18d`, US region); no
peer server relays deliveries, so the only thing standing between the public
internet and the pipeline is the HMAC signature on each request.

Two URLs have to be registered on the Nylas application, and they are not
interchangeable:

| Purpose | URL | Env |
|---|---|---|
| OAuth redirect after hosted auth | `https://spaceline.boxtruckmanage.com/email-agent/api/v1/accounts/callback` | `NYLAS_CALLBACK_URI` |
| Webhook delivery | `https://spaceline.boxtruckmanage.com/email-agent/internal/v1/webhooks/nylas` | — |

The OAuth redirect cannot double as the webhook URL: it answers with a 302 to
the frontend settings page, and Nylas only registers a webhook whose endpoint
echoes the `challenge` query parameter back as plain text (`70005
unable.verify.webhook_url` otherwise).

Registering both, on a fresh application:

```bash
export NYLAS_API_KEY=...   # the value from your .env

curl -X POST https://api.us.nylas.com/v3/applications/callback-uris \
  -H "Authorization: Bearer $NYLAS_API_KEY" -H "Content-Type: application/json" \
  -d '{"url":"https://spaceline.boxtruckmanage.com/email-agent/api/v1/accounts/callback","platform":"web"}'

curl -X POST https://api.us.nylas.com/v3/webhooks \
  -H "Authorization: Bearer $NYLAS_API_KEY" -H "Content-Type: application/json" \
  -d '{"trigger_types":["message.created","grant.expired","grant.deleted"],
       "webhook_url":"https://spaceline.boxtruckmanage.com/email-agent/internal/v1/webhooks/nylas",
       "description":"email-agent inbound broker mail"}'
```

The webhook response carries `webhook_secret` **once**. Put it in
`NYLAS_WEBHOOK_SECRET` before the service handles traffic; until it matches,
every delivery is rejected with a 401 and the broker replies are lost.

`grant.expired` and `grant.deleted` set the company's mailbox `status` to
`expired`/`revoked`, which `GET /api/v1/accounts` reports so a dispatcher is
told to reconnect instead of watching a dead mailbox look healthy.

## Deploying

The server pulls from GitHub. Authentication comes from a git credential file
outside the repo, so no token appears in the remote URL or in `git remote -v`:

```bash
ssh root@95.169.205.181 'cd /home/api/spaceline && ./deploy.sh email_agent'
```

The pull happens once for the whole repo, so the deploy script lives at the
repo root rather than in this directory, and it takes the components to
restart as arguments (`./deploy.sh` alone does every service).

That pulls `main`, installs dependencies, restarts the service and waits on the
health check, failing loudly with the error log if it does not come up. `.env`
is gitignored, so a pull never touches the server's configuration.

The pull is `--ff-only`. It aborts rather than merging if the server's checkout
has commits of its own — inspect them with
`git log --oneline origin/main..HEAD` before deciding what to do with them,
because a commit made directly on the box is running in production.

To re-establish the credential on a fresh box (or after rotating the token):

```bash
umask 077
printf 'https://<github-user>:<token>@github.com\n' > /root/.git-credentials-email-agent
cd /home/api/spaceline
git config --local credential.helper 'store --file=/root/.git-credentials-email-agent'
```

## Tests

```bash
createdb email_agent_test
.venv/bin/python -m pytest -q
```

The suite runs against a real Postgres and stubs every outbound call (Nylas,
OpenAI, the TMS, Telegram). `tests/test_ratecon_verification.py` is the one that
matters most — it pins the rule that a mismatched rate confirmation never
becomes a load.

## Layout

```
config/settings.py       env-driven config
database/models.py       negotiations, messages, suggestions, ratecon checks
database/connection.py   engine, session factory, FastAPI dependency
services/
  auth.py                Django JWT validation, company scoping
  negotiations.py        starting a bid (the only auto-sent email)
  inbound.py             webhook → classify → suggest or verify
  booking.py             verified ratecon → TMS load → driver notice
  suggestions.py         ignore / send / edit & send
  ai.py                  every model call, all schema-constrained JSON
  bid_email.py           the fixed bid template
  nylas_client.py        Nylas v3 REST
  boxtruck.py atrek.py telegram.py    peer services
  events.py              SSE hub
routers/                 HTTP surface
scripts/                 one-off maintenance run by hand
```

## Operational notes

- **Single instance.** The SSE hub is in-process; running replicas needs Redis
  pub/sub behind `services/events.py` (the publish/subscribe surface stays the same).
- **Webhooks are idempotent** via the `processed_webhooks` table, and booking is
  guarded by `negotiations.tms_load_id` plus a `load_number` check in the TMS.
- **One email is stored once, however many copies of it the mailbox holds.**
  A reply written in Gmail or Outlook rather than in this app arrives as
  `message.created` twice whenever the mail client saves its own copy to Sent
  and the provider saves another: two Nylas ids, one email, and the thread
  showed it twice. Messages are fetched with `fields=include_basic_headers`
  and matched on the RFC `Message-Id`, which identifies the email rather than
  one mailbox copy of it (`inbound.duplicate_of`). Where a provider returns no
  headers, an outbound message repeating the text of one sent to the same
  negotiation in the last `DUPLICATE_WINDOW_MINUTES` is taken for the same
  email. Inbound is deliberately left out of that last check: dropping a
  broker message costs a reply draft and the agreed rate read from the thread.
- **Failures are never silent.** Every parse, verify, and booking failure writes a
  row and emits an SSE event a dispatcher can see.
- **A message is matched to its negotiation by thread id, then by broker address
  and subject.** The fallback exists because the thread id is not always there
  to use: some providers return none on the send that opens the thread, and some
  broker systems answer under a thread of their own. Without it, replies were
  dropped and a negotiation showed only the emails this service sent. A
  negotiation missing a thread id adopts the one it matched on, so the fallback
  runs once per thread. A message from a broker we are negotiating with that
  still matches nothing is logged at WARNING.

### Removing duplicate sent copies already stored

Rows written before the Message-Id check do not remove themselves.
`scripts/dedupe_email_messages.py` deletes every copy after the first of the
same outbound email — same `Message-Id`, or same text within
`DUPLICATE_WINDOW_MINUTES` — keeping the copy the app sent itself where there
is one. Inbound messages are never touched: suggestions and ratecon checks
point at them. It changes nothing without `--apply`:

```bash
cd /home/api/spaceline/email-agent
./venv/bin/python -m scripts.dedupe_email_messages                  # look
./venv/bin/python -m scripts.dedupe_email_messages --apply          # delete
./venv/bin/python -m scripts.dedupe_email_messages --company 1 --apply
```

### Backfilling stored message text

`strip_quoted` runs once, when a message is stored, so a fix to it leaves older
rows as they were. `scripts/backfill_message_text.py` re-runs the current
version over the table. It changes nothing without `--apply`, is safe to re-run,
and can be scoped:

```bash
cd /home/api/spaceline/email-agent
./venv/bin/python -m scripts.backfill_message_text                  # look
./venv/bin/python -m scripts.backfill_message_text --apply          # write
./venv/bin/python -m scripts.backfill_message_text --company 1 --apply
```
