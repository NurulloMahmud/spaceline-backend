import os
import shutil
import tempfile
from datetime import datetime, timedelta, timezone as dt_timezone

from django.test import SimpleTestCase, TestCase, override_settings
from rest_framework.test import APIClient

from users.models import CustomUser, Department

from . import logs

URL = '/api/dev/error-logs/'


def stamp(dt, fmt):
    if fmt == 'comma':
        return dt.strftime('%Y-%m-%d %H:%M:%S') + f',{dt.microsecond // 1000:03d}'
    return dt.strftime('%Y/%m/%d %H:%M:%S')


class LogFixtureMixin:
    def setUp(self):
        super().setUp()
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.now = datetime.now(dt_timezone.utc).replace(microsecond=0, tzinfo=None)
        self.paths = {k: os.path.join(self.dir, k + '.log') for k in logs.SERVICE_KEYS}
        override = override_settings(DEV_LOG_PATHS=self.paths)
        override.enable()
        self.addCleanup(override.disable)

    def write(self, key, text):
        with open(self.paths[key], 'w', encoding='utf-8') as fh:
            fh.write(text)

    def ago(self, **kw):
        return self.now - timedelta(**kw)


class ParseTests(LogFixtureMixin, SimpleTestCase):
    def test_email_format(self):
        t1, t2 = self.ago(minutes=30), self.ago(minutes=5)
        self.write('email', (
            f'{stamp(t1, "comma")} - httpx - INFO - HTTP Request: GET https://x "200 OK"\n'
            f'{stamp(t1, "comma")} - services.shared_inbox - ERROR - mailbox denzel@a.com: pass failed: b\'[AUTH] Invalid\'\n'
            f'{stamp(t2, "comma")} - services.shared_inbox - ERROR - mailbox rita@b.com: pass failed: b\'[AUTH] Invalid\'\n'
            f'{stamp(t2, "comma")} - services.inbound - WARNING - no negotiation\n'
        ))
        out = logs.collect('email', '', 24, 200)
        self.assertEqual(len(out['entries']), 1)
        e = out['entries'][0]
        self.assertEqual(e['count'], 2)
        self.assertEqual(e['level'], 'ERROR')
        self.assertTrue(e['message'].startswith('mailbox rita@b.com'))
        self.assertTrue(e['first_at'].endswith('Z'))
        self.assertLess(e['first_at'], e['last_at'])

    def test_python_format_with_traceback(self):
        t = self.ago(minutes=10)
        self.write('sylectus', (
            f'{stamp(t, "comma")} INFO Email uid=1 -> load pronumuk=1\n'
            f'{stamp(t, "comma")} ERROR Email listener failed - reconnecting in 5s.\n'
            'Traceback (most recent call last):\n'
            '  File "/usr/lib/python3.12/imaplib.py", line 1047, in _command_complete\n'
            'imaplib.IMAP4.abort: socket error: EOF\n'
            '\n'
            f'{stamp(t, "comma")} INFO Email uid=2 -> load pronumuk=2\n'
            'stray line after info\n'
        ))
        e = logs.collect('sylectus', '', 24, 200)['entries']
        self.assertEqual(len(e), 1)
        self.assertEqual(e[0]['message'], 'Email listener failed - reconnecting in 5s.')
        self.assertIn('socket error: EOF', e[0]['trace'])
        self.assertNotIn('stray', e[0]['trace'])

    def test_trace_is_capped_at_60_lines(self):
        t = self.ago(minutes=1)
        body = '\n'.join(f'  line {i}' for i in range(200))
        self.write('direct', f'{stamp(t, "comma")} ERROR boom\n{body}\n')
        e = logs.collect('direct', '', 24, 200)['entries'][0]
        self.assertEqual(len(e['trace'].split('\n')), 60)
        self.assertIn('lines omitted', e['trace'])
        self.assertIn('line 199', e['trace'])

    def test_django_new_format_and_old_blocks_skipped(self):
        t = self.ago(minutes=2)
        self.write('django', (
            '/home/api/venv/django/fields.py:1670: RuntimeWarning: naive datetime\n'
            '  warnings.warn(\n'
            'Internal Server Error: /api/users/list/\n'
            'Traceback (most recent call last):\n'
            '  File "old.py", line 1\n'
            f'{stamp(t, "comma")} ERROR django.request: Internal Server Error: /api/users/list/\n'
            'Traceback (most recent call last):\n'
            '  File "new.py", line 2\n'
            'ValueError: nope\n'
            f'{stamp(t, "comma")} WARNING django.request: Unauthorized: /api/x/\n'
        ))
        e = logs.collect('django', '', 24, 200)['entries']
        self.assertEqual(len(e), 1)
        self.assertEqual(e[0]['message'], 'django.request: Internal Server Error: /api/users/list/')
        self.assertIn('ValueError: nope', e[0]['trace'])
        self.assertNotIn('old.py', e[0]['trace'])

    def test_go_format(self):
        t = self.ago(minutes=3)
        self.write('go', (
            f'{stamp(t, "slash")} loads: failed to save structured load: ERROR: relation "loads" does not exist\n'
            f'{stamp(t, "slash")} SSE stream opened: loads/feed user 29 reset="cursor_too_old"\n'
            f'{stamp(t, "slash")} \x1b[32m/home/api/agent/internal/bids/repo.go:81\n'
            '\x1b[0m\x1b[33m[0.889ms] \x1b[34;1m[rows:0]\x1b[0m SELECT "error" FROM "load_bids"\n'
            f'[GIN] {t.strftime("%Y/%m/%d - %H:%M:%S")} | 500 | 5ms | 1.1.1.1 | GET "/api/v1/error"\n'
            f'{stamp(t, "slash")} worker PANIC: runtime error: index out of range\n'
        ))
        e = logs.collect('go', '', 24, 200)['entries']
        self.assertEqual(len(e), 2)
        msgs = {x['message'][:20] for x in e}
        self.assertIn('worker PANIC: runtim', msgs)
        for x in e:
            self.assertEqual(x['trace'], '')

    def test_hub_format_like_go(self):
        t = self.ago(minutes=3)
        self.write('hub', (
            f'{stamp(t, "slash")} POST /t/v1/drivers/lookup\n'
            f'{stamp(t, "slash")} carrier sync failed: 502 bad gateway\n'
        ))
        e = logs.collect('hub', '', 24, 200)['entries']
        self.assertEqual([x['message'] for x in e], ['carrier sync failed: 502 bad gateway'])

    def test_bad_bytes_and_missing_files(self):
        t = self.ago(minutes=1)
        with open(self.paths['email'], 'wb') as fh:
            fh.write(f'{stamp(t, "comma")} - a - ERROR - bad \xff\xfe bytes\n'.encode('latin-1'))
        out = logs.collect(None, '', 24, 200)
        self.assertEqual(len(out['entries']), 1)
        by_key = {s['key']: s['readable'] for s in out['services']}
        self.assertTrue(by_key['email'])
        self.assertFalse(by_key['django'])
        self.assertEqual(len(out['services']), 6)

    def test_unreadable_path_is_a_directory(self):
        os.remove(self.paths['go']) if os.path.exists(self.paths['go']) else None
        os.mkdir(self.paths['go'])
        out = logs.collect(None, '', 24, 200)
        self.assertFalse({s['key']: s['readable'] for s in out['services']}['go'])


