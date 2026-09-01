"""
Remove the duplicate copies of sent emails already stored on negotiations.

A reply written in a mail client rather than in this app reached the webhook
twice — the copy the client saved to Sent and the copy the provider saved,
each with its own Nylas id — and both were stored, so the negotiations page
showed the same email twice. `inbound.duplicate_of` now recognises the second
copy on the way in; the rows written before that do not remove themselves.

This walks the outbound messages of each negotiation and deletes every copy
after the first of the same email, where "the same email" means the same
Message-Id, or the same body within
`inbound.DUPLICATE_WINDOW_MINUTES` — the same two tests the live path uses.

Outbound only, deliberately. Inbound messages are pointed at by
`suggestions.in_reply_to_message_id` and `ratecon_checks.email_message_id`,
and they were never duplicated by this: the second copy exists because *we*
sent the mail.

The copy that is kept is the one the app wrote itself where there is one
(it names the dispatcher who sent it), otherwise the oldest.

    # look, change nothing
    python -m scripts.dedupe_email_messages

    # delete
    python -m scripts.dedupe_email_messages --apply

    # one negotiation, or a single company
    python -m scripts.dedupe_email_messages --negotiation <uuid> --apply
    python -m scripts.dedupe_email_messages --company 12 --apply

Run it from the service directory, with the same environment the service uses
(it reads DATABASE_URL through config.settings).
"""
import argparse
import logging
import sys
from datetime import timedelta

from database import models
from database.connection import get_session
from services.inbound import DUPLICATE_WINDOW_MINUTES, body_fingerprint

logger = logging.getLogger("dedupe")

BATCH = 200
WINDOW = timedelta(minutes=DUPLICATE_WINDOW_MINUTES)


def preview(text: str, width: int = 90) -> str:
    flat = " ".join((text or "").split())
    return flat[:width] + ("…" if len(flat) > width else "")


def _better_copy(a: models.EmailMessage, b: models.EmailMessage) -> models.EmailMessage:
    """Of two rows for one email, the one worth keeping."""
    if (a.sent_by_user_id is None) != (b.sent_by_user_id is None):
        return a if a.sent_by_user_id is not None else b
    return a if a.created_at <= b.created_at else b


def duplicates(messages: list[models.EmailMessage]) -> list[tuple]:
    """
    (kept, dropped) for every outbound message that is another copy of one
    already in the list. `messages` must be ordered oldest first.
    """
    pairs = []
    kept: list[models.EmailMessage] = []

    for message in messages:
        fingerprint = body_fingerprint(message.body_text)
        match = None
        for candidate in kept:
            same_email = (
                message.rfc_message_id
                and candidate.rfc_message_id == message.rfc_message_id
            )
            same_words = (
                fingerprint
                and body_fingerprint(candidate.body_text) == fingerprint
                and abs(message.created_at - candidate.created_at) <= WINDOW
            )
            if same_email or same_words:
                match = candidate
                break

        if match is None:
            kept.append(message)
            continue

        keep = _better_copy(match, message)
        drop = message if keep is match else match
        if keep is not match:
            kept[kept.index(match)] = keep
        pairs.append((keep, drop))

    return pairs


def dedupe(
    *,
    apply: bool,
    company_id: int | None = None,
    negotiation_id: str | None = None,
    show: int = 10,
) -> tuple[int, int]:
    """Returns (negotiations scanned, duplicate messages removed)."""
    scanned = 0
    removed = 0
    shown = 0
    last_id = None

    with get_session() as session:
        while True:
            query = session.query(models.Negotiation)
            if negotiation_id:
                query = query.filter(models.Negotiation.id == negotiation_id)
            if company_id is not None:
                query = query.filter(models.Negotiation.company_id == company_id)
            if last_id is not None:
                query = query.filter(models.Negotiation.id > last_id)

            negotiations = query.order_by(models.Negotiation.id).limit(BATCH).all()
            if not negotiations:
                break

            for negotiation in negotiations:
                last_id = negotiation.id
                scanned += 1

                messages = (
                    session.query(models.EmailMessage)
                    .filter(
                        models.EmailMessage.negotiation_id == negotiation.id,
                        models.EmailMessage.direction == "outbound",
                    )
                    .order_by(models.EmailMessage.created_at)
                    .all()
                )
                for keep, drop in duplicates(messages):
                    removed += 1
                    if shown < show:
                        shown += 1
                        print(f"\n  negotiation {negotiation.id}")
                        print(f"    keeping {keep.id} ({keep.nylas_message_id})")
                        print(f"    dropping {drop.id} ({drop.nylas_message_id})")
                        print(f"    text:   {preview(drop.body_text)}")
                    if apply:
                        session.delete(drop)

            if apply:
                # Per batch: the service caps how long a transaction may sit
                # idle, and this walks the whole table.
                session.commit()

            print(f"  ... {scanned} negotiations, {removed} duplicates", file=sys.stderr)

        if not apply:
            session.rollback()

    return scanned, removed


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply", action="store_true",
        help="delete the duplicate rows (without this nothing is changed)",
    )
    parser.add_argument("--company", type=int, help="limit to one company id")
    parser.add_argument("--negotiation", help="limit to one negotiation uuid")
    parser.add_argument(
        "--show", type=int, default=10, help="how many samples to print (default 10)",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    scanned, removed = dedupe(
        apply=args.apply,
        company_id=args.company,
        negotiation_id=args.negotiation,
        show=args.show,
    )

    print(f"\n{scanned} negotiations scanned, {removed} duplicate messages found.")
    if removed and not args.apply:
        print("Nothing was deleted. Re-run with --apply to remove them.")
    elif removed:
        print("Deleted.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
