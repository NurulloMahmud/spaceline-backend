"""
What happens when a broker replies.

Every inbound message produces exactly one dispatcher-visible outcome:
  - a drafted reply awaiting approval, or
  - a booked load, or
  - an alert (mismatch, unreadable ratecon, failed booking).

Nothing is ever sent to the broker from here.
"""
import asyncio
import logging
import re
from html import unescape
from typing import Optional

from sqlalchemy.orm import Session

from database import models
from services import booking, events, suggestions
from services.ai import ai
from services.boxtruck import BoxTruckError, boxtruck
from services.negotiations import load_summary_text, reply_subject
from services.nylas_client import NylasError, nylas

logger = logging.getLogger(__name__)

QUOTE_MARKERS = (
    "-----original message-----",
    "________________________________",
    "\nfrom: ",
)

# The quote lives inside a container in the HTML. It has to be cut before the
# tags are flattened, otherwise the quoted text survives as ordinary prose and
# the model reads our own previous email as if the broker had written it.
HTML_QUOTE_CONTAINERS = (
    r"<blockquote",
    r"<div[^>]+class=[\"']?[^\"'>]*gmail_quote",
    r"<div[^>]+id=[\"']?(appendonsend|divRplyFwdMsg)",   # Outlook
    r"<hr[^>]+id=[\"']?stopSpelling",                    # Outlook
    r"<div[^>]+class=[\"']?[^\"'>]*moz-cite-prefix",     # Thunderbird
    r"<div[^>]+class=[\"']?[^\"'>]*yahoo_quoted",
)

# "On Tue, 4 Aug 2026 at 17:21, Someone <a@b.com> wrote:" — the attribution
# line clients put above a quote. Bounded so a stray "on ... wrote:" in real
# prose cannot swallow the whole message.
ATTRIBUTION = re.compile(r"\bon\b.{0,200}?\bwrote:", re.IGNORECASE | re.DOTALL)

FORWARD_MARKER = re.compile(r"-+\s*forwarded message\s*-+", re.IGNORECASE)

# Outlook and Word send a <head> full of VML behaviour rules and CSS. Those are
# markup machinery, not words: flattening tags alone leaves the stylesheet
# sitting on top of the broker's actual sentence, which is how replies started
# arriving as "v\:* {behavior:url(#default#VML);} ... We can do $2,850".
NON_CONTENT_BLOCK = re.compile(r"(?is)<(style|script|head|title|xml)\b[^>]*>.*?</\1\s*>")
HTML_COMMENT = re.compile(r"(?s)<!--.*?-->")

# Safety net for a stylesheet that arrives unclosed and so survives the pass
# above. Only a line that is *entirely* a CSS rule is dropped, and only when its
# braces hold declarations, so prose containing braces is kept.
CSS_LEFTOVER = re.compile(
    r"""(?x)
    ^(?:
        [-\w.\#*\\:,\[\]="'~+>()\s]{0,200}\{[^{}]{0,2000}:[^{}]{0,2000}\}
      | [{}]
      | @(?:media|font-face|import|charset|page|namespace)\b.{0,300}
    )$"""
)

# Zero-width characters mail clients use for spacing. They render as nothing and
# only make the stored text harder to read.
INVISIBLE = re.compile(r"[\u200b-\u200d\u2060\ufeff\u00ad]")


