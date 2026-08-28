"""
Re-clean the stored text of email messages.

`strip_quoted` runs once, when a message is stored, so every row keeps
whatever that function produced on the day it arrived. When the function is
fixed — as it was for the Outlook stylesheets that were landing on top of
brokers' replies — the rows already in the table keep the old text, and the
dispatcher reading a negotiation from last week still sees

    v\\:* {behavior:url(#default#VML);}
    o\\:* {behavior:url(#default#VML);}
    ...

above the broker's sentence. This walks the table and runs the current
`strip_quoted` over each body again.

Safe to re-run: `strip_quoted` is pure, and text it has already cleaned comes
back unchanged.

    # look, change nothing
    python -m scripts.backfill_message_text

    # write
    python -m scripts.backfill_message_text --apply

    # one negotiation, or a single company
    python -m scripts.backfill_message_text --negotiation <uuid> --apply
    python -m scripts.backfill_message_text --company 12 --apply

Run it from the service directory, with the same environment the service uses
(it reads DATABASE_URL through config.settings).
"""
import argparse
import logging
import sys

from database import models
from database.connection import get_session
from services.inbound import strip_quoted

logger = logging.getLogger("backfill")

BATCH = 500


def preview(text: str, width: int = 100) -> str:
    """One line, so a diff of two bodies stays readable in a terminal."""
    flat = " ".join((text or "").split())
    return flat[:width] + ("…" if len(flat) > width else "")


def backfill(
    *,
    apply: bool,
    company_id: int | None = None,
    negotiation_id: str | None = None,
    show: int = 10,
) -> tuple[int, int]:
    """Returns (rows scanned, rows whose text changed)."""
    scanned = 0
    changed = 0
    shown = 0
    last_id = None

    with get_session() as session:
        while True:
            query = session.query(models.EmailMessage)
            if negotiation_id:
                query = query.filter(models.EmailMessage.negotiation_id == negotiation_id)
            if company_id is not None:
                query = query.join(
                    models.Negotiation,
                    models.Negotiation.id == models.EmailMessage.negotiation_id,
                ).filter(models.Negotiation.company_id == company_id)
            if last_id is not None:
                query = query.filter(models.EmailMessage.id > last_id)

            rows = query.order_by(models.EmailMessage.id).limit(BATCH).all()
            if not rows:
                break

            for row in rows:
                last_id = row.id
                scanned += 1
                cleaned = strip_quoted(row.body_text)
                if cleaned == (row.body_text or ""):
                    continue

                changed += 1
                if shown < show:
                    shown += 1
                    print(f"\n  message {row.id} ({row.direction})")
                    print(f"    before: {preview(row.body_text)}")
                    print(f"    after:  {preview(cleaned)}")
                if apply:
                    row.body_text = cleaned

            if apply:
                # Per batch, not once at the end: the service sets an
                # idle-in-transaction timeout on its connections, and a table
                # this walks whole would sit in one transaction far past it.
                session.commit()

            print(f"  ... {scanned} scanned, {changed} to rewrite", file=sys.stderr)

        if not apply:
            # A dry run assigns nothing, so there is nothing to undo. Explicit
            # anyway, so the commit in get_session() can never write a run the
            # operator asked to only look at.
            session.rollback()

    return scanned, changed


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply", action="store_true",
        help="write the cleaned text back (without this nothing is changed)",
    )
    parser.add_argument("--company", type=int, help="limit to one company id")
    parser.add_argument("--negotiation", help="limit to one negotiation uuid")
    parser.add_argument(
        "--show", type=int, default=10,
        help="how many before/after samples to print (default 10)",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    scanned, changed = backfill(
        apply=args.apply,
        company_id=args.company,
        negotiation_id=args.negotiation,
        show=args.show,
    )

    print(f"\n{scanned} messages scanned, {changed} with text to rewrite.")
    if changed and not args.apply:
        print("Nothing was written. Re-run with --apply to keep the cleaned text.")
    elif changed:
        print("Written.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
