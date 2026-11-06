"""Name a GPS position after the nearest ZIP code this server has geocoded.

There is no reverse geocoding here. Every ZIP that Google geocoded for us is
kept in ZipLocation (see geocode_zip), and a position from the driver app takes
the nearest of those. That costs no API call, and the answers get better as
dispatchers type more ZIPs.
"""

import logging
import math
import re
from typing import Any

from .haversine import haversine
from .models import ZipLocation

logger = logging.getLogger(__name__)

ZIP_RE = re.compile(r"^\d{5}$")
# A known ZIP centre this close names the position outright. Further out the
# position is described as a distance from it, and past FAR_MILES only the
# coordinates are shown.
NEAR_MILES = 20.0
FAR_MILES = 100.0
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
    latitude: float, longitude: float, within_miles: float = FAR_MILES
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


def zip_label(place: ZipLocation) -> str:
    city_state = ", ".join(p for p in (place.city, place.state) if p)
    return f"{city_state} {place.zip_code}".strip()


def place_for_position(latitude: float, longitude: float) -> dict[str, str]:
    """The driver's current_* place fields for a GPS position. Never the mailing address."""
    found = nearest_zip(latitude, longitude)
    if found and found[1] <= NEAR_MILES:
        place = found[0]
        return {
            "current_zip": place.zip_code,
            "current_city": place.city,
            "current_state": place.state,
            "current_address": zip_label(place),
        }
    if found:
        place, miles = found
        description = f"{round(miles):.0f} mi from {zip_label(place)}"
    else:
        description = f"{latitude:.4f}, {longitude:.4f}"
    # A city or ZIP that far off would be wrong; the Tracking page shows the
    # description instead (it falls back to current_address).
    return {
        "current_zip": "",
        "current_city": "",
        "current_state": "",
        "current_address": description,
    }
