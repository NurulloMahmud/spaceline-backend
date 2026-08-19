from io import BytesIO
import threading
import time
import re
import pdfplumber
from pdf2image import convert_from_path
import pytesseract
import requests
from openai import OpenAI
from django.utils import timezone
import json
from datetime import date
from config import settings

client = OpenAI(api_key=settings.AI_ACCESS_TOKEN)

RATE_CONFIRMATION_SCHEMA = {
    "name": "extract_rate_confirmation_data",
    "parameters": {
        "title": "RateConfirmationData",
        "type": "object",
        "properties": {
            "broker_name":    {"type": "string"},
            "load_number":    {"type": "string"},
            "total_rate_usd": {"type": "number"},
            "pickup_addresses": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "facility":  {"type": "string"},
                        "city":      {"type": "string"},
                        "state":     {"type": "string"},
                        "zip_code":  {"type": "string"},
                        "address":   {"type": "string"},
                        "driver_instructions": {"type": "string"},
                        "date_time": {
                            "oneOf": [
                                {"type": "string"},
                                {"type": "array", "items": {"type": "string"}}
                            ]
                        }
                    },
                    "required": ["facility", "city", "state", "zip_code", "address"]
                }
            },
            "delivery_locations": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "facility":  {"type": "string"},
                        "city":      {"type": "string"},
                        "state":      {"type": "string"},
                        "zip_code":  {"type": "string"},
                        "address":   {"type": "string"},
                        "driver_instructions": {"type": "string"},
                        "date_time": {
                            "oneOf": [
                                {"type": "string"},
                                {"type": "array", "items": {"type": "string"}}
                            ]
                        }
                    },
                    "required": ["facility", "city", "state", "zip_code", "address"]
                }
            },
            "special_instructions": {"type": "string"}
        },
        "required": ["load_number", "total_rate_usd", "pickup_addresses", "delivery_locations"]
    }
}

def extract_text_from_pdf(pdf_path):
    text_pages = []
    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            text = page.extract_text()
            if not text:
                image = convert_from_path(pdf_path, first_page=page.page_number, last_page=page.page_number)[0]
                text = pytesseract.image_to_string(image)
            text_pages.append(text.strip())
    return "\n\n".join(text_pages)


def parse_rate_confirmation(file):
    file_bytes = BytesIO(file.read())
    file_bytes.name = file.name
    uploaded = client.files.create(
        file=file_bytes,
        purpose="assistants"
    )
    file_id = uploaded.id

    try:
        request_time = time.time()
        response = client.responses.create(
            model="gpt-4.1",
            temperature=0,
            input=[
            {
                "role": "system",
                "content": (
                    "You are a logistics document parser. Return only valid JSON that matches the schema. "
                    "For each pickup and delivery stop, extract the following:\n"
                    "- 'facility': the raw facility name\n"
                    "- 'city': the city name\n"
                    "- 'zip_code': the zip code\n"
                    "- 'address': the full address\n"
                    "- 'date_time': if there's one date/time, return as a string; if two or more, return as an array of strings; if date is available but doesnt have specific time, return time according to stop, if its pickup address then 00:00 if delivery address then 23:59\n"
                    "- 'driver_instructions': include ONLY driver instructions relevant to that specific stop, if it's longer thatn 500 characters, summarize it clear and concisely, if no instructions provided return an empty string.\n"
                    "If the date/time includes a timezone offset (e.g., '2025-02-02T20:00-07:00'), remove the timezone so it becomes '2025-02-02T20:00'.\n"
                    "Do not include seconds or timezone information. Do not reformat the date/time otherwise."
                    "Do not combine any fields. Do not include pickup_time or delivery_time separately — all times should be per stop using 'date_time'.\n"
                    "Include all pickup and delivery stops in the same order as the document. "
                    "If any field is missing, return null or an empty string."
                    "- Also extract 'special_instructions' as a field. If it's longer than 500 characters, summarize it clearly and concisely."
                ),
            },
            {"role": "user", 
             "content": [
                        {
                            "type": "input_file",
                            "file_id": file_id
                        }
                    ]},
            ],
            tools=[
                {
                    "type": "function",
                    "name": "extract_rate_confirmation_data",
                    "parameters": RATE_CONFIRMATION_SCHEMA["parameters"]
                }
            ],
            tool_choice={"type": "function", "name": "extract_rate_confirmation_data"}
        )
        response_time = time.time()
        print(f"[AI PARSER] Response received from AI in {response_time - request_time:.2f} seconds")
        for item in response.output:
            if item.type == "function_call" and item.name == "extract_rate_confirmation_data":
                return json.loads(item.arguments)
        raise Exception("No tool call returned")
    finally:
        client.files.delete(file_id)


