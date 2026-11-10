# Driver team, optional vehicle, drawer data - API (release 1.4)

Backend side of `btm-driver-app/docs/SPEC-1.4.md` sections 2.4 and 2.5, for the
web app (section 3). All paths are under the TMS API base (`VITE_API_URL`, which
already ends in `/api`), so `/hiring/...` below is `{VITE_API_URL}/hiring/...`.

Every endpoint here needs the usual JWT (`Authorization: Bearer ...`), except the
public invite-link endpoints, which use `?token=`. This deployment is one
carrier: a driver that does not exist (or is not visible) is a `404`.

---

## 1. Vehicle optional on driver creation

Applies to every creation path:

| Path | Endpoint |
| --- | --- |
| Invite link form (JSON or multipart) | `POST /hiring/driver/invite/submit/?token=` |
| Invite link, old multipart form | `POST /hiring/driver/invite/?token=` |
| Onboarding "Add driver request" (and the document-AI prefill that feeds it) | `POST /hiring/hiring/request/` |
| Quick create | `POST /hiring/drivers/` |

Rules:

- **Empty vehicle block -> no Vehicle, no error.** Empty means every vehicle value
  is missing, `null`, `""`, `0` or an empty list, and no vehicle file is sent.
- **Anything filled -> the usual required fields apply** (and a Vehicle is created):
  - `hiring/request/` and `driver/invite/`: `vehicle__vehicle_type`, `vehicle__make`,
    `vehicle__model`, `vehicle__year`, `vehicle__payload`, `vehicle__gvw`. Errors come
    back per field as before: `400 {"vehicle__make": ["This field is required."]}`.
  - `driver/invite/submit/`: no vehicle field was ever required there; a partly
    filled vehicle is created as before (empty make/model, 0 payload/gvw).
  - A vehicle file (`vehicle_files`) counts as filling the vehicle block: the
    files need a vehicle to hang on. On `hiring/request/` that means the six
    fields above are then required.
- `driver/invite/submit/` reply: `files.vehicle` is `[]` and nothing else changes
  when no vehicle was made. The reply now also carries `"vehicle_id": <id or null>`.

Example (invite form, no vehicle):

```json
POST /hiring/driver/invite/submit/?token=...
{
  "company_name": "Bob Hauling LLC",
  "driver": {"driver_full_name": "Bob Driver", "phone": "+15550001111"},
  "vehicle": {"make": "", "model": "", "dock": [], "equipment": []}
}

201 {"detail": "Driver created.", "driver_id": 41, "vehicle_id": null, "w9_url": "...",
     "contract_url": "...", "files": {"driver": [], "company": [], "vehicle": []}}
```

---

## 2. Edit driver (and quick create): optional `vehicle` block

`PATCH /hiring/drivers/{id}/` and `POST /hiring/drivers/` accept an optional
`vehicle` object. It **creates the driver's vehicle when the driver has none**.

```json
PATCH /hiring/drivers/41/
{
  "full_name": "Bob Driver",
  "vehicle": {
    "vehicle_type": "Box Truck",
    "make": "Ford",
    "model": "E-450",
    "year": 2021,
    "payload": 4200,
    "gvw": 14500,
    "vin": "1FDXE4FS0MDC12345",
    "registration_plate": "ABC123",
    "length": 192, "width": 96, "height": 90,
    "door_open_width": 90, "door_open_height": 84,
    "dock_height": "Dock high", "ramps": "No",
    "equipment": ["Lift-Gate", "E-Tracks"]
  }
}
```

- Fields: any `Vehicle` column (`vehicle_type, make, model, year, payload, gvw, vin,
  registration_plate, registration_expiry_date, registration_state,
  insurance_company, insurance_expiry_date, policy_number, length, width, height,
  door_open_width, door_open_height, dock_height, ramps, note`) plus `equipment`
  (list of names).
- `vehicle` missing, `null`, `{}` or all empty: ignored (the driver edit goes through).
- Partly filled: `vehicle_type, make, model, year, payload, gvw` are required.
  `400 {"vehicle": {"make": ["This field is required."]}}`. Nothing is saved, the
  driver fields included.
- The driver already has a vehicle: `400 {"vehicle": ["This driver already has a vehicle. Edit it from the vehicle."]}`.
  Nothing is saved. (Edit an existing vehicle with `PATCH /hiring/vehicles/{id}/` as today.)
- Reply: the driver as `GET /hiring/drivers/{id}/` returns it; its `vehicle` is the new one.
  A `VehicleHistory` row "Vehicle added from Edit driver" is written.
- The same works on multipart: send `vehicle` as a JSON string.

---

## 3. Second driver (team): `POST /hiring/drivers/{id}/team/`

Sets or clears the second driver on driver `{id}`'s vehicle (its first vehicle by
id: `vehicle.second_driver`).

Request:

```json
{"second_driver_id": 57}      // set
{"second_driver_id": null}    // clear
```

Reply `200`:

```json
{
  "driver_id": 41,
  "vehicle_id": 12,
  "second_driver": {"id": 57, "full_name": "Ann Lee"},
  "moved_from": {"id": 33, "full_name": "Sam Roe"},
  "replaced": null
}
```

