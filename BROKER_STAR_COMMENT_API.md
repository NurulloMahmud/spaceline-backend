# Broker Star & Comment — API Reference (2026-08-19)

Star rating and a comment now live directly on the `Broker` record itself —
there is no separate ratings model. Both fields go through the existing
broker CRUD endpoint; nothing new was added specifically for them.

| Service | Base URL |
|---|---|
| **TMS** (Django, this repo) | `/api/billing` |

All endpoints below require a normal dispatcher `Authorization` header —
same auth as every other authenticated TMS endpoint. There is no
role restriction: any logged-in user can create, read, update, or delete a
broker, including its `star`/`comment`.

---

## 1. The two new fields

On every `Broker` object (create, update, and read responses):

| Field | Type | Notes |
|---|---|---|
| `star` | integer | Defaults to `0` on a new broker. No enforced range in the API — pick and stick to a scale on the frontend (e.g. 0–5). |
| `comment` | string \| null | Free text. Optional. |

---

## 2. List / retrieve brokers

```
GET /api/billing/brokers/
GET /api/billing/brokers/?search=circle
GET /api/billing/brokers/{id}/
```

`search` matches against `name`, `address`, `city`, `email`, `phone_number`,
`mc`. Standard paginated list response.

**Example broker object:**

```json
{
  "id": 42,
  "name": "CIRCLE LOGISTICS",
  "mc": "846834",
  "address": "10921 Reed Hartman Highway STE 323",
  "city": "Cincinnati",
  "state": "OH",
  "zipcode": "45242",
  "phone_number": "+1 (630) 426-3362",
  "star": 4,
  "comment": "Pays fast, easy to work with on rate changes.",
  "email": "operation@shipluxellc.com",
  "billing_email": null,
  "ai_type": "GPT",
  "note": null,
  "created_at": "2026-08-15T10:30:00Z",
  "last_updated": "2026-08-19T09:12:00Z"
}
```

---

## 3. Update star / comment on an existing broker

```
PATCH /api/billing/brokers/{id}/
Content-Type: application/json
```

Send only the fields you're changing — this is a partial update.

```json
{ "star": 5, "comment": "Great to work with, quick to confirm rates." }
```

**Response `200`:** the full updated broker object (same shape as above).

---

## 4. Creating a broker

```
POST /api/billing/brokers/
Content-Type: application/json
```

Required: `name`. Everything else, including `star`/`comment`, is optional.

```json
{ "name": "CIRCLE LOGISTICS", "mc": "846834", "star": 0 }
```

Two behaviors worth knowing before building the create form:

### a) Creating with only a name — de-dupes automatically

If `mc` is omitted (or blank) and a broker with the **same name**
(case-insensitive) already exists, the API returns that **existing**
broker instead of creating a new one. Still a `201`, but check the
returned `id` — it may not be a new record. This means a name-only
create is always safe to call, even if the broker might already exist.

### b) Creating (or updating) with an `mc` that's already taken — rejected

`mc` is no longer required to be unique at the database level, but the API
still enforces it in application logic. If you submit an `mc` that already
belongs to a different broker, you get:

```json
// 400
{ "mc": ["A broker with this MC already exists."] }
```

This applies on both `POST` (create) and `PATCH`/`PUT` (update) — trying to
change an existing broker's `mc` to one already used elsewhere gets the same
rejection. A broker keeps its own `mc` fine on update (it's excluded from
the collision check against itself).

**Practical guidance for the create form:** if you have an `mc` for the
broker, send it — you'll either get a clean create or a clear "already
exists" error to handle (e.g. prompt the user to search instead). If you
don't have an `mc` yet, name-only create is safe and will reuse an existing
match rather than duplicate it.

---

## 5. Deleting a broker

```
DELETE /api/billing/brokers/{id}/
```

Standard delete, no special behavior for `star`/`comment`.
