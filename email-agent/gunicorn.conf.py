"""
Process manager for email-agent.

Run with:  gunicorn -c gunicorn.conf.py main:app

Why gunicorn in front of uvicorn, when `uvicorn main:app` also works:

  This service is a single event loop that makes blocking psycopg2 calls. If
  one of those calls wedges (a pool starved of connections, a stalled socket),
  the loop stops answering *and* stops responding to SIGTERM, so the only way
  back was a manual `supervisorctl restart`. The database timeouts in
  database/connection.py now bound every wait, but gunicorn is the backstop:
  its arbiter pings each worker, and a worker whose loop is frozen past
  `timeout` seconds is killed and respawned automatically.

WORKERS: kept at 1 on purpose. The SSE hub in services/events.py holds its
subscribers in process memory, and the stale-bid sweeper in main.py starts one
task per process. Raising `workers` needs both of those moved out first (a
single sweeper program, Postgres LISTEN/NOTIFY or Redis for the hub) and the
per-worker pool shrunk so total connections stay under Postgres max_connections.
Override with GUNICORN_WORKERS only once that work is done.
"""
import os

bind = os.getenv("GUNICORN_BIND", "127.0.0.1:8100")
workers = int(os.getenv("GUNICORN_WORKERS", "1"))
worker_class = "uvicorn.workers.UvicornWorker"

# A worker silent this long is assumed wedged: killed and replaced. Must stay
# comfortably above the longest legitimate handler — an inbound webhook can sit
# through a Nylas fetch plus two model calls — so it only ever trips on a real
# freeze, never on a slow-but-alive request.
timeout = int(os.getenv("GUNICORN_TIMEOUT", "120"))
graceful_timeout = int(os.getenv("GUNICORN_GRACEFUL_TIMEOUT", "30"))
keepalive = 5

accesslog = "-"
errorlog = "-"
loglevel = os.getenv("GUNICORN_LOGLEVEL", "info")
