"""Keep the driver app's payouts equal to accounting's.

Accounting keeps one Payout per load: what the driver is paid for it. Its
`gross` is already net of the Quick Pay fee (take_quick_pay_fee moves the fee
out of gross and into `quick_pay_fee`), and `amount`, gross minus the expenses
the payout takes (PayoutExpense), is what accounting pays. The driver app gets:

    gross         gross + quick_pay_fee, what the load earned the driver
    lines         the fee, then each expense, as negative amounts
    deductions    the expenses taken (not the fee, which has its own field)
    net           Payout.amount, so gross + lines = net

Every change that moves those numbers, and a payout's delete or move to
another driver, is pushed after the transaction commits. Nothing here may break
accounting: hub errors are logged and `manage.py sync_hub_payouts` resends all.
"""

import logging
import threading
from decimal import Decimal
from typing import Any, Callable

from django.db import connections, transaction

from accounting.models import ZERO, Payout
from accounting.services import QUICK_PAY_PERCENT
from billing.models import Load, LoadStop

from .services import driver_hub

logger = logging.getLogger(__name__)


def external_id(payout_id: int) -> str:
    return f"payout-{payout_id}"


def _money(value: Decimal | None) -> float:
    return float((value or ZERO).quantize(Decimal("0.01")))


def _place(stop: LoadStop | None) -> str:
    if stop is None:
        return ""
    return ", ".join(
        p for p in ((stop.city or "").strip(), (stop.state or "").strip()) if p
    )


def load_summary(load: Load) -> dict[str, Any]:
    stops = list(load.loadstop_set.order_by("order", "id"))
    pickup = next((s for s in stops if s.load_pickup), stops[0] if stops else None)
    drop = next((s for s in stops if s.load_drop), stops[-1] if stops else None)
    reference = load.load_number or (f"SH - {load.shipment}" if load.shipment else "")
    return {
        "reference": reference,
        "pick_up_at": _place(pickup),
        "deliver_to": _place(drop),
        "pick_up_date": driver_hub.rfc3339(load.pickup_date),
        "delivery_date": driver_hub.rfc3339(load.drop_date),
    }


def payout_body(payout: Payout) -> dict[str, Any] | None:
    """What POST /t/v1/payouts gets for this payout, or None if its driver is not in the app."""
    load = payout.load
    driver = load.driver if load.driver_id else None
    if driver is None or not driver.hub_driver_id:
        return None
    fee = payout.quick_pay_fee or ZERO
    links = list(
        payout.expenses.select_related("deduction", "deduction__type").order_by("id")
    )
    lines = []
    if fee > 0:
        lines.append(
            {
                "kind": "fee",
                "label": f"Quick Pay fee ({QUICK_PAY_PERCENT.normalize()}%)",
                "amount": -_money(fee),
            }
        )
    for link in links:
        deduction = link.deduction
        label = (
            (deduction.type.name if deduction.type_id else None)
            or deduction.note
            or "Expense"
        )
        lines.append(
            {"kind": "expense", "label": label, "amount": -_money(link.amount)}
        )
    return {
        "external_id": external_id(payout.id),
        "hub_driver_id": int(driver.hub_driver_id),
        "status": payout.status,
        "gross": _money(payout.gross + fee),
        "quick_pay_fee": _money(fee),
        "deductions": _money(sum((link.amount for link in links), ZERO)),
        "net": _money(payout.amount),
        "paid_at": driver_hub.rfc3339(payout.paid_at),
        "load": load_summary(load),
        "lines": lines,
    }


def push_payout(payout_id: int) -> bool | None:
    """Send one payout as it is now. True if sent, None if there was nothing to send."""
    payout = (
        Payout.objects.select_related("load", "load__driver")
        .filter(pk=payout_id)
        .first()
    )
    if payout is None:
        return None
    body = payout_body(payout)
    if body is None:
        return None
    driver_hub.upsert_payout(body)
    return True


