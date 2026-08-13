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
Point the Nylas webhook at `POST /internal/v1/webhooks/nylas` for the
`message.created` trigger, and set `NYLAS_CALLBACK_URI` to this service's
`/api/v1/accounts/callback`.

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
