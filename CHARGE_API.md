# Billing / Charge — API Reference (2026-09-10)

The company's **payment method + billing history** for using the platform. Cards, upcoming
charges, and the paid ledger all live in a separate internal **billing service** (Stripe under
the hood). This repo's `charge` app is a thin proxy: the frontend calls `/api/charge/*`, and
those views call the billing service server-side with our API key. **The frontend never calls
the billing service or Stripe directly.**

| Service | Base URL | Auth |
|---|---|---|
| **TMS** (Django, this repo) | `/api/charge` | `Authorization: Bearer <JWT>` |

The payer is the **company** — every user in a company shares one payment method and one
transaction history. There is nothing to "create"; a company's billing record springs into
existence the first time it adds a card.

---

## Who can do what

| Action | Allowed |
|---|---|
| View summary / cards / upcoming / transactions / recurring | any authenticated user with a company |
| Add a card, set default, remove a card | **management** or **billing** department only (else `403`) |

A user with no company gets `400`. Recurring plans are **read-only** here — they're set up by
the platform owner, not the tenant.

---

## The flow, end to end

1. On the billing screen, call **1. Summary**. It always returns `200` with a stable shape,
   even for a company that has never set up billing (`has_card: false`, empty lists).
2. If `has_card` is false → show "Add a payment method". Call **5. Start card setup**, then
   `window.location = checkout_url`.
3. Stripe collects the card on its own hosted page and redirects back to your `return_url` with
   `?billing_setup_session=<id>&checkout_session_id=cs_...` appended (or `&canceled=1`).
   **The redirect is not confirmation.** On that page, poll **6. Card setup status** until
   `status: "completed"` (then `resulting_card_id` is set) or `expired` / `canceled`.
4. Show upcoming charges from **3. Upcoming** and history from **4. Transactions**.

---

## 1. Summary

`GET /api/charge/summary/`

```json
{
  "external_user_id": "42",
  "has_card": true,
  "cards_count": 2,
  "default_card": { "id": "uuid", "brand": "visa", "last4": "4242", "exp_month": 12,
                    "exp_year": 2030, "funding": "credit", "is_default": true,
                    "status": "active", "created_at": "..." },
  "active_recurring_count": 1,
  "next_charge": { "recurring_id": "uuid", "period": "2026-10", "scheduled_date": "2026-10-08",
                   "amount": 4200, "currency": "usd", "status": "scheduled",
                   "date_overridden": false, "description": "Monthly plan" },
  "lifetime_paid": [ { "currency": "usd", "amount": 12345 } ]
}
```

`amount` is always **minor units** (cents). `next_charge` is `null` if there are no active plans.

## 2. List cards

`GET /api/charge/cards/` → array of the `default_card` shape above (default first). `[]` if none.

## 3. Upcoming charges ("what will I be billed")

`GET /api/charge/upcoming/`

```json
[ { "recurring_id": "uuid", "period": "2026-10", "scheduled_date": "2026-10-08",
    "amount": 4200, "currency": "usd", "status": "scheduled",
    "date_overridden": false, "description": "Monthly plan" } ]
```

Sorted by date ascending. `[]` if none.

## 4. Transactions (the paid ledger)

`GET /api/charge/transactions/?type=&period=YYYY-MM&limit=50&offset=0`

```json
[ { "id": "uuid", "type": "charge", "amount": 4200, "currency": "usd", "charge_id": "uuid",
    "period": "2026-09", "description": "Monthly plan", "stripe_object_id": "pi_...",
    "occurred_at": "2026-09-08T09:00:01Z" } ]
```

Newest first. `type` ∈ `charge`, `refund`, `dispute`, `dispute_reversal`. `amount` is **signed**
— `charge` / `dispute_reversal` positive, `refund` / `dispute` negative. All params optional.

## 5. Start card setup

`POST /api/charge/cards/setup/`  · *management / billing only*

```json
// request (both optional)
{ "return_url": "https://app.example.com/billing/cards", "client_reference": "anything" }
```
```json
// response
{ "id": "uuid", "status": "pending", "checkout_url": "https://checkout.stripe.com/...",
  "publishable_key": "pk_test_...", "resulting_card_id": null, "created_at": "..." }
```

Redirect the browser to `checkout_url`. If `return_url` is omitted, the backend uses
`BILLING_CARD_RETURN_URL` (defaults to `FRONTEND_URL` + `/billing/cards`). In production the
`return_url` host must be on the billing project's allow-list — coordinate with the backend
owner; `localhost` is always allowed in dev.

## 6. Card setup status

`GET /api/charge/cards/setup/<session_id>/`

```json
{ "id": "uuid", "status": "pending|completed|expired|canceled",
  "resulting_card_id": "uuid|null", "return_url": "...", "created_at": "..." }
```

`completed` + `resulting_card_id` = the card is saved. Poll this after the Stripe redirect (also
covers the user closing the tab without returning — the card can still land as `completed`).

## 7. Set default card

`POST /api/charge/cards/<card_id>/default/`  · *management / billing only* → the updated card

## 8. Remove a card

`DELETE /api/charge/cards/<card_id>/`  · *management / billing only* → `{ "message": "card removed" }`

## 9. Recurring plans (read-only)

`GET /api/charge/recurring/?status=active|paused|canceled`

```json
[ { "id": "uuid", "amount": 4200, "currency": "usd", "day_of_month": 8,
    "description": "Monthly plan", "status": "active", "start_date": "2026-01-01",
    "end_date": null, "card_id": null, "created_at": "..." } ]
```

---

## Errors

| Status | When |
|---|---|
| `400` | your account isn't linked to a company |
| `401` | missing / invalid JWT |
| `403` | card mutation attempted by a non management/billing user |
| `404` | unknown `card_id` / `session_id` |
| `409` | e.g. defaulting a removed card |
| `502` | billing service unreachable |

Read views (`summary`, `cards`, `upcoming`, `transactions`, `recurring`) never return `404` for
a brand-new company — they return the empty shape so you can render one consistent screen.