class _Pending:
    """The payouts one transaction touched, sent once when it commits."""

    def __init__(self) -> None:
        self.push: set[int] = set()
        self.delete: set[int] = set()
        self.done = False

    def __call__(self) -> None:
        self.done = True
        steps: list[tuple[str, int, Callable[[], Any]]] = [
            ("delete", pk, lambda pk=pk: driver_hub.delete_payout(external_id(pk)))
            for pk in sorted(self.delete)
        ] + [("push", pk, lambda pk=pk: push_payout(pk)) for pk in sorted(self.push)]
        for position, (what, pk, step) in enumerate(steps):
            try:
                step()
            except driver_hub.DriverHubError as exc:
                logger.warning(
                    "payout %s not %s: %s",
                    pk,
                    "deleted on the hub" if what == "delete" else "sent to the hub",
                    exc,
                )
                if exc.status_code >= 500:
                    # Down or unreachable: do not wait on it once per payout.
                    left = len(steps) - position - 1
                    if left:
                        logger.warning(
                            "%s more payout change(s) not sent; sync_hub_payouts resends",
                            left,
                        )
                    return
            except Exception:
                logger.exception("payout %s not synced to the hub", pk)


def _schedule(kind: str, payout_id: int | None) -> None:
    """Add to this transaction's batch: one on_commit callback however many saves."""
    if not payout_id or not driver_hub.configured():
        return
    connection = transaction.get_connection()
    if connection.in_atomic_block:
        for _savepoints, callback, _robust in connection.run_on_commit:
            if isinstance(callback, _Pending) and not callback.done:
                getattr(callback, kind).add(payout_id)
                return
    pending = _Pending()
    getattr(pending, kind).add(payout_id)
    # Outside a transaction this runs at once.
    transaction.on_commit(pending)


def schedule_push(payout_id: int | None) -> None:
    _schedule("push", payout_id)


def schedule_delete(payout_id: int | None) -> None:
    """Remove the payout from the hub (before any push of it in the same batch)."""
    _schedule("delete", payout_id)


def resend_all(
    stdout: Callable[[str], Any] | None = None, driver_id: int | None = None
) -> dict[str, int]:
    """Push every payout of a driver in the app (or of one driver), oldest first."""
    counts = {"sent": 0, "failed": 0}
    rows = Payout.objects.filter(load__driver__hub_driver_id__isnull=False)
    if driver_id is not None:
        rows = rows.filter(load__driver_id=driver_id)
    ids = list(rows.order_by("id").values_list("id", flat=True))
    for position, pk in enumerate(ids):
        try:
            if push_payout(pk):
                counts["sent"] += 1
        except driver_hub.DriverHubError as exc:
            counts["failed"] += 1
            logger.warning("payout %s not sent to the hub: %s", pk, exc)
            if exc.status_code == 502:
                counts["failed"] += len(ids) - position - 1
                if stdout:
                    stdout("The hub is unreachable; stopped.")
                break
        except Exception:
            counts["failed"] += 1
            logger.exception("payout %s not sent to the hub", pk)
    return counts


def _in_background(work: Callable[[], Any]) -> None:
    """Run off the request thread, with its own database connection."""

    def run() -> None:
        try:
            work()
        except Exception:
            logger.exception("driver payouts not sent to the hub")
        finally:
            connections.close_all()

    threading.Thread(target=run, daemon=True).start()


def push_driver_payouts_later(driver_id: int | None) -> None:
    """A driver was just linked to the app: send the payouts they already have.

    After commit and in the background: the hub is often waiting on our answer
    to its own push while this runs, and a driver can have many payouts.
    """
    if not driver_id or not driver_hub.configured():
        return
    if not Payout.objects.filter(load__driver_id=driver_id).exists():
        return

    def send() -> None:
        counts = resend_all(driver_id=driver_id)
        logger.info("driver %s linked: payouts sent to the hub %s", driver_id, counts)

    transaction.on_commit(lambda: _in_background(send))