def escape_markdown(text):
    escape_chars = r"_*\[\]()~`>#+-=|{}.!\\"
    return re.sub(f"([{re.escape(escape_chars)}])", r"\\\1", str(text))

STATE_TIMEZONE = {
    'CT': 'America/New_York', 'DE': 'America/New_York', 'FL': 'America/New_York',
    'GA': 'America/New_York', 'IN': 'America/New_York', 'KY': 'America/New_York',
    'ME': 'America/New_York', 'MD': 'America/New_York', 'MA': 'America/New_York',
    'MI': 'America/New_York', 'NH': 'America/New_York', 'NJ': 'America/New_York',
    'NY': 'America/New_York', 'NC': 'America/New_York', 'OH': 'America/New_York',
    'PA': 'America/New_York', 'RI': 'America/New_York', 'SC': 'America/New_York',
    'VT': 'America/New_York', 'VA': 'America/New_York', 'WV': 'America/New_York',
    'DC': 'America/New_York',
    'AL': 'America/Chicago', 'AR': 'America/Chicago', 'IL': 'America/Chicago',
    'IA': 'America/Chicago', 'KS': 'America/Chicago', 'LA': 'America/Chicago',
    'MN': 'America/Chicago', 'MS': 'America/Chicago', 'MO': 'America/Chicago',
    'NE': 'America/Chicago', 'ND': 'America/Chicago', 'OK': 'America/Chicago',
    'SD': 'America/Chicago', 'TN': 'America/Chicago', 'TX': 'America/Chicago',
    'WI': 'America/Chicago',
    'AZ': 'America/Phoenix', 'CO': 'America/Denver', 'ID': 'America/Denver',
    'MT': 'America/Denver', 'NM': 'America/Denver', 'UT': 'America/Denver',
    'WY': 'America/Denver',
    'CA': 'America/Los_Angeles', 'NV': 'America/Los_Angeles',
    'OR': 'America/Los_Angeles', 'WA': 'America/Los_Angeles',
    'AK': 'America/Anchorage',
    'HI': 'Pacific/Honolulu',
}

def optimized_sort_loads(loads):
        import pytz
        from django.utils import timezone
        from .utils import STATE_TIMEZONE
        from datetime import datetime
        if not loads:
            return []
        
        state_timezones = {}
        default_tz = pytz.timezone('America/Chicago')
        unique_states = set()
        for load in loads:
            first_stop = None
            if hasattr(load, 'needed_stops_cache') and load.needed_stops_cache:
                first_stop = load.needed_stops_cache[0] if load.needed_stops_cache else None
            
            if first_stop and first_stop.facility and first_stop.facility.state:
                state = first_stop.facility.state.upper()
                unique_states.add(state)
        
        for state in unique_states:
            tz_name = STATE_TIMEZONE.get(state, 'America/Chicago')
            state_timezones[state] = pytz.timezone(tz_name)
        status_priority_map = {
            'Dispatch': 1,
            'Billing': 2
        }
        
        sort_data = []
        for load in loads:
            if not load.pickup_date:
                local_pickup = timezone.make_aware(datetime.max, timezone.utc)
            else:
                state_code = None
                first_stop = None
                if hasattr(load, 'needed_stops_cache') and load.needed_stops_cache:
                    first_stop = load.needed_stops_cache[0] if load.needed_stops_cache else None
                
                if first_stop and first_stop.facility and first_stop.facility.state:
                    state_code = first_stop.facility.state.upper()
                
                tz = state_timezones.get(state_code, default_tz)
                if timezone.is_naive(load.pickup_date):
                    pickup_utc = timezone.make_aware(load.pickup_date, timezone.utc)
                else:
                    pickup_utc = load.pickup_date
                
                local_pickup = pickup_utc.astimezone(tz)
            
            status_priority = 3
            if load.status and load.status.name:
                status_priority = status_priority_map.get(load.status.name, 3)
            created_ts = -load.created_at.timestamp() if load.created_at else 0
            
            sort_data.append((local_pickup, status_priority, created_ts, load))
        sort_data.sort(key=lambda x: (x[0], x[1], x[2]))
        return [item[3] for item in sort_data]
    

