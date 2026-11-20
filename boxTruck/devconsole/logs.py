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
error look new. Python warnings printed to stderr (``file.py:12: RuntimeWarning: ...``)
end an entry instead of joining its trace.

Each service's log and its rotated copy (``<path>.1``) are read, up to MAX_FILE_BYTES from
the end of each. A busy log fills 5 MB in about an hour, so reading only the tail hid most
of the day. A regex pass in C finds the error lines first, and only those lines and the
lines under them are parsed. What a file has given is kept in memory by inode, so the next
call (the console polls every 20 s) reads only the bytes written since, and a file that
was just rotated to ``.1`` is not read again.
"""
import hashlib
import os
import re
import threading
import time
from datetime import datetime, timedelta, timezone as dt_timezone

from decouple import config
from django.conf import settings

MAX_FILE_BYTES = 64 * 1024 * 1024
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
# A Python warning printed to stderr, and the source line under it.
PY_WARNING_RE = re.compile(r'^\S+\.py:\d+: \w*Warning: ')
# Old Django error banners with no timestamp.
DJANGO_BANNER_RE = re.compile(
    r'^(Internal Server Error|Not Found|Unauthorized|Bad Request|Forbidden|'
    r'Method Not Allowed|Too Many Requests|Service Unavailable): '
)

# Where an error entry can start, per format, matched at a line start (bytes, so the file
# is never decoded whole). NEEDLES are plain words every such line holds: finding them with
# bytes.find runs at memchr speed, where a multiline regex walks every byte in Python's re.
ERROR_START_RE = {
    'email': re.compile(rb'\d{4}-\d\d-\d\d \d\d:\d\d:\d\d[,.]\d{1,6} - \S+ - (?:ERROR|CRITICAL) - '),
    'py': re.compile(rb'\d{4}-\d\d-\d\d \d\d:\d\d:\d\d(?:[,.]\d{1,6})? (?:ERROR|CRITICAL) '),
    'go': re.compile(rb'(?i)\d{4}/\d\d/\d\d \d\d:\d\d:\d\d [^\n]*?(?:failed|error|panic)'),
}
NEEDLES = {
    'email': (b' - ERROR - ', b' - CRITICAL - '),
    'py': (b' ERROR ', b' CRITICAL '),
    'go': (b'failed', b'error', b'panic'),  # searched in the lowercased bytes
}
# Any line that starts a new entry; an error entry's lines run up to the next one.
ANY_START_RE = re.compile(rb'(?m)^\d{4}[-/]\d\d[-/]\d\d \d\d:\d\d:\d\d')
STAMP_AT_RE = re.compile(rb'\d{4}[-/]\d\d[-/]\d\d \d\d:\d\d:\d\d')
LAST_START_MAX_LINES = 20000
STAMP_LEN = 19

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


def _stamp_bytes(dt: datetime, fmt: str) -> bytes:
    pattern = '%Y/%m/%d %H:%M:%S' if fmt == 'go' else '%Y-%m-%d %H:%M:%S'
    return dt.astimezone(dt_timezone.utc).strftime(pattern).encode()


def _candidate_lines(data: bytes, fmt: str) -> list[int]:
    """Start offsets of the lines that hold one of the format's needles, in order."""
    hay = data.lower() if fmt == 'go' else data
    starts = set()
    for needle in NEEDLES[fmt]:
        pos = hay.find(needle)
        while pos >= 0:
            line_start = data.rfind(b'\n', 0, pos) + 1
            starts.add(line_start)
            line_end = data.find(b'\n', pos)
            if line_end < 0:
                break
            pos = hay.find(needle, line_end + 1)
    return sorted(starts)


def error_slices(data: bytes, fmt: str, cutoff: datetime | None = None) -> str:
    """Only the error lines of `data` (at or after `cutoff`) and the lines under each,
    as text for parse(). Every other line is dropped without being looked at in Python."""
    start_re = ERROR_START_RE[fmt]
    floor = _stamp_bytes(cutoff, fmt) if cutoff is not None else b''
    parts = []
    pos = 0
    for begin in _candidate_lines(data, fmt):
        if begin < pos or not start_re.match(data, begin):
            continue
        if floor and data[begin:begin + STAMP_LEN] < floor:
            continue
        line_end = data.find(b'\n', begin)
        if line_end < 0:
            end = len(data)
        else:
            nxt = ANY_START_RE.search(data, line_end + 1)
            end = nxt.start() if nxt else len(data)
        parts.append(data[begin:end].decode('utf-8', errors='replace'))
        pos = end
    return '\n'.join(p.rstrip('\n') for p in parts)


