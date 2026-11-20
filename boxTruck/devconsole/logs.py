"""Read the tail of each service's error log and group the errors.

Formats handled (timestamps are server local time, which is UTC):

- email agent:  ``2026-11-20 10:15:20,854 - logger.name - ERROR - message``
- sylectus, direct, django (after the LOGGING change):
  ``2026-11-19 22:14:45,888 ERROR message`` (django adds ``name: `` before the message)
- go agent, driver hub: ``2026/08/13 14:29:20 message`` with no level; a line that says
  failed, error or panic (any case) counts as an error. GIN request lines and gorm SQL
  echo are skipped.

Lines without a timestamp continue the entry above them (tracebacks). Old Django blocks
(``Internal Server Error: /path`` + traceback, written before Django had timestamps) are
skipped: they have no time, and attributing them to the file's mtime would make every old
error look new.
"""
import hashlib
import os
import re
from datetime import datetime, timedelta, timezone as dt_timezone

from decouple import config
from django.conf import settings

TAIL_BYTES = 5 * 1024 * 1024
MAX_TRACE_LINES = 60
MAX_LINE_CHARS = 2000

SERVICES = [
    # key, label, default path, format
    ('django', 'Django API', '/var/log/boxTruck.err.log', 'py'),
    ('email', 'Email agent', '/var/log/email_agent.err.log', 'email'),
    ('go', 'Go loads agent', '/var/log/agent/err.log', 'go'),
    ('sylectus', 'Sylectus listener', '/var/log/spaceline/sylectus.err.log', 'py'),
    ('direct', 'Direct receiver', '/var/log/spaceline/sylectus_direct.err.log', 'py'),
    ('hub', 'Driver hub', '/var/log/driver-hub.err.log', 'go'),
]
SERVICE_KEYS = {s[0] for s in SERVICES}

EMAIL_RE = re.compile(
    r'^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)[,.](\d{1,6}) - (\S+) - '
    r'(DEBUG|INFO|WARNING|ERROR|CRITICAL) - (.*)$'
)
PY_RE = re.compile(
    r'^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)(?:[,.](\d{1,6}))? '
    r'(DEBUG|INFO|WARNING|ERROR|CRITICAL) (.*)$'
)
GO_RE = re.compile(r'^(\d{4})/(\d\d)/(\d\d) (\d\d:\d\d:\d\d) (.*)$')
GO_ERROR_RE = re.compile(r'failed|error|panic', re.IGNORECASE)
# Old Django error banners with no timestamp.
DJANGO_BANNER_RE = re.compile(
    r'^(Internal Server Error|Not Found|Unauthorized|Bad Request|Forbidden|'
    r'Method Not Allowed|Too Many Requests|Service Unavailable): '
)

_EMAIL_ADDR = re.compile(r'[\w.+-]+@[\w-]+(?:\.[\w-]+)+')
_UUID = re.compile(r'\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b')
_HEX0X = re.compile(r'\b0x[0-9a-fA-F]+\b')
_HEXLONG = re.compile(r'\b(?=[0-9a-fA-F]*\d)[0-9a-fA-F]{8,}\b')
_DIGITS = re.compile(r'\d+')
_WS = re.compile(r'\s+')


def normalise(message: str) -> str:
    text = _EMAIL_ADDR.sub('<email>', message)
    text = _UUID.sub('<uuid>', text)
    text = _HEX0X.sub('<hex>', text)
    text = _HEXLONG.sub('<hex>', text)
    text = _DIGITS.sub('<n>', text)
    return _WS.sub(' ', text).strip()


def group_id(service: str, normalised: str) -> str:
    return hashlib.sha1(f'{service}\0{normalised}'.encode('utf-8', 'replace')).hexdigest()[:16]


def log_path(key: str, default: str) -> str:
    override = (getattr(settings, 'DEV_LOG_PATHS', None) or {}).get(key)
    if override:
        return str(override)
    return config(f'DEV_LOG_{key.upper()}', default=default)


def read_tail(path: str, limit: int = TAIL_BYTES):
    """Last `limit` bytes of the file as text; None when it cannot be read."""
    try:
        with open(path, 'rb') as fh:
            size = os.fstat(fh.fileno()).st_size
            start = max(0, size - limit)
            fh.seek(start)
            data = fh.read(limit)
    except OSError:
        return None
    if start > 0:
        # The first line is almost surely cut in half.
        nl = data.find(b'\n')
        data = data[nl + 1:] if nl >= 0 else b''
    return data.decode('utf-8', errors='replace')


def _utc(dt: datetime) -> datetime:
    return dt.replace(tzinfo=dt_timezone.utc)


