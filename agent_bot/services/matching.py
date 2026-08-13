import math
import logging
from typing import Optional
from services.ai import ai

logger = logging.getLogger(__name__)

VEHICLE_TYPE_MAP = {
    "CARGO VAN":      "Cargo Van",
    "SMALL STRAIGHT": "Small Straight",
    "LARGE STRAIGHT": "Large Straight",
}


def haversine(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    R = 3958.8
    lat1, lng1, lat2, lng2 = map(math.radians, [lat1, lng1, lat2, lng2])
    dlat = lat2 - lat1
    dlng = lng2 - lng1
    a = math.sin(dlat/2)**2 + math.cos(lat1)*math.cos(lat2)*math.sin(dlng/2)**2
    return R * 2 * math.asin(math.sqrt(a))


def normalize_vehicle_type(raw: str) -> str:
    return VEHICLE_TYPE_MAP.get(raw.upper().strip(), raw.strip())


def check_dimensions(load: dict, vehicle: dict) -> tuple[bool, str]:
    dims    = load.get("dims")
    pieces  = load.get("pieces", 1) or 1
    weight  = load.get("weight", 0) or 0
    notes   = load.get("notes", "") or ""

    if not dims or dims == [0, 0, 0]:
        if notes:
            extracted = ai.extract_dims_from_notes(notes)
            if extracted:
                dims   = extracted.get("dims")
                pieces = extracted.get("pieces") or pieces
                weight = extracted.get("weight") or weight

    v_length  = vehicle.get("length") or 0
    v_width   = vehicle.get("width") or 0
    v_height  = vehicle.get("height") or 0
    v_payload = vehicle.get("payload") or 0

    if not v_length and not v_width and not v_height:
        return True, "no vehicle dims — skipping dimension check"

    if dims and len(dims) == 3:
        total_length = dims[0] * pieces
        load_width   = dims[1]
        load_height  = dims[2]

        if v_length and total_length > v_length:
            return False, f"too long: {total_length}\" > {v_length}\""
        if v_width and load_width > v_width:
            return False, f"too wide: {load_width}\" > {v_width}\""
        if v_height and load_height > v_height:
            return False, f"too tall: {load_height}\" > {v_height}\""

    if weight and v_payload and weight > v_payload:
        return False, f"too heavy: {weight}lbs > {v_payload}lbs"

    return True, "ok"


def matches_driver(
    load: dict,
    driver: dict,
    pref,
    dead_head: float,
) -> tuple[bool, str]:

    vehicle = driver.get("vehicle")
    if not vehicle:
        return False, "no vehicle"

    load_vtype   = normalize_vehicle_type(
        load.get("suggested_truck") or load.get("vehicle_type") or ""
    )
    driver_vtype = normalize_vehicle_type(vehicle.get("vehicle_type", ""))

    if load_vtype.lower() != driver_vtype.lower():
        return False, f"type mismatch: load={load_vtype} driver={driver_vtype}"

    if dead_head > 50:
        return False, f"dead head {dead_head:.1f}mi > 50mi"

    fits, reason = check_dimensions(load, vehicle)
    if not fits:
        return False, reason

    if pref:
        if not pref.available:
            return False, "driver unavailable"

        load_miles = load.get("miles", 0)
        if pref.local_only and load_miles > 100:
            return False, f"local only but load is {load_miles}mi"
        if pref.max_miles and load_miles > pref.max_miles:
            return False, f"load {load_miles}mi > max {pref.max_miles}mi"

    return True, "ok"