def strip_quoted(body: str) -> str:
    """
    Trim quoted history so the model reads only what is new.

    Everything downstream depends on this: the classifier, the drafted reply
    and — critically — the agreed-rate reading, which would otherwise find our
    own quoted price and treat it as the broker's.
    """
    if not body:
        return ""

    # 1. Cut the quote while the HTML structure still identifies it.
    cut = len(body)
    for pattern in HTML_QUOTE_CONTAINERS:
        match = re.search(pattern, body, re.IGNORECASE)
        if match:
            cut = min(cut, match.start())
    text = body[:cut]

    # 2. Drop the markup machinery before flattening, or its contents survive
    #    as prose. Comments go second, so a stylesheet commented out the old
    #    Netscape way (<style><!-- ... --></style>) is already gone.
    text = NON_CONTENT_BLOCK.sub(" ", text)
    text = HTML_COMMENT.sub(" ", text)

    # 3. Flatten to text. Block-level tags become newlines so the line-based
    #    checks below still see structure.
    text = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</tr>", "\n", text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = unescape(text)
    text = text.replace(" ", " ")
    text = INVISIBLE.sub("", text)
    text = re.sub(r"[ \t]+", " ", text)
    # Every line carries the space its opening tag became. Trimming it here,
    # rather than at the end, lets the quote markers below match.
    text = re.sub(r"(?m)^[ \t]+|[ \t]+$", "", text)

    # 4. Cut on textual markers, for plain-text mail and anything the
    #    structural pass missed.
    lowered = text.lower()
    cut = len(text)
    for marker in QUOTE_MARKERS:
        idx = lowered.find(marker)
        if idx != -1:
            cut = min(cut, idx)
    for pattern in (ATTRIBUTION, FORWARD_MARKER):
        match = pattern.search(text)
        if match:
            cut = min(cut, match.start())
    text = text[:cut]

    # 5. Keep only the lines a person would have typed.
    lines = [
        line for line in text.splitlines()
        if not line.startswith(">") and not CSS_LEFTOVER.match(line)
    ]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def thread_text(negotiation: models.Negotiation, limit: int = 20) -> str:
    parts = []
    for msg in negotiation.messages[-limit:]:
        who = "CARRIER (us)" if msg.direction == "outbound" else "BROKER"
        parts.append(f"--- {who} ---\n{msg.body_text or ''}")
    return "\n\n".join(parts)


def negotiation_context(negotiation: models.Negotiation) -> str:
    load = negotiation.load_snapshot or {}
    return "\n".join([
        f"Our bid to the broker: ${negotiation.bid_amount:,.2f}",
        f"Current agreed rate: "
        + (f"${negotiation.agreed_amount:,.2f}" if negotiation.agreed_amount else "not settled yet"),
        f"Broker: {negotiation.broker_name or negotiation.broker_email}",
        "",
        load_summary_text(load),
    ])


def pdf_attachments(message: dict) -> list[dict]:
    out = []
    for att in message.get("attachments") or []:
        filename = (att.get("filename") or "").lower()
        content_type = (att.get("content_type") or "").lower()
        if filename.endswith(".pdf") or "pdf" in content_type:
            out.append(att)
    return out


async def handle_inbound_message(
    session: Session,
    account: models.EmailAccount,
    message: dict,
) -> None:
    """Entry point from the Nylas webhook, after grant/thread resolution."""
    nylas_message_id = message.get("id")
    thread_id = message.get("thread_id")

    negotiation = (
        session.query(models.Negotiation)
        .filter(
            models.Negotiation.company_id == account.company_id,
            models.Negotiation.nylas_thread_id == thread_id,
        )
        .first()
    )
    if not negotiation:
        logger.info(f"inbound message {nylas_message_id}: no negotiation for thread {thread_id}, ignoring")
        return

    if negotiation.status in (models.BOOKED, models.CLOSED):
        logger.info(f"negotiation {negotiation.id} is {negotiation.status}; storing message only")

    already = (
        session.query(models.EmailMessage)
        .filter(models.EmailMessage.nylas_message_id == nylas_message_id)
        .first()
    )
    if already:
        logger.info(f"inbound message {nylas_message_id} already stored, skipping")
        return

    from_email = ""
    senders = message.get("from") or []
    if senders and isinstance(senders[0], dict):
        from_email = senders[0].get("email", "")

    body_text = strip_quoted(message.get("body") or message.get("snippet") or "")
    attachments = pdf_attachments(message)

    stored = models.EmailMessage(
        negotiation_id=negotiation.id,
        nylas_message_id=nylas_message_id,
        direction="inbound",
        from_email=from_email,
        to_email=account.email_address,
        subject=message.get("subject"),
        body_text=body_text,
        has_attachments=bool(attachments),
        attachments=[
            {"id": a.get("id"), "filename": a.get("filename"), "size": a.get("size")}
            for a in attachments
        ],
    )
    session.add(stored)
    session.flush()
    session.refresh(negotiation)

    if negotiation.status in (models.BOOKED, models.CLOSED):
        return

    history = thread_text(negotiation)
    classification = await asyncio.to_thread(
        ai.classify_inbound,
        thread_text=history,
        message_text=body_text,
        attachments=[a.get("filename") for a in attachments],
    )

    if classification is None:
        logger.error(f"negotiation {negotiation.id}: classification failed, defaulting to reply draft")
        classification = {"contains_ratecon": False, "intent": "other"}

    if classification.get("contains_ratecon") and attachments:
        await handle_ratecon(
            session=session,
            account=account,
            negotiation=negotiation,
            message_row=stored,
            attachments=attachments,
            nylas_message_id=nylas_message_id,
        )
        return

    negotiation.status = models.NEGOTIATING
    await create_reply_suggestion(
        session=session,
        negotiation=negotiation,
        message_row=stored,
        intent=classification.get("intent"),
        history=history,
        body_text=body_text,
    )


