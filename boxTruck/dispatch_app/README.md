# dispatch_app

The carrier side of BTM Dispatch, the dispatchers' phone app (spec:
`btm-dispatch-app/docs/SPEC-1.0.md`, sections 3 to 6). A BTM user links one
phone at a time through the hub (`DRIVER_HUB_URL`, the same hub as the driver
app). The hub keeps a device token; every few minutes it trades it here for a
10 minute access token, and the phone calls this API, the Go loads service and
the email agent with that token directly.

## Model

`DispatchDevice` (table `dispatch_devices`): uuid `id` (the token's `dsid`
claim), `user` (kept as null after the user is deleted), `token_hash` (SHA-256
of `dsp_` + 40 random URL-safe characters; the token itself is never stored),
`hub_workspace_id`, `platform`, `device_label`, `created_at`, `last_seen_at`,
`revoked_at`, `revoked_reason` (`replaced`, `unlinked`, `hub`, `user_deleted`).
A partial unique constraint allows one live (not revoked) device per user.

## Endpoints

Errors are `{"error": "...", "code": "..."}` except where the authentication
class answers (DRF style `{"detail": "...", "code": "..."}`).

Web, normal JWT (`Authorization: Bearer <access>`):

| Method and path | Reply |
| --- | --- |
| `POST /api/dispatch-app/link-code/` | `201 {code, qr_text, expires_at, created_at}` from the hub's `/t/v1/dispatch/link-codes`; `created_at` is Django's clock (`2026-11-21T10:00:00Z`), to compare with `linked_at`. `503 hub_not_configured` without `DRIVER_HUB_URL` / `DRIVER_HUB_TOKEN`, `409 user_inactive`, `502 hub_unreachable` |
| `DELETE /api/dispatch-app/link-code/` | The dialog closed: the hub's `/t/v1/dispatch/link-codes/cancel {btm_user_id}` kills the user's unused codes (background, best effort). `{ok: true}` |
| `GET /api/dispatch-app/device/` | `{device: null}` or `{device: {platform, device_label, linked_at, last_seen_at}}` |
| `DELETE /api/dispatch-app/device/` | Revokes the live device (`unlinked`), then the hub's `/t/v1/dispatch/unlink` after commit in a background thread (also when nothing was live). `{ok: true}` |

A phone's own token (one with `dsid`) is refused on all four: `403 {"code": "phone_token"}`. Only the web makes codes and unlinks.

Hub, header `X-Hub-Token: <DRIVER_HUB_TOKEN>` (no JWT):

| Method and path | Body | Reply |
| --- | --- | --- |
| `POST /api/dispatch-app/devices/` | `{btm_user_id, hub_workspace_id, platform, device_label}` | `201 {device_id, device_token, user: {id, first_name, last_name, email, is_active}, mailbox}`. Revokes the user's other live devices first (`replaced`, no hub call: the hub asked and applies the rule itself). `404 user_not_found`, `409 user_inactive` |
| `POST /api/dispatch-app/session/` | `{device_token}` | `200 {access, expires_at, user, mailbox}` (`expires_at` RFC 3339 UTC, `2026-11-21T10:10:00Z`); `401 device_revoked`. Also for an inactive user (`user.is_active` false) |
| `POST /api/dispatch-app/devices/revoke/` | `{device_id}` or `{device_token}` | `200 {revoked: bool}`, also when already revoked or unknown |

Email agent, header `X-Internal-Secret: <INTERNAL_SERVICE_SECRET>`:

| Method and path | Body | Reply |
| --- | --- | --- |
| `POST /api/dispatch-app/internal/notify/` | `{btm_user_id, kind, title, body, data}`, kind `broker_reply`, `bid_won` or `bid_booked` | `200 {queued: bool}` at once; the hub's `/t/v1/dispatch/notify` is called after, in a thread, 3 s timeout. `false` when nobody is linked, the user is inactive or the hub is not configured |

`mailbox` is the user's own bid mailbox address (Admin > Mailboxes), read from
the email agent's `GET /internal/v1/accounts/users/<id>` on
`EMAIL_AGENT_INTERNAL_URL`, or `null` when there is none, it is revoked, or the
agent did not answer within 3 s.

## Phone tokens

`services.mint_access` builds the token with the web login's own
`CustomTokenObtainPairSerializer.get_token(user).access_token`, so the claims
are exactly the web's: `token_type`, `exp`, `iat`, `jti`, `user_id`,
`department`, `company`, `company_id`; plus `dsid`. Lifetime 10 minutes. (The
web token has no `username`, `first_name` or `last_name` claims; the login
returns them in the response body only, so the phone token has none either.)

`DEFAULT_AUTHENTICATION_CLASSES` is `dispatch_app.authentication.DispatchJWTAuthentication`,
a subclass of simplejwt's `JWTAuthentication`:

- no `dsid` claim: simplejwt's code path, untouched (the tests compare both
  classes on every kind of token);
- `dsid`: the device must exist, be live and belong to the token's `user_id`,
  else `401 {"detail": ..., "code": "device_revoked"}`. An inactive user gets
  through only to `GET /api/salaries/leaderboard/` and
  `GET /api/salaries/leaderboard/<own id>/loads/` (the Performance tab), else
  `403 {"detail": ..., "code": "user_inactive"}`. `last_seen_at` moves at most
  once a minute.

