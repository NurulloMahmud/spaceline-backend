# Driver Sign Link — API Reference (2026-08-20)

For an **already-registered** driver who still needs to review and sign
their W-9 and contractor agreement. Staff generates a one-time link; the
driver opens it with no login, reviews their info and the two documents,
signs them client-side, and uploads the signed copies back. The link stops
working the moment that upload succeeds.

| Service | Base URL |
|---|---|
| **TMS** (Django, this repo) | `/api/hiring` |

---

## The flow, end to end

1. Staff (logged in) calls **1. Generate a sign link** for a specific driver.
2. Staff sends that link to the driver (text, email, whatever).
3. Driver opens the link — no login. Frontend calls **2. Get driver info**
   with the `token` from the URL to render the page: driver's own info, the
   vehicle, the company, and the current (unsigned) W-9 and contract PDFs to
   display/download for review.
4. Driver signs both documents (however the frontend collects a signature —
   canvas drawing burned into the PDF, typed name, whatever you're doing for
   this).
5. Frontend calls **3. Upload signed documents** with the token, the
   `driver_id` (from step 2's response), and the two signed files.
6. That's it — the link is now dead. Calling step 2 or step 3 again with the
   same token returns `"Link has expired or is inactive."`

---

## 1. Generate a sign link (staff-authenticated)

```
POST /api/hiring/driver/sign-link/
Authorization: Bearer <dispatcher JWT>
Content-Type: application/json
```

```json
{ "driver_id": 42 }
```

**Response `201`:**

```json
{ "link": "https://spaceline.boxtruckmanage.com/driver-sign/?token=6ba84fe4-38ba-468e-bfc7-7043bf21a976" }
```

The `driver-sign/` path in that URL is a placeholder — point it at whatever
route the frontend uses for this page. The link is valid for **7 days** or
until it's used once, whichever comes first.

**Errors:** `400` if `driver_id` is missing; `404` if that driver doesn't exist.

---

## 2. Get driver info (public — no auth)

```
GET /api/hiring/driver/sign-info/?token=<token from the link>
```

**Response `200`:**

```json
{
  "driver": {
    "id": 42,
    "full_name": "Ricardo Carmona",
    "phone_number": "(618) 823-9219",
    "email": "dwayneandmariemoore@gmail.com",
    "address": null,
    "unit_number": null,
    "city": "Westchester",
    "state": "IL",
    "zip_code": "60154"
  },
  "vehicle": {
    "id": 2,
    "vehicle_type": "Cargo Van",
    "make": "ford",
    "model": "ytut",
    "year": 2020
  },
  "company": {
    "name": "Rodriguez Freight Solutions",
    "mc": "846834",
    "address": "742 Evergreen Terrace",
    "city": "Springfield",
    "state": "Illinois",
    "zipcode": "62704"
  },
  "files": [
    { "id": 101, "name": "W-9 (Generated)", "url": "https://.../driver_files/w9_42.pdf" },
    { "id": 102, "name": "Contractor Agreement (Generated)", "url": "https://.../driver_files/contract_42.pdf" }
  ]
}
```

`vehicle` and `company` are `null` if the driver doesn't have one on file —
handle that case in the UI. `files` only ever contains the generated W-9 and
contract (never other documents on the driver, and never anything already
signed from a prior round).

**Deliberately not included:** SSN, bank/deposit details, or anything else
sensitive. This endpoint has no auth, so only what's needed to review and
sign is returned.

**Errors:**
| Status | Meaning |
|---|---|
| `400` | Missing/invalid `token`, or `"Link has expired or is inactive."` (already used, or past its 7-day window) |

---

## 3. Upload the signed documents

```
POST /api/hiring/driver/invite/documents/?token=<same token>
Content-Type: multipart/form-data
```

Same upload endpoint the registration flow uses — nothing sign-link-specific
about its shape.

**Fields:**

| Field | Type | Notes |
|---|---|---|
| `driver_id` | integer | from step 2's `driver.id` |
| `files` | file[] | repeat the `files` key per file — send both signed PDFs here |
| `names` | string[] | repeat `names` per file, same order/count as `files` (e.g. `"Signed W-9"`, `"Signed Contract"`) |

`files` and `names` must be the same length.

**Response `201`:**

```json
{
  "detail": "Documents uploaded.",
  "files": [
    { "id": 201, "name": "Signed W-9", "url": "https://.../driver_files/..." },
    { "id": 202, "name": "Signed Contract", "url": "https://.../driver_files/..." }
  ]
}
```

**On success, the link immediately deactivates.** Any further call to step
2 or step 3 with the same token returns `400` `"Link has expired or is
inactive."` — if something goes wrong after this point, staff needs to
generate a fresh link (step 1) for the driver to try again.

**Errors:** `400` missing/invalid/expired token, `files`/`names` count
mismatch, or missing `driver_id`; `404` if `driver_id` doesn't belong to the
company that owns this link.
