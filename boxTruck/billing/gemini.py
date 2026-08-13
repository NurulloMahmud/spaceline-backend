import os
import time
import json
from io import BytesIO
from typing import List, Union, Optional
from pydantic import BaseModel
from google import genai
from google.genai import types

from config import settings

class Stop(BaseModel):
    facility: str
    city: str
    state: str
    zip_code: str
    address: str
    driver_instructions: str
    date_time: Union[str, List[str]]

class RateConfirmationData(BaseModel):
    broker_name: Optional[str] = None
    load_number: str
    total_rate_usd: float
    pickup_addresses: List[Stop]
    delivery_locations: List[Stop]
    special_instructions: Optional[str] = ""

client = genai.Client(api_key=settings.GEMINI_API_KEY)

def parse_rate_confirmation_gemini(file_content):
    file_name = file_content.name
    file_bytes = file_content.read()
    uploaded_file = client.files.upload(
        file=BytesIO(file_bytes),
        config={'mime_type': 'application/pdf', 'display_name': file_name}
    )

    while uploaded_file.state.name == "PROCESSING":
        time.sleep(1)
        uploaded_file = client.files.get(name=uploaded_file.name)

    try:
        request_time = time.time()
        system_instruction = (
            "You are a logistics document parser. Return only valid JSON that matches the schema. "
                    "For each pickup and delivery stop, extract the following:\n"
                    "- 'facility': the raw facility name\n"
                    "- 'city': the city name\n"
                    "- 'state': the state name, if its more than 2 characters, abbreviate the state\n"
                    "- 'zip_code': the zip code\n"
                    "- 'address': the full address\n"
                    "- 'date_time': if there's one date/time, return as a string; if two or more, return as an array of strings.\n"
                    "- 'driver_instructions': include ONLY driver instructions relevant to that specific stop, if it's longer thatn 500 characters, summarize it clear and concisely, if no instructions provided return an empty string.\n"
                    "If the date/time includes a timezone offset (e.g., '2025-02-02T20:00-07:00'), remove the timezone so it becomes '2025-02-02T20:00'.\n"
                    "if the date/time is in format of '12/20/2025 0900', convert it to '2025-12-20 09:00'.\n"
                    "Do not include seconds or timezone information. Do not reformat the date/time otherwise."
                    "Do not combine any fields. Do not include pickup_time or delivery_time separately — all times should be per stop using 'date_time'.\n"
                    "Include all pickup and delivery stops in the same order as the document. "
                    "If any field is missing, return null or an empty string."
                    "- Also extract 'special_instructions' as a field. If it's longer than 500 characters, summarize it clearly and concisely."
        )

        response = client.models.generate_content(
            model="gemini-3-flash-preview",
            contents=[uploaded_file, "Extract the rate confirmation data from this file."],
            config=types.GenerateContentConfig(
                system_instruction=system_instruction,
                response_mime_type="application/json",
                response_schema=RateConfirmationData,
                temperature=0,
            ),
        )

        response_time = time.time()
        print(f"[GEMINI PARSER] Processed {file_name} in {response_time - request_time:.2f} seconds")
        return response.parsed.model_dump()

    except Exception as e:
        print(f"Error parsing file {file_name}: {e}")
        return None

    finally:
        client.files.delete(name=uploaded_file.name)