def send_message(chat_id: str, message: str):
    telegram_url = f"https://api.telegram.org/bot{settings.TELEGRAM_WEB_APP_TOKEN}/sendMessage"

    requests.post(
        telegram_url,
        data={
            "chat_id": chat_id,
            "text": message,
            "parse_mode": "Markdown",
        },
        timeout=5
    )

def format_dt(dt):
    if not dt:
        return "N/A"
    return timezone.localtime(dt).strftime("%d/%m/%Y %H:%M")

def get_quarter_range(year, quarter):
    if quarter == 1:
        return date(year, 1, 1), date(year, 3, 31)
    if quarter == 2:
        return date(year, 4, 1), date(year, 6, 30)
    if quarter == 3:
        return date(year, 7, 1), date(year, 9, 30)
    if quarter == 4:
        return date(year, 10, 1), date(year, 12, 31)
 

def send_group_message(chat_id: str, text: str):
    telegram_url = f"https://api.telegram.org/bot{settings.TELEGRAM_WEB_APP_TOKEN}/sendMessage"
    def escape_markdown(text):
        escape_chars = r'[]()~`>#+-=|{}.!'
        return re.sub(f'([{re.escape(escape_chars)}])', r'\\\1', text)

    escaped_message = escape_markdown(text)
    try:
        resp = requests.post(
            telegram_url,
            data={
                "chat_id": chat_id,
                "text": escaped_message,
                "parse_mode": "MarkdownV2",
            },
            timeout=10
        )
        resp.raise_for_status()
        result = resp.json()
        if not result.get("ok"):
            print(f"[ERROR] Telegram API error: {result}")
        else:
            print(f"[DEBUG] Message sent to {chat_id}")
    except requests.exceptions.RequestException as e:
        print(f"[ERROR] Failed to send Telegram message: {e}")

def summarize_requirements(text):
    if not text:
        return "N/A"

    response = client.responses.create(
        model="gpt-4.1-mini",
        temperature=0,
        input=[
            {
                "role": "system",
                "content": (
                    "Summarize the following stop requirements into a numbered list.\n"
                    "Rules:\n"
                    "• Each rule must be one clear instruction\n"
                    "• Maximum 15-18 words per rule\n"
                    "• Use imperative style (MUST / DO NOT / USE / SIGN IN)\n"
                    "• Remove repetition and extra explanations\n"
                    "• Keep operational meaning unchanged\n"
                    "• Make it easy for dispatchers and drivers to understand"
                )
            },
            {
                "role": "user",
                "content": text
            }
        ]
    )
    return response.output_text

def clean_address(address, city, state, zipcode):
    if not address:
        return "N/A"

    prompt = (
        f"Clean the following address by removing city, state, and zipcode. "
        f"Return only the street address.\n\n"
        f"Raw Address: {address}\n"
        f"City: {city}, State: {state}, Zipcode: {zipcode}"
    )

    response = client.responses.create(
        model="gpt-4.1-mini",
        temperature=0,
        input=[
            {"role": "system", "content": "You are an assistant that cleans addresses."},
            {"role": "user", "content": prompt}
        ]
    )

    cleaned_address = response.output_text.strip()
    return cleaned_address if cleaned_address else address

_recalc_timers = {}
_recalc_lock = threading.Lock()

def debounce_recalculate_miles(load_id, delay=3):
    from .tasks import calculate_empty_miles_multi_background, calculate_loaded_miles_background

    with _recalc_lock:
        existing = _recalc_timers.get(load_id)
        if existing:
            existing.cancel()

        def fire():
            with _recalc_lock:
                _recalc_timers.pop(load_id, None)
            calculate_empty_miles_multi_background(load_id)
            calculate_loaded_miles_background(load_id)

        timer = threading.Timer(delay, fire)
        _recalc_timers[load_id] = timer
        timer.start()



def broker_missing_info(load):
    broker = load.broker
    if not broker:
        return 'load has no broker assigned'
    if not broker.mc:
        return 'broker is missing an MC number'
    if not broker.address:
        return 'broker is missing an address'
    return None

