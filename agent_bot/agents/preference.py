import logging
from datetime import datetime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo
from services.ai import ai
from database.repository import DriverPreferenceRepo

logger = logging.getLogger(__name__)
CHICAGO_TZ = ZoneInfo("America/Chicago")


def calculate_expiry(scope: Optional[str]) -> Optional[datetime]:
    now = datetime.now(CHICAGO_TZ)
    if scope is None or scope == "today":
        midnight = now.replace(hour=23, minute=59, second=59, microsecond=0)
        return midnight.astimezone(timezone.utc)

    if scope == "week":
        days_until_sunday = (6 - now.weekday()) % 7 or 7
        sunday = now + timedelta(days=days_until_sunday)
        sunday_midnight = sunday.replace(hour=23, minute=59, second=59, microsecond=0)
        return sunday_midnight.astimezone(timezone.utc)

    if scope == "until_further_notice":
        return None

    midnight = now.replace(hour=23, minute=59, second=59, microsecond=0)
    return midnight.astimezone(timezone.utc)

class PreferenceAgent:

    def __init__(self, pref_repo: DriverPreferenceRepo):
        self.repo = pref_repo

    async def handle(self, message: str, driver: dict, chat_id: str) -> Optional[str]:
        if not ai.is_preference_message(message):
            return None

        prefs = ai.extract_preferences(message)
        if not prefs:
            return None

        driver_id = driver["id"]
        scope     = prefs.get("scope")
        expires   = calculate_expiry(scope)

        update_fields = {
            "full_name":          driver.get("full_name"),
            "preference_note":    message,
            "preference_expires": expires,
        }

        if prefs.get("available") is not None:
            update_fields["available"] = prefs["available"]

        if prefs.get("local_only") is not None:
            update_fields["local_only"] = prefs["local_only"]

        if prefs.get("max_miles") is not None:
            update_fields["max_miles"] = prefs["max_miles"]

        if prefs.get("available") is True and prefs.get("local_only") is False:
            update_fields["local_only"]         = False
            update_fields["max_miles"]          = None
            update_fields["preference_expires"] = None

        self.repo.upsert(driver_id, chat_id, **update_fields)

        parts = []
        if prefs.get("available") is False:
            parts.append("marked as unavailable")
        elif prefs.get("available") is True:
            parts.append("marked as available")
        if prefs.get("local_only"):
            parts.append("local loads only")
        if prefs.get("max_miles"):
            parts.append(f"max {prefs['max_miles']} miles")
        if expires:
            parts.append("resets at midnight")

        summary = ", ".join(parts) or "preferences updated"
        logger.info(f"Driver {driver_id} preferences: {summary}")
        return f"✅ Got it! {summary.capitalize()}."
        