## How revocation reaches the other services

Django checks its own table on every request. The Go loads service and the
email agent check Redis: `dispatch:dsid:<dsid>` = `active` or `inactive`.

| Event | Redis | Hub |
| --- | --- | --- |
| Device created | set `active`, TTL 11 min (after commit) | (the hub asked) |
| Session exchange | set `active` / `inactive`, TTL 11 min, then the device and user are read again: revoked meanwhile -> key deleted and `401 device_revoked`; `is_active` changed meanwhile -> value rewritten | |
| Revoke (replaced, web unlink, hub revoke, user deleted) | key deleted (after commit) | web unlink and user delete call `/t/v1/dispatch/unlink` |
| `is_active` changes (admin, users API; any `save()`) | flipped for the live device | `/t/v1/dispatch/users/status` (background, after commit) |

A missing key means revoked (or idle for 11 minutes, when the last 10 minute
token has long expired). `QuerySet.update(is_active=...)` skips the signals;
nothing in the app does that outside tests, and the next session exchange
writes the right value anyway.

Redis is `DISPATCH_REDIS_URL` (default `redis://localhost:6379/0`), the same
server and database as Celery's broker and the loads service's `REDIS_ADDR` /
`REDIS_DB` (checked on the server 2026-11-21: `localhost:6379`, DB 0, no
password). A Redis failure is logged and never breaks a Django request.

- Go loads service (makhammatovb/agent): `Authenticate` rejects a `dsid`
  token whose key is missing (401 `device_revoked`), lets an `inactive` one
  reach only the bids-by-day analytics, rechecks streams every 15 s, and fails
  closed when Redis errors.
- Email agent (`email-agent/services/dispatch.py`): `services/auth.py` checks
  the key for any `dsid` token: missing 401 `device_revoked`, `inactive` 403
  `user_inactive` (the Inbox is locked for inactive users, so nothing there is
  open), Redis down 503 `auth_unavailable`. Its event stream rechecks every
  15 s and ends when the check fails or the token expires; a phone token in
  `?token=` is refused (401 `use_header`). Web tokens never touch Redis.

## Link email

When the hub links a phone (`devices/`), the BTM user gets an email after
commit, in a background thread, through Django's own mail path
(`EMAIL_BACKEND`, anymail with Resend, from `DEFAULT_FROM_EMAIL`): subject "BTM
Dispatch linked to your account", body "BTM Dispatch was linked to <device
label> on Nov 21, 2026 at 4:02 PM CST. If this was not you, unlink it in
Profile > Mobile app." (the deployment's `TIME_ZONE`). Skipped quietly without
`RESEND_API_KEY` or without an email address on the user.

## Secrets and retries

`X-Hub-Token` and `X-Internal-Secret` are compared in constant time
(`hmac.compare_digest`), and an empty configured secret lets nobody in.
A Redis delete that fails on revoke is tried again in a background thread
after 0.5, 1, 2, 4 and 8 seconds (about 15 s), then logged as an error; the
11 minute TTL is the backstop.

## Background hub calls

`users/status`, `unlink` (web unlink and user delete) and `notify` run after
commit in a non-daemon thread (`background.py`): the response never waits on
the hub, and a short-lived process (management command, shell) waits for the
call at exit instead of dropping it. The calls carry the hub client's
timeouts (5 s status and unlink, 3 s notify), which bound that wait. These are
requests timeouts (connect, then each read), so a hub that answers byte by byte
could stretch one; an unreachable or silent hub cannot.

## Notifications

Fire and forget, after commit, in a background thread, skipped when nobody is
linked:

- `broker_reply` (email agent, `services/inbound.py`): a broker's message lands
  on a conversation; it goes to the negotiation's `dispatcher_user_id`. Title
  "<broker> replied", body the first line of the reply, `data {thread_id, load_id}`.
- `bid_won` / `bid_booked` (email agent, a SQLAlchemy listener on
  `Negotiation.status`): the negotiation reaches `ratecon_received` or
  `mismatch` (the web's "Won"), or `booked`. Title "Your bid won" / "Load
  booked", body the route ("Chicago, IL to Detroit, MI"), `data {load_id,
  bid_id}` where `bid_id` is the negotiation id. Nothing sets `booked` since
  auto-booking was removed (2026-11-20); the listener covers it if it returns.

The email agent posts these to `/api/dispatch-app/internal/notify/`, which
holds the hub token and knows who is linked.

## Settings

| Setting | Default | |
| --- | --- | --- |
| `DRIVER_HUB_URL`, `DRIVER_HUB_TOKEN` | hub URL, empty | Same as the driver app integration |
| `DISPATCH_REDIS_URL` | `redis://localhost:6379/0` | Needs the `redis` package (requirements.txt) |
| `EMAIL_AGENT_INTERNAL_URL`, `INTERNAL_SERVICE_SECRET` | | Mailbox lookup, notify route |

Email agent: `DISPATCH_REDIS_URL` (same default), `BOXTRUCK_BASE_URL`,
`BOXTRUCK_INTERNAL_SECRET`.

## Tests

```bash
LOCAL=True ./venv/bin/python manage.py test dispatch_app
```

The hub (`requests.request` in the hub client) and Redis (`state._client`) are
replaced by in-memory stand-ins; background calls run inline.
