from django.db.models.functions import TruncDay
from datetime import timedelta
from django.db.models import Sum

def week_chart(qs, start_date):
    DAYS = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun']
    daily = (
        qs.annotate(day=TruncDay('pickup_date'))
        .values('day')
        .annotate(earnings=Sum('driver_pay'))
        .order_by('day')
    )
    earnings_map = {entry['day'].date(): float(entry['earnings'] or 0) for entry in daily}
    result = []
    for i in range(7):
        day = (start_date + timedelta(days=i)).date()
        result.append({
            "label": DAYS[i],
            "earnings": earnings_map.get(day, 0),
        })
    return result


def month_chart(qs, start_date):
    daily = (
        qs.annotate(day=TruncDay('pickup_date'))
        .values('day')
        .annotate(earnings=Sum('driver_pay'))
        .order_by('day')
    )
    weeks = {"W1": 0, "W2": 0, "W3": 0, "W4": 0}
    for entry in daily:
        day_num = entry['day'].day
        earnings = float(entry['earnings'] or 0)
        if day_num <= 7:
            weeks["W1"] += earnings
        elif day_num <= 14:
            weeks["W2"] += earnings
        elif day_num <= 21:
            weeks["W3"] += earnings
        else:
            weeks["W4"] += earnings
    return [{"label": k, "earnings": round(v, 2)} for k, v in weeks.items()]


def year_chart(qs, start_date):
    daily = (
        qs.annotate(day=TruncDay('pickup_date'))
        .values('day')
        .annotate(earnings=Sum('driver_pay'))
        .order_by('day')
    )
    quarters = {"Q1": 0, "Q2": 0, "Q3": 0, "Q4": 0}
    for entry in daily:
        month = entry['day'].month
        earnings = float(entry['earnings'] or 0)
        if month <= 3:
            quarters["Q1"] += earnings
        elif month <= 6:
            quarters["Q2"] += earnings
        elif month <= 9:
            quarters["Q3"] += earnings
        else:
            quarters["Q4"] += earnings
    return [{"label": k, "earnings": round(v, 2)} for k, v in quarters.items()]