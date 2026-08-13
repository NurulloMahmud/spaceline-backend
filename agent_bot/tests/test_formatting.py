"""Drivers read these messages, so the timestamps have to read like times."""
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DATABASE_URL", "postgresql+psycopg2://localhost/unused")
os.environ.setdefault("OPENAI_API_KEY", "test-key")

from agents.loads import format_place, format_when  # noqa: E402


def test_feed_timestamp_becomes_readable():
    assert format_when("2026-07-23T15:00:00-04:00", "Chicago, IL 60609") == (
        "2026-07-23 15:00 (Chicago, IL)"
    )


def test_local_time_is_shown_as_given_not_converted():
    """The offset is the stop's own local time; shifting it would mislead."""
    assert format_when("2026-07-23T09:00:00-07:00", "Los Angeles, CA 90001") == (
        "2026-07-23 09:00 (Los Angeles, CA)"
    )


def test_timestamp_without_a_place():
    assert format_when("2026-07-23T15:00:00-04:00") == "2026-07-23 15:00"


def test_datetime_objects_are_accepted():
    value = datetime(2026, 7, 23, 15, 0, tzinfo=timezone.utc)
    assert format_when(value, "Dover, OK 73734") == "2026-07-23 15:00 (Dover, OK)"


def test_missing_timestamp_reads_as_na():
    assert format_when(None) == "N/A"
    assert format_when("") == "N/A"


def test_unparseable_timestamp_is_passed_through_untouched():
    assert format_when("sometime tuesday") == "sometime tuesday"


def test_zip_is_stripped_from_places():
    assert format_place("Chicago, IL 60609") == "Chicago, IL"
    assert format_place("Council Bluffs, IA 51501-1234") == "Council Bluffs, IA"
    assert format_place("Lincoln, NE") == "Lincoln, NE"
    assert format_place(None) == ""


def test_a_place_that_ends_in_digits_is_not_mangled():
    assert format_place("Highway 101") == "Highway 101"
