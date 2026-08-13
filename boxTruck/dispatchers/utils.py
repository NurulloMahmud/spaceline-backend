from billing.models import Load
import calendar
from decimal import Decimal
from datetime import date, datetime, time, timedelta
from django.utils import timezone
from collections import defaultdict
from django.db import models
from .models import DispatchSalary, DispatchBonus


def get_tier_percentage(company, total_profit, start_date=None):
    if total_profit <= 0:
        return Decimal('0')
    if start_date is None:
        start_date = timezone.now().date()
    year = start_date.year
    month = start_date.month
    first_day = datetime(year, month, 1).date()
    last_day = datetime(year, month, calendar.monthrange(year, month)[1]).date()
    tier = DispatchSalary.objects.filter(
        models.Q(company=company) | models.Q(company__isnull=True),
        min_amount__lte=total_profit,
        created_at__date__gte=first_day,
        created_at__date__lte=last_day,
    ).filter(
        models.Q(max_amount__gte=total_profit) | models.Q(max_amount__isnull=True)
    ).order_by('-created_at').first()
    if not tier:
        tier = DispatchSalary.objects.filter(
            models.Q(company=company) | models.Q(company__isnull=True),
            min_amount__lte=total_profit,
        ).filter(
            models.Q(max_amount__gte=total_profit) | models.Q(max_amount__isnull=True)
        ).order_by('-created_at').first()
    if not tier:
        return Decimal('0')
    return tier.percentage or Decimal('0')

def get_daily_bonus(company, day_profit):
    bonus = DispatchBonus.objects.filter(
        company=company,
        min_daily_profit__lte=day_profit,
    ).filter(
        models.Q(max_daily_profit__gte=day_profit) | models.Q(max_daily_profit__isnull=True)
    ).first()
    if not bonus:
        return Decimal('0')
    return bonus.bonus_amount

def is_probation(user):
    if not user.started_working_date:
        return False
    return date.today() <= user.started_working_date + timedelta(days=60)

def count_working_days(start, end):
    count = 0
    current = start
    while current <= end:
        if current.weekday() < 5:
            count += 1
        current += timedelta(days=1)
    return count

def calculate_prorated_fixed(dispatcher, start_date, end_date):
    today = date.today()
    actual_start = max(start_date, dispatcher.started_working_date)
    actual_end = min(end_date, today)
    if actual_start > actual_end:
        return Decimal('0'), 0, 0

    month_start = actual_start.replace(day=1)
    last_day = calendar.monthrange(actual_start.year, actual_start.month)[1]
    month_end = actual_start.replace(day=last_day)
    total_working_days_in_month = count_working_days(month_start, month_end)
    if total_working_days_in_month == 0:
        return Decimal('0'), 0, 0

    working_days_in_period = count_working_days(actual_start, actual_end)
    daily_fixed = Decimal('200') / Decimal(total_working_days_in_month)
    prorated_fixed = daily_fixed * Decimal(working_days_in_period)
    return prorated_fixed.quantize(Decimal('0.01')), working_days_in_period, total_working_days_in_month

def calculate_dispatcher_kpi(dispatcher, loads, company, start_date, end_date):
    local_tz = timezone.get_current_timezone()
    days_map = defaultdict(list)
    for load in loads:
        if load.pickup_date:
            local_pickup = load.pickup_date.astimezone(local_tz)
            day = local_pickup.date()
            days_map[day].append(load)

    total_profit = Decimal('0')
    for load in loads:
        carrier = load.carrier_pay or Decimal('0')
        driver = load.driver_pay or Decimal('0')
        total_profit += carrier - driver

    percentage = get_tier_percentage(company, total_profit, start_date)
    flex_salary = total_profit * percentage / Decimal('100')
    if is_probation(dispatcher):
            fixed_salary, working_days_in_period, total_working_days = calculate_prorated_fixed(
            dispatcher, start_date, end_date
        )
    else:
        fixed_salary = Decimal('0')
        working_days_in_period = count_working_days(start_date, end_date)
        total_working_days = count_working_days(
            start_date.replace(day=1),
            start_date.replace(day=calendar.monthrange(start_date.year, start_date.month)[1])
        )
    total_salary = flex_salary + fixed_salary
    days_result = []
    total_bonus = Decimal('0')
    current_day = start_date
    while current_day <= end_date:
        day_loads = days_map.get(current_day, [])
        day_profit = Decimal('0')
        for load in day_loads:
            carrier = load.carrier_pay or Decimal('0')
            driver = load.driver_pay or Decimal('0')
            day_profit += carrier - driver

        bonus = Decimal('0')
        if day_profit > 0:
            bonus = get_daily_bonus(company, day_profit) or Decimal('0')
            total_bonus += bonus

        if day_profit > 0 or bonus > 0:
            days_result.append({
                'date': current_day.isoformat(),
                'day_profit': float(day_profit),
                'bonus': float(bonus),
            })

        current_day += timedelta(days=1)
    loads_result = []
    for load in loads:
        carrier = load.carrier_pay or Decimal('0')
        driver = load.driver_pay or Decimal('0')
        loads_result.append({
            'id': load.id,
            'load_number': load.load_number,
            'shipment': f"SH - {load.shipment}" if load.shipment else None,
            'carrier_pay': float(carrier),
            'driver_pay': float(driver),
            'profit': float(carrier - driver),
            'pickup_date': load.pickup_date.astimezone(local_tz).isoformat() if load.pickup_date else None,  # ← fix
            'drop_date':   load.drop_date.astimezone(local_tz).isoformat() if load.drop_date else None, 
            'driver': {
                'id': load.driver.id,
                'full_name': load.driver.full_name,
            } if load.driver else None,
            'status': {
                'id': load.status.id,
                'name': load.status.name,
            } if load.status else None,
        })

    return {
        'id': dispatcher.id,
        'username': dispatcher.username,
        'first_name': dispatcher.first_name,
        'last_name': dispatcher.last_name,
        'started_working_date': dispatcher.started_working_date.isoformat() if dispatcher.started_working_date else None,
        'is_probation': is_probation(dispatcher),
        'period_profit': float(total_profit),
        'flex_salary': float(flex_salary),
        'fixed_salary': float(fixed_salary),
        'fixed_salary_breakdown': {
            'daily_fixed': float(Decimal('200') / Decimal(total_working_days)) if total_working_days else 0,
            'working_days_in_period': working_days_in_period,
            'total_working_days_in_month': total_working_days,
        },
        'total_salary': float(total_salary),
        'total_bonus': float(total_bonus),
        'grand_total': float(total_salary + total_bonus),
        'days': days_result,
        'loads': loads_result,
    }

