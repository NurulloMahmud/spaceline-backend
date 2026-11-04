"""
Change which events the application's Nylas webhook delivers, in place.

The webhook was registered with `message.created`, which makes Nylas read
every new message in every connected mailbox to deliver it. Gmail charges
those reads to each mailbox's own API quota, and a mailbox on the Sylectus
alert list runs out of it, so the service now reads the shared mailbox over
IMAP instead (services.shared_inbox) and the webhook keeps only the grant
events. Updating the triggers keeps the webhook's secret, so nothing in .env
changes and no restart is needed.

    python -m scripts.nylas_webhook_triggers \\
        --webhook-url https://spaceline.boxtruckmanage.com/email-agent/internal/v1/webhooks/nylas

    # show the application's webhooks and stop
    python -m scripts.nylas_webhook_triggers --list

The API key comes from .env through config.settings, like the service itself.
"""
import argparse
import sys

import httpx

from config.settings import config

GRANT_ONLY = ["grant.expired", "grant.deleted"]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--webhook-url", default="")
    parser.add_argument("--triggers", default=",".join(GRANT_ONLY),
                        help="comma-separated trigger types to keep (default: %(default)s)")
    parser.add_argument("--list", action="store_true", help="print the webhooks and change nothing")
    args = parser.parse_args()

    if not config.NYLAS_API_KEY:
        print("NYLAS_API_KEY is empty in .env; nothing to do", file=sys.stderr)
        return 1
    headers = {"Authorization": f"Bearer {config.NYLAS_API_KEY}", "Content-Type": "application/json"}
    base = config.NYLAS_API_URI.rstrip("/")

    with httpx.Client(timeout=60) as client:
        listed = client.get(f"{base}/v3/webhooks", headers=headers)
        if listed.status_code != 200:
            print(f"Nylas refused the key ({listed.status_code}); nothing changed", file=sys.stderr)
            return 1
        hooks = listed.json().get("data") or []
        for hook in hooks:
            print(f"{hook.get('id')}  {hook.get('status')}  {hook.get('webhook_url')}  {hook.get('trigger_types')}")
        if args.list:
            return 0
        if not args.webhook_url:
            print("--webhook-url is required to change a webhook", file=sys.stderr)
            return 1

        wanted = [t.strip() for t in args.triggers.split(",") if t.strip()]
        matching = [h for h in hooks if h.get("webhook_url") == args.webhook_url]
        if not matching:
            print(f"no webhook registered for {args.webhook_url}; nothing changed", file=sys.stderr)
            return 1
        for hook in matching:
            if sorted(hook.get("trigger_types") or []) == sorted(wanted):
                print(f"{hook['id']} already delivers {wanted}")
                continue
            updated = client.put(
                f"{base}/v3/webhooks/{hook['id']}",
                headers=headers,
                json={
                    "trigger_types": wanted,
                    "webhook_url": hook["webhook_url"],
                    "description": hook.get("description") or "email-agent",
                },
            )
            if updated.status_code not in (200, 201):
                print(f"update of {hook['id']} failed ({updated.status_code}): {updated.text[:300]}", file=sys.stderr)
                return 1
            print(f"{hook['id']} now delivers {wanted}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
