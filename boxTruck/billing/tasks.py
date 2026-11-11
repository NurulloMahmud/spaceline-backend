from decimal import Decimal
import time
from billing.mile import calculate_empty_miles
from .models import Load, LoadStop
import threading
from .utils import send_group_message, send_message, format_dt, summarize_requirements, clean_address


def calculate_loaded_miles_background(load_id):
    from .mile import calculate_loaded_miles
    from .models import Load

    def _task():
        try:
            load = Load.objects.get(id=load_id)
            miles = calculate_loaded_miles(load)
        except Exception as e:
            print(f"Error calculating loaded miles for Load {load_id}: {e}")
    thread = threading.Thread(target=_task)
    thread.start()

def calculate_empty_miles_background(load_id, last_stop_data):

    def task():
        try:
            load = Load.objects.get(id=load_id)
            first_stop = (
                load.loadstop_set
                .select_related('facility')
                .order_by('order')
                .first()
            )
            if not first_stop:
                return

            origin = {
                "address": last_stop_data["address"],
                "city": last_stop_data["city"],
                "state": last_stop_data["state"],
                "zipcode": last_stop_data["zipcode"],
            }
            destination = {
                "address": first_stop.facility.address,
                "city": first_stop.facility.city,
                "state": first_stop.facility.state,
                "zipcode": first_stop.facility.zipcode,
            }
            empty_miles = calculate_empty_miles(origin, destination)
            load.empty_miles = empty_miles
            load.save(update_fields=["empty_miles"])
        except Exception as e:
            print(f"[EMPTY MILES ERROR] Load {load_id}: {e}")
    threading.Thread(target=task, daemon=True).start()

def notify_drivers_async(load_id: int):
    def task():
        try:
            load = Load.objects.select_related(
                'broker'
            ).get(id=load_id)
        except Load.DoesNotExist:
            return

        broker_name = load.broker.name if load.broker else "N/A"
        pickup_date = format_dt(load.pickup_date)
        drop_date = format_dt(load.drop_date)
        load_number = load.load_number or "N/A"
        bot_username = "@cosmos_app_bot".replace("_", "\\_")

        message = (
            f"🚛 *New Load Assigned*\n\n"
            f"• *Broker:* {broker_name}\n"
            f"• *Load #:* {load_number}\n\n"
            # f"• *Pick Up:* {pickup_date}\n"
            # f"• *Drop Off:* {drop_date}\n\n"
            f"📲 View full details in the bot:\n"
            f"👉 {bot_username}"
        )

        driver_qs = Load.objects.filter(
            load=load,
            driver__telegram_group_id__isnull=False
        )

        telegram_group_ids = set(
            driver_qs.values_list('driver__telegram_group_id', flat=True)
        )

        if not telegram_group_ids and load.driver and load.driver.telegram_group_id:
            telegram_group_ids.add(load.driver.telegram_group_id)

        for chat_id in telegram_group_ids:
            try:
                send_message(chat_id, message)
            except Exception:
                pass
    threading.Thread(target=task, daemon=True).start()

def format_appointment(start_dt, end_dt):
    if not start_dt or not end_dt:
        return "N/A"
    if start_dt.date() == end_dt.date():
        date_str = start_dt.strftime("%b %-d, %Y")
        time_str = f"{start_dt.strftime('%H:%M')} - {end_dt.strftime('%H:%M')}"
        return f"{date_str} | {time_str}"
    else:
        return f"{start_dt.strftime('%b %-d, %Y | %H:%M')} - {end_dt.strftime('%b %-d, %Y | %H:%M')}"

