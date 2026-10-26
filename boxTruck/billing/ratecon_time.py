"""
Times on a rate confirmation: what the readers are told to return, and how a
returned time is turned into a datetime.

The readers (billing/utils.py for OpenAI, billing/gemini.py for Gemini) used to
be told to strip the timezone, which threw away the one thing that says where
"08:00" is. They now keep the zone the document prints, in either of two forms
the frontend already understands: a zone name after the time ("2026-10-24T08:00
EST") or an offset ("2026-10-24T08:00-05:00"). A time with no zone stays plain.
"""
import re
from datetime import datetime
from zoneinfo import ZoneInfo

from django.utils.dateparse import parse_datetime

DATE_TIME_ZONE_RULE = (
    "If the document prints a timezone for the date/time, keep it exactly in the form it "
    "appears: either a zone name after the time (e.g. '2026-10-24T08:00 EST'), or an offset "
    "(e.g. '2026-10-24T08:00-05:00'). If the document prints no timezone, return the date/time "
    "without one (e.g. '2026-10-24T08:00'); never guess or add a timezone it does not state. "
    "Do not include seconds. Do not reformat the date/time otherwise.\n"
)

_E, _C, _M, _P = 'America/New_York', 'America/Chicago', 'America/Denver', 'America/Los_Angeles'

# Zone names as rate cons write them. "EST" in summer still means Eastern wall
# time, so a name maps to the region, not to a fixed offset — the same reading
# the load form applies to these strings.
NAMED_ZONES = {
    'EST': _E, 'EDT': _E, 'ET': _E, 'EASTERN': _E,
    'CST': _C, 'CDT': _C, 'CT': _C, 'CENTRAL': _C,
    'MST': _M, 'MDT': _M, 'MT': _M, 'MOUNTAIN': _M,
    'PST': _P, 'PDT': _P, 'PT': _P, 'PACIFIC': _P,
    'AKST': 'America/Anchorage', 'AKDT': 'America/Anchorage', 'ALASKA': 'America/Anchorage',
    'HST': 'Pacific/Honolulu', 'HAWAII': 'Pacific/Honolulu',
}

_NAMED_TIME = re.compile(
    r'^(\d{4})[-/](\d{2})[-/](\d{2})(?:[T ](\d{1,2}):(\d{2})(?::\d{2}(?:\.\d+)?)?)?\s*([A-Za-z ]+)$'
)


def parse_ratecon_datetime(value):
    """
    A rate-con time as a datetime, or None when it cannot be read.

    One with a zone name or an offset becomes an aware datetime for that exact
    moment. One with no zone is returned naive, as before.
    """
    if isinstance(value, list):
        value = value[0] if value else None
    if not value:
        return None
    text = str(value).strip()

    m = _NAMED_TIME.match(text)
    if m:
        zone = NAMED_ZONES.get(re.sub(r'[^A-Za-z]', '', m.group(6)).upper())
        if not zone:
            return None
        year, month, day, hour, minute = m.group(1, 2, 3, 4, 5)
        try:
            return datetime(
                int(year), int(month), int(day), int(hour or 0), int(minute or 0),
                tzinfo=ZoneInfo(zone),
            )
        except ValueError:
            return None

    try:
        return parse_datetime(text.replace('/', '-')) or parse_datetime(text)
    except ValueError:
        return None
