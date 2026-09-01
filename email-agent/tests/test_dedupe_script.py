"""
The cleanup for duplicate sent copies already in the table.

It opens its own sessions through get_session(), so these tests commit their
fixtures first and read the result back the same way.
"""
from datetime import datetime, timedelta, timezone

from database import models
from scripts.dedupe_email_messages import dedupe

REPLY = "We can come down to $3,000 on this one."


def _sent(negotiation, *, nylas_id, body=REPLY, minutes_ago=0, rfc_id=None, user_id=None):
    return models.EmailMessage(
        negotiation_id=negotiation.id,
        nylas_message_id=nylas_id,
        rfc_message_id=rfc_id,
        direction="outbound",
        from_email="dispatch@shipluxellc.com",
        to_email="broker@acme-logistics.com",
        subject="Re: Bid",
        body_text=body,
        sent_by_user_id=user_id,
        created_at=datetime.now(timezone.utc) - timedelta(minutes=minutes_ago),
    )


def test_a_dry_run_reports_without_deleting(session, negotiation):
    session.add_all([
        _sent(negotiation, nylas_id="copy-a"),
        _sent(negotiation, nylas_id="copy-b"),
    ])
    session.commit()

    assert dedupe(apply=False, show=0) == (1, 1)

    session.expire_all()
    assert session.query(models.EmailMessage).count() == 2


def test_apply_removes_the_second_copy(session, negotiation):
    session.add_all([
        _sent(negotiation, nylas_id="copy-a"),
        _sent(negotiation, nylas_id="copy-b"),
    ])
    session.commit()

    assert dedupe(apply=True, show=0) == (1, 1)

    session.expire_all()
    assert session.query(models.EmailMessage).one().nylas_message_id == "copy-a"


def test_the_copy_the_app_sent_is_the_one_kept(session, negotiation):
    """It is the row that names the dispatcher who sent the email."""
    session.add_all([
        _sent(negotiation, nylas_id="mailbox-copy", minutes_ago=1),
        _sent(negotiation, nylas_id="app-copy", user_id=7),
    ])
    session.commit()

    assert dedupe(apply=True, show=0) == (1, 1)

    session.expire_all()
    kept = session.query(models.EmailMessage).one()
    assert kept.nylas_message_id == "app-copy" and kept.sent_by_user_id == 7


def test_copies_sharing_a_message_id_go_even_with_different_text(
    session, negotiation
):
    session.add_all([
        _sent(negotiation, nylas_id="copy-a", rfc_id="abc@mail"),
        _sent(negotiation, nylas_id="copy-b", rfc_id="abc@mail", body="Same mail, "
              "different text stored."),
    ])
    session.commit()

    assert dedupe(apply=True, show=0) == (1, 1)


def test_two_different_replies_are_both_left_alone(session, negotiation):
    session.add_all([
        _sent(negotiation, nylas_id="reply-1"),
        _sent(negotiation, nylas_id="reply-2", body="Actually, $3,050 and we are set."),
    ])
    session.commit()

    assert dedupe(apply=True, show=0) == (1, 0)
    session.expire_all()
    assert session.query(models.EmailMessage).count() == 2


def test_the_same_words_sent_the_next_day_are_left_alone(session, negotiation):
    session.add_all([
        _sent(negotiation, nylas_id="chase-1", minutes_ago=1440),
        _sent(negotiation, nylas_id="chase-2"),
    ])
    session.commit()

    assert dedupe(apply=True, show=0) == (1, 0)


def test_inbound_messages_are_never_touched(session, negotiation):
    """
    A broker's message is pointed at by suggestions and ratecon checks, and
    was never the thing being duplicated.
    """
    session.add_all([
        models.EmailMessage(
            negotiation_id=negotiation.id, nylas_message_id="in-1",
            direction="inbound", body_text="Can you do 3000?",
        ),
        models.EmailMessage(
            negotiation_id=negotiation.id, nylas_message_id="in-2",
            direction="inbound", body_text="Can you do 3000?",
        ),
    ])
    session.commit()

    assert dedupe(apply=True, show=0) == (1, 0)
    session.expire_all()
    assert session.query(models.EmailMessage).count() == 2


def test_running_it_twice_finds_nothing_the_second_time(session, negotiation):
    session.add_all([
        _sent(negotiation, nylas_id="copy-a"),
        _sent(negotiation, nylas_id="copy-b"),
    ])
    session.commit()

    assert dedupe(apply=True, show=0) == (1, 1)
    assert dedupe(apply=True, show=0) == (1, 0)


def test_one_negotiation_can_be_cleaned_on_its_own(session, negotiation, load_snapshot):
    other = models.Negotiation(
        company_id=1, load_uuid="load-uuid-2", load_snapshot=load_snapshot,
        bid_amount=2900.0, broker_email="broker@acme-logistics.com",
        subject="Bid — another load", status=models.BID_SENT,
    )
    session.add(other)
    session.flush()
    session.add_all([
        _sent(negotiation, nylas_id="a-1"),
        _sent(negotiation, nylas_id="a-2"),
        _sent(other, nylas_id="b-1"),
        _sent(other, nylas_id="b-2"),
    ])
    session.commit()

    assert dedupe(apply=True, show=0, negotiation_id=str(negotiation.id)) == (1, 1)

    session.expire_all()
    assert session.query(models.EmailMessage).count() == 3
