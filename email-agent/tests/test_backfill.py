"""
The backfill re-runs the current strip_quoted over text already in the table.

It opens its own sessions through get_session(), so these tests commit their
fixtures first and read the result back the same way.
"""
from database import models
from scripts.backfill_message_text import backfill

# What the old strip_quoted left in the table: the tags are long gone, so only
# the leaked stylesheet text remains to be recognised.
DIRTY = (
    "v\\:* {behavior:url(#default#VML);}\n"
    "o\\:* {behavior:url(#default#VML);}\n"
    ".shape {behavior:url(#default#VML);}\n"
    "We can do $2,850 all in."
)
CLEAN = "We can do $2,850 all in."


def _message(negotiation, body, direction="inbound"):
    return models.EmailMessage(
        negotiation_id=negotiation.id, direction=direction, body_text=body
    )


def test_a_dry_run_reports_without_writing(session, negotiation):
    session.add(_message(negotiation, DIRTY))
    session.commit()

    scanned, changed = backfill(apply=False, show=0)
    assert (scanned, changed) == (1, 1)

    session.expire_all()
    assert session.query(models.EmailMessage).one().body_text == DIRTY


def test_apply_rewrites_the_stored_text(session, negotiation):
    session.add(_message(negotiation, DIRTY))
    session.commit()

    scanned, changed = backfill(apply=True, show=0)
    assert (scanned, changed) == (1, 1)

    session.expire_all()
    assert session.query(models.EmailMessage).one().body_text == CLEAN


def test_text_that_is_already_clean_is_left_alone(session, negotiation):
    session.add(_message(negotiation, "Sounds good, sending the ratecon."))
    session.commit()

    assert backfill(apply=True, show=0) == (1, 0)


def test_running_it_twice_changes_nothing_the_second_time(session, negotiation):
    session.add(_message(negotiation, DIRTY))
    session.commit()

    assert backfill(apply=True, show=0) == (1, 1)
    assert backfill(apply=True, show=0) == (1, 0)


def test_it_can_be_scoped_to_one_negotiation(session, load_snapshot, negotiation):
    other = models.Negotiation(
        company_id=2,
        load_uuid="load-uuid-2",
        load_snapshot=load_snapshot,
        bid_amount=1000.0,
        broker_email="someone@other-brokerage.com",
        subject="Bid — elsewhere",
        status=models.BID_SENT,
    )
    session.add(other)
    session.flush()
    session.add(_message(negotiation, DIRTY))
    session.add(_message(other, DIRTY))
    session.commit()

    assert backfill(apply=True, negotiation_id=str(negotiation.id), show=0) == (1, 1)

    session.expire_all()
    bodies = {
        m.negotiation_id: m.body_text for m in session.query(models.EmailMessage).all()
    }
    assert bodies[negotiation.id] == CLEAN
    assert bodies[other.id] == DIRTY


def test_it_can_be_scoped_to_one_company(session, load_snapshot, negotiation):
    other = models.Negotiation(
        company_id=2,
        load_uuid="load-uuid-2",
        load_snapshot=load_snapshot,
        bid_amount=1000.0,
        broker_email="someone@other-brokerage.com",
        subject="Bid — elsewhere",
        status=models.BID_SENT,
    )
    session.add(other)
    session.flush()
    session.add(_message(negotiation, DIRTY))
    session.add(_message(other, DIRTY))
    session.commit()

    assert backfill(apply=True, company_id=2, show=0) == (1, 1)

    session.expire_all()
    bodies = {
        m.negotiation_id: m.body_text for m in session.query(models.EmailMessage).all()
    }
    assert bodies[other.id] == CLEAN
    assert bodies[negotiation.id] == DIRTY
