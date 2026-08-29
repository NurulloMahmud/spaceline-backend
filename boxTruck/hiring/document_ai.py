"""AI extraction of hiring documents (CDL, registration, COI, W-9, ...).

The driver-onboarding form asks for the same handful of facts that are
already printed on the documents the driver has to upload anyway. This module
hands one uploaded file to OpenAI and gets those facts back as structured
JSON, so the frontend can prefill the form instead of making someone retype a
license by hand.

Nothing here touches the database. Extraction is a suggestion: the caller
returns it to the frontend, a human confirms it, and the existing
create/update endpoints are what actually persist anything.

The one flat `ExtractedDocument` schema covers every supported document type,
with every field optional — a structured-output model fills a flat schema far
more reliably than it picks a branch out of a union, and one shape for all
document types is also what makes merging a batch of them straightforward.
Field names are deliberately the *model* field names (`cdl_expiration`, not
`expiry`), so the frontend can merge a section into its form state directly.
"""
import base64
import io
import logging
import mimetypes
import re
import threading
from datetime import date
from enum import Enum
from typing import Optional

from dateutil import parser as date_parser
from django.conf import settings
from pydantic import BaseModel

logger = logging.getLogger(__name__)

# Same OpenAI account the rest of the project runs on (ai/services.py). One
# constant to bump if the account moves to a newer vision model.
MODEL_NAME = getattr(settings, 'DOCUMENT_AI_MODEL', 'gpt-4o')
REQUEST_TIMEOUT_SECONDS = 60

# What the model accepts directly; anything else is rejected before we spend a
# call. HEIC is not in this set — iPhone photos are converted to JPEG first,
# see `_prepare_upload`.
SUPPORTED_MIME_TYPES = {
    'application/pdf',
    'image/jpeg',
    'image/png',
    'image/webp',
    'image/gif',
}

# Accepted from the browser, converted before it is sent. iPhones hand over
# HEIC often enough that refusing it would just mean drivers' documents
# bouncing for a reason they cannot act on.
CONVERTED_MIME_TYPES = {'image/heic', 'image/heif'}

ACCEPTED_MIME_TYPES = SUPPORTED_MIME_TYPES | CONVERTED_MIME_TYPES

MAX_FILE_BYTES = 15 * 1024 * 1024
MAX_FILES_PER_REQUEST = 10


class DocumentType(str, Enum):
    CDL = 'cdl'
    VEHICLE_REGISTRATION = 'vehicle_registration'
    INSURANCE_COI = 'insurance_coi'
    MC_AUTHORITY = 'mc_authority'
    W9 = 'w9'
    VOIDED_CHECK = 'voided_check'
    MEDICAL_CARD = 'medical_card'
    UNKNOWN = 'unknown'


DOCUMENT_TYPE_LABELS = {
    DocumentType.CDL: "Driver's License / CDL",
    DocumentType.VEHICLE_REGISTRATION: 'Vehicle Registration',
    DocumentType.INSURANCE_COI: 'Certificate of Insurance',
    DocumentType.MC_AUTHORITY: 'MC / USDOT Authority',
    DocumentType.W9: 'W-9',
    DocumentType.VOIDED_CHECK: 'Voided Check / Bank Letter',
    DocumentType.MEDICAL_CARD: 'Medical Examiner Certificate',
    DocumentType.UNKNOWN: 'Unrecognized Document',
}


class ExtractedDocument(BaseModel):
    """Every field any supported document can carry. All optional — the model
    fills only what is actually printed on the page it was given."""

    document_type: DocumentType

    # --- Driver ---
    full_name: Optional[str] = None
    dob: Optional[str] = None
    address: Optional[str] = None
    city: Optional[str] = None
    state: Optional[str] = None
    zip_code: Optional[str] = None
    phone_number: Optional[str] = None
    email: Optional[str] = None
    ssn: Optional[str] = None
    cdl_number: Optional[str] = None
    cdl_state: Optional[str] = None
    cdl_class: Optional[str] = None
    cdl_issue_date: Optional[str] = None
    cdl_expiration: Optional[str] = None
    cdl_endorsement: Optional[str] = None
    medical_exp_date: Optional[str] = None

    # --- The contractor's own company ---
    company_name: Optional[str] = None
    company_business_as: Optional[str] = None
    company_mc: Optional[str] = None
    company_employer_id: Optional[str] = None
    company_business_type: Optional[str] = None
    company_address: Optional[str] = None
    company_city: Optional[str] = None
    company_state: Optional[str] = None
    company_zipcode: Optional[str] = None
    company_phone_number: Optional[str] = None
    company_email: Optional[str] = None

    # --- Vehicle ---
    vin: Optional[str] = None
    make: Optional[str] = None
    model: Optional[str] = None
    year: Optional[int] = None
    registration_plate: Optional[str] = None
    registration_state: Optional[str] = None
    registration_expiry_date: Optional[str] = None
    gvw: Optional[int] = None
    payload: Optional[int] = None
    insurance_company: Optional[str] = None
    policy_number: Optional[str] = None
    insurance_expiry_date: Optional[str] = None

    # --- Deposit ---
    bank_name: Optional[str] = None
    routing_number: Optional[str] = None
    account_number: Optional[str] = None