def _parse_dt(date_part: str, ms: str | None) -> datetime | None:
    try:
        dt = datetime.strptime(date_part, '%Y-%m-%d %H:%M:%S')
    except ValueError:
        return None
    if ms:
        dt = dt.replace(microsecond=int(ms.ljust(6, '0')[:6]))
    return _utc(dt)


def _iso(dt: datetime) -> str:
    return dt.astimezone(dt_timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.') + f'{dt.microsecond // 1000:03d}Z'


def _clip(line: str) -> str:
    return line if len(line) <= MAX_LINE_CHARS else line[:MAX_LINE_CHARS] + ' ...'


def _clip_trace(lines: list[str]) -> list[str]:
    if len(lines) <= MAX_TRACE_LINES:
        return lines
    head, tail = 20, MAX_TRACE_LINES - 21
    omitted = len(lines) - head - tail
    return lines[:head] + [f'... {omitted} lines omitted ...'] + lines[-tail:]


def parse(text: str, fmt: str):
    """Yield dicts {at, level, message, trace} for each error entry in `text`."""
    current = None  # entry being collected, or None when lines should be dropped

    def finish():
        if current is not None:
            current['trace'] = _clip_trace(current['trace'])
            return current
        return None

    for raw in text.split('\n'):
        line = raw.rstrip('\r')
        start = None  # (dt, level, message) when this line starts an entry
        if fmt == 'email':
            m = EMAIL_RE.match(line)
            if m:
                start = (_parse_dt(m.group(1), m.group(2)), m.group(4), m.group(5))
        elif fmt == 'py':
            m = PY_RE.match(line)
            if m:
                start = (_parse_dt(m.group(1), m.group(2)), m.group(3), m.group(4))
        else:
            m = GO_RE.match(line)
            if m:
                try:
                    dt = _utc(datetime.strptime(
                        f'{m.group(1)}-{m.group(2)}-{m.group(3)} {m.group(4)}', '%Y-%m-%d %H:%M:%S'))
                except ValueError:
                    dt = None
                msg = m.group(5)
                if '\x1b[' in msg or '[rows:' in msg:
                    level = None  # gorm SQL echo
                elif GO_ERROR_RE.search(msg):
                    level = 'ERROR'
                else:
                    level = 'INFO'
                start = (dt, level, msg)

        if start is not None:
            done = finish()
            if done is not None:
                yield done
            dt, level, message = start
            if dt is not None and level in ('ERROR', 'CRITICAL'):
                current = {'at': dt, 'level': level, 'message': _clip(message), 'trace': []}
            else:
                current = None
            continue

        # No timestamp: a continuation, a banner, or noise.
        if line.startswith('[GIN]') or (fmt == 'py' and DJANGO_BANNER_RE.match(line)):
            done = finish()
            if done is not None:
                yield done
            current = None
            continue
        if current is not None and line.strip():
            current['trace'].append(_clip(line))

    done = finish()
    if done is not None:
        yield done


def collect(service: str | None, q: str, hours: int, limit: int) -> dict:
    cutoff = datetime.now(dt_timezone.utc) - timedelta(hours=hours)
    needle = q.lower()
    services = []
    groups: dict[str, dict] = {}

    for key, label, default, fmt in SERVICES:
        text = read_tail(log_path(key, default))
        services.append({'key': key, 'label': label, 'readable': text is not None})
        if text is None or (service and service != key):
            continue
        for entry in parse(text, fmt):
            if entry['at'] < cutoff:
                continue
            trace = '\n'.join(entry['trace'])
            if needle and needle not in entry['message'].lower() and needle not in trace.lower():
                continue
            norm = normalise(entry['message'])
            gid = group_id(key, norm)
            group = groups.get(gid)
            if group is None:
                groups[gid] = {
                    'id': gid, 'service': key, 'level': entry['level'],
                    'message': entry['message'], 'trace': trace,
                    'first': entry['at'], 'last': entry['at'], 'count': 1,
                }
                continue
            group['count'] += 1
            group['first'] = min(group['first'], entry['at'])
            if entry['at'] >= group['last']:
                group['last'] = entry['at']
                group['message'] = entry['message']
                group['trace'] = trace
                group['level'] = entry['level']

    ordered = sorted(groups.values(), key=lambda g: g['last'], reverse=True)[:limit]
    entries = [{
        'id': g['id'], 'service': g['service'], 'level': g['level'],
        'message': g['message'], 'trace': g['trace'],
        'first_at': _iso(g['first']), 'last_at': _iso(g['last']), 'count': g['count'],
    } for g in ordered]
    return {'services': services, 'entries': entries}