def first_stamp(data: bytes, fmt: str) -> datetime | None:
    m = ANY_START_RE.search(data)
    if not m:
        return None
    raw = m.group(0).decode()
    try:
        return _utc(datetime.strptime(raw, '%Y/%m/%d %H:%M:%S' if fmt == 'go' else '%Y-%m-%d %H:%M:%S'))
    except ValueError:
        return None


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

        # No timestamp: a continuation, a banner, a warning, or noise.
        if line.startswith('[GIN]') or (
            fmt == 'py' and (DJANGO_BANNER_RE.match(line) or PY_WARNING_RE.match(line))
        ):
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


_cache: dict = {}
_cache_lock = threading.Lock()
CACHE_IDLE_SECONDS = 6 * 3600
HEAD_BYTES = 256


def _scan(data: bytes, fmt: str) -> list:
    return list(parse(error_slices(data, fmt), fmt))


def file_entries(path: str, fmt: str):
    """(error entries, first timestamp read) for one file, or None when it cannot be read.

    Entries up to the start of the last timestamped line are final and kept; the lines
    from there on may still grow a traceback, so they are parsed again next time.
    """
    try:
        with open(path, 'rb') as fh:
            st = os.fstat(fh.fileno())
            key = (st.st_dev, st.st_ino)
            head = fh.read(HEAD_BYTES)
            with _cache_lock:
                hit = _cache.get(key)
                # A truncated or replaced file (same inode, other bytes) is read afresh.
                if hit is None or st.st_size < hit['done'] or hit['fmt'] != fmt or hit['head'] != head:
                    start = max(0, st.st_size - MAX_FILE_BYTES)
                    hit = {'fmt': fmt, 'head': head, 'done': start, 'entries': [], 'oldest': None}
                    if start > 0:
                        # Started mid-file: the first line is almost surely cut in half.
                        fh.seek(start)
                        nl = fh.read(64 * 1024).find(b'\n')
                        hit['done'] = start + nl + 1 if nl >= 0 else st.st_size
                    _cache[key] = hit
                hit['used'] = time.monotonic()
                fh.seek(hit['done'])
                data = fh.read(st.st_size - hit['done'])
                base = hit['done']
                if hit['oldest'] is None:
                    hit['oldest'] = first_stamp(data, fmt)
                boundary = _last_start(data) or 0
                if boundary:
                    hit['entries'].extend(_scan(data[:boundary], fmt))
                    hit['done'] = base + boundary
                pending = _scan(data[boundary:], fmt)
                return hit['entries'] + pending, hit['oldest']
    except OSError:
        return None


def _last_start(data: bytes) -> int | None:
    """Offset of the last line that starts with a timestamp, walking back from the end."""
    end = len(data)
    for _ in range(LAST_START_MAX_LINES):
        if end <= 0:
            return None
        start = data.rfind(b'\n', 0, end - 1) + 1
        if STAMP_AT_RE.match(data, start):
            return start
        if start == 0:
            return None
        end = start
    return None


def _prune_cache() -> None:
    now = time.monotonic()
    with _cache_lock:
        for key in [k for k, v in _cache.items() if now - v.get('used', now) > CACHE_IDLE_SECONDS]:
            del _cache[key]


def collect(service: str | None, q: str, hours: int, limit: int) -> dict:
    cutoff = datetime.now(dt_timezone.utc) - timedelta(hours=hours)
    needle = q.lower()
    services = []
    groups: dict[str, dict] = {}
    _prune_cache()

    for key, label, default, fmt in SERVICES:
        path = log_path(key, default)
        current = file_entries(path, fmt)
        info = {'key': key, 'label': label, 'readable': current is not None, 'since': None}
        services.append(info)
        if current is None or (service and service != key):
            continue
        rotated = file_entries(path + '.1', fmt) if os.path.exists(path + '.1') else None
        parts = [p for p in (rotated, current) if p is not None]
        oldest = next((p[1] for p in parts if p[1] is not None), None)
        # How far back the log goes, when that is inside the window asked for.
        if oldest is not None and oldest > cutoff:
            info['since'] = _iso(oldest)
        for entry in (e for p in parts for e in p[0]):
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