# Which section of the onboarding form each extracted field belongs to, and
# what it is called there. Sections match the four records the invite flow
# creates: Driver, DriverCompany, Vehicle, Deposit.
FIELD_TARGETS = {
    'full_name': ('driver', 'full_name'),
    'dob': ('driver', 'dob'),
    'address': ('driver', 'address'),
    'city': ('driver', 'city'),
    'state': ('driver', 'state'),
    'zip_code': ('driver', 'zip_code'),
    'phone_number': ('driver', 'phone_number'),
    'email': ('driver', 'email'),
    'ssn': ('driver', 'ssn'),
    'cdl_number': ('driver', 'cdl_number'),
    'cdl_state': ('driver', 'cdl_state'),
    'cdl_class': ('driver', 'cdl_class'),
    'cdl_issue_date': ('driver', 'cdl_issue_date'),
    'cdl_expiration': ('driver', 'cdl_expiration'),
    'cdl_endorsement': ('driver', 'cdl_endorsement'),
    'medical_exp_date': ('driver', 'medical_exp_date'),
    'company_name': ('company', 'name'),
    'company_business_as': ('company', 'business_as'),
    'company_mc': ('company', 'mc'),
    'company_employer_id': ('company', 'employer_id'),
    'company_business_type': ('company', 'business_type'),
    'company_address': ('company', 'address'),
    'company_city': ('company', 'city'),
    'company_state': ('company', 'state'),
    'company_zipcode': ('company', 'zipcode'),
    'company_phone_number': ('company', 'phone_number'),
    'company_email': ('company', 'email'),
    'vin': ('vehicle', 'vin'),
    'make': ('vehicle', 'make'),
    'model': ('vehicle', 'model'),
    'year': ('vehicle', 'year'),
    'registration_plate': ('vehicle', 'registration_plate'),
    'registration_state': ('vehicle', 'registration_state'),
    'registration_expiry_date': ('vehicle', 'registration_expiry_date'),
    'gvw': ('vehicle', 'gvw'),
    'payload': ('vehicle', 'payload'),
    'insurance_company': ('vehicle', 'insurance_company'),
    'policy_number': ('vehicle', 'policy_number'),
    'insurance_expiry_date': ('vehicle', 'insurance_expiry_date'),
    'bank_name': ('deposit', 'bank_name'),
    'routing_number': ('deposit', 'routing_number'),
    'account_number': ('deposit', 'account_number'),
}

# Never prefilled silently. The frontend must make the user confirm each of
# these against the document before it submits them.
SENSITIVE_FIELDS = {'ssn', 'routing_number', 'account_number'}

DATE_FIELDS = {
    'dob', 'cdl_issue_date', 'cdl_expiration', 'medical_exp_date',
    'registration_expiry_date', 'insurance_expiry_date',
}

# Fields that being expired makes the document itself useless for onboarding.
EXPIRY_FIELDS = {
    'cdl_expiration', 'medical_exp_date', 'registration_expiry_date',
    'insurance_expiry_date',
}

STATE_FIELDS = {'state', 'cdl_state', 'company_state', 'registration_state'}

DIGITS_ONLY_FIELDS = {'routing_number', 'account_number', 'company_mc'}