def notify_group_async(load_id: int, delay_seconds: int = 5):
    def task():
        time.sleep(delay_seconds)
        try:
            load = Load.objects.select_related('broker').get(id=load_id)
        except Load.DoesNotExist:
            return
        load_driver = Load.objects.filter(load=load)
        if not load_driver.exists():
            return
        broker_name = load.broker.name if load.broker else "N/A"
        load_number = load.load_number or "N/A"
        empty_miles = load.empty_miles or 0
        loaded_miles = load.loaded_miles or 0
        rate = load.driver_pay or 0

        sections = [
            ("Trailer Pickup", {"trailer_pickup": True}),
            ("Pickup", {"load_pickup": True}),
            ("Partial", {"partial": True}),
            ("Delivery", {"destination": True}),
            ("Trailer Drop", {"trailer_drop": True}),
        ]
        stop_lines = []
        for section_name, filter_kwargs in sections:
            stops = LoadStop.objects.select_related('facility').filter(
                load=load, **filter_kwargs
            ).order_by('order')
            if not stops.exists():
                continue

            for stop in stops:
                raw_address = stop.address
                city = stop.city
                state = stop.state
                zipcode = stop.zipcode

                cleaned_street = clean_address(raw_address, city, state, zipcode)
                if section_name in ["Trailer Pickup", "Pickup"]:
                    notes_label = "PUNotes"
                else:
                    notes_label = "DELNotes"

                address = f"{cleaned_street}, {stop.city}, {stop.state}, {stop.zipcode}"
                raw_notes = stop.requirements or ""
                summary = summarize_requirements(raw_notes) if raw_notes else "N/A"
                stop_lines.extend([
                    f"📍 *{section_name}*",
                    "⌚ *Working Hours:* Must Check",
                    f"📌 *Address:* {address}",
                    f"*{notes_label}:*\n{summary}",
                    "----------------------------------------\n"
                ])
            if not load_driver or not load_driver.telegram_group_id:
                continue

            message_lines = [
                f"🚛 *PO Load*",
                f"*Broker:* {broker_name}",
                f"*Load #:* {load_number}",
                f"*Empty Miles:* {empty_miles} mi",
                f"*Loaded Miles:* {loaded_miles} mi",
            ]

            message_lines.append("----------------------------------------\n")
            message_lines.extend(stop_lines)
            message_lines.extend([
                "👉 */rules* - Dispatch rules & penalties.",
                "⚠️ *Follow instructions. Missing info = penalty. Keep us updated.*"
            ])
            message = "\n".join(message_lines)
            try:
                send_group_message(chat_id=load_driver.telegram_group_id, text=message)
            except Exception as e:
                pass
    threading.Thread(target=task, daemon=True).start()

def calculate_empty_miles_multi_background(load_id):
    from .mile import calculate_empty_miles_multi
    from .models import Load, LoadStop

    def task():
        try:
            load = Load.objects.get(id=load_id)
            stops = list(LoadStop.objects.filter(load=load).order_by('order'))
            if not stops:
                return

            def stop_to_waypoint(s):
                return {
                    "address": s.address,
                    "city":    s.city,
                    "state":   s.state,
                    "zipcode": s.zipcode,
                }

            load_pickup_stop  = next((s for s in stops if s.load_pickup),  None)
            last_location_stop = next((s for s in stops if s.last_location), None)
            trailer_pickups   = [s for s in stops if s.trailer_pickup]
            pre_load_stops = []
            if load_pickup_stop:
                if last_location_stop:
                    pre_load_stops.append(stop_to_waypoint(last_location_stop))
                for s in trailer_pickups:
                    pre_load_stops.append(stop_to_waypoint(s))
                pre_load_stops.append(stop_to_waypoint(load_pickup_stop))
            elif last_location_stop and trailer_pickups:
                pre_load_stops.append(stop_to_waypoint(last_location_stop))
                for s in trailer_pickups:
                    pre_load_stops.append(stop_to_waypoint(s))

            load_drop_stop = next((s for s in stops if s.load_drop), None)
            trailer_drops  = [s for s in stops if s.trailer_drop]
            post_load_stops = []
            if load_drop_stop and trailer_drops:
                post_load_stops.append(stop_to_waypoint(load_drop_stop))
                for s in trailer_drops:
                    post_load_stops.append(stop_to_waypoint(s))

            total_empty_miles = Decimal("0")
            if len(pre_load_stops) >= 2:
                pre_miles = calculate_empty_miles_multi(pre_load_stops)
                total_empty_miles += pre_miles

            if len(post_load_stops) >= 2:
                post_miles = calculate_empty_miles_multi(post_load_stops)
                total_empty_miles += post_miles
            load.empty_miles = total_empty_miles
            load.save(update_fields=["empty_miles"])

        except Exception as e:
            print(f"[EMPTY MILES ERROR] Load {load_id}: {e}")

    threading.Thread(target=task, daemon=True).start()
