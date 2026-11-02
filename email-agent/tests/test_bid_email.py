"""The bid email is a fixed template — these lock its shape."""
import pytest

from services import bid_email


DISPATCHER = {"full_name": "Jane Dispatcher", "username": "jane@shipluxellc.com"}


def test_body_matches_the_load_window_composer(load_snapshot, company_profile, driver_profile):
    body = bid_email.build_body(
        bid_amount=3200.0,
        load=load_snapshot,
        driver=driver_profile,
        company=company_profile,
        dispatcher=DISPATCHER,
    )

    assert body == (
        "All in: $3,200\n"
        "Miles out: 4\n"
        "ETA: 9mins\n"
        "Dimensions: 288 x 96 x 96 in\n"
        "Type: Large straight\n"
        "MC: 846834\n"
        "ALL BIDS ARE VALID 15 MINS\n"
        "\n"
        "ShipLuxe LLC\n"
        "MC: 846834\n"
        "10921 Reed Hartman Highway STE 323, Cincinnati, OH 45242\n"
        "+1 (630) 426-3362\n"
        "operation@shipluxellc.com\n"
        "\n"
        "Jane Dispatcher\n"
        "Dispatcher\n"
        "jane@shipluxellc.com"
    )


def test_html_body_uses_the_composer_styling(load_snapshot, company_profile, driver_profile):
    html = bid_email.build_html_body(
        bid_amount=3200.0,
        load=load_snapshot,
        driver=driver_profile,
        company=company_profile,
        dispatcher=DISPATCHER,
    )
    assert "border: 2px dashed #86c99b" in html
    assert '<span style="color: #15803d; font-weight: 700">$3,200</span>' in html
    assert '<p style="margin: 0 0 14px; color: #c0262d; font-weight: 700">ALL BIDS ARE VALID 15 MINS</p>' in html
    assert 'width="110" height="110"' in html
    assert "white-space:pre-wrap" not in html


def test_body_never_leaks_the_driver_amount(load_snapshot, company_profile, driver_profile):
    body = bid_email.build_body(
        bid_amount=3200.0,
        load=load_snapshot,
        driver=driver_profile,
        company=company_profile,
        dispatcher=DISPATCHER,
    )
    assert "2400" not in body
    assert "2,400" not in body


def test_validity_minutes_come_from_the_company(load_snapshot, company_profile, driver_profile):
    company_profile["bid_validity_minutes"] = 30
    body = bid_email.build_body(
        bid_amount=1500.0,
        load=load_snapshot,
        driver=driver_profile,
        company=company_profile,
        dispatcher=None,
    )
    assert "ALL BIDS ARE VALID 30 MINS" in body


def test_missing_driver_leaves_only_the_price_and_mc(load_snapshot, company_profile):
    body = bid_email.build_body(
        bid_amount=900.0,
        load=load_snapshot,
        driver=None,
        company=company_profile,
        dispatcher=None,
    )
    assert body.startswith("All in: $900\nMC: 846834\nALL BIDS")
    for label in ("Miles out", "ETA", "Dimensions", "Type"):
        assert label not in body
    assert "N/A" not in body


def test_dispatcher_without_an_email_username_signs_with_the_company_address(
    load_snapshot, company_profile, driver_profile
):
    body = bid_email.build_body(
        bid_amount=900.0,
        load=load_snapshot,
        driver=driver_profile,
        company=company_profile,
        dispatcher={"full_name": "Jane Dispatcher", "username": "jane"},
    )
    assert body.endswith("Dispatcher\noperation@shipluxellc.com")


def test_html_body_embeds_the_logo(load_snapshot, company_profile, driver_profile):
    html = bid_email.build_html_body(
        bid_amount=3200.0,
        load=load_snapshot,
        driver=driver_profile,
        company=company_profile,
        dispatcher=DISPATCHER,
    )
    assert "https://cdn.example.com/logo.png" in html
    # the logo sits between the validity line and the letterhead
    assert html.index("ALL BIDS ARE VALID") < html.index("logo.png")
    assert html.index("logo.png") < html.index("ShipLuxe LLC")


