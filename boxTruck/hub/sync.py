"""Create or update the hiring records from a hub driver profile.

The hub is the source of truth for a connected driver: on every profile,
vehicle or document change it posts the whole profile here and we overwrite
our copy. Matching is by hub_driver_id first, then by the email the invite
went to, so the Driver row the dispatcher invited is the one that gets linked.
"""
import logging
from datetime import date, datetime

import requests
from django.core.files.base import ContentFile
from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_date, parse_datetime

from hiring.models import (
    Driver, DriverCompany, DriverFile, DriverStatus, Vehicle, VehicleEquipment, VehicleFile,
)

logger = logging.getLogger(__name__)

DRIVER_DOC_KINDS = ('license', 'medical_card', 'other')
VEHICLE_DOC_KINDS = ('insurance', 'registration')
DOC_TITLES = {
    'license': 'Driver License',
    'medical_card': 'Medical Card',
    'insurance': 'Insurance',
    'registration': 'Registration',
}


def fetch_file(url):
    """Download a hub upload. Returns bytes or None; a missing file never blocks the sync."""
    try:
        resp = requests.get(url, timeout=20)
        resp.raise_for_status()
    except requests.RequestException as exc:
        logger.warning('hub document %s could not be fetched: %s', url, exc)
        return None
    return resp.content


def _text(value, limit=255):
    if value is None:
        return ''
    return str(value).strip()[:limit]


def _int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _date(value):
    if not value:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    parsed = parse_datetime(str(value))
    if parsed:
        return parsed.date()
    return parse_date(str(value)[:10])


def default_status():
    return (
        DriverStatus.objects.filter(name__iexact='pending').first()
        or DriverStatus.objects.order_by('id').first()
        or DriverStatus.objects.create(name='Pending')
    )


def find_driver(hub_driver_id, email):
    driver = Driver.objects.filter(hub_driver_id=hub_driver_id).first()
    if driver or not email:
        return driver
    return (
        Driver.objects
        .filter(email__iexact=email, hub_driver_id__isnull=True)
        .order_by('id')
        .first()
    )


@transaction.atomic
def upsert_driver(payload):
    hub_driver_id = _int(payload.get('hub_driver_id'))
    if not hub_driver_id:
        raise ValueError('hub_driver_id is required')
    data = payload.get('driver') or {}
    email = _text(data.get('email'))

    driver = find_driver(hub_driver_id, email)
    if driver is None:
        driver = Driver(status=default_status())

    driver.hub_driver_id = hub_driver_id
    driver.hub_invite_status = 'active'
    full_name = ' '.join(p for p in (_text(data.get('first_name')), _text(data.get('last_name'))) if p)
    if full_name:
        driver.full_name = full_name
    elif not driver.full_name:
        driver.full_name = email or f'Driver {hub_driver_id}'
    if email:
        driver.email = email
    if data.get('phone'):
        driver.phone_number = _text(data.get('phone'))
    for ours, theirs in (('address', 'address'), ('city', 'city'), ('state', 'state'), ('zip_code', 'zip')):
        if data.get(theirs):
            setattr(driver, ours, _text(data.get(theirs), 250 if ours == 'city' else 55 if ours == 'state' else 20 if ours == 'zip_code' else 255))
    lat, lng = data.get('lat'), data.get('lng')
    if lat is not None and lng is not None:
        try:
            driver.current_latitude = float(lat)
            driver.current_longitude = float(lng)
            driver.current_location_updated_at = timezone.now()
        except (TypeError, ValueError):
            pass

    company = _upsert_company(driver, data)
    if company:
        driver.driver_company = company
    driver.save()

    vehicle = _upsert_vehicle(driver, payload.get('vehicle'))
    _apply_documents(driver, vehicle, payload.get('documents') or [])
    return driver


def _upsert_company(driver, data):
    fields = {
        'mc': _text(data.get('mc')),
        'employer_id': _text(data.get('employer_id')),
        'name': _text(data.get('company_name')),
        'business_as': _text(data.get('business_as')),
        'business_type': _text(data.get('business_type')),
        'zipcode': _text(data.get('zip')),
        'state': _text(data.get('state')),
        'city': _text(data.get('city')),
        'address': _text(data.get('address')),
    }
    if not any(fields[k] for k in ('mc', 'employer_id', 'name', 'business_as')):
        return driver.driver_company
    company = driver.driver_company or DriverCompany()
    for key, value in fields.items():
        if value or not getattr(company, key, ''):
            setattr(company, key, value)
    company.phone_number = _text(data.get('phone')) or company.phone_number or ''
    company.email = _text(data.get('email')) or company.email
    company.applicant_first_name = _text(data.get('first_name')) or company.applicant_first_name
    company.applicant_last_name = _text(data.get('last_name')) or company.applicant_last_name
    company.save()
    return company


