# Broker Blacklist — API Reference (2026-09-04)

A company can keep a list of brokers it refuses to work with. Entries are
scoped to the logged-in user's company: you only ever see, create, or delete
your own company's list, and two companies can blacklist the same broker
independently.

| Service | Base URL |
|---|---|
| **TMS** (Django, this repo) | `/api/billing` |

Auth is the standard JWT header used by every other TMS endpoint:

```
Authorization: Bearer <access token>
```

No `company` field is ever sent from the frontend — the backend takes it from
the token. Sending one in the body is silently ignored.

---

## 1. The entry object

Every response below returns objects of this shape:

```json
{
  "id": 1,
  "name": "Priority 1 Inc",
  "mc": "312916",
  "reason": "Did not pay on load #4412",
  "created_by": 7,
  "created_by_name": "b.makhammatov",
  "created_at": "2026-09-04T06:40:47.228183-05:00"
}
```

| Field | Type | Notes |
|---|---|---|
| `id` | integer | Read-only. Use it for `DELETE`. |
| `name` | string | Broker name. May be `""` when the entry was created from an MC alone. |
| `mc` | string | **Normalized** by the backend — see §3. May be `""`. |
| `reason` | string \| null | Free text, optional. Why this broker was blacklisted. |
| `created_by` | integer \| null | User id. Read-only. |
| `created_by_name` | string | Username of whoever added the entry. Read-only. |
| `created_at` | ISO 8601 datetime | Read-only. |

---

## 2. Endpoints

```
GET    /api/billing/broker-blacklist/          list the company's entries
POST   /api/billing/broker-blacklist/          add an entry
GET    /api/billing/broker-blacklist/{id}/     one entry
DELETE /api/billing/broker-blacklist/{id}/     remove an entry
```

### List

```
GET /api/billing/broker-blacklist/
```

Returns a **plain JSON array** — this endpoint is *not* paginated, so don't
look for `count` / `results`. Default order is newest first (`-created_at`).

`?ordering=` works on any field of the object, e.g. `?ordering=name` or
`?ordering=-created_at`.

> There is **no** working `?search=` on this endpoint — passing one is ignored
> and you get the full list back. Filter client-side; the list is small.

### Create

```
POST /api/billing/broker-blacklist/
Content-Type: application/json
```

```json
{
  "name": "Priority 1 Inc",
  "mc": "MC 0312916",
  "reason": "Did not pay on load #4412"
}
```

All three fields are optional individually, but **at least one of `name` or
`mc` must be usable** (see §3), otherwise you get a `400`.

**Two possible success codes — both are success, and both return the entry:**

| Status | Meaning |
|---|---|
| `201 Created` | A new entry was added. |
| `200 OK` | This broker was **already blacklisted**; you get the existing entry back (same `id`, original `reason` and `created_at` — nothing is overwritten). |

The POST is idempotent on purpose, so a "Blacklist this broker" button can be
pressed twice without erroring or creating duplicates. Treat `200` and `201`
the same in the UI, or show "already on the list" for `200`.

### Delete

```
DELETE /api/billing/broker-blacklist/{id}/
```

`204 No Content` on success. `404` if the id doesn't exist **or belongs to
another company** — from the frontend those are the same case: it's not your
entry.

---

## 3. MC normalization — the important part

The MC coming off the load board is free text and is not validated at the
source. The backend reduces whatever you send to **bare digits with leading
zeros stripped** before storing or matching:

| You send | Stored as |
|---|---|
| `"MC 312916"` | `"312916"` |
| `"312916 "` | `"312916"` |
| `"mc#312916"` | `"312916"` |
| `"0312916"` | `"312916"` |
| `"BROKER M.C. NOT ON FILE"` | `""` |
| `"N/A"`, `"000"`, `null` | `""` |

So you can pass the load board's raw MC string straight through — no need to
clean it on the frontend. Just be aware the `mc` you get back in the response
will differ from what you sent.

**Why anything without digits becomes `""`:** dozens of unrelated brokerages
post the literal text `BROKER M.C. NOT ON FILE`. If that were kept as an MC,
blacklisting one of those brokers would hide all of them. Such entries fall
back to matching on **name** instead.

### How duplicates are decided

- If the normalized `mc` is non-empty → the entry is identified by
  **(company, mc)**. The `name` you send is only used when creating the row;
  posting the same MC again with a different name returns the *existing* row
  and does not rename it.
- If the normalized `mc` is empty → the entry is identified by
  **(company, name)**, **case-insensitively**. `"Circle Logistics"` and
  `"CIRCLE LOGISTICS"` are the same entry.

This means the same broker name *may* legitimately appear more than once in
the list when each row carries a different MC. Key your React list on `id`,
never on `name`.

---

## 4. Errors

| Status | When | Body |
|---|---|---|
| `400` | Neither a name nor an MC with a digit in it | `{"non_field_errors": ["Provide a broker name or an MC number with at least one digit in it."]}` |
| `400` | MC normalizes to more than 20 digits | `{"mc": ["That is not an MC number — it has more than 20 digits in it."]}` |
| `401` | Missing / expired token | standard DRF auth error |
| `404` | `{id}` not found, or belongs to another company | standard DRF detail |

Example of the `400` a user can actually trigger — typing only whitespace, or
picking a load whose MC field reads `BROKER M.C. NOT ON FILE` while the broker
name is also blank:

```json
{ "non_field_errors": ["Provide a broker name or an MC number with at least one digit in it."] }
```

Surface that message as-is; it's written for the end user.

---

## 5. Suggested frontend flow

**Settings → Blacklisted Brokers page**

1. `GET /api/billing/broker-blacklist/` on mount → render the array.
2. "Add" form with `name`, `mc`, `reason` → `POST`.
   - `201` → prepend the returned object to the list.
   - `200` → the broker was already there; highlight the existing row instead
     of adding a duplicate (the returned `id` tells you which).
   - `400` → show `non_field_errors[0]` under the form.
3. Row "Remove" button → `DELETE .../{id}/` → drop the row on `204`.

**"Blacklist this broker" from a load / broker screen**

Post the broker's `name` and its raw `mc` straight from the record you already
have, plus an optional `reason` from a prompt. Don't pre-check whether it's
already blacklisted — the POST handles that for you and returns `200` with the
existing entry.

---

## 6. Notes

- **Editing is not part of the intended design.** `PUT` / `PATCH` currently
  still respond on `/{id}/`, but that is an oversight and they are planned to
  be removed — do not build an edit form. To change an entry, delete it and
  create a new one.
- **Access is expected to be restricted to Management.** Right now any
  authenticated user reaches these endpoints, but the intended rule is
  Management-only. Gate the page behind the management role on the frontend so
  nothing breaks when the backend restriction lands.
- There is a separate internal endpoint,
  `GET /api/billing/internal/broker-blacklist-bulk/`, used service-to-service
  with an `X-Internal-Secret` header. It is **not** for the frontend.
