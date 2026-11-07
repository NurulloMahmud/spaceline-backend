"""
Which loads pick up (or deliver) on a given calendar day.

A load's pick-up and drop times are the facility's local time, and the Loads
page shows them that way on its "Local" tab: a New York pick-up at 00:00 on
Nov 6 reads "Nov 6 00:00 EST". The date filter must count days the same way,
or that load lands on Nov 5 (its Central day) and goes missing from a
"Nov 6" filter. On a fixed zone tab (Eastern, Pacific, ...) every time reads
in that zone, so days follow that zone instead.

The facility's zone is worked out exactly as the frontend does it
(src/pages/loadsV2/zones.js and src/pages/loadBoardV2/time.js): the stop's
state, refined by its ZIP's coordinates for states split between two zones,
and Central when the stop has no state. The ZIP coordinates are the same
table the frontend loads (public/lb2/zip-points.txt), copied to billing/data.
"""
from datetime import date, datetime, time, timedelta
from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo

from django.db.models import Q, QuerySet
from rest_framework.exceptions import ValidationError

from .models import LoadStop
from .utils import STATE_TIMEZONE

HOME_ZONE = 'America/Chicago'
LOCAL = 'local'
DATE_FIELDS = ('pickup_date', 'drop_date')
FIXED_ZONES = (
    'America/New_York', 'America/Chicago', 'America/Denver', 'America/Phoenix',
    'America/Los_Angeles', 'America/Anchorage', 'Pacific/Honolulu',
)
# Every facility zone lies between these two, so a day starts no earlier than
# midnight Eastern and ends no later than midnight Hawaii.
_EARLIEST = ZoneInfo('America/New_York')
_LATEST = ZoneInfo('Pacific/Honolulu')

_E, _C, _M, _P = 'America/New_York', 'America/Chicago', 'America/Denver', 'America/Los_Angeles'
_ZIP_FILE = Path(__file__).resolve().parent / 'data' / 'zip-points.txt'


@lru_cache(maxsize=1)
def _zip_points() -> dict[str, tuple[float, float]]:
    """ZIP -> (lat, lng). The file is "zipDelta,lat100,-lng100;" records in base 36."""
    points: dict[str, tuple[float, float]] = {}
    try:
        text = _ZIP_FILE.read_text()
    except OSError:
        return points
    code = 0
    for rec in text.split(';'):
        parts = rec.split(',')
        if len(parts) < 3 or not parts[0]:
            continue
        code += int(parts[0], 36)
        points[str(code).zfill(5)] = (int(parts[1], 36) / 100, -int(parts[2], 36) / 100)
    return points


def _refine(state: str, lat: float, lng: float, zone: str) -> str:
    """States split between two zones, by rough county lines (same as the frontend)."""
    if state == 'FL':
        return _C if lng < -85.0 else _E
    if state == 'TN':
        return _E if lng > -85.3 else _C
    if state == 'KY':
        return _E if lng > -86.0 else _C
    if state == 'IN':
        return _C if lng < -86.9 and (lat > 41.0 or lat < 38.4) else _E
    if state == 'MI':
        return _C if lng < -87.6 and lat > 45.0 else _E
    if state == 'TX':
        return _M if lng < -104.9 else _C
    if state == 'ND':
        return _M if lng < -101.0 and lat < 47.5 else _C
    if state == 'SD':
        return _M if lng < -100.6 else _C
    if state == 'NE':
        return _M if lng < -101.0 else _C
    if state == 'KS':
        return _M if lng < -101.5 else _C
    if state == 'ID':
        return _P if lat > 45.5 else _M
    if state == 'OR':
        return _M if lng > -117.6 and lat < 44.5 else _P
    return zone


def stop_zone(state: str | None, zipcode: str | None) -> str:
    """The facility's zone for a stop; Central when the stop has no state."""
    st = str(state or '').strip().upper()
    if len(st) != 2 or not st.isalpha():
        return HOME_ZONE
    zone = STATE_TIMEZONE.get(st, HOME_ZONE)
    point = _zip_points().get(str(zipcode or '').strip()[:5])
    return _refine(st, point[0], point[1], zone) if point else zone


def _facility_stop(stops: list[dict], field: str) -> dict | None:
    """The pick-up time belongs to the shipper, the drop time to the receiver."""
    if not stops:
        return None
    if field == 'pickup_date':
        return next((s for s in stops if s['load_pickup']), stops[0])
    return next((s for s in reversed(stops) if s['load_drop']), stops[-1])


def _midnight(day: date, zone: ZoneInfo) -> datetime:
    return datetime.combine(day, time.min, tzinfo=zone)


def parse_date_zone(value: str | None, default: str = HOME_ZONE) -> str:
    """`date_zone` query param: "local" or one of the Loads page's fixed zones."""
    zone = (value or '').strip() or default
    if zone != LOCAL and zone not in FIXED_ZONES:
        raise ValidationError({'error': f"Invalid date_zone. Use 'local' or one of {', '.join(FIXED_ZONES)}."})
    return zone


def filter_by_day(
    queryset: QuerySet, field: str, start: date | None, end: date | None, zone: str
) -> QuerySet:
    """Loads whose `field` falls on a day from `start` to `end` (either may be open) in `zone`."""
    if field not in DATE_FIELDS:
        raise ValueError(field)
    if start is None and end is None:
        return queryset
    if zone != LOCAL:
        tz = ZoneInfo(zone)
        if start is not None:
            queryset = queryset.filter(**{f'{field}__gte': _midnight(start, tz)})
        if end is not None:
            queryset = queryset.filter(**{f'{field}__lt': _midnight(end + timedelta(days=1), tz)})
        return queryset

    # Loads well inside the range are in it whatever their zone. Only those within
    # a few hours of either edge need their facility's zone looked up.
    sure = Q()
    maybe = Q()
    if start is not None:
        sure &= Q(**{f'{field}__gte': _midnight(start, _LATEST)})
        maybe &= Q(**{f'{field}__gte': _midnight(start, _EARLIEST)})
    if end is not None:
        sure &= Q(**{f'{field}__lt': _midnight(end + timedelta(days=1), _EARLIEST)})
        maybe &= Q(**{f'{field}__lt': _midnight(end + timedelta(days=1), _LATEST)})

    edge = dict(queryset.filter(maybe).exclude(sure).values_list('id', field))
    stops: dict[int, list[dict]] = {}
    for s in (
        LoadStop.objects.filter(load_id__in=edge.keys())
        .order_by('load_id', 'order', 'id')
        .values('load_id', 'state', 'zipcode', 'load_pickup', 'load_drop')
    ):
        stops.setdefault(s['load_id'], []).append(s)

    keep = []
    for load_id, when in edge.items():
        stop = _facility_stop(stops.get(load_id, []), field)
        zone_name = stop_zone(stop['state'], stop['zipcode']) if stop else HOME_ZONE
        day = when.astimezone(ZoneInfo(zone_name)).date()
        if (start is None or day >= start) and (end is None or day <= end):
            keep.append(load_id)
    return queryset.filter(sure | Q(id__in=keep))
