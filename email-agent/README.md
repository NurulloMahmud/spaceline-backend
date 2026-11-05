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
   to the broker over SMTP from the dispatcher's own mailbox, as a reply to
   the Sylectus alert by Message-ID, or from the company's shared mailbox for
   dispatchers without one.
2. The broker replies. The answer lands in the dispatcher's mailbox, which
   the service reads over IMAP (`services/shared_inbox.py`); the agent
   classifies the message and drafts a reply. The dispatcher can **ignore**,
   **send**, or **edit & send**.
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

### Mail transport

Mail moves over IMAP and SMTP with app passwords; no provider API and no
third-party mail service is involved. Nylas, which carried the first version,
was retired on 2026-11-05: its grants and webhook were deleted and the client
code removed. The `nylas_*` columns on `email_accounts`, `email_messages` and
`negotiations` are legacy names kept to avoid a migration; `nylas_grant_id`
holds `imap:<uuid>` on every live mailbox row.

### Shared mailbox over IMAP

The company's shared mailbox is read as the catch-all for negotiations that
predate per-dispatcher mailboxes and for anything copied to it. The service
polls it with an app password:

| Env | Meaning |
|---|---|
| `SHARED_INBOX_ADDRESS` | the shared mailbox, which must also be connected as the company's shared `EmailAccount` |
| `SHARED_INBOX_APP_PASSWORD` | its Gmail app password |
| `SHARED_INBOX_IMAP_HOST` | default `imap.gmail.com` |
| `SHARED_INBOX_FOLDER` | default `INBOX` |
| `SHARED_INBOX_POLL_SECONDS` | default 15 |

Both of the first two empty leaves that reader off; the dispatchers' own
mailboxes are read regardless. It keeps its place in
`shared_inbox_cursors` and starts at the folder's end the first time it sees
it. Messages from any of the company's own mailboxes (the Cc copy of a bid)
and Sylectus alerts are skipped. A reply matches its negotiation by the
Message-IDs it quotes in In-Reply-To and References, which every outbound
row now records, before the subject-and-broker fallback.

### Dispatcher mailboxes with an app password

`Admin > Mailboxes` (frontend) lets an admin connect a dispatcher's mailbox
with an app password: email address, the app password, and the IMAP and
SMTP hosts, which the page prefills per provider. The service signs in both
ways before storing anything (`POST /api/v1/mailboxes/test`,
`PUT /api/v1/mailboxes/users/{id}`). Such a mailbox (`email_accounts.transport
= 'imap'`) sends over SMTP from its own address, as a reply to the Sylectus
alert by Message-ID, and is read over IMAP by the same reader as the shared
mailbox, matching only its own negotiations. No Google API is involved, so
the per-user quota cannot refuse a bid.

| Env | Meaning |
|---|---|
| `MAILBOX_ADMIN_USERS` | comma-separated BTM user ids allowed on the page and its endpoints |
| `MAILBOX_SECRET_KEY` | Fernet key the app passwords are encrypted with; generate once, never rotate without re-entering passwords |

### Reliability and alerts

Sending and reading are separate connections, and neither depends on the
other. A bid opens its own SMTP connection; a busy server (421), a dropped
or slow connection is tried again after 2, 5 and 10 seconds before the
dispatcher sees an error (`services/outbound.py`). A refused password fails
at once and marks the mailbox `failing`, which is the only way a mailbox
loses its `active` status: the inbox reader's own errors (a read timeout, a
dropped connection) are kept in `email_accounts.last_error` for the Mailboxes
page but never stop bids.

The reader holds one standing IMAP connection per mailbox
(`services/shared_inbox.py`), opened on first use and replaced on the spot
when the server drops it, instead of signing in afresh on every pass.

`services/alerts.py` watches every minute and messages a Telegram chat when a
mailbox's password is refused, a bid could not be sent after the retries, a
mailbox's reads have failed for ten minutes, or the reader has not finished a
pass for five. Each condition is said once, repeated hourly while it lasts,
and once more when it clears.

| Env | Meaning |
|---|---|
| `ALERT_BOT_TOKEN` | Telegram bot the alerts come from; defaults to `IP_ALERT_BOT_TOKEN` |
| `ALERT_CHAT_ID` | chat they go to; defaults to `IP_ALERT_CHAT_ID`. Both empty: alerts only reach the log |

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

The suite runs against a real Postgres and stubs every outbound call (SMTP,
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
  inbound.py             broker message → classify → suggest or verify
  shared_inbox.py        the shared mailbox and every app-password mailbox over IMAP, feeding inbound.py
  outbound.py            the one send door, SMTP from the mailbox's own address
  imap_smtp.py           plain IMAP/SMTP sign-in check, message build and send
  secrets.py             app passwords encrypted at rest (MAILBOX_SECRET_KEY)
  booking.py             verified ratecon → TMS load → driver notice
  suggestions.py         ignore / send / edit & send
  ai.py                  every model call, all schema-constrained JSON
  bid_email.py           the fixed bid template
  boxtruck.py atrek.py telegram.py    peer services
  events.py              SSE hub
routers/                 HTTP surface
scripts/                 one-off maintenance run by hand
```

## Operational notes

- **Single instance.** The SSE hub is in-process; running replicas needs Redis
  pub/sub behind `services/events.py` (the publish/subscribe surface stays the same).
- **Reads are idempotent** via the `processed_webhooks` table (one row per
  message the IMAP reader handled), and booking is guarded by
  `negotiations.tms_load_id` plus a `load_number` check in the TMS.
- **One email is stored once, however many copies of it the mailbox holds.**
  The same email can reach the service more than once (a copy in the
  dispatcher's inbox and one in the shared catch-all, for instance). Messages
  are matched on the RFC `Message-Id`, which identifies the email rather than
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
