# Document Parsing (AI Autofill) — API Reference (2026-08-29)

Upload the documents a driver sent — license, registration, COI, W-9, voided
check — and get back the fields printed on them, grouped by the form section
they belong to, ready to prefill the driver-creation form.

| Service | Base URL |
|---|---|
| **TMS** (Django, this repo) | `/api/hiring` |

**Nothing is saved.** This endpoint creates no `Driver`, stores no file, and
writes nothing to the database. It returns a *suggestion* for a human to
confirm; the existing create/update endpoints
(`/driver/invite/submit/`, `/hiring/request/`, `/drivers/{id}/`, `/deposits/`)
are still what persist anything. A bad extraction costs nothing but a retry.

---

## Parse documents

```
POST /api/hiring/documents/parse/
Authorization: Bearer <JWT>
Content-Type: multipart/form-data
```

| Field | Type | Notes |
|---|---|---|
| `files` | file[] | repeat the key per file. `file` is accepted as an alias for a single upload |
| `document_types` | string[] | **optional** hint per file, positional against `files`. Fewer hints than files is fine |

- Up to **10 files** per request, **15 MB** each.
- Accepted: PDF, JPEG, PNG, WebP, GIF, and HEIC/HEIF (iPhone photos are
  converted to JPEG server-side). Anything else comes back as a per-file
  error without an AI call being spent.
- Files are parsed **concurrently**, so a batch of five costs roughly one
  document's latency (~2–5s), not five.

### Document types

Passing `document_types` is only a hint — the backend reports what the
document actually *is*, so a mislabelled upload gets flagged instead of being
forced into the wrong shape.

| `document_type` | What it fills |
|---|---|
| `cdl` | Driver: name, dob, address, all `cdl_*` |
| `vehicle_registration` | Vehicle: vin, make, model, year, plate, reg state/expiry, gvw |
| `insurance_coi` | Vehicle: insurance_company, policy_number, insurance_expiry_date |
| `mc_authority` | Company: mc, legal name, address |
| `w9` | Company: employer_id, business_type, name, business_as |
| `voided_check` | Deposit: bank_name, routing_number, account_number |
| `medical_card` | Driver: medical_exp_date |
| `unknown` | Not recognized — extraction is returned but flagged in `warnings` |

---

## Response `200`

```json
{
  "results": [
    {
      "file_name": "license.jpg",
      "document_type": "cdl",
      "document_type_label": "Driver's License / CDL",
      "fields": {
        "full_name": "RICARDO CARMONA",
        "dob": "1988-07-02",
        "cdl_number": "C123-4567-8901",
        "cdl_state": "IL",
        "cdl_class": "C",
        "cdl_issue_date": "2022-03-14",
        "cdl_expiration": "2030-03-14",
        "cdl_endorsement": "N",
        "address": "742 EVERGREEN TERRACE",
        "city": "SPRINGFIELD",
        "state": "IL",
        "zip_code": "62704"
      },
      "warnings": [],
      "error": null
    },
    {
      "file_name": "registration.pdf",
      "document_type": "vehicle_registration",
      "document_type_label": "Vehicle Registration",
      "fields": {
        "vin": "1FTBW2CM1JKA00001",
        "make": "FORD",
        "model": "TRANSIT 350",
        "year": 2018,
        "registration_plate": "PGY-4471",
        "registration_state": "OH",
        "registration_expiry_date": "2026-11-30",
        "gvw": 9500,
        "company_name": "BOB HAULING LLC"
      },
      "warnings": [],
      "error": null
    }
  ],
  "prefill": {
    "driver": { "full_name": "RICARDO CARMONA", "cdl_number": "C123-4567-8901", "...": "..." },
    "company": { "name": "BOB HAULING LLC" },
    "vehicle": { "vin": "1FTBW2CM1JKA00001", "year": 2018, "...": "..." },
    "deposit": {}
  },
  "conflicts": [],
  "sensitive_fields": []
}
```

### `prefill` — what the form should use

The merged, form-shaped payload. **Keys are the real model field names**, so
each section drops straight into form state with no mapping table:

| Section | Goes to |
|---|---|
| `driver` | `Driver` fields — matches the `driver` object / flat driver keys on submit |
| `company` | `DriverCompany` fields — the `company_*` keys on submit (`name` → `company_name`, `zipcode` → `company_zip`, …) |
| `vehicle` | `Vehicle` fields |
| `deposit` | `Deposit` fields (`/deposits/`, **not** `/driver-companies/` — see DEPOSITS_API.md) |

Dates are always `YYYY-MM-DD`. States are always 2-letter. EIN comes back as
`XX-XXXXXXX`. `year`, `gvw`, `payload` are integers. A field the model
couldn't read cleanly is **omitted**, never returned half-parsed — so a value
that is present is safe to put in an input.

### `results` — per-file detail

Use it to show the user what each upload contributed, and to surface
`warnings`. Notable warnings:

- `"cdl_expiration is 2020-01-31, which has already passed."` — expired document
- `"Uploaded as 'cdl' but looks like 'insurance_coi' (Certificate of Insurance)."` — mislabelled
- `"routing_number has 8 digits, expected 9 — verify it."`
- `"This document was not recognized as one of the supported types…"`

A file that failed has a non-null `error` and empty `fields`; **the rest of
the batch still succeeds**, and `prefill` is built from the ones that worked.

### `conflicts` — two documents disagree

```json
[{
  "section": "vehicle", "field": "vin",
  "value": "1FTBW2CM1JKA00001", "from_file": "registration.pdf",
  "other_value": "1FTBW2CM1JKA99999", "other_file": "coi.pdf"
}]
```

First file in upload order wins in `prefill`; the disagreement is reported
rather than silently resolved. Show these — a registration and a COI naming
different VINs is exactly what a recruiter needs to look at.

### `sensitive_fields` — require confirmation

```json
[{ "section": "deposit", "field": "routing_number" }]
```

SSN, routing number, and account number are extracted but listed here. **Do
not silently autofill them** — make the user confirm each against the
document before submitting.

---

## Errors

| Status | Meaning |
|---|---|
| `400` | No file sent, or more than 10 files |
| `401` | Missing/expired JWT |

Per-file problems (unsupported type, oversized, empty, unreadable, AI failure)
are **not** request-level errors — they come back as `error` on that file's
entry with `200` overall.

---

## Suggested frontend flow

1. Recruiter drops in whatever the driver sent (one file or ten).
2. `POST /documents/parse/` with all of them; optionally tag each with a
   `document_types` hint if your UI already asks what the file is.
3. Merge `prefill.driver` / `.company` / `.vehicle` / `.deposit` into the
   form, marking each touched field as AI-filled so the user sees what to check.
4. Show `warnings` per file and `conflicts` inline on the affected fields.
5. Leave `sensitive_fields` blank-but-suggested, requiring an explicit confirm.
6. Recruiter reviews, corrects, and submits through the normal endpoints.
7. The files themselves still get uploaded through
   `/driver/invite/documents/` — parsing does not store them.
