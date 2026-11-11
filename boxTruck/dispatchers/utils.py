from decimal import Decimal


def driver_pay_before_fee(load):
    payout = getattr(load, 'payout', None)
    return (load.driver_pay or Decimal('0')) + (payout.quick_pay_fee if payout else Decimal('0'))
