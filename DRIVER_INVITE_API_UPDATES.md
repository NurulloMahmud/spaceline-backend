# Driver Invite / Onboarding — API Updates (2026-08-15)

New backend work only. This does not re-document the existing invite-link
flow (`GenerateInviteLinkView`, the multipart `DriverBulkCreateInviteView`) —
those are unchanged. This covers three new endpoints and a handful of new
fields.

| Service | Base URL |
|---|---|
| **TMS** (Django, this repo) | `/api` |

All endpoints below are **public** (no `Authorization` header) — they're
called from the driver-facing registration form, before the driver has any
account. Each is scoped by the invite `token` from the registration link
instead.

---

## 1. Check if a driver already exists (email / phone)

Call this before/while the driver fills the form, to warn about duplicates
before they submit.

```
POST /api/hiring/driver/exists/
Content-Type: application/json
```

**Request** — either field alone is fine, but at least one is required:

```json
{ "email": "driver@example.com", "phone_number": "+12049820348" }
```

**Response** `200`:

```json
{ "email_exists": true, "phone_exists": false }
```

**Errors:** `400` if both `email` and `phone_number` are omitted.

Notes:
- Email match is case-insensitive. Phone match is an exact string match
  against however it's stored — if you reformat phone numbers before sending
  (dashes, `+1`, spaces), be consistent, since a differently-formatted
  duplicate won't be caught.
- This checks across **all companies**, not just the inviting one.

---

## 2. New fields on driver creation (existing endpoints)

These four fields now exist on `Driver` / `Vehicle` and are accepted by the
**existing** multipart bulk-create endpoints
(`POST /api/hiring/hiring/request/`, `POST /api/hiring/driver/invite/`) and
returned on every driver/vehicle read endpoint (`GET /api/hiring/drivers/`,
`/drivers/{id}/`, `/vehicles/`, etc.):

| Field | On | Type |
|---|---|---|
| `tax_exempt` | Driver | boolean |
| `payee_code` | Driver | string |
| `fatca_reporting_code` | Driver | string |
| `vehicle__dock_height` (write) / `dock_height` (read) | Vehicle | string |

Nothing else about those two endpoints changed.

---

## 3. Submit driver registration (new, JSON, no files)

This is the new primary registration endpoint — the frontend sends form
values only, no file uploads. The backend creates the driver/company/vehicle
records **and generates a W-9 and the inviting company's contractor
agreement**, returning URLs to both.

```
POST /api/hiring/driver/invite/submit/?token=<invite token>
Content-Type: application/json
```

### Request body

Matches the payload shape already in use — flat `company_*` keys, plus
nested `driver` and `vehicle` objects. Duplicated top-level `driver_full_name`
/ `phone` are fine (both read, nested takes priority).

```json
{
  "company_name": "Rodriguez Freight Solutions",
  "company_applicant_first_name": "Jane",
  "company_applicant_last_name": "Rodriguez",
  "company_doing_business": "Rodriguez Freight",
  "company_email": "jane@example.com",
  "company_phone": "+12049820348",
  "company_emergency_phone": "+12049820349",
  "company_employer_id": "87-6543210",
  "company_type": "1",
  "company_address": "742 Evergreen Terrace",
  "company_city": "Springfield",
  "company_state": "Illinois",
  "company_zip": "62704",

  "driver": { "driver_full_name": "Jane Rodriguez", "phone": "+12049820348" },

  "vehicle": {
    "make": "Ford",
    "model": "Transit",
    "useful_cargo_length": "144",
    "useful_cargo_width": "70",
    "useful_cargo_height": "70",
    "GVW_lbs": "9500",
    "payload_lbs": "3800",
    "door_width": "60",
    "door_height": "75",
    "dock": ["Ground level"],
    "equipment": ["Lift-gate", "Air ride"]
  }
}
```

**`company_state` and `equipment`/`dock` are plain text, not IDs** — send the
actual state name and equipment names, e.g. `"Illinois"` not a lookup id,
`["Lift-gate", "Air ride"]` not `["4"]`. `dock` accepts either a single
string or a one-item array.

**`company_type`** is the W-9 federal tax classification, `"1"`–`"7"`,
positionally: `1` Individual/sole proprietor, `2` C corporation, `3` S
corporation, `4` Partnership, `5` Trust/estate, `6` LLC, `7` Other.

Required: `company_name` and `driver_full_name` (nested or top-level).
Everything else is optional and defaults to blank/zero if omitted.

### Response `201`

```json
{
  "detail": "Driver created.",
  "driver_id": 42,
  "w9_url": "https://.../driver_files/w9_42.pdf",
  "contract_url": "https://.../driver_files/contract_42.pdf"
}
```

Both PDFs are pre-filled from the submitted data. **Neither is signed yet** —
signature and date are intentionally left blank. Show them to the driver for
review, collect a signature client-side, then send the signed files back
through the upload endpoint below.

### Errors

| Status | Meaning |
|---|---|
| `400` | Missing/invalid `token`, expired or inactive invite link, missing `company_name`/`driver_full_name`, or a files/names count mismatch |
| `409` | This link already registered a driver |
| `422` | The inviting company has no contract template configured yet (`Company.contract_template_text` unset) — driver/company/vehicle records are **not** created in this case, ask the company to set one up first |

