"""
The bid email sent to a broker. This is a fixed template, not AI-generated -
the numbers in it are the offer, so they are rendered verbatim from the
negotiation and never paraphrased by a model.

The dispatcher sees this email in the load window before sending it, and that
window normally posts its own HTML (`body_html` on the create request). This
template is for callers that post none, so it has to read the same: its markup
is copied from `html` in src/pages/loadBoardV2/Workspace.jsx in the frontend.
Change the two together.

Placeholders come from three places:
  All in          the dispatcher's bid amount
  Miles out, ETA  deadhead from the driver's current position to pickup
  Dimensions      the driver's vehicle cargo box (what we can carry)
  Type            the driver's vehicle type
  letterhead      the company profile in the TMS
  signature       the dispatcher who placed the bid
"""
import re
from html import escape
from html.parser import HTMLParser
from typing import Optional

from services.geo import haversine


def _esc(value) -> str:
    return escape(str(value if value is not None else ""), quote=True)


def _fmt_money(amount: float) -> str:
    return f"{amount:,.2f}".rstrip("0").rstrip(".")


def format_phone(value) -> str:
    """"6305551234" -> "+1 (630) 555-1234"; anything that is not a US number stays as stored."""
    digits = re.sub(r"\D+", "", str(value or ""))
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    digits = digits[:10]
    if len(digits) == 10:
        return f"+1 ({digits[:3]}) {digits[3:6]}-{digits[6:]}"
    return str(value or "").strip()


def vehicle_dimensions(vehicle: Optional[dict]) -> str:
    """Cargo box as L x W x H in inches; blank unless all three are known."""
    if not vehicle:
        return ""
    try:
        box = [float(vehicle.get(k) or 0) for k in ("length", "width", "height")]
    except (TypeError, ValueError):
        return ""
    if not all(n > 0 for n in box):
        return ""
    return " x ".join(f"{n:g}" for n in box) + " in"


def vehicle_type(driver: Optional[dict]) -> str:
    """"LARGE STRAIGHT" -> "Large straight"."""
    vehicle = (driver or {}).get("vehicle") or {}
    return str(vehicle.get("vehicle_type") or (driver or {}).get("vehicle_type") or "").capitalize()


def miles_out(driver: Optional[dict], load: dict) -> str:
    """Deadhead from where the driver is now to the pickup; blank when unknown."""
    if not driver:
        return ""
    d_lat = driver.get("current_latitude")
    d_lng = driver.get("current_longitude")
    p_lat = load.get("pick_up_latitude")
    p_lng = load.get("pick_up_longitude")
    if not all([d_lat, d_lng, p_lat, p_lng]):
        return ""
    try:
        return f"{haversine(float(d_lat), float(d_lng), float(p_lat), float(p_lng)):.0f}"
    except (TypeError, ValueError):
        return ""


def eta_text(miles: str) -> str:
    """Driving time for the deadhead: "9mins", "1h 25mins", "2h"."""
    if not miles:
        return ""
    total = max(5, round(float(miles) * 0.92 + 5))
    hours, mins = divmod(total, 60)
    if not hours:
        return f"{mins}mins"
    return f"{hours}h {mins}mins" if mins else f"{hours}h"


# The reference a broker recognises, in the order we prefer it. `order_number`
# is the broker's own number for the load and is the only one of these that
# means anything on their side — it is what the bid board shows as
# "Reference#". `id` is atrek's internal row id: always present, always
# plausible-looking, and meaningless to the broker, so it sits last and only
# runs when a feed gives us nothing better.
REFERENCE_FIELDS = ("order_number", "load_id", "reference_number", "id")


def load_reference(load: dict) -> str:
    for field in REFERENCE_FIELDS:
        value = load.get(field)
        if value not in (None, "", 0):
            return str(value).strip()
    return ""


def build_subject(load: dict) -> str:
    """For a bid with no posting email to reply to; otherwise it is "Re: <posting subject>"."""
    pickup = load.get("pick_up_at") or "Pickup"
    delivery = load.get("deliver_to") or "Delivery"
    ref = load_reference(load)
    route = f"{pickup} \u2192 {delivery}"
    return f"Ref {ref} \u00b7 {route}" if ref else route


def _row(label: str, value, green: bool = False) -> str:
    if value in (None, ""):
        return ""
    style = "color: #15803d; font-weight: 700" if green else ""
    return f'<p style="margin: 0 0 10px"><b>{label}:</b> <span style="{style}">{_esc(value)}</span></p>'