async def handle_own_send(
    session: Session,
    account: models.EmailAccount,
    message: dict,
) -> None:
    """
    A message sent from the dispatch mailbox that did not go through this app —
    someone replied to the broker straight from Gmail or Outlook.

    It is recorded as an outbound message on the negotiation so the thread stays
    complete. That matters beyond display: thread_text() feeds the classifier,
    the reply drafter and extract_agreed_rate, so a rate agreed in an out-of-app
    reply would otherwise be invisible and a correctly-priced rate confirmation
    would be rejected as a mismatch.

    Nothing is classified and no reply is drafted — we do not answer ourselves.
    """
    nylas_message_id = message.get("id")
    thread_id = message.get("thread_id")

    negotiation = (
        session.query(models.Negotiation)
        .filter(
            models.Negotiation.company_id == account.company_id,
            models.Negotiation.nylas_thread_id == thread_id,
        )
        .first()
    )
    if not negotiation:
        logger.info(f"own send {nylas_message_id}: no negotiation for thread {thread_id}, ignoring")
        return

    already = (
        session.query(models.EmailMessage)
        .filter(models.EmailMessage.nylas_message_id == nylas_message_id)
        .first()
    )
    if already:
        # Sent through the app; it was recorded at send time.
        logger.info(f"own send {nylas_message_id} already stored, skipping")
        return

    recipients = message.get("to") or []
    to_email = ""
    if recipients and isinstance(recipients[0], dict):
        to_email = recipients[0].get("email", "")

    attachments = pdf_attachments(message)
    stored = models.EmailMessage(
        negotiation_id=negotiation.id,
        nylas_message_id=nylas_message_id,
        direction="outbound",
        from_email=account.email_address,
        to_email=to_email or negotiation.broker_email,
        subject=message.get("subject"),
        body_text=strip_quoted(message.get("body") or message.get("snippet") or ""),
        has_attachments=bool(attachments),
        attachments=[
            {"id": a.get("id"), "filename": a.get("filename"), "size": a.get("size")}
            for a in attachments
        ],
        # Null on an outbound message is what marks it as sent outside the app;
        # every send this service makes stamps the dispatcher who authorised it.
        sent_by_user_id=None,
    )
    session.add(stored)
    session.flush()

    logger.info(
        f"negotiation {negotiation.id}: recorded a reply sent outside the app "
        f"({nylas_message_id})"
    )

    # The dispatcher has answered this broker themselves, so every draft the
    # agent was still holding for them is moot. Left pending, they piled up in
    # the panel asking for a decision that had already been made in Gmail.
    superseded = suggestions.supersede_pending(
        session, negotiation.id, suggestions.REPLIED_ELSEWHERE
    )

    events.publish(
        events.NEGOTIATION_UPDATED,
        company_id=negotiation.company_id,
        negotiation_id=negotiation.id,
        load_uuid=negotiation.load_uuid,
        status=negotiation.status,
        message_id=str(stored.id),
        sent_outside_app=True,
        superseded=superseded,
        pending_suggestions=0,
    )