class GroupingAndFilterTests(LogFixtureMixin, SimpleTestCase):
    def test_normalise(self):
        a = logs.normalise('negotiation c93f3f8e-1d55-4b22-9ab7-53fbf7cfe12d: failed (400) for a@b.com id 0xdeadbeef 12ab34cd56')
        b = logs.normalise('negotiation 11111111-2222-3333-4444-555555555555: failed (500) for z@y.org id 0x1234 99ff88ee77')
        self.assertEqual(a, b)
        self.assertEqual(logs.group_id('email', a), logs.group_id('email', b))
        self.assertNotEqual(logs.group_id('email', a), logs.group_id('go', a))

    def test_grouping_counts_and_times(self):
        lines = ''.join(
            f'{stamp(self.ago(minutes=m), "comma")} ERROR worker {m} crashed on uid {m * 7}\n'
            for m in (50, 40, 30)
        )
        self.write('direct', lines)
        e = logs.collect('direct', '', 24, 200)['entries']
        self.assertEqual(len(e), 1)
        self.assertEqual(e[0]['count'], 3)
        first = datetime.strptime(e[0]['first_at'], '%Y-%m-%dT%H:%M:%S.%fZ')
        last = datetime.strptime(e[0]['last_at'], '%Y-%m-%dT%H:%M:%S.%fZ')
        self.assertEqual(last - first, timedelta(minutes=20))

    def test_filters_service_q_hours_limit_and_order(self):
        self.write('email', (
            f'{stamp(self.ago(hours=30), "comma")} - a - ERROR - too old\n'
            f'{stamp(self.ago(hours=3), "comma")} - a - ERROR - Invalid Credentials for mailbox\n'
            f'{stamp(self.ago(hours=1), "comma")} - a - ERROR - smtp refused\n'
        ))
        self.write('sylectus', (
            f'{stamp(self.ago(minutes=5), "comma")} ERROR listener died\n'
            'Traceback (most recent call last):\n'
            'ConnectionResetError: by PEER\n'
        ))
        everything = logs.collect(None, '', 24, 200)['entries']
        self.assertEqual([x['message'] for x in everything],
                         ['listener died', 'smtp refused', 'Invalid Credentials for mailbox'])
        self.assertEqual(len(logs.collect('email', '', 24, 200)['entries']), 2)
        self.assertEqual(len(logs.collect(None, '', 48, 200)['entries']), 4)
        self.assertEqual(len(logs.collect(None, '', 2, 200)['entries']), 2)
        self.assertEqual(len(logs.collect(None, 'INVALID creds', 24, 200)['entries']), 0)
        self.assertEqual(len(logs.collect(None, 'invalid cred', 24, 200)['entries']), 1)
        self.assertEqual([x['service'] for x in logs.collect(None, 'by peer', 24, 200)['entries']], ['sylectus'])
        self.assertEqual(len(logs.collect(None, '', 24, 1)['entries']), 1)

    def test_only_last_5_mb_is_read(self):
        old = f'{stamp(self.ago(minutes=1), "comma")} - a - ERROR - ancient marker\n'
        filler = f'{stamp(self.ago(minutes=1), "comma")} - a - INFO - ' + 'x' * 900 + '\n'
        recent = f'{stamp(self.ago(minutes=1), "comma")} - a - ERROR - recent marker\n'
        with open(self.paths['email'], 'w') as fh:
            fh.write(old)
            for _ in range((logs.TAIL_BYTES // len(filler)) + 50):
                fh.write(filler)
            fh.write(recent)
        self.assertGreater(os.path.getsize(self.paths['email']), logs.TAIL_BYTES)
        msgs = [x['message'] for x in logs.collect('email', '', 24, 200)['entries']]
        self.assertEqual(msgs, ['recent marker'])


class PermissionTests(LogFixtureMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.dev = CustomUser.objects.create_user(
            username='dev', password='x', department=Department.objects.create(name='Developer'))
        self.mgmt = CustomUser.objects.create_user(
            username='mgmt', password='x', department=Department.objects.create(name='Management'))
        self.write('email', f'{stamp(self.ago(minutes=2), "comma")} - services.booking - ERROR - booking failed (400)\n')

    def get(self, user, params=''):
        client = APIClient()
        if user:
            client.force_authenticate(user)
        return client.get(URL + params)

    def test_developer_200_with_shape(self):
        res = self.get(self.dev)
        self.assertEqual(res.status_code, 200)
        self.assertEqual({s['key'] for s in res.data['services']}, set(logs.SERVICE_KEYS))
        entry = res.data['entries'][0]
        self.assertEqual(set(entry), {'id', 'service', 'level', 'message', 'trace', 'first_at', 'last_at', 'count'})

    def test_management_403_and_anonymous_401(self):
        self.assertEqual(self.get(self.mgmt).status_code, 403)
        self.assertIn(self.get(None).status_code, (401, 403))

    def test_query_params(self):
        self.assertEqual(len(self.get(self.dev, '?service=email&q=BOOKING&hours=1&limit=5').data['entries']), 1)
        self.assertEqual(len(self.get(self.dev, '?service=go').data['entries']), 0)
        self.assertEqual(self.get(self.dev, '?service=nope').status_code, 400)
        self.assertEqual(self.get(self.dev, '?hours=abc&limit=-3').status_code, 200)
