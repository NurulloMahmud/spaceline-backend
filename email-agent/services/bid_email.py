"""
The bid email sent to a broker. This is a fixed template, not AI-generated —
the numbers in it are the offer, so they are rendered verbatim from the
negotiation and never paraphrased by a model.

Placeholders come from three places:
  RATE            the dispatcher's bid amount
  DIMENSIONS      the driver's vehicle cargo box (what we can carry)
  MILES OUT       deadhead from the driver's current position to pickup
  VEHICLE         the driver's vehicle type
  Truck equipment the driver's vehicle equipment list
  letterhead      the company profile in the TMS
  signature       the dispatcher who placed the bid
"""
from typing import Optional

from services.geo import haversine


def _fmt_money(amount: float) -> str:
    if amount == int(amount):
        return f"{int(amount):,}"
    return f"{amount:,.2f}"


def vehicle_dimensions(vehicle: Optional[dict]) -> str:
    """Cargo box as L x W x H in inches; blank when the vehicle has no dims."""
    if not vehicle:
        return "N/A"
    length, width, height = vehicle.get("length"), vehicle.get("width"), vehicle.get("height")
    if not any([length, width, height]):
        return "N/A"
    parts = [f'{v}"' if v else '?"' for v in (length, width, height)]
    dims = " x ".join(parts)
    payload = vehicle.get("payload")
    return f"{dims} / {payload:,} lbs" if payload else dims


def vehicle_equipment(vehicle: Optional[dict]) -> str:
    if not vehicle:
        return "N/A"
    names = [
        e.get("name")
        for e in (vehicle.get("equipments") or [])
        if isinstance(e, dict) and e.get("name")
    ]
    ramps = vehicle.get("ramps")
    if ramps and ramps not in names:
        names.append(f"Ramps: {ramps}")
    return ", ".join(names) if names else "N/A"


def miles_out(driver: Optional[dict], load: dict) -> str:
    """Deadhead from where the driver is now to the pickup."""
    if not driver:
        return "N/A"
    d_lat = driver.get("current_latitude")
    d_lng = driver.get("current_longitude")
    p_lat = load.get("pick_up_latitude")
    p_lng = load.get("pick_up_longitude")
    if not all([d_lat, d_lng, p_lat, p_lng]):
        return "N/A"
    try:
        return f"{haversine(float(d_lat), float(d_lng), float(p_lat), float(p_lng)):.0f}"
    except (TypeError, ValueError):
        return "N/A"


def build_subject(load: dict) -> str:
    pickup = load.get("pick_up_at") or "Pickup"
    delivery = load.get("deliver_to") or "Delivery"
    ref = load.get("load_id") or load.get("id") or ""
    base = f"Bid — {pickup} to {delivery}"
    return f"{base} (Ref {ref})" if ref else base


def build_body(
    *,
    bid_amount: float,
    load: dict,
    driver: Optional[dict],
    company: dict,
    dispatcher: Optional[dict],
) -> str:
    vehicle = (driver or {}).get("vehicle")

    vehicle_type = (
        (vehicle or {}).get("vehicle_type")
        or load.get("suggested_truck")
        or load.get("vehicle_type")
        or "N/A"
    )

    validity = company.get("bid_validity_minutes") or 15
    company_name = (company.get("name") or "").upper()
    company_mc = company.get("mc") or ""
    company_address = company.get("address") or ""
    company_phone = company.get("phone_number") or ""
    company_email = company.get("email") or ""
    dispatcher_name = (dispatcher or {}).get("full_name") or ""

    lines = [
        f"RATE: ${_fmt_money(bid_amount)}",
        "",
        f"DIMENSIONS: {vehicle_dimensions(vehicle)}",
        "",
        f"MILES OUT: {miles_out(driver, load)}",
        "",
        f"MC: {company_mc}",
        "",
        f"VEHICLE: {vehicle_type}",
        "",
        f"Truck equipment: {vehicle_equipment(vehicle)}",
        "",
        f"ALL BIDS ARE VALID {validity} MINUTES!",
        "",
        company_name,
        f"MC {company_mc}",
        f"Address: {company_address}",
        f"Phone: {company_phone}",
        company_email,
        "",
        dispatcher_name,
        f"✉: {company_email}",
        f"☎: {company_phone}",
    ]
    return "\n".join(line for line in lines if line is not None)


def build_html_body(
    *,
    bid_amount: float,
    load: dict,
    driver: Optional[dict],
    company: dict,
    dispatcher: Optional[dict],
) -> str:
    """Same content as build_body, with the company logo when one is set."""
    text = build_body(
        bid_amount=bid_amount,
        load=load,
        driver=driver,
        company=company,
        dispatcher=dispatcher,
    )
    logo_url = company.get("logo_url")
    logo_html = (
        f'<p><img src="{logo_url}" alt="{company.get("name", "")}" '
        f'style="max-height:80px;" /></p>'
        if logo_url
        else ""
    )
    escaped = (
        text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    )
    # The logo sits where "Logo" appears in the template: above the letterhead.
    marker = f"ALL BIDS ARE VALID {company.get('bid_validity_minutes') or 15} MINUTES!"
    head, _, tail = escaped.partition(marker)
    body_html = (
        f'<div style="font-family:Arial,Helvetica,sans-serif;font-size:14px;white-space:pre-wrap;">'
        f"{head}{marker}</div>{logo_html}"
        f'<div style="font-family:Arial,Helvetica,sans-serif;font-size:14px;white-space:pre-wrap;">{tail}</div>'
    )
    return body_html
