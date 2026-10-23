# New analytics endpoints — frontend guide

Three read-only endpoints. All use the same login token as the rest of the app:

```
Authorization: Bearer <access_token>
```

Every result is limited to the logged-in user's company, except where noted.

| # | What | Service | URL |
|---|---|---|---|
| 1 | Average bid speed | atrek | `GET /api/v1/analytics/dispatch` (new field `avg_bid_seconds`) |
| 2 | Broker response | email-agent | `GET /email-agent/api/v1/negotiations/stats` |
| 3 | Drivers report | Django | `GET /api/billing/drivers-report/` |

Use the same host you already use for each service (`/api/v1/` → atrek, `/email-agent/` → email-agent, `/api/` → Django).

---

## 1. Average bid speed — `avg_bid_seconds`

No new URL. The existing dispatch analytics endpoint now has one more field.

```
GET /api/v1/analytics/dispatch?range=7d
```

Query params are unchanged:

| Param | Values | Default |
|---|---|---|
| `range` | `24h`, `7d`, `1m`, `12m`, `custom` | `24h` |
| `start`, `end` | RFC3339 timestamps, only with `range=custom` | — |

New in the response:

```json
{
  "success": true,
  "data": {
    "bids_count": 225,
    "avg_bid_seconds": 412.5,
    "dispatchers": [
      { "id": 48, "name": "Lawrence Washington", "bids_count": 51, "avg_bid_seconds": 301.2 },
      { "id": 50, "name": "Marcus Walker",       "bids_count": 27, "avg_bid_seconds": null }
    ]
  }
}
```

(Other fields such as `loads_count`, `brokers`, `states` are unchanged and omitted here.)

- `avg_bid_seconds` is the average time, **in seconds**, from a load being posted to our company's **first** bid on it. Only loads whose first bid falls inside the period are counted.
- On a dispatcher, it is the average for that dispatcher's own first bids.
- `null` means there was nothing to average in that period. Show "—", not 0.
- Format it yourself for display (for example 412.5 → "6m 52s").

---

## 2. Broker response

How many bids were sent in a period, and how many got at least one email reply from the broker.

```
GET /email-agent/api/v1/negotiations/stats?start=2026-10-01&end=2026-10-31
```

| Param | Format | Default |
|---|---|---|
| `start` | ISO date (`2026-10-01`) or datetime | 30 days before `end` |
| `end` | ISO date or datetime. A date alone covers that whole day. | now |

Response:

```json
{
  "period": { "start": "2026-10-01T00:00:00+00:00", "end": "2026-10-31T23:59:59.999999+00:00" },
  "bids_sent": 3121,
  "broker_replied": 390,
  "reply_rate": 12.5,
  "dispatchers": [
    { "id": 7, "name": "Jane Dispatcher", "bids_sent": 1800, "broker_replied": 240, "reply_rate": 13.3 },
    { "id": 8, "name": "John Dispatcher", "bids_sent": 1321, "broker_replied": 150, "reply_rate": 11.4 }
  ]
}
```

- `reply_rate` is a percentage (12.5 means 12.5%). It is `null` when `bids_sent` is 0.
- A bid is counted by the date it was sent.
- "Replied" means the broker sent at least one email back. Automatic replies (out-of-office, etc.) are counted too.
- `dispatchers` is sorted by `bids_sent`, highest first. `name` can be `null` if the user could not be looked up.

Errors:

| Status | `detail.code` | When |
|---|---|---|
| 422 | `invalid_period` | `start` / `end` is not a valid date, or `end` is before `start` |
| 403 | — | the account is not linked to a company |

---

## 3. Drivers report

One row per driver: number of loads, carrier pay, driver pay and profit.

```
GET /api/billing/drivers-report/?start_date=2026-10-01&end_date=2026-10-31
```

| Param | Format | Default |
|---|---|---|
| `start_date` | `YYYY-MM-DD` | today |
| `end_date` | `YYYY-MM-DD` | today |
| `date_type` | `pickup_date`, `drop_date` or `delivered_at` — which load date the period applies to | `pickup_date` |
| `company` | company id. **Only** management, billing and payroll users; ignored for everyone else | all companies |

Response (a plain list, no wrapper), sorted by profit, highest first:

```json
[
  {
    "driver__id": 12,
    "driver__full_name": "Ann Driver",
    "driver__unit_number": "U1",
    "total_loads": 2,
    "total_carrier_pay": "1500.00",
    "total_driver_pay": "1000.00",
    "total_profit": "500.00"
  }
]
```

- Money values are **strings** with two decimals — convert before summing or formatting.
- `total_profit` = carrier pay − driver pay.
- Cancelled and rejected loads are not included. Loads without a driver are not included.
- Dispatch users always see only their own company. Management, billing and payroll see all companies, or one if `company` is given.
- `driver__unit_number` can be `null`.

Errors (all `400`, body `{ "message": "..." }`):

| Message | When |
|---|---|
| `Invalid date format. Use YYYY-MM-DD.` | a date is missing its format or unparseable |
| `end_date cannot be before start_date.` | the dates are reversed |
| `date_type must be one of pickup_date, drop_date, delivered_at.` | unknown `date_type` |
