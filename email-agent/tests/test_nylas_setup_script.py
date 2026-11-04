"""The .env rewrite in scripts/nylas_production_setup must touch only its own keys."""
from scripts.nylas_production_setup import set_values, webhooks_at


def test_an_application_with_no_webhooks_answers_null():
    assert webhooks_at({"data": None}, "https://x/hook") == []


def test_only_webhooks_for_the_url_are_picked():
    payload = {"data": [{"id": "a", "webhook_url": "https://x/hook"}, {"id": "b", "webhook_url": "https://y"}]}
    assert [w["id"] for w in webhooks_at(payload, "https://x/hook")] == ["a"]


def test_existing_keys_are_replaced_in_place_and_the_rest_kept():
    lines = [
        "# nylas",
        "NYLAS_API_KEY=old-key",
        'JWT_SECRET=a$b"c=d',
        "NYLAS_CLIENT_ID=old-client",
    ]
    out = set_values(lines, {"NYLAS_API_KEY": "new-key", "NYLAS_CLIENT_ID": "new-client"})
    assert out == [
        "# nylas",
        "NYLAS_API_KEY=new-key",
        'JWT_SECRET=a$b"c=d',
        "NYLAS_CLIENT_ID=new-client",
    ]


def test_missing_keys_are_appended():
    out = set_values(["A=1"], {"NYLAS_WEBHOOK_SECRET": "s"})
    assert out == ["A=1", "NYLAS_WEBHOOK_SECRET=s"]


def test_a_commented_key_is_left_alone():
    out = set_values(["# NYLAS_API_KEY=x"], {"NYLAS_API_KEY": "y"})
    assert out == ["# NYLAS_API_KEY=x", "NYLAS_API_KEY=y"]