def build_html_body(
    *,
    bid_amount: float,
    load: dict,
    driver: Optional[dict],
    company: dict,
    dispatcher: Optional[dict],
) -> str:
    vehicle = (driver or {}).get("vehicle")
    out = miles_out(driver, load)
    mc = company.get("mc") or ""
    offer = (
        _row("All in", f"${_fmt_money(bid_amount)}", True)
        + _row("Miles out", out)
        + _row("ETA", eta_text(out))
        + _row("Dimensions", vehicle_dimensions(vehicle))
        + _row("Type", vehicle_type(driver))
        + _row("MC", mc)
    )
    validity = company.get("bid_validity_minutes") or 15
    name = company.get("name") or ""
    phone = format_phone(company.get("phone_number"))
    company_email = company.get("email") or ""
    username = (dispatcher or {}).get("username") or ""
    dispatcher_name = (dispatcher or {}).get("full_name") or username
    dispatcher_email = username if re.search(r"@.+\.", username) else company_email
    logo_url = company.get("logo_url")
    logo = (
        f'<img src="{_esc(logo_url)}" alt="{_esc(name)}" width="110" height="110" '
        f'style="display: block; width: 110px; height: 110px; object-fit: contain; margin: 0 0 10px">'
        if logo_url
        else ""
    )
    return f"""<div style="font-family: Arial, Helvetica, sans-serif; font-size: 14px; color: #1f2328; line-height: 1.45">
<div style="display: inline-block; min-width: 240px; padding: 14px 18px 4px; border: 2px dashed #86c99b; border-radius: 10px; margin-bottom: 14px">{offer}</div>
<p style="margin: 0 0 14px; color: #c0262d; font-weight: 700">ALL BIDS ARE VALID {_esc(validity)} MINS</p>
<hr style="border: 0; border-top: 1px solid #e3e3df; margin: 0 0 14px">
{logo}
<p style="margin: 0 0 2px; font-weight: 700; font-size: 15px">{_esc(name)}</p>
<p style="margin: 0 0 8px; color: #555a64">MC: {_esc(mc)}</p>
<p style="margin: 0; color: #555a64">{_esc(company.get("address") or "")}<br>{_esc(phone)}<br><a href="mailto:{_esc(company_email)}" style="color: #15803d">{_esc(company_email)}</a></p>
<hr style="border: 0; border-top: 1px solid #e3e3df; margin: 14px 0">
<p style="margin: 0 0 2px; font-weight: 700">{_esc(dispatcher_name)}</p>
<p style="margin: 0; color: #555a64">Dispatcher<br><a href="mailto:{_esc(dispatcher_email)}" style="color: #15803d">{_esc(dispatcher_email)}</a></p>
</div>"""


class _TextExtractor(HTMLParser):
    BLOCKS = {"p", "div", "ul", "ol", "li", "table", "tr", "h1", "h2", "h3", "h4", "h5", "h6"}
    HIDDEN = {"style", "script", "head", "title"}

    def __init__(self):
        super().__init__()
        self.text = ""
        self.hidden = 0

    def _break(self, lines: int) -> None:
        have = len(self.text) - len(self.text.rstrip("\n"))
        self.text += "\n" * max(0, lines - have)

    def handle_starttag(self, tag, attrs):
        if tag in self.HIDDEN:
            self.hidden += 1
        elif tag == "br":
            self.text += "\n"
        elif tag == "hr":
            self._break(2)
        elif tag in self.BLOCKS:
            self._break(1)

    def handle_endtag(self, tag):
        if tag in self.HIDDEN:
            self.hidden = max(0, self.hidden - 1)
        elif tag in self.BLOCKS:
            self._break(1)

    def handle_data(self, data):
        if self.hidden:
            return
        chunk = re.sub(r"\s+", " ", data)
        if chunk == " " and (not self.text or self.text.endswith("\n")):
            return
        self.text += chunk


def html_to_text(html: str) -> str:
    """The readable copy of a sent email, for the thread view and the AI's context."""
    parser = _TextExtractor()
    parser.feed(html or "")
    parser.close()
    lines = [line.strip() for line in parser.text.split("\n")]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def build_body(
    *,
    bid_amount: float,
    load: dict,
    driver: Optional[dict],
    company: dict,
    dispatcher: Optional[dict],
) -> str:
    """The plain-text copy of build_html_body."""
    return html_to_text(
        build_html_body(
            bid_amount=bid_amount,
            load=load,
            driver=driver,
            company=company,
            dispatcher=dispatcher,
        )
    )