US_STATES = {
    'alabama': 'AL', 'alaska': 'AK', 'arizona': 'AZ', 'arkansas': 'AR',
    'california': 'CA', 'colorado': 'CO', 'connecticut': 'CT', 'delaware': 'DE',
    'district of columbia': 'DC', 'florida': 'FL', 'georgia': 'GA', 'hawaii': 'HI',
    'idaho': 'ID', 'illinois': 'IL', 'indiana': 'IN', 'iowa': 'IA',
    'kansas': 'KS', 'kentucky': 'KY', 'louisiana': 'LA', 'maine': 'ME',
    'maryland': 'MD', 'massachusetts': 'MA', 'michigan': 'MI', 'minnesota': 'MN',
    'mississippi': 'MS', 'missouri': 'MO', 'montana': 'MT', 'nebraska': 'NE',
    'nevada': 'NV', 'new hampshire': 'NH', 'new jersey': 'NJ', 'new mexico': 'NM',
    'new york': 'NY', 'north carolina': 'NC', 'north dakota': 'ND', 'ohio': 'OH',
    'oklahoma': 'OK', 'oregon': 'OR', 'pennsylvania': 'PA', 'puerto rico': 'PR',
    'rhode island': 'RI', 'south carolina': 'SC', 'south dakota': 'SD',
    'tennessee': 'TN', 'texas': 'TX', 'utah': 'UT', 'vermont': 'VT',
    'virginia': 'VA', 'washington': 'WA', 'west virginia': 'WV',
    'wisconsin': 'WI', 'wyoming': 'WY',
}

SYSTEM_INSTRUCTION = """\
You are a document parser for a trucking company's driver-onboarding system.
You are given one scanned or photographed document and must return JSON
matching the provided schema.

First identify what the document is and set `document_type` to one of:
- "cdl" — a driver's license or commercial driver's license
- "vehicle_registration" — a state vehicle registration card/certificate
- "insurance_coi" — a certificate of insurance / auto liability or cargo policy
- "mc_authority" — an FMCSA operating authority letter, MC/USDOT certificate
- "w9" — IRS Form W-9
- "voided_check" — a voided check or a bank account verification letter
- "medical_card" — a DOT medical examiner's certificate
- "unknown" — anything else, or unreadable

Then fill ONLY the fields that are actually printed on this document. Leave
every other field null. Never guess, never infer a value from context, and
never carry a value over from a different field because it looks similar.
If a value is present but unreadable, leave it null.

Field rules:
- Dates: return exactly as printed; do not reformat or reorder them.
- `full_name`: the person's name as printed, first name first.
- `state` fields: the U.S. state; two-letter abbreviation is fine.
- `company_*` fields describe the CONTRACTOR'S OWN business (the carrier,
  the entity on the W-9 or authority letter), never the insurer, the bank,
  the issuing state agency, or the DMV.
- `insurance_company` is the insurer's name; `policy_number` is the policy
  or certificate number.
- `company_business_type` is the W-9 federal tax classification, returned as
  a digit string: "1" individual/sole proprietor, "2" C corporation,
  "3" S corporation, "4" partnership, "5" trust/estate, "6" LLC, "7" other.
- `gvw` and `payload` are pounds, digits only.
- `routing_number` is the 9-digit ABA number, `account_number` the account
  number — from the MICR line of a check, not the check number.
- `cdl_endorsement` is the endorsement codes as printed (e.g. "H, N"), and
  `cdl_class` the license class letter (e.g. "A", "B", "C").
"""


_client_lock = threading.Lock()
_openai_client = None


def _client():
    """One lazily-built OpenAI client, reused for the life of the process.

    Lazy so a missing API key surfaces on the request that needs it rather
    than breaking startup for the whole app. Cached because the client owns an
    HTTP connection pool that it closes when it is garbage collected — a fresh
    client per call can be collected out from under the request that created
    it, and rebuilding the pool per document would throw away connection reuse
    across a batch anyway.
    """
    global _openai_client

    if _openai_client is None:
        with _client_lock:
            if _openai_client is None:
                from openai import OpenAI

                _openai_client = OpenAI(
                    api_key=settings.AI_ACCESS_TOKEN,
                    timeout=REQUEST_TIMEOUT_SECONDS,
                    max_retries=1,
                )
    return _openai_client


def detect_mime_type(uploaded_file):
    """The browser's content_type when it gave us a usable one, the filename
    extension otherwise — phone uploads routinely arrive as
    application/octet-stream."""
    content_type = (getattr(uploaded_file, 'content_type', '') or '').split(';')[0].strip().lower()
    if content_type in ACCEPTED_MIME_TYPES:
        return content_type

    guessed, _ = mimetypes.guess_type(uploaded_file.name or '')
    if guessed in ACCEPTED_MIME_TYPES:
        return guessed

    return None


