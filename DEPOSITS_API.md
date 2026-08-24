# Deposits — API Reference (2026-08-24)

Bank/deposit info (bank name, routing number, account number, plus a
mailing address) for a driver. Each driver has **at most one** deposit
record — the frontend needs to check whether one exists before deciding
whether to create or update.

| Service | Base URL |
|---|---|
| **TMS** (Django, this repo) | `/api/hiring` |

---

## ⚠️ Common mistake: don't PATCH `driver-companies` for this

`bank_name`, `routing_number`, and `account_number` live **only** on the
`Deposit` model. They do **not** exist on `DriverCompany`
(`/driver-companies/{id}/`), even though the two resources share some
similarly-named address fields (`address`, `city`, `state`,
`zip_code`/`zipcode`) and are both tied to a driver.

If you `PATCH /driver-companies/{id}/` with `bank_name`/`routing_number`/
`account_number` in the body, DRF silently ignores those keys — they
aren't declared fields on that serializer, so the request returns `200`
with **no error and no actual change**. That's the bug: it looks like it
worked, but the deposit was never touched.

**Always send deposit fields to `/deposits/`, never `/driver-companies/`.**

---

## The flow, end to end

1. Frontend needs to show/edit a driver's deposit info. Call **1. Get the
   driver's deposit** with `?driver=<driver_id>`.
2. **If the response array is empty (`[]`)** — this driver has no deposit
   yet. Render an empty/create form, then call **2. Create a deposit** on
   submit (`POST`).
3. **If the response array has one item** — render the form pre-filled
   with it, then call **3. Update a deposit** on submit (`PATCH`), using
   that item's `id`.

There is no upsert endpoint — the frontend is responsible for this
check-then-create-or-update branch.

> Note: most drivers created through the `/drivers-bulk/` onboarding flow
> already get a `Deposit` row at creation time. The empty-array case
> mainly shows up for drivers that predate that flow, or that otherwise
> never had one set.

---

## 1. Get the driver's deposit

```
GET /api/hiring/deposits/?driver=<driver_id>
Authorization: Bearer <dispatcher JWT>
```

**Response `200`:**

```json
[
  {
    "id": 7,
    "driver": { "id": 42, "full_name": "Ricardo Carmona" },
    "company": "Wells Fargo",
    "address": "123 Main St",
    "city": "Westchester",
    "state": "IL",
    "zip_code": "60154",
    "bank_name": "Wells Fargo",
    "routing_number": "111000025",
    "account_number": "0001112223",
    "created_at": "2026-01-10T18:32:00Z",
    "updated_at": "2026-06-02T14:05:00Z"
  }
]
```

No deposit on file for that driver → **`200` with `[]`** (not a `404`,
not `null`). Treat an empty array as "go create one."

This is a standard filtered list endpoint (`filterset_fields = ['driver']`),
so it always returns an array — expect at most one item since a driver
can only have one deposit.

---

## 2. Create a deposit

```
POST /api/hiring/deposits/
Authorization: Bearer <dispatcher JWT>
Content-Type: application/json
```

```json
{
  "driver": 42,
  "company": "Wells Fargo",
  "address": "123 Main St",
  "city": "Westchester",
  "state": "IL",
  "zip_code": "60154",
  "bank_name": "Wells Fargo",
  "routing_number": "111000025",
  "account_number": "0001112223"
}
```

`driver` is required; every other field is optional. **Response `201`**
returns the created object in the same shape as list above.

**Errors:** `400` if `driver` is missing/invalid, or if this driver
already has a deposit (`"deposit with this driver already exists."`) —
if you hit that, you raced the empty-array check and should switch to
`PATCH` instead (see below).

---

## 3. Update a deposit

```
PATCH /api/hiring/deposits/{id}/
Authorization: Bearer <dispatcher JWT>
Content-Type: application/json
```

`{id}` is the deposit's own `id` from step 1's response — **not** the
driver's id.

```json
{
  "bank_name": "Chase",
  "routing_number": "021000021",
  "account_number": "0009998887"
}
```

Send only the fields being changed — partial update. **Response `200`**
returns the full updated object.

Every `PATCH` that actually changes a value is logged to deposit
history automatically server-side (who changed what, from what to
what) — nothing extra the frontend needs to do for that.

---

## Field reference

| Field | Type | Notes |
|---|---|---|
| `id` | integer | read-only |
| `driver` | integer (write) / object (read) | required on create; one deposit per driver, enforced server-side |
| `company` | string | optional, free text |
| `address` | string | optional |
| `city` | string | optional |
| `state` | string | optional |
| `zip_code` | string | optional |
| `bank_name` | string | optional |
| `routing_number` | string | optional |
| `account_number` | string | optional |
| `created_at` / `updated_at` | datetime | read-only |

---

## Optional: deposit history

```
GET /api/hiring/deposit-history/?deposit=<deposit_id>
Authorization: Bearer <dispatcher JWT>
```

Returns a list of `{ id, deposit, changed_by: {id, first_name, last_name, username}, description, created_at }`
entries, most useful for an audit-trail view. Written automatically on
every field-changing `PATCH` to `/deposits/{id}/` — the frontend never
writes to this endpoint directly.
