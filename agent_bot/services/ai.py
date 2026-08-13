import json
import logging
from typing import Optional
from openai import OpenAI
from config.settings import config

logger = logging.getLogger(__name__)


class AIService:
    def __init__(self):
        self.client = OpenAI(api_key=config.OPENAI_API_KEY)

    def _chat(self, system: str, user: str, max_tokens: int = 300) -> str:
        response = self.client.chat.completions.create(
            model="gpt-4o-mini",
            max_tokens=max_tokens,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        )
        return response.choices[0].message.content.strip()

    def is_location_message(self, message: str) -> bool:
        result = self._chat(
            "Does this message contain a location (city, state, zip code)? Reply only: yes or no",
            message,
            max_tokens=5,
        )
        return result.lower() == "yes"

    def extract_location(self, message: str) -> Optional[dict]:
        result = self._chat(
            """Extract zip code, city, and state from this message.
JSON only, no explanation.
Format: {"zip": "75201", "city": "Dallas", "state": "TX"}
If not found: {"zip": null, "city": null, "state": null}""",
            message,
        )
        try:
            data = json.loads(result)
            return data if data.get("zip") or data.get("city") else None
        except Exception:
            return None

    def is_preference_message(self, message: str) -> bool:
        result = self._chat(
            "Does this message from a truck driver express work preferences "
            "(availability, max miles, local only, off today, etc.)? Reply only: yes or no",
            message,
            max_tokens=5,
        )
        return result.lower() == "yes"

    def extract_preferences(self, message: str) -> Optional[dict]:
        result = self._chat(
            """Extract driver work preferences from this trucking driver message.
                JSON only, no explanation.
                Format:
                {
                "available": true|false|null,
                "local_only": true|false|null,
                "max_miles": <number>|null,
                "scope": "today"|"week"|"until_further_notice"|null
                }
                Rules:
                - If driver mentions a mile limit like "max 300 miles", "no more than 300", "300 miles max" → max_miles=300
                - If driver says "local only" or "local loads" → local_only=true
                - If driver says "off today", "not available" → available=false
                - If driver says "available again", "ready to work" → available=true, local_only=false
                - If message mentions "today" → scope="today"
                - If message mentions "this week" → scope="week"
                - If no time scope mentioned but has a restriction → scope="today" (default)
                - If driver says "available again" with no time scope → scope="until_further_notice"
                Examples:
                "local only today max 100 miles" → {"available": true, "local_only": true, "max_miles": 100, "scope": "today"}
                "300 miles max" → {"available": true, "local_only": false, "max_miles": 300, "scope": "today"}
                "off today" → {"available": false, "local_only": null, "max_miles": null, "scope": "today"}
                "available again" → {"available": true, "local_only": false, "max_miles": null, "scope": "until_further_notice"}
                "no more than 200 miles this week" → {"available": true, "local_only": false, "max_miles": 200, "scope": "week"}
                If no preference info at all: null""",
                        message,
                    )
        try:
            if result.lower().strip() == "null":
                return None
            return json.loads(result)
        except Exception:
            return None

    def extract_dims_from_notes(self, notes: str) -> Optional[dict]:
        result = self._chat(
            """Extract shipment dimensions from freight broker notes.
JSON only, no explanation.
Format: {"dims": [L, W, H], "pieces": 1, "weight": 300}
All dims in inches, weight in lbs.
If pieces have individual dims, calculate total: pieces * each_dim for length.
If not found: {"dims": null, "pieces": null, "weight": null}""",
            notes,
        )
        try:
            data = json.loads(result)
            return data if data.get("dims") else None
        except Exception:
            return None

    def classify_offer_reply(self, message: str) -> Optional[dict]:
        """
        Read a driver's reply to a load offer.

        Only a stated price counts as a bid. Everything else — questions,
        chatter, tagging a colleague, a bare "ok" — is `other`, and the bot
        stays quiet rather than guessing. Returns None if the model fails,
        which the caller also treats as "do nothing".
        """
        try:
            response = self.client.chat.completions.create(
                model="gpt-4o-mini",
                temperature=0,
                max_tokens=200,
                response_format={
                    "type": "json_schema",
                    "json_schema": {
                        "name": "offer_reply",
                        "strict": True,
                        "schema": {
                            "type": "object",
                            "properties": {
                                "intent": {
                                    "type": "string",
                                    "enum": ["bid", "interested", "decline", "other"],
                                },
                                "amount": {"type": ["number", "null"]},
                            },
                            "required": ["intent", "amount"],
                            "additionalProperties": False,
                        },
                    },
                },
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "A truck driver replied to a load offer. Classify the reply.\n"
                            "intent=bid: the driver states a price they want for this load. "
                            "Set amount to that number in dollars.\n"
                            "intent=interested: the driver accepts or shows interest but "
                            "names no price (\"yes\", \"I'll take it\"). amount is null.\n"
                            "intent=decline: the driver clearly turns the load down.\n"
                            "intent=other: anything else — questions, comments, greetings, "
                            "mentions of other people, or text unrelated to the load. "
                            "When in doubt use other.\n"
                            "A bare number is a price. Never invent an amount."
                        ),
                    },
                    {"role": "user", "content": message},
                ],
            )
            data = json.loads(response.choices[0].message.content)
        except Exception as e:
            logger.error(f"classify_offer_reply failed: {e}")
            return None

        if data.get("intent") == "bid":
            amount = data.get("amount")
            if not isinstance(amount, (int, float)) or amount <= 0:
                # A bid without a usable price is not a bid.
                return {"intent": "interested", "amount": None}
        return data

    def extract_bid_intent(self, message: str) -> Optional[dict]:
        result = self._chat(
            """Extract driver bid intent from message.
JSON only, no explanation.
Format: {"intent": "accept|reject|counter", "amount": 1200.00}
intent = accept (agrees or proposes amount)
intent = reject (declines)
intent = counter (proposes different amount)
amount = dollar amount or null""",
            message,
        )
        try:
            return json.loads(result)
        except Exception:
            return None
        
    def is_load_inquiry(self, message: str) -> bool:
        result = self._chat(
            "Is this message from a truck driver asking about available loads, work, or freight? "
            "Reply only: yes or no",
            message,
            max_tokens=5,
        )
        return result.lower().strip() == "yes"

ai = AIService()