def _to_jpeg(file_bytes):
    """Re-encode an image the model will not take (HEIC) as JPEG.

    pillow-heif is an optional import: if it is not installed the conversion
    fails with a message the uploader can act on, rather than the whole module
    refusing to load on a box that has not pip-installed yet.
    """
    from PIL import Image

    try:
        import pillow_heif

        pillow_heif.register_heif_opener()
    except ImportError as exc:
        raise ValueError(
            'HEIC images are not supported on this server. Re-save the photo '
            'as JPEG or PNG and upload it again.'
        ) from exc

    with Image.open(io.BytesIO(file_bytes)) as image:
        buffer = io.BytesIO()
        image.convert('RGB').save(buffer, format='JPEG', quality=90)
    return buffer.getvalue()


def _prepare_upload(file_bytes, mime_type):
    """Return (bytes, mime_type) in a form the model accepts."""
    if mime_type in CONVERTED_MIME_TYPES:
        return _to_jpeg(file_bytes), 'image/jpeg'
    return file_bytes, mime_type


def _content_part(file_bytes, mime_type, file_name):
    """One document as a Responses API content part.

    Files are sent inline as base64 data URLs rather than uploaded to the
    Files API first: the upload/poll/delete round trip roughly doubles the
    latency of a request, and nothing here needs the file to outlive the call.
    """
    encoded = base64.b64encode(file_bytes).decode()
    if mime_type == 'application/pdf':
        return {
            'type': 'input_file',
            'filename': file_name or 'document.pdf',
            'file_data': f'data:application/pdf;base64,{encoded}',
        }
    return {
        'type': 'input_image',
        'image_url': f'data:{mime_type};base64,{encoded}',
        'detail': 'high',
    }


def _normalize_date(value):
    """Return (iso_string, parsed_date). Documents print dates every way there
    is; the form needs one. dayfirst is off — these are U.S. documents."""
    try:
        parsed = date_parser.parse(str(value), dayfirst=False).date()
    except (ValueError, OverflowError, TypeError):
        return None, None
    return parsed.isoformat(), parsed


def _normalize_state(value):
    text = str(value).strip()
    if len(text) == 2:
        return text.upper()
    return US_STATES.get(text.lower(), text)


def _normalize_ein(value):
    digits = re.sub(r'\D', '', str(value))
    if len(digits) == 9:
        return f'{digits[:2]}-{digits[2:]}'
    return str(value).strip()


def normalize(extracted):
    """Clean up one model response into form-ready values.

    Returns (fields, warnings) where `fields` is flat — grouping into sections
    happens later, after a batch has been merged. Values the model returned in
    an unusable shape are dropped rather than passed through, so the frontend
    never has to defend against a half-parsed date landing in a date input.
    """
    fields = {}
    warnings = []
    today = date.today()

    for name, value in extracted.items():
        if name == 'document_type' or value in (None, ''):
            continue

        if isinstance(value, str):
            value = value.strip()
            if not value:
                continue

        if name in DATE_FIELDS:
            iso, parsed = _normalize_date(value)
            if iso is None:
                warnings.append(f"Could not read {name} ('{value}') as a date — left blank.")
                continue
            if name in EXPIRY_FIELDS and parsed < today:
                warnings.append(f'{name} is {iso}, which has already passed.')
            value = iso
        elif name in STATE_FIELDS:
            value = _normalize_state(value)
        elif name == 'company_employer_id':
            value = _normalize_ein(value)
        elif name in DIGITS_ONLY_FIELDS:
            value = re.sub(r'\D', '', str(value)) or None
            if value is None:
                continue

        if name == 'routing_number' and len(value) != 9:
            warnings.append(f'routing_number has {len(value)} digits, expected 9 — verify it.')

        fields[name] = value

    return fields, warnings


