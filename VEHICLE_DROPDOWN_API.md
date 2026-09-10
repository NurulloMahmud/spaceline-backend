# Vehicle Dropdown — API Reference (2026-09-09)

Returns the vehicles the logged-in user is allowed to see, for use in a
vehicle picker / dropdown. Each vehicle carries its driver and second driver
(if any), including their current location fields.

| Service | Base URL |
|---|---|
| **TMS** (Django, this repo) | `/api/hiring` |

Auth is the standard JWT header used by every other TMS endpoint:

```
Authorization: Bearer <access token>
```

---

## 1. Endpoint

```
GET /api/hiring/vehicle-dropdown/
```

Returns a **plain JSON array** — this endpoint is *not* paginated.

**Visibility rule:** if the logged-in user's department is `management`,
`billing`, or `payroll` (case-insensitive), you get **every** vehicle in the
system. Otherwise you only get vehicles whose driver belongs to your own
company.

### Query params

| Param | Type | Required | Effect |
|---|---|---|---|
| `requested_date` | `YYYY-MM-DD` | no | See §3, `address_updated`. |

Passing a malformed `requested_date` (not `YYYY-MM-DD`) returns:

```json
400 { "error": "Invalid requested_date format. Use YYYY-MM-DD." }
```

---

## 2. Response shape

```json
[
  {
    "id": 42,
    "vin": "1FTBW2CM5NKA12345",
    "driver": {
      "id": 7,
      "full_name": "John Smith",
      "current_address": "123 Main St",
      "current_city": "Dallas",
      "current_state": "TX",
      "current_zip": "75201",
      "current_longitude": -96.797,
      "current_latitude": 32.7767,
      "company": "Spaceline LLC",
      "address_updated": true
    },
    "second_driver": null
  }
]
```

| Field | Type | Notes |
|---|---|---|
| `id` | integer | Vehicle id. |
| `vin` | string \| null | |
| `driver` | object \| null | `null` only if the vehicle somehow has no primary driver. |
| `second_driver` | object \| null | `null` when there is no second driver. |

`driver` and `second_driver` share the same shape:

| Field | Type | Notes |
|---|---|---|
| `id` | integer | Driver id. |
| `full_name` | string | |
| `current_address` / `current_city` / `current_state` / `current_zip` | string \| null | The driver's last-known address, as free text fields (not necessarily geocoded together). |
| `current_longitude` / `current_latitude` | float \| null | |
| `company` | string \| null | Driver's company name. |
| `address_updated` | boolean \| null | See §3. |

---

## 3. `address_updated`

This tells you whether the driver's record has changed **on or after**
`requested_date`:

- `?requested_date` **omitted** → `address_updated` is always `null`. Don't
  show a badge/flag in this case.
- `?requested_date=2026-09-01` passed → `address_updated` is `true` if the
  driver row was last saved on or after `2026-09-01`, else `false`.

**Important caveat:** this flag is driven by the driver's general
`updated_at` timestamp, not a field dedicated to address/zip changes. It will
read `true` if *any* field on the driver was edited on/after that date (e.g.
their phone number), not only if the address specifically changed. Treat it
as "has this driver record been touched since X", not as a precise
address-only diff.

The flag is computed independently for `driver` and `second_driver` — a
vehicle can have `driver.address_updated: true` and
`second_driver.address_updated: false` at the same time.

---

## 4. Example

```
GET /api/hiring/vehicle-dropdown/?requested_date=2026-09-01
Authorization: Bearer <token>
```

Use this to flag, in a vehicle picker, which drivers' info might be stale
relative to some reference date (e.g. "has this driver's info been updated
since the load was requested?") — check `driver.address_updated` (and
`second_driver.address_updated` where relevant) per row.

---

## 5. Errors

| Status | When |
|---|---|
| `400` | `requested_date` present but not a valid `YYYY-MM-DD` date. |
| `401` | Missing / expired token. |
