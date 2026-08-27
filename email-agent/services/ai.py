"""
Every model call returns structured JSON against an explicit schema. A
malformed response is retried once and then treated as a failure — the agent
never guesses at a number that decides whether a load gets booked.
"""
import json
import logging
from typing import Any, Optional

from openai import OpenAI

from config.settings import config

logger = logging.getLogger(__name__)

# The driver's own bid must never appear in broker-facing text.
BROKER_FACING_RULES = """
You are writing on behalf of a trucking carrier's dispatch team to a freight broker.
Rules you must never break:
- Never mention the driver's pay, the driver's own bid, or the carrier's margin.
- Never agree to a rate, cancel, or commit to anything the dispatcher has not stated.
- Keep it short, professional and plain — no marketing language, no emoji.
- Sign off as the dispatch team; do not invent names, phone numbers or addresses.
"""


class AIService:
    def __init__(self):
        self.client = OpenAI(
            api_key=config.OPENAI_API_KEY,
            timeout=config.OPENAI_TIMEOUT_SECONDS,
            max_retries=config.OPENAI_MAX_RETRIES,
        )
        self.model = config.OPENAI_MODEL

    def _json_call(
        self,
        system: str,
        user: str,
        schema: dict,
        schema_name: str,
        max_tokens: int = 1200,
    ) -> Optional[dict]:
        for attempt in (1, 2):
            try:
                response = self.client.chat.completions.create(
                    model=self.model,
                    max_tokens=max_tokens,
                    temperature=0,
                    response_format={
                        "type": "json_schema",
                        "json_schema": {
                            "name": schema_name,
                            "strict": True,
                            "schema": schema,
                        },
                    },
                    messages=[
                        {"role": "system", "content": system},
                        {"role": "user", "content": user},
                    ],
                )
                return json.loads(response.choices[0].message.content)
            except Exception as e:
                logger.error(f"AI call {schema_name} attempt {attempt} failed: {e}")
                if attempt == 2:
                    return None
        return None

    # --- inbound classification ----------------------------------------

    def classify_inbound(self, thread_text: str, message_text: str, attachments: list[str]) -> Optional[dict]:
        schema = {
            "type": "object",
            "properties": {
                "intent": {
                    "type": "string",
                    "enum": [
                        "counter_offer", "accept", "question",
                        "ratecon_promise", "ratecon_attached",
                        "rejection", "other",
                    ],
                },
                "contains_ratecon": {"type": "boolean"},
                "ratecon_attachment_name": {"type": ["string", "null"]},
                "quoted_amount": {"type": ["number", "null"]},
                "reasoning": {"type": "string"},
            },
            "required": [
                "intent", "contains_ratecon", "ratecon_attachment_name",
                "quoted_amount", "reasoning",
            ],
            "additionalProperties": False,
        }
        user = (
            f"THREAD SO FAR:\n{thread_text}\n\n"
            f"NEW MESSAGE FROM BROKER:\n{message_text}\n\n"
            f"PDF ATTACHMENTS ON THIS MESSAGE: {attachments or 'none'}"
        )
        return self._json_call(
            system=(
                "You classify freight broker emails for a carrier's dispatch team. "
                "contains_ratecon is true only when an attached PDF is a rate confirmation "
                "(also called rate con, carrier confirmation, or load confirmation) for this load. "
                "quoted_amount is any dollar rate the broker states in this message, else null."
            ),
            user=user,
            schema=schema,
            schema_name="inbound_classification",
        )

    # --- reply drafting -------------------------------------------------

    def draft_reply(self, context: str, thread_text: str, message_text: str) -> Optional[dict]:
        schema = {
            "type": "object",
            "properties": {
                "intent": {"type": "string"},
                "draft_subject": {"type": "string"},
                "draft_body": {"type": "string"},
                "reasoning": {"type": "string"},
            },
            "required": ["intent", "draft_subject", "draft_body", "reasoning"],
            "additionalProperties": False,
        }
        user = (
            f"NEGOTIATION CONTEXT:\n{context}\n\n"
            f"THREAD SO FAR:\n{thread_text}\n\n"
            f"BROKER'S LATEST MESSAGE (reply to this):\n{message_text}"
        )
        return self._json_call(
            system=(
                BROKER_FACING_RULES
                + "\nDraft a reply the dispatcher can send as-is. If the broker countered "
                "below our rate, hold our number and justify it briefly using the load facts. "
                "If the broker accepted, ask them to send the rate confirmation. "
                "reasoning explains your draft to the dispatcher, not to the broker."
            ),
            user=user,
            schema=schema,
            schema_name="reply_draft",
        )

    def draft_mismatch_reply(self, context: str, discrepancies: list[str]) -> Optional[dict]:
        schema = {
            "type": "object",
            "properties": {
                "draft_subject": {"type": "string"},
                "draft_body": {"type": "string"},
                "reasoning": {"type": "string"},
            },
            "required": ["draft_subject", "draft_body", "reasoning"],
            "additionalProperties": False,
        }
        bullet_list = "\n".join(f"- {d}" for d in discrepancies)
        return self._json_call(
            system=(
                BROKER_FACING_RULES
                + "\nThe broker sent a rate confirmation that does not match what was agreed. "
                "Write a short, polite message pointing out every discrepancy and asking for a "
                "corrected rate confirmation. State the agreed terms plainly. Do not accept the "
                "document and do not suggest we will run the load as written."
            ),
            user=f"NEGOTIATION CONTEXT:\n{context}\n\nDISCREPANCIES FOUND:\n{bullet_list}",
            schema=schema,
            schema_name="mismatch_reply",
        )

    # --- verification ---------------------------------------------------

    def extract_agreed_rate(self, thread_text: str, opening_bid: float) -> Optional[dict]:
        schema = {
            "type": "object",
            "properties": {
                "agreed_amount": {"type": ["number", "null"]},
                "confident": {"type": "boolean"},
                "evidence": {"type": "string"},
            },
            "required": ["agreed_amount", "confident", "evidence"],
            "additionalProperties": False,
        }
        return self._json_call(
            system=(
                "Read this carrier/broker email thread and determine the final agreed rate "
                "for the load, in US dollars. The carrier opened at the stated opening bid. "
                "Only report an amount both sides converged on. Set confident=false if the "
                "thread does not clearly settle on a number; then agreed_amount must be null. "
                "evidence quotes the sentence that settles it."
            ),
            user=f"CARRIER'S OPENING BID: ${opening_bid}\n\nTHREAD:\n{thread_text}",
            schema=schema,
            schema_name="agreed_rate",
        )

    def verify_ratecon(
        self,
        agreed_amount: float,
        load_summary: str,
        thread_text: str,
        parsed_ratecon: dict[str, Any],
    ) -> Optional[dict]:
        schema = {
            "type": "object",
            "properties": {
                "price_ok": {"type": "boolean"},
                "ratecon_amount": {"type": ["number", "null"]},
                "locations_ok": {"type": "boolean"},
                "dates_ok": {"type": "boolean"},
                "discrepancies": {"type": "array", "items": {"type": "string"}},
            },
            "required": [
                "price_ok", "ratecon_amount", "locations_ok",
                "dates_ok", "discrepancies",
            ],
            "additionalProperties": False,
        }
        user = (
            f"AGREED RATE: ${agreed_amount}\n\n"
            f"LOAD AS POSTED:\n{load_summary}\n\n"
            f"EMAIL THREAD (may contain agreed changes to stops or times):\n{thread_text}\n\n"
            f"PARSED RATE CONFIRMATION:\n{json.dumps(parsed_ratecon, indent=2, default=str)}"
        )
        return self._json_call(
            system=(
                "You verify a freight rate confirmation against what the carrier and broker agreed.\n"
                "price_ok: true only if the rate confirmation's total rate equals the agreed rate exactly.\n"
                "locations_ok: compare pickup and delivery city/state/zip against the posted load and any "
                "changes agreed in the thread. Formatting differences, abbreviations, added facility names, "
                "and a zip matching a named city are all fine. A different city, state or a stop that "
                "appears or disappears is not.\n"
                "dates_ok: compare pickup and delivery dates the same way. Time-window or format "
                "differences are fine; a different calendar day is not.\n"
                "discrepancies: one clear sentence per problem, written for a dispatcher, quoting both "
                "the agreed value and the rate confirmation's value. Empty when everything matches.\n"
                "Be strict about money and lenient about formatting."
            ),
            user=user,
            schema=schema,
            schema_name="ratecon_verification",
        )


ai = AIService()