def parse_document(uploaded_file, document_type=None):
    """Extract structured fields from one uploaded document.

    `document_type` is an optional hint from the caller; when given it is
    passed to the model as context but the model still reports what it
    actually sees, so a mislabelled upload is caught rather than forced into
    the wrong shape.

    Returns a dict shaped like the per-file entry of the API response. Errors
    are returned in it, not raised — one unreadable file in a batch of five
    should not lose the other four.
    """
    result = {
        'file_name': uploaded_file.name,
        'document_type': None,
        'document_type_label': None,
        'fields': {},
        'warnings': [],
        'error': None,
    }

    mime_type = detect_mime_type(uploaded_file)
    if mime_type is None:
        result['error'] = (
            'Unsupported file type. Upload a PDF or an image '
            '(JPEG, PNG, WebP, HEIC).'
        )
        return result

    file_bytes = uploaded_file.read()
    if len(file_bytes) > MAX_FILE_BYTES:
        result['error'] = f'File is larger than {MAX_FILE_BYTES // (1024 * 1024)}MB.'
        return result
    if not file_bytes:
        result['error'] = 'File is empty.'
        return result

    try:
        file_bytes, mime_type = _prepare_upload(file_bytes, mime_type)
    except ValueError as exc:
        result['error'] = str(exc)
        return result
    except Exception:
        logger.exception('Could not convert %s for upload', uploaded_file.name)
        result['error'] = 'This image could not be read. Try re-saving it as JPEG.'
        return result

    prompt = 'Extract the onboarding data from this document.'
    if document_type:
        prompt += (
            f" The uploader labelled it '{document_type}'. Treat that as a hint"
            ' only — set document_type to what the document actually is.'
        )

    request = {
        'model': MODEL_NAME,
        'instructions': SYSTEM_INSTRUCTION,
        'input': [{
            'role': 'user',
            'content': [
                {'type': 'input_text', 'text': prompt},
                _content_part(file_bytes, mime_type, uploaded_file.name),
            ],
        }],
        'text_format': ExtractedDocument,
        'temperature': 0,
    }

    try:
        try:
            response = _client().responses.parse(**request)
        except Exception as exc:
            # Reasoning models reject `temperature` outright. Retrying without
            # it means bumping MODEL_NAME does not silently break this.
            if 'temperature' not in str(exc):
                raise
            logger.info('%s rejects temperature; retrying without it', MODEL_NAME)
            request.pop('temperature')
            response = _client().responses.parse(**request)
        parsed = response.output_parsed
    except Exception:
        logger.exception('Document parse failed for %s', uploaded_file.name)
        result['error'] = 'Could not analyze this file.'
        return result

    if parsed is None:
        result['error'] = 'The document could not be read.'
        return result

    extracted = parsed.model_dump()
    detected = extracted.get('document_type') or DocumentType.UNKNOWN
    detected = DocumentType(detected) if not isinstance(detected, DocumentType) else detected

    fields, warnings = normalize(extracted)

    if detected is DocumentType.UNKNOWN:
        warnings.append(
            'This document was not recognized as one of the supported types; '
            'anything read off it should be checked carefully.'
        )
    if document_type and detected.value != document_type and detected is not DocumentType.UNKNOWN:
        warnings.append(
            f"Uploaded as '{document_type}' but looks like "
            f"'{detected.value}' ({DOCUMENT_TYPE_LABELS[detected]})."
        )

    result['document_type'] = detected.value
    result['document_type_label'] = DOCUMENT_TYPE_LABELS[detected]
    result['fields'] = fields
    result['warnings'] = warnings
    return result


def build_prefill(results):
    """Merge a batch of per-file extractions into one form-shaped payload.

    Files are merged in upload order and the first non-empty value for a field
    wins. Where a later file disagrees, the disagreement is reported rather
    than silently resolved — a registration and a COI naming different states
    is exactly the kind of thing a recruiter needs to look at, not something
    to pick a winner for.
    """
    prefill = {'driver': {}, 'company': {}, 'vehicle': {}, 'deposit': {}}
    sources = {}
    conflicts = []
    sensitive = []

    for result in results:
        for name, value in result['fields'].items():
            target = FIELD_TARGETS.get(name)
            if target is None:
                continue
            section, field = target

            if field in prefill[section]:
                existing = prefill[section][field]
                if str(existing).strip().lower() != str(value).strip().lower():
                    conflicts.append({
                        'section': section,
                        'field': field,
                        'value': existing,
                        'from_file': sources[(section, field)],
                        'other_value': value,
                        'other_file': result['file_name'],
                    })
                continue

            prefill[section][field] = value
            sources[(section, field)] = result['file_name']
            if name in SENSITIVE_FIELDS:
                sensitive.append({'section': section, 'field': field})

    return prefill, conflicts, sensitive