- `second_driver`: who is on the truck now (`null` after a clear).
- `moved_from`: the picked driver was the second driver on another driver's
  truck; that team was cleared. `{id, full_name}` of that other truck's owner,
  else `null`. Toast: "Ann Lee moved from Sam Roe's truck".
- `replaced`: the truck already had a different second driver, who was taken
  off (by this pick, or by a clear); `{id, full_name}` or `null`.
- Picking the driver who is already the second driver: `200`, nothing changes,
  `moved_from` and `replaced` are `null`.

Errors:

| Status | Body | When |
| --- | --- | --- |
| `400` | `{"detail": "second_driver_id is required (null clears).", "code": "invalid"}` | key missing or not a number |
| `400` | `{"detail": "A driver cannot be their own second driver.", "code": "same_driver"}` | `second_driver_id == id` |
| `404` | `{"detail": "No Driver matches the given query."}` | driver `{id}` or the picked driver does not exist / is not visible |
| `409` | `{"detail": "Add a vehicle to this driver first.", "code": "no_vehicle"}` | driver `{id}` has no vehicle (set or clear) |

Permission: any signed-in user, like the other `/hiring/drivers/` actions.
A `VehicleHistory` row is written for each truck that changed.

To remove the second driver from the second driver's own drawer, call the
endpoint on the **owner** (`team_with.id`) with `null`.

---

## 4. Drawer data: `GET /hiring/driver-company-modal/?driver={id}`

Existing endpoint (`useGetDriverCompanyModal`), with additions. Old keys stay.

Top level, new:

- `vehicles`: every vehicle of the drivers in `drivers[]`, plus every vehicle
  one of them drives as second driver. Shape below. `shared_with` is set when
  the drawer's driver (`?driver=`) is that vehicle's second driver.

Each `drivers[]` entry, new keys:

| Key | Meaning |
| --- | --- |
| `vehicle` | Unchanged meaning (the driver's own first vehicle, or `null`), now in the vehicle shape below. |
| `shared_vehicles` | Vehicles where this driver is the second driver, each with `shared_with` = the owner. |
| `second_driver` | `{id, full_name}` of the second driver on this driver's own vehicle, or `null`. |
| `team_with` | The team partner either way: the second driver on their truck, or the owner of the truck they drive as second driver. `null` when not teamed. |
| `team_role` | `"owner"` (has a second driver), `"second"` (drives someone's truck as second driver) or `null`. |
| `is_team_partner` | `true` when the entry is listed only because it is the drawer driver's team partner from another company (or with no company). Show it with the "Second driver" pill. |

The drawer driver's team partner is always in `drivers[]`, even from another
company.

Vehicle shape (in `vehicles`, `drivers[].vehicle`, `drivers[].shared_vehicles`):

```json
{
  "id": 12,
  "vehicle_type": "Box Truck",
  "make": "Ford",
  "model": "E-450",
  "year": 2021,
  "registration_plate": "ABC123",
  "vin": "1FDXE4FS0MDC12345",
  "driver": {"id": 41, "full_name": "Bob Driver"},
  "second_driver": {"id": 57, "full_name": "Ann Lee"},
  "shared_with": null
}
```

`shared_with` rule, one sentence: it is the owner `{id, full_name}` whenever the
vehicle is listed for its second driver: always in `shared_vehicles`, and in
`vehicles` / `drivers[].vehicle` when the drawer's driver is the second driver.

Example: Ann (57) is second driver on Bob's (41) truck 12; Ann's drawer:

```json
GET /hiring/driver-company-modal/?driver=57
{
  "id": null, "name": null, "documents": [],
  "drivers": [
    {"id": 57, "full_name": "Ann Lee", "vehicle": null,
     "shared_vehicles": [{"id": 12, "...": "...", "shared_with": {"id": 41, "full_name": "Bob Driver"}}],
     "second_driver": null, "team_with": {"id": 41, "full_name": "Bob Driver"},
     "team_role": "second", "is_team_partner": false, "...": "..."},
    {"id": 41, "full_name": "Bob Driver",
     "vehicle": {"id": 12, "...": "...", "second_driver": {"id": 57, "full_name": "Ann Lee"},
                 "shared_with": {"id": 41, "full_name": "Bob Driver"}},
     "shared_vehicles": [], "second_driver": {"id": 57, "full_name": "Ann Lee"},
     "team_with": {"id": 57, "full_name": "Ann Lee"}, "team_role": "owner",
     "is_team_partner": true, "...": "..."}
  ],
  "vehicles": [{"id": 12, "...": "...", "shared_with": {"id": 41, "full_name": "Bob Driver"}}]
}
```

The Panel's merge (`detail.vehicles`, then each `drivers[].vehicle`, by id) gives
the same object either way, so "Shared with Bob Driver" survives it.

---

## 5. Driver-app documents (for the drawer's document list)

No API change, for reference. Files the driver uploaded in the app arrive as
`DriverFile` rows named `"<title> (driver app)"` with `hub_kind` set
(`license`, `medical_card`, `other`). Dispatcher files named with "license",
"licence", "CDL", "DL" or "medical" (for example the "Driver license" slot or
"Medical card") are sent to the driver app; other files (W-9, contract, driving
record, photos) stay with the carrier.
