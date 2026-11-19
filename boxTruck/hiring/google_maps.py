import logging
from typing import Any

import requests
from config import settings

logger = logging.getLogger(__name__)

GEOCODE_URL = "https://maps.googleapis.com/maps/api/geocode/json"
# Where the city name comes from when Google has no locality for the spot
# (rural areas, some towns), best first.
CITY_TYPES = (
    "locality",
    "postal_town",
    "sublocality_level_1",
    "sublocality",
    "administrative_area_level_3",
    "neighborhood",
)


def geocode_zip(zip_code: str) -> dict[str, Any] | None:
    try:
        response = requests.get(
            GEOCODE_URL,
            params={
                "address": zip_code,
                "key": settings.GOOGLE_MAPS_API_KEY,
            },
            timeout=5,
        )
        data = response.json()
        if data["status"] != "OK" or not data["results"]:
            return None

        result = data["results"][0]
        location = result["geometry"]["location"]
        components = result["address_components"]

        city = state = address = postal_code = ""
        for component in components:
            types = component["types"]
            if "locality" in types:
                city = component["long_name"]
            elif "administrative_area_level_1" in types:
                state = component["short_name"]
            elif "route" in types:
                address = component["long_name"]
            elif "postal_code" in types:
                postal_code = component["long_name"][:5]

        geo = {
            "lat": location["lat"],
            "lng": location["lng"],
            "city": city,
            "state": state,
            "zip": postal_code,
            "address": result.get("formatted_address", address),
        }
    except Exception:
        return None
    # Kept so a driver-app GPS position can later be named after this ZIP.
    from .places import remember_zip

    remember_zip(zip_code, geo)
    return geo


def reverse_geocode(latitude: float, longitude: float) -> dict[str, str] | None:
    """City, state code and ZIP of a GPS position, or None when Google cannot name it."""
    try:
        response = requests.get(
            GEOCODE_URL,
            params={
                "latlng": f"{latitude:.6f},{longitude:.6f}",
                "language": "en",
                "key": settings.GOOGLE_MAPS_API_KEY,
            },
            timeout=5,
        )
        data = response.json()
    except Exception as exc:
        logger.warning("Reverse geocode of %s,%s failed: %s", latitude, longitude, exc)
        return None
    status = data.get("status") if isinstance(data, dict) else None
    if status != "OK":
        if status != "ZERO_RESULTS":
            logger.warning(
                "Reverse geocode of %s,%s: %s %s",
                latitude,
                longitude,
                status,
                (data or {}).get("error_message", "") if isinstance(data, dict) else "",
            )
        return None

    # Results run from the street address out to the country. Take each part
    # from the most precise result that has it.
    found: dict[str, str] = {}
    for result in data.get("results") or []:
        for component in result.get("address_components") or []:
            types = component.get("types") or []
            for kind in (*CITY_TYPES, "administrative_area_level_1", "postal_code"):
                if kind in types and kind not in found:
                    name = (
                        component.get("short_name")
                        if kind == "administrative_area_level_1"
                        else component.get("long_name")
                    )
                    if name:
                        found[kind] = str(name).strip()
    city = next((found[k] for k in CITY_TYPES if found.get(k)), "")
    state = found.get("administrative_area_level_1", "")
    zip_code = found.get("postal_code", "")
    if not city and not zip_code:
        return None
    return {"city": city, "state": state, "zip": zip_code}
