# Driver Company Sharing — API Update (2026-09-12)

`DriverCompany` (a driver's own legal business entity — MC number, EIN, DBA,
address, used for hiring paperwork) can now be **shared by several drivers**.
Previously every driver had their own `DriverCompany` row, even when two
drivers really worked under the same business — that forced you to type the
same name/MC/EIN into a new record for each one, with no way to keep them in
sync. Now one `DriverCompany` row can have many drivers attached to it.

This is a backend/data-model change. Two response shapes changed as a direct
result, and one new field was added to driver-creation forms. Everything else
— including the self-registration invite flow — is unchanged.

---

## 1. What did **not** change

- **Invite-link self-registration** (`POST /api/hiring/driver/invite/submit/`,
  `POST /api/hiring/driver/invite/`) still works exactly as before: every
  submission creates its own new `DriverCompany`. There is no way for an
  applicant to attach to an existing company, and there was never meant to be
  — they don't know internal company ids.
- **Adding a vehicle to an existing driver** already worked before this
  change and still works the same way: `POST /api/hiring/vehicles/` with an
  existing `driver` id. Nothing about vehicles changed here.
- The driver-company **field list itself** (`name`, `mc`, `employer_id`,
  `phone_number`, `business_as`, `business_type`, `address`, `city`, `state`,
  `zipcode`, `email`) is unchanged.

---

## 2. Response shape change: `driver` → `drivers` (a list)

Affects:
- `GET /api/hiring/driver-companies/` and `GET /api/hiring/driver-companies/{id}/`
- `GET /api/hiring/driver-company-modal/?driver={id}`

Both used to return a single nested `driver` object. They now return a
`drivers` **array**, since a company can have more than one.

**Before:**
```json
{
  "id": 12,
  "name": "Bob Hauling LLC",
  "mc": "846834",
  "driver": {
    "id": 7,
    "full_name": "Bob Smith",
    "phone_number": "+15550001111",
    "email": null,
    "emergency_phone_number": null
  }
}
```

**After:**
```json
{
  "id": 12,
  "name": "Bob Hauling LLC",
  "mc": "846834",
  "drivers": [
    {
      "id": 7,
      "full_name": "Bob Smith",
      "phone_number": "+15550001111",
      "email": null,
      "emergency_phone_number": null
    },
    {
      "id": 8,
      "full_name": "Second Driver",
      "phone_number": "+15550002222",
      "email": null,
      "emergency_phone_number": null
    }
  ]
}
```

`driver-company-modal` additionally nests `status`, `vehicle`, and `manager`
per driver in that array — same per-driver fields as before, just now one
entry per attached driver instead of a single object.

**If your UI currently reads `response.driver.full_name` (singular) for
either of these endpoints, it needs to read `response.drivers[0].full_name`
(or render the full list) instead.**

Nothing else in the API changed shape. In particular, the driver-facing
endpoints that show "a driver's company" as a single object are unaffected:
- `GET /api/hiring/driver/sign-info/?token=...` still returns a single
  `company` object (or `null`) for that one driver.
- `GET /api/hiring/vehicle-dropdown/`'s `driver.company` string is unrelated —
  that's always been the *internal dispatch company* name, not `DriverCompany`.

---

## 3. New field: `driver_company` (staff only — attach to an existing company)

Staff can now attach a driver to an **existing** `DriverCompany` instead of
typing in a new one, by passing its id.

### Single driver — create or edit

```
POST  /api/hiring/drivers/
PATCH /api/hiring/drivers/{id}/
```

```json
{
  "full_name": "New Driver",
  "driver_company": 12,
  "...": "other driver fields as usual"
}
```

- `driver_company` is optional and nullable. Omit it (or send `null`) to
  leave a driver with no company, same as today.
- `GET /api/hiring/drivers/{id}/` now includes `"driver_company": 12` (or
  `null`) — a **raw id**, not a nested object. This sits alongside the
  existing `company` field, which is unrelated (internal dispatch company,
  nested object, unchanged). To show the company's name/MC/etc for a driver,
  call `driver-company-modal?driver={id}` or `driver-companies/{id}/`
  separately.

### Bulk create (staff form)

```
POST /api/hiring/hiring/request/
```

This form previously *required* `company__name`, `company__mc`,
`company__employer_id`, and `company__phone_number` on every submission. Now
provide **exactly one** of:

- an existing company — send `driver_company: <id>` and omit the
  `company__*` fields entirely, **or**
- a new company — send the `company__*` fields as before and omit
  `driver_company`.

Sending both, or neither, is a `400`:

```json
// both driver_company and company__* fields sent
{ "non_field_errors": ["Provide either driver_company or new company details, not both."] }

// neither sent
{
  "company__name": ["This field is required when driver_company is not provided."],
  "company__mc": ["This field is required when driver_company is not provided."],
  "company__employer_id": ["This field is required when driver_company is not provided."],
  "company__phone_number": ["This field is required when driver_company is not provided."]
}
```

When `driver_company` is used, the generated W-9 and contractor agreement
PDFs are filled from that existing company's data on file (not from blank
`company__*` fields), so no other part of that form needs to change.

**This capability is staff-only.** The public invite endpoints
(`driver/invite/`, `driver/invite/submit/`) never accept `driver_company` —
if it's somehow present in a submission there, it's silently dropped and a
new company is created as usual, so there's no risk of an applicant attaching
themselves to someone else's business by guessing an id.

---

## 4. Deleting a company with drivers attached

```
DELETE /api/hiring/driver-companies/{id}/
```

Now returns `400` instead of silently deleting, if the company still has one
or more drivers attached:

```json
{ "detail": "This company still has drivers attached. Detach them before deleting it." }
```

Detach drivers first (set their `driver_company` to `null` via
`PATCH /api/hiring/drivers/{id}/`), then delete the company.