def get_previous_3_months(reference_month: date):
    months = []
    year, month = reference_month.year, reference_month.month
    for _ in range(3):
        month -= 1
        if month == 0:
            month = 12
            year -= 1
        months.insert(0, (year, month))
    local_tz = timezone.get_current_timezone()
    result = []
    for y, m in months:
        start = date(y, m, 1)
        last_day = calendar.monthrange(y, m)[1]
        end = date(y, m, last_day)
        start_dt = timezone.make_aware(datetime.combine(start, time.min), local_tz)
        end_dt = timezone.make_aware(datetime.combine(end, time.max), local_tz)
        label = start.strftime('%B')
        result.append((start_dt, end_dt, label))
    return result

def get_dispatcher_gross(dispatcher, company, start_dt, end_dt):
    total = Load.objects.filter(
        company=company,
        booked_by=dispatcher,
        pickup_date__gte=start_dt,
        pickup_date__lte=end_dt,
    ).exclude(
        status__name__iexact='rejected'
    ).aggregate(
        gross=models.Sum(models.F('carrier_pay') - models.F('driver_pay'))
    )['gross']
    return total or Decimal('0')

def calculate_target(last_three_months):
    if len(last_three_months) != 3:
        raise ValueError("Exactly 3 months are required.")

    weights = [0.20, 0.30, 0.50]
    baseline = sum(
        m["gross"] * weight
        for m, weight in zip(last_three_months, weights)
    )
    target = round(baseline * 1.05, 2)

    return {"target": target}

def calculate_bonus(actual_gross, target):
    return 100 if actual_gross >= target else 0

def get_company_gross_by_dispatcher(company, start_dt, end_dt):
    rows = Load.objects.filter(
        company=company,
        pickup_date__gte=start_dt,
        pickup_date__lte=end_dt,
        booked_by__isnull=False,
    ).exclude(
        status__name__iexact='rejected'
    ).values('booked_by').annotate(
        gross=models.Sum(models.F('carrier_pay') - models.F('driver_pay'))
    )
    return {row['booked_by']: row['gross'] or Decimal('0') for row in rows}

def get_company_dispatcher_targets(company, current_month_start, dispatcher_ids=None):
    months = get_previous_3_months(current_month_start)
    month_grosses = [
        get_company_gross_by_dispatcher(company, start_dt, end_dt)
        for start_dt, end_dt, label in months
    ]
    if dispatcher_ids is None:
        dispatcher_ids = set()
        for mg in month_grosses:
            dispatcher_ids.update(mg.keys())

    results = {}
    for dispatcher_id in dispatcher_ids:
        last_three_months = []
        for i in range(3):
            gross = month_grosses[i].get(dispatcher_id, Decimal('0'))
            if gross == 0:
                gross = Decimal('3000')
            last_three_months.append({
                "month": months[i][2],
                "gross": float(gross)
            })
        results[dispatcher_id] = {
            **calculate_target(last_three_months),
            "based_on": last_three_months,
        }
    return results

def get_company_actual_gross_by_dispatcher(company, month_start_dt, now_dt):
    return get_company_gross_by_dispatcher(company, month_start_dt, now_dt)