async def create_reply_suggestion(
    session: Session,
    negotiation: models.Negotiation,
    message_row: models.EmailMessage,
    intent: Optional[str],
    history: str,
    body_text: str,
) -> Optional[models.Suggestion]:
    draft = await asyncio.to_thread(
        ai.draft_reply,
        context=negotiation_context(negotiation),
        thread_text=history,
        message_text=body_text,
    )
    if not draft:
        logger.error(f"negotiation {negotiation.id}: reply draft failed")
        suggestion = models.Suggestion(
            negotiation_id=negotiation.id,
            in_reply_to_message_id=message_row.id,
            kind=models.KIND_REPLY,
            intent=intent,
            draft_subject=reply_subject(negotiation),
            draft_body="",
            ai_reasoning="The assistant could not draft a reply for this message. Please write one.",
            status=models.PENDING,
        )
    else:
        suggestion = models.Suggestion(
            negotiation_id=negotiation.id,
            in_reply_to_message_id=message_row.id,
            kind=models.KIND_REPLY,
            intent=draft.get("intent") or intent,
            draft_subject=reply_subject(negotiation),
            draft_body=draft.get("draft_body"),
            ai_reasoning=draft.get("reasoning"),
            status=models.PENDING,
        )

    # The new draft is written against the whole thread, so anything older is
    # answering a message this one already accounts for.
    suggestions.supersede_pending(
        session, negotiation.id, suggestions.NEWER_DRAFT, keep_id=suggestion.id
    )

    session.add(suggestion)
    session.flush()

    events.publish(
        events.SUGGESTION_CREATED,
        company_id=negotiation.company_id,
        negotiation_id=negotiation.id,
        load_uuid=negotiation.load_uuid,
        suggestion_id=str(suggestion.id),
        kind=suggestion.kind,
        intent=suggestion.intent,
        broker_email=negotiation.broker_email,
    )
    return suggestion


async def handle_ratecon(
    session: Session,
    account: models.EmailAccount,
    negotiation: models.Negotiation,
    message_row: models.EmailMessage,
    attachments: list[dict],
    nylas_message_id: str,
) -> None:
    """
    Parse, verify, and only then book. A failure at any step is surfaced to
    dispatch — a rate confirmation is never dropped silently.
    """
    negotiation.status = models.RATECON_RECEIVED
    session.flush()

    attachment = attachments[0]
    filename = attachment.get("filename") or "ratecon.pdf"

    try:
        content = await nylas.download_attachment(
            grant_id=account.nylas_grant_id,
            attachment_id=attachment.get("id"),
            message_id=nylas_message_id,
        )
    except NylasError as e:
        record_parse_failure(session, negotiation, message_row, filename, f"Could not download the attachment: {e}")
        return

    broker = await boxtruck.resolve_broker(
        name=negotiation.broker_name or "",
        mc=negotiation.broker_mc or "",
        email=negotiation.broker_email,
    )
    if broker:
        negotiation.tms_broker_id = broker.get("id")
        negotiation.broker_mc = broker.get("mc")

    try:
        parsed = await boxtruck.parse_ratecon(
            filename=filename,
            content=content,
            broker_id=(broker or {}).get("id"),
        )
    except BoxTruckError as e:
        record_parse_failure(session, negotiation, message_row, filename, str(e))
        return

    history = thread_text(negotiation)

    # The agreed rate is our bid unless the thread clearly settled on another.
    agreed = negotiation.bid_amount
    rate_reading = await asyncio.to_thread(
        ai.extract_agreed_rate, history, negotiation.bid_amount
    )
    if rate_reading and rate_reading.get("confident") and rate_reading.get("agreed_amount") is not None:
        agreed = float(rate_reading["agreed_amount"])
    negotiation.agreed_amount = agreed

    verdict = await asyncio.to_thread(
        ai.verify_ratecon,
        agreed_amount=agreed,
        load_summary=load_summary_text(negotiation.load_snapshot or {}),
        thread_text=history,
        parsed_ratecon=parsed,
    )

    if verdict is None:
        record_parse_failure(
            session, negotiation, message_row, filename,
            "The rate confirmation was read, but it could not be verified against the agreed terms.",
            parsed=parsed,
        )
        return

    # Price is decided arithmetically, not by the model's opinion of it.
    ratecon_amount = verdict.get("ratecon_amount")
    if ratecon_amount is None:
        ratecon_amount = parsed.get("total_rate_usd")
    price_ok = False
    if ratecon_amount is not None:
        from config.settings import config

        delta_cents = abs(round(float(ratecon_amount) * 100) - round(agreed * 100))
        price_ok = delta_cents <= config.PRICE_TOLERANCE_CENTS

    discrepancies = list(verdict.get("discrepancies") or [])
    if not price_ok:
        stated = f"${float(ratecon_amount):,.2f}" if ratecon_amount is not None else "no rate found"
        price_note = (
            f"Rate confirmation shows {stated} but we agreed ${agreed:,.2f}."
        )
        if not any("agreed" in d.lower() and "$" in d for d in discrepancies):
            discrepancies.insert(0, price_note)

    locations_ok = bool(verdict.get("locations_ok"))
    dates_ok = bool(verdict.get("dates_ok"))
    passed = price_ok and locations_ok and dates_ok

    check = models.RateconCheck(
        negotiation_id=negotiation.id,
        email_message_id=message_row.id,
        attachment_filename=filename,
        parsed_data=parsed,
        agreed_amount=agreed,
        ratecon_amount=float(ratecon_amount) if ratecon_amount is not None else None,
        price_ok=price_ok,
        locations_ok=locations_ok,
        dates_ok=dates_ok,
        discrepancies=discrepancies,
        outcome=models.OUTCOME_PASSED if passed else models.OUTCOME_MISMATCH,
    )
    session.add(check)
    session.flush()

    if not passed:
        await record_mismatch(session, negotiation, message_row, check, discrepancies)
        return

    await booking.book_verified_load(
        session=session,
        negotiation=negotiation,
        parsed=parsed,
        ratecon=(filename, content),
    )


