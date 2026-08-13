import requests
import re
from .models import LoadStop
from django.core.exceptions import ValidationError
from decimal import Decimal
from config import settings

def extract_street_only(address):
    cleaned = re.sub(r',\s*[^,]+,\s*[A-Za-z\s]+\s*\d{5}.*$', '', address).strip()
    cleaned = re.sub(r',\s*\d{5}.*$', '', cleaned).strip()
    return cleaned

def build_pcmiler_stop(address, city, state, zipcode):
    return {
        "Address": {
            "StreetAddress": extract_street_only(address),
            "City": city,
            "State": state,
            "Zip": zipcode,
            "CountryPostalFilter": 1,
        },
        "Region": "4"
    }


def calculate_loaded_miles(load):
    from .models import LoadStop
    from decimal import Decimal

    stops = list(
        LoadStop.objects
        .filter(load=load)
        .order_by('order')
    )

    load_pickup = next((s for s in stops if s.load_pickup), None)
    load_drop   = next((s for s in stops if s.load_drop),   None)

    if not load_pickup or not load_drop:
        print(f"[LOADED MILES] Missing pickup or drop stop for Load {load.id}")
        return Decimal("0")

    waypoints = [
        {"address": load_pickup.address, "city": load_pickup.city,
         "state": load_pickup.state,     "zipcode": load_pickup.zipcode},
        {"address": load_drop.address,   "city": load_drop.city,
         "state": load_drop.state,       "zipcode": load_drop.zipcode},
    ]
    miles = calculate_empty_miles_multi(waypoints)
    load.loaded_miles = miles
    load.save(update_fields=["loaded_miles"])
    return miles

def calculate_empty_miles(origin, destination):
    payload = {
        "ReportRoutes": [
            {
                "ReportTypes": [
                    {
                        "__type": "MileageReportType:http://pcmiler.alk.com/APIs/v1.0"
                    }
                ],
                "Stops": [
                    build_pcmiler_stop(
                        origin["address"],
                        origin["city"],
                        origin["state"],
                        origin["zipcode"],
                    ),
                    build_pcmiler_stop(
                        destination["address"],
                        destination["city"],
                        destination["state"],
                        destination["zipcode"],
                    ),
                ],
            }
        ]
    }
    response = requests.post(
        settings.PC_MILER_URL,
        json=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": settings.PC_MILER_KEY, 
        },
        timeout=15,
    )
    response.raise_for_status()
    data = response.json()
    try:
        report = data[0]
        report_lines = report["ReportLines"]
        if len(report_lines) < 2:
            raise ValueError("PC Miler returned insufficient route data")
        total_miles = report_lines[-1]["TMiles"]
        return Decimal(total_miles)
    except (KeyError, IndexError, TypeError) as e:
        raise ValueError(f"Invalid PC Miler response format: {data}") from e


def calculate_empty_miles_multi(waypoints: list[dict]) -> Decimal:
    if len(waypoints) < 2:
        raise ValueError("At least 2 waypoints required to calculate empty miles.")

    pcmiler_stops = [
        build_pcmiler_stop(
            wp["address"],
            wp["city"],
            wp["state"],
            wp["zipcode"],
        )
        for wp in waypoints
    ]

    payload = {
        "ReportRoutes": [
            {
                "ReportTypes": [
                    {
                        "__type": "MileageReportType:http://pcmiler.alk.com/APIs/v1.0"
                    }
                ],
                "Stops": pcmiler_stops,
            }
        ]
    }

    response = requests.post(
        settings.PC_MILER_URL,
        json=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": settings.PC_MILER_KEY,
        },
        timeout=20,
    )
    response.raise_for_status()
    data = response.json()

    try:
        report = data[0]
        report_lines = report["ReportLines"]
        if not report_lines:
            raise ValueError("PC Miler returned no route data.")
        return Decimal(report_lines[-1]["TMiles"])
    except (KeyError, IndexError, TypeError) as e:
        raise ValueError(f"Invalid PC Miler response format: {data}") from e

def calculate_empty_miles_for_load(load):
    stops = list(
        LoadStop.objects
        .filter(load=load)
        .order_by('order')
    )

    if not stops:
        load.empty_miles = Decimal("0")
        load.save(update_fields=["empty_miles"])
        return Decimal("0")

    def stop_to_waypoint(stop):
        return {
            "address": stop.address,
            "city": stop.city,
            "state": stop.state,
            "zipcode": stop.zipcode,
        }

    last_location_stop = next((s for s in stops if s.last_location), None)
    trailer_pickups = [s for s in stops if s.trailer_pickup]
    load_pickup_stop = next((s for s in stops if s.load_pickup), None)
    preload_stops = []

    if last_location_stop:
        preload_stops.append(stop_to_waypoint(last_location_stop))

    for stop in trailer_pickups:
        preload_stops.append(stop_to_waypoint(stop))

    if load_pickup_stop:
        preload_stops.append(stop_to_waypoint(load_pickup_stop))

    load_drop_stop = next((s for s in stops if s.load_drop), None)
    trailer_drops = [s for s in stops if s.trailer_drop]
    postload_stops = []
    if load_drop_stop:
        postload_stops.append(stop_to_waypoint(load_drop_stop))

    for stop in trailer_drops:
        postload_stops.append(stop_to_waypoint(stop))

    total_empty_miles = Decimal("0")
    if len(preload_stops) >= 2:
        pre_miles = calculate_empty_miles_multi(preload_stops)
        total_empty_miles += pre_miles

    if len(postload_stops) >= 2:
        post_miles = calculate_empty_miles_multi(postload_stops)
        total_empty_miles += post_miles
    load.empty_miles = total_empty_miles
    load.save(update_fields=["empty_miles"])
    return total_empty_miles
