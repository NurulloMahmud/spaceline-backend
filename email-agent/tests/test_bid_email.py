"""The bid email is a fixed template — these lock its shape."""
from services import bid_email


def test_body_matches_template(load_snapshot, company_profile, driver_profile):
    body = bid_email.build_body(
        bid_amount=3200.0,
        load=load_snapshot,
        driver=driver_profile,
        company=company_profile,
        dispatcher={"full_name": "Jane Dispatcher"},
    )

    assert "RATE: $3,200" in body
    assert 'DIMENSIONS: 288" x 96" x 96" / 12,000 lbs' in body
    assert "MILES OUT: 4" in body  # driver is ~4mi from the Chicago pickup
    assert "MC: 846834" in body
    assert "VEHICLE: Large Straight" in body
    assert "Truck equipment: Liftgate, Pallet Jack, Ramps: Yes" in body
    assert "ALL BIDS ARE VALID 15 MINUTES!" in body

    # letterhead
    assert "SHIPLUXE LLC" in body
    assert "MC 846834" in body
    assert "Address: 10921 Reed Hartman Highway STE 323, Cincinnati, OH 45242" in body
    assert "Phone: 630-426-3362" in body
    assert "operation@shipluxellc.com" in body

    # signature
    assert "Jane Dispatcher" in body
    assert "✉: operation@shipluxellc.com" in body
    assert "☎: 630-426-3362" in body


def test_body_never_leaks_the_driver_amount(load_snapshot, company_profile, driver_profile):
    body = bid_email.build_body(
        bid_amount=3200.0,
        load=load_snapshot,
        driver=driver_profile,
        company=company_profile,
        dispatcher={"full_name": "Jane Dispatcher"},
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
    assert "ALL BIDS ARE VALID 30 MINUTES!" in body


def test_missing_vehicle_degrades_to_na(load_snapshot, company_profile):
    body = bid_email.build_body(
        bid_amount=900.0,
        load=load_snapshot,
        driver=None,
        company=company_profile,
        dispatcher=None,
    )
    assert "DIMENSIONS: N/A" in body
    assert "MILES OUT: N/A" in body
    assert "Truck equipment: N/A" in body
    # falls back to the load's own vehicle type
    assert "VEHICLE: Large Straight" in body


def test_html_body_embeds_the_logo(load_snapshot, company_profile, driver_profile):
    html = bid_email.build_html_body(
        bid_amount=3200.0,
        load=load_snapshot,
        driver=driver_profile,
        company=company_profile,
        dispatcher={"full_name": "Jane Dispatcher"},
    )
    assert "https://cdn.example.com/logo.png" in html
    # the logo sits between the validity line and the letterhead
    assert html.index("ALL BIDS ARE VALID") < html.index("logo.png")
    assert html.index("logo.png") < html.index("SHIPLUXE LLC")


def test_subject_references_the_lane(load_snapshot):
    assert bid_email.build_subject(load_snapshot) == (
        "Bid — Chicago, IL to Detroit, MI (Ref 55012)"
    )
