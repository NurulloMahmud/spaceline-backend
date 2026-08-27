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
ssh root@95.169.204.54 'cd /home/api/email-agent && bash deploy.sh'
```

That pulls `main`, installs dependencies, restarts the service and waits on the
health check, failing loudly with the error log if it does not come up. `.env`
is gitignored, so a pull never touches the server's configuration.

To re-establish the credential on a fresh box (or after rotating the token):

```bash
umask 077
printf 'https://<github-user>:<token>@github.com\n' > /root/.git-credentials-email-agent
cd /home/api/email-agent
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
```

## Operational notes

- **Single instance.** The SSE hub is in-process; running replicas needs Redis
  pub/sub behind `services/events.py` (the publish/subscribe surface stays the same).
- **Webhooks are idempotent** via the `processed_webhooks` table, and booking is
  guarded by `negotiations.tms_load_id` plus a `load_number` check in the TMS.
- **Failures are never silent.** Every parse, verify, and booking failure writes a
  row and emits an SSE event a dispatcher can see.