def record_parse_failure(
    session: Session,
    negotiation: models.Negotiation,
    message_row: models.EmailMessage,
    filename: str,
    error: str,
    parsed: Optional[dict] = None,
) -> None:
    logger.error(f"negotiation {negotiation.id}: ratecon unusable — {error}")

    check = models.RateconCheck(
        negotiation_id=negotiation.id,
        email_message_id=message_row.id,
        attachment_filename=filename,
        parsed_data=parsed,
        outcome=models.OUTCOME_PARSE_FAILED,
        error=error,
    )
    session.add(check)

    suggestion = models.Suggestion(
        negotiation_id=negotiation.id,
        in_reply_to_message_id=message_row.id,
        kind=models.KIND_PARSE_FAILURE,
        draft_subject=reply_subject(negotiation),
        draft_body="",
        ai_reasoning=(
            f"A rate confirmation ({filename}) arrived but could not be read automatically. "
            f"Please open the email, check the document, and book the load manually.\n\nDetail: {error}"
        ),
        status=models.PENDING,
    )
    session.add(suggestion)
    session.flush()

    events.publish(
        events.RATECON_PARSE_FAILED,
        company_id=negotiation.company_id,
        negotiation_id=negotiation.id,
        load_uuid=negotiation.load_uuid,
        suggestion_id=str(suggestion.id),
        filename=filename,
        error=error,
        broker_email=negotiation.broker_email,
    )


async def record_mismatch(
    session: Session,
    negotiation: models.Negotiation,
    message_row: models.EmailMessage,
    check: models.RateconCheck,
    discrepancies: list[str],
) -> None:
    negotiation.status = models.MISMATCH
    logger.warning(
        f"negotiation {negotiation.id}: ratecon rejected — {'; '.join(discrepancies)}"
    )

    draft = await asyncio.to_thread(
        ai.draft_mismatch_reply,
        context=negotiation_context(negotiation),
        discrepancies=discrepancies,
    )
    suggestion = models.Suggestion(
        negotiation_id=negotiation.id,
        in_reply_to_message_id=message_row.id,
        kind=models.KIND_MISMATCH,
        intent="ratecon_mismatch",
        draft_subject=reply_subject(negotiation),
        draft_body=(draft or {}).get("draft_body") or "",
        ai_reasoning=(draft or {}).get("reasoning")
        or "The rate confirmation does not match the agreed terms. The load was not created.",
        status=models.PENDING,
    )
    session.add(suggestion)
    session.flush()

    events.publish(
        events.RATECON_MISMATCH,
        company_id=negotiation.company_id,
        negotiation_id=negotiation.id,
        load_uuid=negotiation.load_uuid,
        suggestion_id=str(suggestion.id),
        ratecon_check_id=str(check.id),
        discrepancies=discrepancies,
        agreed_amount=check.agreed_amount,
        ratecon_amount=check.ratecon_amount,
        broker_email=negotiation.broker_email,
    )