def test_values_are_escaped(load_snapshot, company_profile, driver_profile):
    company_profile["name"] = "A&B <Freight>"
    html = bid_email.build_html_body(
        bid_amount=1.0,
        load=load_snapshot,
        driver=driver_profile,
        company=company_profile,
        dispatcher=DISPATCHER,
    )
    assert "A&amp;B &lt;Freight&gt;" in html
    assert "<Freight>" not in html


@pytest.mark.parametrize(
    "miles, eta",
    [("", ""), ("0", "5mins"), ("4", "9mins"), ("60", "1h"), ("120", "1h 55mins"), ("125", "2h")],
)
def test_eta_matches_the_board(miles, eta):
    assert bid_email.eta_text(miles) == eta


@pytest.mark.parametrize(
    "stored, shown",
    [
        ("6304263362", "+1 (630) 426-3362"),
        ("+16304263362", "+1 (630) 426-3362"),
        ("630-426-3362", "+1 (630) 426-3362"),
        ("ext 12", "ext 12"),
        (None, ""),
    ],
)
def test_phone_is_shown_like_the_rest_of_the_app(stored, shown):
    assert bid_email.format_phone(stored) == shown


def test_html_to_text_reads_a_hand_edited_composer():
    html = (
        "<div>Hi Sam,</div><div><br></div><div>Can do <b>$1,500</b> &amp; on time.</div>"
        "<ul><li>Liftgate</li><li>Team</li></ul><style>p{color:red}</style>"
    )
    assert bid_email.html_to_text(html) == (
        "Hi Sam,\n\nCan do $1,500 & on time.\nLiftgate\nTeam"
    )


def test_subject_matches_the_composer(load_snapshot):
    assert bid_email.build_subject(load_snapshot) == (
        "Ref 55012 \u00b7 Chicago, IL \u2192 Detroit, MI"
    )


# --- the reference number the broker reads -----------------------------------
#
# A real atrek payload carries `order_number` (the broker's own number for the
# load) and `id` (atrek's internal row id). It carries no `load_id` at all —
# the fixture above does, which is why quoting the wrong number went unnoticed
# until a broker was emailed "Ref 2129500" for their load 1183963.

ATREK_SHAPED_LOAD = {
    "id": 2129500,
    "order_number": "1183963",
    "pick_up_at": "Lake Orion, MI 48359",
    "deliver_to": "Fountain Inn, SC 29644",
}


def test_subject_quotes_the_brokers_own_reference_not_our_row_id():
    """order_number is the number the broker can look up; id means nothing to them."""
    subject = bid_email.build_subject(ATREK_SHAPED_LOAD)
    assert subject == (
        "Ref 1183963 \u00b7 Lake Orion, MI 48359 \u2192 Fountain Inn, SC 29644"
    )
    assert "2129500" not in subject


def test_reference_falls_back_through_to_the_row_id():
    """Each fallback runs only when everything ahead of it is absent."""
    assert bid_email.load_reference({"order_number": "A1", "load_id": 2, "id": 3}) == "A1"
    assert bid_email.load_reference({"load_id": 2, "id": 3}) == "2"
    assert bid_email.load_reference({"reference_number": "R9", "id": 3}) == "R9"
    assert bid_email.load_reference({"id": 3}) == "3"


def test_blank_reference_fields_are_skipped():
    """A feed that sends an empty or zero reference must not print '(Ref )'."""
    assert bid_email.load_reference({"order_number": "", "id": 7}) == "7"
    assert bid_email.load_reference({"order_number": None, "id": 7}) == "7"
    assert bid_email.load_reference({"order_number": 0, "id": 7}) == "7"
    assert bid_email.load_reference({}) == ""


def test_subject_omits_the_reference_when_there_is_none():
    assert bid_email.build_subject(
        {"pick_up_at": "Chicago, IL", "deliver_to": "Detroit, MI"}
    ) == "Chicago, IL \u2192 Detroit, MI"