---

## 3a. Sending files with the registration (new)

The same endpoint also accepts `multipart/form-data`, so paperwork can go up
with the form instead of in a second call. **Send files here whenever they
belong to the company or the vehicle** — the upload endpoint in section 4
only ever files things under the driver.

```
POST /api/hiring/driver/invite/submit/?token=<invite token>
Content-Type: multipart/form-data
```

Three independent pairs, each a repeated file key plus a repeated name key:

| Files key | Names key | Stored on |
|---|---|---|
| `driver_files` | `driver_file_names` | the driver (`DriverFile`) — license, medical card, signed W-9 |
| `company_files` | `company_file_names` | the driver's company (`CompanyFile`) — MC/USDOT, W-9, COI |
| `vehicle_files` | `vehicle_file_names` | the vehicle (`VehicleFile`) — registration, insurance, truck photos |

Each pair must be the same length — a mismatch returns `400` and **nothing is
created**, not a driver with half its documents. All six keys are optional;
sending none is a valid registration. Names are free text and become the
file's label in the TMS. These are the same key names the older multipart
endpoint (`POST /driver/invite/`) already uses.

Under multipart the `driver` and `vehicle` objects have to be
**JSON-stringified** — everything else stays a flat field:

```js
const form = new FormData();
form.append('company_name', 'Rodriguez Freight Solutions');
form.append('company_state', 'Illinois');
form.append('driver', JSON.stringify({ driver_full_name: 'Jane Rodriguez', phone: '+12049820348' }));
form.append('vehicle', JSON.stringify({ make: 'Ford', model: 'Transit', dock: ['Ground level'], equipment: ['Lift-gate'] }));

form.append('vehicle_files', registrationFile);
form.append('vehicle_file_names', 'Registration');
form.append('vehicle_files', insuranceFile);
form.append('vehicle_file_names', 'Insurance');
form.append('company_files', mcAuthorityFile);
form.append('company_file_names', 'MC Authority');

await fetch(`/api/hiring/driver/invite/submit/?token=${token}`, { method: 'POST', body: form });
```

`dock` and `equipment` may also be sent as repeated top-level form keys if
that's easier than nesting them. Booleans (`tax_exempt`) are read as
`"true"`/`"1"`/`"yes"`/`"on"` — any other string, `"false"` included, is false.

The response is the same as the JSON one plus a `files` object grouping what
was stored:

```json
{
  "detail": "Driver created.",
  "driver_id": 42,
  "w9_url": "https://.../driver_files/w9_42.pdf",
  "contract_url": "https://.../driver_files/contract_42.pdf",
  "files": {
    "driver": [],
    "company": [ { "id": 12, "name": "MC Authority", "url": "https://.../company_files/..." } ],
    "vehicle": [ { "id": 34, "name": "Registration", "url": "https://.../vehicle_files/..." } ]
  }
}
```

**A plain JSON body still works unchanged** — nothing about section 3 changed
for a frontend that sends no files.

---

## 4. Upload additional / signed documents (new)

For anything after the initial submit: extra required documents (license,
insurance, MC/USDOT, vehicle photos, etc.) and the **signed-back** W-9 and
contract PDFs from step 3.

```
POST /api/hiring/driver/invite/documents/?token=<invite token>
Content-Type: multipart/form-data
```

**Fields:**

| Field | Type | Notes |
|---|---|---|
| `driver_id` | integer | from the step-3 response |
| `files` | file[] | repeat the `files` key per file |
| `names` | string[] | repeat the `names` key per file, same order/count as `files` |

`files` and `names` must be the same length — mismatched counts return `400`.

**Response `201`:**

```json
{
  "detail": "Documents uploaded.",
  "files": [
    { "id": 101, "name": "Driver's License", "url": "https://.../driver_files/..." },
    { "id": 102, "name": "Signed W-9", "url": "https://.../driver_files/..." }
  ]
}
```

**Errors:** `400` missing/invalid token or `files`/`names` count mismatch;
`404` if `driver_id` doesn't belong to the company that owns this invite
token.

---

## Summary of what needs frontend work

| # | Endpoint | Work needed |
|---|---|---|
| 1 | `POST /driver/exists/` | Call on blur/debounce for email + phone fields, show inline duplicate warning |
| 2 | — | Nothing required; new fields are additive |
| 3 | `POST /driver/invite/submit/` | Switch the registration form's final submit to this endpoint; send `company_state` as a name, `equipment`/`dock` as name arrays; on success, render the two returned PDF URLs for driver review |
| 3a | `POST /driver/invite/submit/` | To attach paperwork at registration, post it as multipart with `driver_files` / `company_files` / `vehicle_files` and their `*_file_names`, `driver` and `vehicle` JSON-stringified. Required for anything that belongs to the company or the vehicle |
| 4 | `POST /driver/invite/documents/` | Build the document-upload step: required docs + the signed-back W-9/contract after the driver signs client-side. Everything sent here files under the **driver** |
