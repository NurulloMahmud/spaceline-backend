"""
Point this service at a Nylas application: its API key, its client id, and a
webhook of its own, all written into .env.

The key is typed at a hidden prompt and the webhook secret goes straight
from Nylas into the file, so neither appears on screen, in shell history or
in a log. Nothing is restarted; run `supervisorctl restart email_agent` after.

    python -m scripts.nylas_production_setup \
        --client-id 26d268e8-7806-4508-a031-22bb3fda710f \
        --webhook-url https://spaceline.boxtruckmanage.com/email-agent/internal/v1/webhooks/nylas

    # also let these BTM user ids connect their own mailbox ("*" for everyone)
    ... --personal-users 12

Grants belong to an application, so every mailbox connected under the old
one has to be connected again afterwards.

A webhook already registered for the same URL on the application is
replaced, because Nylas shows a secret only once and a rerun needs a fresh
one.
"""
import argparse
import getpass
import os
import shutil
import sys
import time

import httpx

TRIGGERS = ["message.created", "grant.expired", "grant.deleted"]


def read_env(path: str) -> list[str]:
    with open(path) as f:
        return f.read().splitlines()


def set_values(lines: list[str], values: dict[str, str]) -> list[str]:
    """Replace each KEY= line in place, append the ones that are missing.
    Every other line is kept byte for byte."""
    remaining = dict(values)
    out = []
    for line in lines:
        key = line.split("=", 1)[0].strip() if "=" in line and not line.lstrip().startswith("#") else None
        if key in remaining:
            out.append(f"{key}={remaining.pop(key)}")
        else:
            out.append(line)
    out.extend(f"{k}={v}" for k, v in remaining.items())
    return out


def current(lines: list[str], key: str) -> str:
    for line in lines:
        if line.startswith(f"{key}="):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    return ""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--env", default=".env")
    parser.add_argument("--client-id", required=True)
    parser.add_argument("--webhook-url", required=True)
    parser.add_argument("--api-uri", default="https://api.us.nylas.com")
    parser.add_argument("--personal-users", default=None)
    args = parser.parse_args()

    if not os.path.exists(args.env):
        print(f"{args.env} not found; run this from the email-agent directory", file=sys.stderr)
        return 1

    api_key = getpass.getpass("Nylas API key for this application (input hidden): ").strip()
    if not api_key:
        print("no key entered, nothing changed", file=sys.stderr)
        return 1

    lines = read_env(args.env)
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    base = args.api_uri.rstrip("/")

    with httpx.Client(timeout=60) as client:
        # A cheap call that fails fast on a wrong or mistyped key.
        probe = client.get(f"{base}/v3/webhooks", headers=headers)
        if probe.status_code != 200:
            print(f"Nylas refused the key ({probe.status_code}); nothing changed", file=sys.stderr)
            return 1

        existing = [w for w in probe.json().get("data", []) if w.get("webhook_url") == args.webhook_url]
        for hook in existing:
            client.delete(f"{base}/v3/webhooks/{hook['id']}", headers=headers)

        # Nylas checks the URL with a challenge before it answers, so the
        # running service must be reachable at it.
        created = client.post(
            f"{base}/v3/webhooks",
            headers=headers,
            json={
                "trigger_types": TRIGGERS,
                "webhook_url": args.webhook_url,
                "description": "email-agent inbound broker mail",
            },
        )
        if created.status_code not in (200, 201):
            print(f"webhook creation failed ({created.status_code}): {created.text[:300]}", file=sys.stderr)
            return 1
        secret = (created.json().get("data") or {}).get("webhook_secret", "")
        if not secret:
            print("Nylas returned no webhook secret; nothing changed", file=sys.stderr)
            return 1

    values = {
        "NYLAS_API_KEY": api_key,
        "NYLAS_CLIENT_ID": args.client_id,
        "NYLAS_API_URI": base,
        "NYLAS_WEBHOOK_SECRET": secret,
    }
    if args.personal_users is not None:
        values["PERSONAL_MAILBOX_USERS"] = args.personal_users

    backup = f"{args.env}.bak-{int(time.time())}"
    shutil.copy2(args.env, backup)
    with open(args.env, "w") as f:
        f.write("\n".join(set_values(lines, values)) + "\n")

    print(f"wrote {', '.join(values)} to {args.env} (previous copy: {backup})")
    print(f"webhook registered for {args.webhook_url} with {', '.join(TRIGGERS)}")
    if current(lines, "NYLAS_CALLBACK_URI") == "":
        print("note: NYLAS_CALLBACK_URI is empty in .env; hosted auth will refuse to start")
    print("now run: supervisorctl restart email_agent")
    return 0


if __name__ == "__main__":
    sys.exit(main())
