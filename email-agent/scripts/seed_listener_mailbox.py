"""
Record the mailbox the posted-loads listener already reads, so Admin >
Mailboxes shows it and a developer can change it from there.

Reads EMAIL_ADDRESS, EMAIL_APP_PASSWORD, EMAIL_FOLDER and EMAIL_IMAP_HOST
from the listener's own .env (never printed), proves the sign-in over IMAP,
and writes the `listener_mailbox` row for the company that owns the shared
mailbox. Does nothing when a row already exists unless --replace is given.

    python -m scripts.seed_listener_mailbox --env /home/api/spaceline/sylectus/.env
"""
import argparse
import sys

from database.connection import get_session
from database import models
from services import imap_smtp, secrets


def read_env(path: str) -> dict:
    out = {}
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            out[key.strip()] = value.strip().strip('"').strip("'")
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--env", required=True, help="the listener's .env")
    parser.add_argument("--company-id", type=int, default=None, help="defaults to the company of the shared mailbox")
    parser.add_argument("--replace", action="store_true")
    args = parser.parse_args()

    env = read_env(args.env)
    address = env.get("EMAIL_ADDRESS", "").strip().lower()
    password = env.get("EMAIL_APP_PASSWORD", "")
    folder = env.get("EMAIL_FOLDER", "INBOX") or "INBOX"
    host = env.get("EMAIL_IMAP_HOST", "imap.gmail.com") or "imap.gmail.com"
    if not address or not password:
        print("EMAIL_ADDRESS or EMAIL_APP_PASSWORD is missing from that .env; nothing written", file=sys.stderr)
        return 1

    result = imap_smtp.check(
        imap_smtp.Credentials(address=address, password=password, imap_host=host, imap_port=993, folder=folder),
        smtp=False,
    )
    if not result.imap_ok:
        print(f"{address} did not sign in over IMAP: {result.imap_error}; nothing written", file=sys.stderr)
        return 1

    with get_session() as session:
        company_id = args.company_id
        if company_id is None:
            shared = (
                session.query(models.EmailAccount)
                .filter(models.EmailAccount.user_id.is_(None))
                .order_by(models.EmailAccount.created_at)
                .first()
            )
            if shared is None:
                print("no shared mailbox found to take the company id from; pass --company-id", file=sys.stderr)
                return 1
            company_id = shared.company_id
        existing = (
            session.query(models.ListenerMailbox)
            .filter(models.ListenerMailbox.company_id == company_id)
            .first()
        )
        if existing is not None and not args.replace:
            print(f"a listener mailbox is already set ({existing.email_address}, {existing.folder}); use --replace to overwrite")
            return 0
        row = existing or models.ListenerMailbox(company_id=company_id, secret="")
        row.email_address = address
        row.imap_host = host
        row.imap_port = 993
        row.folder = folder
        row.secret = secrets.encrypt(password)
        row.updated_by_user_id = None
        session.add(row)
    print(f"listener mailbox set to {address}, folder {folder!r}, {result.inbox_messages} messages, {result.alerts_today} alerts in the last day")
    return 0


if __name__ == "__main__":
    sys.exit(main())
