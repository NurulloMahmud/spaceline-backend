"""Name a driver-app GPS position as "City, ST ZIP".

The exact coordinates are always kept on the driver; these helpers only give
the place the web shows. Google reverse geocoding names the position. When
Google cannot answer, the nearest ZIP this server has geocoded (kept in
ZipLocation, see geocode_zip) names it if one is close; otherwise the place is
left blank rather than showing raw coordinates.
"""

import logging
import math
import re
from typing import Any

from .google_maps import reverse_geocode
from .haversine import haversine
from .models import ZipLocation

logger = logging.getLogger(__name__)

ZIP_RE = re.compile(r"^\d{5}$")
# Without Google, a known ZIP centre this close names the position. Further
# out a city or ZIP would be wrong, so the place stays blank.
NEAR_MILES = 20.0
MILES_PER_DEGREE = 69.0


def remember_zip(zip_code: Any, geo: dict[str, Any]) -> None:
    """Keep a geocoded ZIP. Only a plain 5-digit ZIP that Google answered with the same ZIP."""
    code = str(zip_code or "").strip()
    if not ZIP_RE.match(code) or geo.get("zip") != code:
        return
    try:
        latitude, longitude = float(geo["lat"]), float(geo["lng"])
    except (KeyError, TypeError, ValueError):
        return
    try:
        ZipLocation.objects.update_or_create(
            zip_code=code,
            defaults={
                "city": str(geo.get("city") or "")[:255],
                "state": str(geo.get("state") or "")[:55],
                "latitude": latitude,
                "longitude": longitude,
            },
        )
    except Exception:
        # Naming app positions is a nicety; it must never fail a ZIP update.
        logger.exception("ZIP %s could not be kept", code)


def nearest_zip(
    latitude: float, longitude: float, within_miles: float = NEAR_MILES
) -> tuple[ZipLocation, float] | None:
    """The closest known ZIP centre and its distance in miles, or None if none is that close."""
    dlat = within_miles / MILES_PER_DEGREE
    dlng = within_miles / (
        MILES_PER_DEGREE * max(math.cos(math.radians(latitude)), 0.01)
    )
    candidates = ZipLocation.objects.filter(
        latitude__range=(latitude - dlat, latitude + dlat),
        longitude__range=(longitude - dlng, longitude + dlng),
    )
    best: tuple[ZipLocation, float] | None = None
    for place in candidates:
        miles = haversine(latitude, longitude, place.latitude, place.longitude)
        if miles <= within_miles and (best is None or miles < best[1]):
            best = (place, miles)
    return best


def place_label(city: str, state: str, zip_code: str) -> str:
    """Like "Columbus, OH 43215", with whatever parts are known."""
    city_state = ", ".join(p for p in (city, state) if p)
    return f"{city_state} {zip_code}".strip()


def _place_fields(city: str, state: str, zip_code: str) -> dict[str, str]:
    return {
        "current_zip": zip_code[:255],
        "current_city": city[:255],
        "current_state": state[:255],
        "current_address": place_label(city, state, zip_code)[:255],
    }


def place_for_position(latitude: float, longitude: float) -> dict[str, str]:
    """The driver's current_* place fields for a GPS position. Never the mailing address."""
    geo = reverse_geocode(latitude, longitude)
    if geo:
        return _place_fields(geo["city"], geo["state"], geo["zip"])
    found = nearest_zip(latitude, longitude)
    if found:
        place = found[0]
        return _place_fields(place.city, place.state, place.zip_code)
    # Google is unreachable and no known ZIP is close: no place rather than
    # raw coordinates. The next position push, or name_app_positions, tries again.
    return _place_fields("", "", "")
