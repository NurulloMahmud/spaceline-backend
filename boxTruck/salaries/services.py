"""The month's pay for every employee.

Three parts, each on the loads the employee booked (`Load.booked_by`), with Cancelled and
Rejected loads left out and profit measured as it was booked (carrier pay minus driver pay before
the Quick Pay fee, the same rule dispatcher pay has always used):

* fixed salary: the amount in force for the month;
* commission: the percentage in force, on the profit of the loads whose pick-up date falls in the
  month;
* daily bonus: for each day of the month, by the day the loads were booked (created) in Central
  time, the bonus rule in force that day applied to that day's profit.

Salary and percentage carry forward from the last month they were set. A bonus rule applies from
its date on, so changing it later never changes a month that is already paid. Management can also
set an employee's bonus total for one month by hand (BonusOverride); the calculation still runs and
is shown beside it, and the override never carries into the next month.
"""
from calendar import monthrange
from collections import defaultdict
from datetime import date, datetime, time, timedelta
from decimal import Decimal

from django.db.models import Q
from django.utils import timezone

from billing.models import Load, LoadStop
from dispatchers.utils import driver_pay_before_fee
from users.models import CustomUser

from .models import BonusOverride, BonusRule, EmployeeSalary

ZERO = Decimal('0')
CENT = Decimal('0.01')
LEFT_OUT = ('cancelled', 'rejected')
HISTORY_MONTHS = 24


def parse_month(text):
    """'2026-11' -> date(2026, 11, 1). Raises ValueError for anything else."""
    parsed = datetime.strptime(str(text or '').strip(), '%Y-%m')
    return parsed.date().replace(day=1)


def month_key(first):
    return first.strftime('%Y-%m')


def month_end(first):
    return first.replace(day=monthrange(first.year, first.month)[1])


def add_months(first, n):
    year, month = divmod(first.year * 12 + first.month - 1 + n, 12)
    return date(year, month + 1, 1)


def month_span(first):
    """Aware [start, end) of the month in the company's zone (settings.TIME_ZONE, Central)."""
    tz = timezone.get_current_timezone()
    start = timezone.make_aware(datetime.combine(first, time.min), tz)
    end = timezone.make_aware(datetime.combine(add_months(first, 1), time.min), tz)
    return start, end


def local_date(dt):
    return timezone.localtime(dt).date()


def money(value):
    return (value or ZERO).quantize(CENT)


def profit_of(load):
    return (load.carrier_pay or ZERO) - driver_pay_before_fee(load)


def counted(loads):
    qs = loads.filter(booked_by__isnull=False)
    for name in LEFT_OUT:
        qs = qs.exclude(status__name__iexact=name)
    return qs.select_related('payout', 'status')


def commission_loads(first, last=None):
    """Loads whose pick-up falls in the month (or the run of months from `first` to `last`)."""
    start, _ = month_span(first)
    _, end = month_span(last or first)
    return counted(Load.objects.filter(pickup_date__gte=start, pickup_date__lt=end))


def booked_loads(first, last=None):
    """Loads booked (created) in the month (or the run of months from `first` to `last`)."""
    start, _ = month_span(first)
    _, end = month_span(last or first)
    return counted(Load.objects.filter(created_at__gte=start, created_at__lt=end))


def rule_on(rules, day):
    """The rule in force on a day: the latest whose date is on or before it."""
    current = None
    for rule in rules:
        if rule.effective_from <= day and (current is None or rule.effective_from > current.effective_from):
            current = rule
    return current


