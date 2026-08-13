from decimal import Decimal
import json
import time
from django.core.files.base import ContentFile
from billing.mile import calculate_empty_miles
from config import settings
from celery import shared_task
from .models import Load, LoadFile, Batch, BatchLoad, LoadStop, Broker
from django.utils import timezone
from users.models import CustomUser
from .invoice import generate_rts_invoice
import threading
from datetime import datetime, timedelta
from django.utils import timezone
from .utils import escape_markdown, send_group_message, send_message, format_dt, summarize_requirements, clean_address
from .rts import validate_loads_for_rts, generate_rts_csv, generate_invoice_pdf, upload_to_rts, send_to_telegram
from io import BytesIO
import traceback
from .bot import send_rts_upload_message

def async_generate_invoices(batch_id, load_ids, user_id):
    def _generate():
        try:
            loads = Load.objects.filter(id__in=load_ids).select_related('company', 'broker')
            batch = Batch.objects.get(id=batch_id)
            success_count = 0
            failed_count = 0
            failure_details = []
            for load in loads:
                try:
                    pdf_buffer = generate_rts_invoice(load)
                    pdf_content = ContentFile(pdf_buffer.getvalue())
                    load_file = LoadFile(
                        name=f"Invoice: {load.shipment}.pdf",
                        load=load
                    )
                    load_file.file.save(f"rts_invoices/{load.shipment}.pdf", pdf_content)
                    load_file.save()
                    success_count += 1
                except Exception as e:
                    failed_count += 1
                    error_msg = f"Load {load.load_number}: {str(e)}"
                    failure_details.append(error_msg)
                    print(f"Failed to process load {load.id}:")
                    traceback.print_exc()
            message = (
                f"📊 *Invoice Generation Complete*\n"
                f"✅ *Success:* {success_count}\n"
                f"❌ *Failed:* {failed_count}\n"
                f"🔗 *Batch:* {batch.name}"
                f"🔗 *Batch Date:* {batch.date}"
            )
            if failed_count > 0:
                message += "\n\n*Failures:*\n" + "\n".join(failure_details[:5])
                if failed_count > 5:
                    message += f"\n...and {failed_count - 5} more"
            send_rts_upload_message(message)
            send_rts_upload_message(message)
        except Exception as e:
            error_msg = f"🚨 *Invoice Generation Failed*\nError: {str(e)}\n\n*Traceback:*\n{traceback.format_exc()}"
            send_rts_upload_message(error_msg[:4000])
    thread = threading.Thread(target=_generate)
    thread.daemon = True
    thread.start()


@shared_task(bind=True, time_limit=3600, soft_time_limit=3500)  # 1 hour timeout
def process_batch_in_background(self, batch_id, user_id):
    def safe_send(message):
        try:
            safe_msg = escape_markdown(str(message))[:4000]
            send_rts_upload_message(safe_msg)
        except Exception as e:
            print(f"⚠️ Telegram send failed: {str(e)[:500]}")

    try:
        batch = Batch.objects.get(id=batch_id)
        user = CustomUser.objects.get(id=user_id)
        batch_loads = BatchLoad.objects.filter(batch=batch)
        loads = [bl.load for bl in batch_loads]
        total_loads = len(loads)
        initial_message = (
            f"🚀 *Processing Started* 🚀\n"
            f"*Batch:* {escape_markdown(batch.name)}\n"
            f"*User:* {escape_markdown(user.username)}\n"
            f"*Total Loads:* {total_loads}\n"
            f"*Started at:* {timezone.now().strftime('%Y-%m-%d %H:%M:%S')}"
        )
        safe_send(initial_message)
        if total_loads > 999:
            safe_send(f"❌ Batch too large (>{escape_markdown('999')} loads)")
            return
        valid_loads, errors = validate_loads_for_rts(loads)
        if errors:
            error_sample = "\n".join(f"- {e}" for e in errors[:3])
            if len(errors) > 3:
                error_sample += "\n..."
            safe_send(f"⚠️ Validation errors:\n{error_sample}")
            return

        CHUNK_SIZE = 5
        pdf_files_for_rts = []
        pdf_success_count = 0
        pdf_failures = []

        for chunk_idx in range(0, len(valid_loads), CHUNK_SIZE):
            chunk = valid_loads[chunk_idx:chunk_idx + CHUNK_SIZE]

            try:
                for load in chunk:
                    try:
                        load_idx = valid_loads.index(load) + 1
                        safe_send(f"🔄 Processing load {load_idx}/{len(valid_loads)}: {escape_markdown(load.shipment)}")
                        pdf_buffer = generate_invoice_pdf(load)
                        if pdf_buffer:
                            pdf_files_for_rts.append((f"{load.shipment}.pdf", pdf_buffer))
                            pdf_success_count += 1
                        else:
                            pdf_failures.append(f"PDF failed for {load.shipment}")

                    except Exception as load_error:
                        pdf_failures.append(f"Error processing {load.shipment}: {str(load_error)[:200]}")
                        continue

                progress = (
                    f"📊 Progress: {min(chunk_idx + CHUNK_SIZE, len(valid_loads))}/{len(valid_loads)}\n"
                    f"✅ PDFs: {pdf_success_count}\n"
                    f"❌ Failures: {len(pdf_failures)}"
                )
                safe_send(progress)

            except Exception as chunk_error:
                safe_send(f"⚠️ Chunk failed: {str(chunk_error)[:500]}")
                continue

        try:
            csv_content = generate_rts_csv(batch, valid_loads)
            send_to_telegram(csv_content, batch.name, batch.date)
            csv_buffer = BytesIO(csv_content.getvalue().encode('utf-8'))
            csv_buffer.name = 'rts_invoices.csv'
        except Exception as csv_error:
            safe_send(f"❌ CSV generation failed: {str(csv_error)[:500]}")
            raise
        try:
            rts_success, rts_errors = upload_to_rts(batch, pdf_files_for_rts, csv_buffer)
            if not rts_success:
                safe_send(f"⚠️ RTS upload had {len(rts_errors)} errors")
        except Exception as upload_error:
            safe_send(f"❌ RTS upload failed: {str(upload_error)[:500]}")
            raise
        completion_msg = (
            f"✅ *Completed* {escape_markdown(batch.name)}\n"
            f"📄 PDFs: {pdf_success_count}/{len(valid_loads)}\n"
            f"⚠️ Issues: {len(pdf_failures)}\n"
            f"🕒 Duration: {timezone.now().strftime('%Y-%m-%d %H:%M:%S')}"
        )
        safe_send(completion_msg)
    except Exception as e:
        safe_send(f"❌ *Critical failure*: {str(e)[:500]}")
        raise

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
                'broker', 'company'
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
            load = Load.objects.select_related('broker', 'company').get(id=load_id)
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