def _upsert_vehicle(driver, data):
    if not data:
        return driver.vehicles.order_by('id').first()
    vehicle = driver.vehicles.order_by('id').first() or Vehicle(driver=driver, payload=0, gvw=0, make='', model='')
    vehicle.make = _text(data.get('make')) or vehicle.make
    vehicle.model = _text(data.get('model')) or vehicle.model
    vehicle.vehicle_type = _text(data.get('vehicle_type')) or vehicle.vehicle_type
    year = _int(data.get('year'))
    if year:
        vehicle.year = year
    for ours, theirs in (
        ('payload', 'payload_lbs'), ('gvw', 'gvw_lbs'),
        ('length', 'cargo_length_in'), ('width', 'cargo_width_in'), ('height', 'cargo_height_in'),
        ('door_open_width', 'door_width_in'), ('door_open_height', 'door_height_in'),
    ):
        value = _int(data.get(theirs))
        if value is not None:
            setattr(vehicle, ours, value)
    dock = _int(data.get('dock_height_in'))
    dock_type = _text(data.get('dock_type'))
    if dock or dock_type:
        vehicle.dock_height = ' '.join(p for p in (f'{dock} in' if dock else '', dock_type) if p)
    vehicle.save()

    unit = _text(data.get('unit_number'))
    if unit and driver.unit_number != unit:
        Driver.objects.filter(pk=driver.pk).update(unit_number=unit)
        driver.unit_number = unit

    equipment = [e.strip() for e in _text(data.get('equipment'), 1000).split(',') if e.strip()]
    if equipment:
        VehicleEquipment.objects.filter(vehicle=vehicle).delete()
        VehicleEquipment.objects.bulk_create([VehicleEquipment(vehicle=vehicle, name=e[:255]) for e in equipment])
    return vehicle


def _apply_documents(driver, vehicle, documents):
    driver_updates = {}
    vehicle_updates = {}
    for doc in documents:
        kind = _text(doc.get('kind')) or 'other'
        expires = _date(doc.get('expires_at'))
        if kind == 'license' and expires:
            driver_updates['cdl_expiration'] = expires
        elif kind == 'medical_card' and expires:
            driver_updates['medical_exp_date'] = expires
        elif kind == 'insurance' and expires:
            vehicle_updates['insurance_expiry_date'] = expires
        elif kind == 'registration' and expires:
            vehicle_updates['registration_expiry_date'] = expires
        _store_document(driver, vehicle, kind, doc)
    if driver_updates:
        Driver.objects.filter(pk=driver.pk).update(**driver_updates)
    if vehicle and vehicle_updates:
        Vehicle.objects.filter(pk=vehicle.pk).update(**vehicle_updates)


def _store_document(driver, vehicle, kind, doc):
    url = _text(doc.get('file_url'), 500)
    if not url:
        return
    title = _text(doc.get('title')) or DOC_TITLES.get(kind, kind.replace('_', ' ').title())
    name = f'{title} (driver app)'[:255]
    filename = url.rsplit('/', 1)[-1].split('?')[0] or f'{kind}.bin'

    if kind in VEHICLE_DOC_KINDS:
        if vehicle is None:
            return
        existing = VehicleFile.objects.filter(vehicle=vehicle, name=name).first()
        if existing and existing.document and existing.document.name.endswith(filename):
            return
        content = fetch_file(url)
        if content is None:
            return
        VehicleFile.objects.filter(vehicle=vehicle, name=name).delete()
        VehicleFile.objects.create(vehicle=vehicle, name=name, document=ContentFile(content, name=filename))
        return

    existing = DriverFile.objects.filter(driver=driver, name=name).first()
    if existing and existing.document and existing.document.name.endswith(filename):
        return
    content = fetch_file(url)
    if content is None:
        return
    DriverFile.objects.filter(driver=driver, name=name).delete()
    DriverFile.objects.create(driver=driver, name=name, document=ContentFile(content, name=filename))


def remove_driver(hub_driver_id):
    """The driver left this carrier in the app: keep the record, drop the link."""
    driver = Driver.objects.filter(hub_driver_id=_int(hub_driver_id)).first()
    if not driver:
        return None
    driver.hub_driver_id = None
    driver.hub_invite_status = 'left'
    driver.hub_invite_id = None
    driver.save(update_fields=['hub_driver_id', 'hub_invite_status', 'hub_invite_id'])
    return driver