def bonus_for(profit, rule):
    if rule is None or rule.profit_step <= ZERO or rule.bonus_amount <= ZERO or profit <= ZERO:
        return ZERO
    return (profit // rule.profit_step) * rule.bonus_amount


def rule_dict(rule):
    if rule is None:
        return None
    return {
        'id': rule.id,
        'effective_from': rule.effective_from.isoformat(),
        'profit_step': money(rule.profit_step),
        'bonus_amount': money(rule.bonus_amount),
        'created_by': _person(rule.created_by),
        'created_at': rule.created_at,
    }


def _person(user):
    if user is None:
        return None
    return ' '.join(p for p in (user.first_name, user.last_name) if p) or user.username


def settings_through(first):
    """user id -> the EmployeeSalary row in force for the month (set that month or earlier)."""
    found = {}
    rows = EmployeeSalary.objects.filter(month__lte=first).order_by('user_id', '-month')
    for row in rows:
        found.setdefault(row.user_id, row)
    return found


def days_of(loads):
    """(user id, booked day) -> {'date', 'loads', 'profit'} for the loads given."""
    days = {}
    for load in loads:
        key = (load.booked_by_id, local_date(load.created_at))
        day = days.setdefault(key, {'date': key[1], 'loads': 0, 'profit': ZERO})
        day['loads'] += 1
        day['profit'] += profit_of(load)
    return days


def bonus_lines(days, rules):
    """The days with their bonus, newest first."""
    lines = []
    for day in days:
        rule = rule_on(rules, day['date'])
        lines.append({**day, 'rule': rule, 'bonus': bonus_for(day['profit'], rule)})
    return sorted(lines, key=lambda d: d['date'], reverse=True)


def overrides_for(first, last=None):
    """(user id, month) -> amount set by hand, for one month or a run of months."""
    qs = BonusOverride.objects.filter(month__gte=first, month__lte=last or first)
    return {(o.user_id, o.month): o.amount for o in qs}


def pay_for(setting, profit, bonus):
    fixed = setting.fixed_salary if setting else ZERO
    pct = setting.profit_percentage if setting else ZERO
    commission = (profit * pct / Decimal('100')) if pct else ZERO
    return money(fixed), money(commission), money(fixed + commission + bonus)


def user_dict(user):
    return {
        'id': user.id,
        'username': user.username,
        'first_name': user.first_name,
        'last_name': user.last_name,
        'department': user.department.name if user.department else None,
        'started_working_date': user.started_working_date,
        'is_active': user.is_active,
    }


def month_report(first, today=None):
    """One row per employee for the month, plus the bonus rule in force and its changes."""
    today = today or timezone.localdate()
    last = month_end(first)
    through = min(today, last) if today >= first else first
    settings = settings_through(first)
    rules = list(BonusRule.objects.select_related('created_by'))

    profit = defaultdict(lambda: ZERO)
    count = defaultdict(int)
    for load in commission_loads(first):
        profit[load.booked_by_id] += profit_of(load)
        count[load.booked_by_id] += 1

    bonus = defaultdict(lambda: ZERO)
    bonus_days = defaultdict(int)
    by_user = defaultdict(list)
    for (uid, _), day in days_of(booked_loads(first)).items():
        by_user[uid].append(day)
    for uid, days in by_user.items():
        for line in bonus_lines(days, rules):
            bonus[uid] += line['bonus']
            if line['bonus'] > ZERO:
                bonus_days[uid] += 1

    overrides = overrides_for(first)
    touched = set(profit) | set(bonus) | {uid for uid, s in settings.items() if s.month == first} | {uid for uid, _ in overrides}
    users = (
        CustomUser.objects.select_related('department')
        .filter(Q(is_active=True) | Q(id__in=touched))
        .exclude(is_superuser=True, department__isnull=True)
        .order_by('first_name', 'last_name', 'username')
    )
    rows = []
    for user in users:
        setting = settings.get(user.id)
        override = overrides.get((user.id, first))
        paid_bonus = override if override is not None else bonus[user.id]
        fixed, commission, total = pay_for(setting, profit[user.id], paid_bonus)
        rows.append({
            'user': user_dict(user),
            'fixed_salary': fixed if setting else None,
            'profit_percentage': setting.profit_percentage if setting else None,
            'set_for': month_key(setting.month) if setting else None,
            'profit': money(profit[user.id]),
            'loads_count': count[user.id],
            'commission': commission,
            'bonus': money(paid_bonus),
            'bonus_calculated': money(bonus[user.id]),
            'bonus_override': money(override) if override is not None else None,
            'bonus_days': bonus_days[user.id],
            'total': total,
        })

    changes = sorted((r for r in rules if first < r.effective_from <= last), key=lambda r: r.effective_from)
    return {
        'month': month_key(first),
        'through': through,
        'is_current': first <= today <= last,
        'rule': rule_dict(rule_on(rules, through)),
        'rule_at_start': rule_dict(rule_on(rules, first)),
        'rule_changes': [rule_dict(r) for r in changes],
        'rows': rows,
    }


def stops_of(loads):
    """load id -> (origin, destination) as 'City, ST', from the load's stops."""
    by_load = defaultdict(list)
    for stop in LoadStop.objects.filter(load_id__in=[l.id for l in loads]).order_by('load_id', 'order'):
        by_load[stop.load_id].append(stop)
    out = {}
    for load_id, stops in by_load.items():
        pickups = [s for s in stops if s.load_pickup] or stops
        drops = [s for s in stops if s.load_drop] or stops
        out[load_id] = (_place(pickups[0]), _place(drops[-1]))
    return out


def _place(stop):
    return ', '.join(p for p in (stop.city, stop.state) if p) or stop.address or ''


def employee_detail(user, first, today=None):
    """The employee's loads and bonus days for the month, and their pay by month before it."""
    today = today or timezone.localdate()
    rules = list(BonusRule.objects.select_related('created_by'))
    report = month_report(first, today)
    row = next((r for r in report['rows'] if r['user']['id'] == user.id), None)
    if row is None:
        setting = settings_through(first).get(user.id)
        fixed, commission, total = pay_for(setting, ZERO, ZERO)
        row = {
            'user': user_dict(user), 'fixed_salary': fixed if setting else None,
            'profit_percentage': setting.profit_percentage if setting else None,
            'set_for': month_key(setting.month) if setting else None,
            'profit': ZERO, 'loads_count': 0, 'commission': commission, 'bonus': ZERO, 'bonus_calculated': ZERO, 'bonus_override': None, 'bonus_days': 0, 'total': total,
        }

    loads = list(commission_loads(first).filter(booked_by=user).order_by('-pickup_date'))
    places = stops_of(loads)
    load_rows = []
    for load in loads:
        origin, destination = places.get(load.id, ('', ''))
        load_rows.append({
            'id': load.id,
            'shipment': load.shipment,
            'load_number': load.load_number,
            'origin': origin,
            'destination': destination,
            'pickup_date': load.pickup_date,
            'booked_at': load.created_at,
            'status': load.status.name if load.status else None,
            'carrier_pay': money(load.carrier_pay),
            'driver_pay': money(driver_pay_before_fee(load)),
            'profit': money(profit_of(load)),
        })

    days = [d for (uid, _), d in days_of(booked_loads(first).filter(booked_by=user)).items()]
    day_rows = [
        {'date': d['date'], 'loads': d['loads'], 'profit': money(d['profit']), 'bonus': money(d['bonus']), 'rule': rule_dict(d['rule'])}
        for d in bonus_lines(days, rules)
    ]

    history = pay_history(user, first, rules)
    return {'month': report['month'], 'through': report['through'], 'is_current': report['is_current'], 'row': row, 'loads': load_rows, 'bonus_days': day_rows, 'history': history}


def pay_history(user, first, rules):
    """Pay by month, newest first, from the month the employee first had a salary, a load or a
    start date, up to `first`; capped at HISTORY_MONTHS."""
    earliest_candidates = [first]
    if user.started_working_date:
        earliest_candidates.append(user.started_working_date.replace(day=1))
    first_salary = EmployeeSalary.objects.filter(user=user).order_by('month').values_list('month', flat=True).first()
    if first_salary:
        earliest_candidates.append(first_salary)
    first_load = counted(Load.objects.filter(booked_by=user)).order_by('created_at').values_list('created_at', flat=True).first()
    if first_load:
        earliest_candidates.append(local_date(first_load).replace(day=1))
    earliest = max(min(earliest_candidates), add_months(first, -(HISTORY_MONTHS - 1)))

    settings_rows = list(EmployeeSalary.objects.filter(user=user, month__lte=first).order_by('month'))
    pickup = defaultdict(lambda: ZERO)
    pickup_count = defaultdict(int)
    for load in commission_loads(earliest, first).filter(booked_by=user):
        key = local_date(load.pickup_date).replace(day=1)
        pickup[key] += profit_of(load)
        pickup_count[key] += 1
    booked = defaultdict(list)
    for (_, day), d in days_of(booked_loads(earliest, first).filter(booked_by=user)).items():
        booked[day.replace(day=1)].append(d)
    overrides = overrides_for(earliest, first)

    months = []
    cursor = first
    while cursor >= earliest:
        setting = None
        for row in settings_rows:
            if row.month <= cursor:
                setting = row
        calculated = sum((line['bonus'] for line in bonus_lines(booked.get(cursor, []), rules)), ZERO)
        override = overrides.get((user.id, cursor))
        bonus = override if override is not None else calculated
        fixed, commission, total = pay_for(setting, pickup[cursor], bonus)
        months.append({
            'month': month_key(cursor),
            'fixed_salary': fixed if setting else None,
            'profit_percentage': setting.profit_percentage if setting else None,
            'profit': money(pickup[cursor]),
            'loads_count': pickup_count[cursor],
            'commission': commission,
            'bonus': money(bonus),
            'bonus_calculated': money(calculated),
            'bonus_override': money(override) if override is not None else None,
            'total': total if (setting or bonus) else None,
        })
        cursor = add_months(cursor, -1)
    return months


def save_salaries(first, items, by_user):
    """Upsert the month's rows. An item with neither salary amount removes the salary row for that
    month, so the employee goes back to whatever the earlier months said. An item that carries
    `bonus_override` sets the month's bonus by hand, or clears it back to the calculation when
    null; an item without the key leaves the bonus alone."""
    for item in items:
        user_id = item['user']
        if 'bonus_override' in item:
            if item['bonus_override'] is None:
                BonusOverride.objects.filter(user_id=user_id, month=first).delete()
            else:
                BonusOverride.objects.update_or_create(
                    user_id=user_id, month=first,
                    defaults={'amount': item['bonus_override'], 'updated_by': by_user, 'created_by': by_user},
                )
        if 'fixed_salary' not in item and 'profit_percentage' not in item:
            continue
        fixed = item.get('fixed_salary')
        pct = item.get('profit_percentage')
        if fixed is None and pct is None:
            EmployeeSalary.objects.filter(user_id=user_id, month=first).delete()
            continue
        EmployeeSalary.objects.update_or_create(
            user_id=user_id, month=first,
            defaults={'fixed_salary': fixed or ZERO, 'profit_percentage': pct or ZERO, 'updated_by': by_user, 'created_by': by_user},
        )
