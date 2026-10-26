"""
Gunicorn settings for the TMS API.

One worker process with threads, not several workers. A rate-con read takes
4-11 seconds; on a single synchronous worker every other user's request waited
behind it. Threads let those requests run side by side while the process stays
single, which matters because billing.utils.debounce_recalculate_miles keeps
its pending timers in process memory — a second worker would not see them and
the same load would be recalculated twice. Moving that debounce out of process
is the prerequisite for raising `workers`.

Pass this with `-c gunicorn.conf.py`; the bind address stays on the command line.
"""
workers = 1
worker_class = "gthread"
threads = 8

# The default 30s killed the worker mid-request whenever a rate-con parse plus
# the miles calls ran long. nginx still cuts a request off at its own
# proxy_read_timeout (60s unless set), so that is the real ceiling for users.
timeout = 120
graceful_timeout = 30
keepalive = 5
