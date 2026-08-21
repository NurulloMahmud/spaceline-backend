import csv
import os
from pypdf import PdfReader, PdfWriter
import requests
from PIL import Image
from pdf2image import convert_from_bytes
from io import StringIO, BytesIO
from datetime import datetime
from .bot import send_rts_upload_message
from config import settings
from ftplib import FTP_TLS
from typing import List, Tuple
from .utils import escape_markdown


def safe_send(message):
    try:
        send_rts_upload_message(message)
    except Exception as e:
        print(f"⚠️ Telegram send failed: {str(e)}")


def upload_to_rts(batch, pdf_files: List[Tuple[str, BytesIO]], csv_file: BytesIO) -> Tuple[bool, List[str]]:
    failed = []
    batch_name = batch.name.lower()
    if "space" in batch_name:
        client = settings.SPACELINE_CLIENT
        password = settings.SPACELINE_PASSWORD
    elif "priority" in batch_name:
        client = settings.PRIORITY_CLIENT
        password = settings.PRIORITY_PASSWORD
    elif "roadpulse logistics" in batch_name:
        client = settings.ROADPULSE_CLIENT
        password = settings.ROADPULSE_PASSWORD
    elif "tpa cargo solutions" in batch_name:
        client = settings.TPA_CLIENT
        password = settings.TPA_PASSWORD
    else:
        safe_send(f"❌ Unknown company in batch name: {escape_markdown(batch.name)}")
        return (False, [f"Unknown company in batch name: {batch.name}"])

    try:
        ftps = FTP_TLS()
        ftps.connect("ftps.rtsfinancial.com", 21)
        ftps.login(client, password)
        ftps.prot_p()
        safe_send("✅ Connected to *RTS FTPS*")
        for filename, file_data in pdf_files:
            try:
                file_data.seek(0)
                safe_send(f"⬆️ Uploading {escape_markdown(filename)} ...")
                ftps.storbinary(f"STOR {filename}", file_data)
                safe_send(f"✅ Uploaded {escape_markdown(filename)}")
            except Exception as e:
                error_msg = f"{escape_markdown(filename)} - {str(e)}"
                failed.append(error_msg)
                safe_send(f"❌ Failed: {error_msg}")

        try:
            csv_file.seek(0)
            safe_send(f"⬆️ Uploading {escape_markdown(csv_file.name)} ...")
            ftps.storbinary(f"STOR {csv_file.name}", csv_file)
            safe_send(f"✅ Uploaded {escape_markdown(csv_file.name)}")
        except Exception as e:
            error_msg = f"{escape_markdown(csv_file.name)} - {str(e)}"
            failed.append(error_msg)
            safe_send(f"❌ Failed: {error_msg}")
        ftps.quit()
        return (len(failed) == 0, failed)
    except Exception as e:
        error_msg = f"FTPS connection failed: {str(e)}"
        safe_send(f"🚨 {error_msg}")
        return (False, [error_msg])


def generate_invoice_pdf(load):
    print(f"📥 Starting invoice PDF generation for {escape_markdown(load.shipment)}")
    load_files = load.loadfile_set.all()
    invoice_files = []
    pod_files = []
    ratecon_files = []
    other_files = []

    for f in load_files:
        name_lower = f.name.lower()
        if "invoice" in name_lower:
            invoice_files.append(f)
        elif "pod" in name_lower or "bol" in name_lower:
            pod_files.append(f)
        elif "ratecon" in name_lower:
            ratecon_files.append(f)
        else:
            other_files.append(f)
    ordered_files = invoice_files + pod_files + other_files + ratecon_files
    writer = PdfWriter()
    has_content = False
    for load_file in ordered_files:
        file_field = load_file.file
        ext = os.path.splitext(file_field.name)[-1].lower()
        try:
            with file_field.open('rb') as f:
                file_bytes = f.read()

            if ext == '.pdf':
                print(f"Adding PDF: {file_field.name}")
                reader = PdfReader(BytesIO(file_bytes), strict=False)
                for page in reader.pages:
                    try:
                        writer.add_page(page)
                        has_content = True
                    except Exception as e:
                        print(f"⚠️ Skipping page in {file_field.name}: {str(e)}")
                        continue

            elif ext in ['.jpg', '.jpeg', '.png']:
                print(f"Converting image to PDF: {file_field.name}")
                img = Image.open(BytesIO(file_bytes))
                img = img.convert('L')
                img_pdf_buffer = BytesIO()
                img.save(img_pdf_buffer, format='PDF')
                img_pdf_buffer.seek(0)
                img_reader = PdfReader(img_pdf_buffer)
                for page in img_reader.pages:
                    writer.add_page(page)
                has_content = True
        except Exception as e:
            print(f"⚠️ Failed to process {file_field.name}: {str(e)}")
            continue
    if not has_content:
        print(f"⚠️ No valid files found for {load.shipment}")
        return None
    pdf_buffer = BytesIO()
    writer.write(pdf_buffer)
    pdf_buffer.seek(0)
    pdf_buffer.name = f"{load.shipment}.pdf"
    try:
        url = f"https://api.telegram.org/bot{settings.BOT_TOKEN}/sendDocument"
        files = {'document': (pdf_buffer.name, pdf_buffer)}
        data = {'chat_id': settings.RTS_UPLOAD_GROUP, 'caption': f"🧾 Invoice PDF for {escape_markdown(load.shipment)} (8x10 Grayscale)", 'parse_mode': 'MarkdownV2'}
        response = requests.post(url, files=files, data=data)
        safe_send(f"Telegram send status: {response.status_code}")
    except Exception as e:
        safe_send(f"Telegram send failed: {e}")
    pdf_buffer.seek(0)
    return pdf_buffer


def generate_rts_csv(batch, loads):
    def get_rts_client(batch_name: str):
        name = batch_name.lower()
        if "space" in name:
            return settings.SPACELINE_CLIENT
        elif "priority" in name:
            return settings.PRIORITY_CLIENT
        elif "roadpulse logistics" in name:
            return settings.ROADPULSE_CLIENT
        elif "tpa cargo solutions" in name:
            return settings.TPA_CLIENT
        else:
            safe_send(f"❌ Unknown company in batch name: {escape_markdown(batch_name)}")
            return None

    rts_client = get_rts_client(batch.name)
    if not rts_client:
        return None
    output = StringIO()
    writer = csv.writer(output)
    writer.writerow([
        'Client', 'Invoice#', 'DebtorNo', 'Debtor Name',
        'Load #', 'InvDate', 'InvAmt'
    ])
    today_date = datetime.now().strftime('%m/%d/%Y')
    for load in loads:
        if not all([
            load.shipment,
            load.broker,
            load.load_number,
            load.carrier_pay
        ]):
            continue

        writer.writerow([
            rts_client,
            load.shipment,
            load.broker.name,
            load.broker.name,
            load.load_number,
            today_date,
            str(load.carrier_pay)
        ])
    return output


def validate_loads_for_rts(loads):
    valid_loads = []
    errors = []
    seen_invoices = set()
    for load in loads:
        missing_fields = []
        if not load.shipment:
            missing_fields.append('shipment')
        if not load.broker:
            missing_fields.append('broker')
        if not load.load_number:
            missing_fields.append('load_number')
        if not load.carrier_pay:
            missing_fields.append('carrier_pay')
        if missing_fields:
            errors.append(f"Load {escape_markdown(load.shipment) or 'N/A'} missing required fields: {', '.join(missing_fields)}")
            continue
        if load.shipment in seen_invoices:
            errors.append(f"Duplicate invoice number: {escape_markdown(load.shipment)}")
            continue
        seen_invoices.add(load.shipment)
        if load.carrier_pay < 0:
            errors.append(f"Load {escape_markdown(load.shipment)} has invalid amount: {escape_markdown(load.carrier_pay)}")
            continue
        valid_loads.append(load)
    return valid_loads, errors


def send_to_telegram(csv_content, batch_name=None, batch_date=None):
    try:
        message = "📊 RTS Invoice CSV Preview"
        if batch_name:
            message += f" for batch: {escape_markdown(batch_name)} in {batch_date}"
        csv_bytes = csv_content.getvalue().encode('utf-8')
        file_obj = BytesIO(csv_bytes)
        file_obj.name = 'rts_invoices.csv'
        url = f"https://api.telegram.org/bot{settings.BOT_TOKEN}/sendDocument"
        files = {'document': file_obj}
        data = {'chat_id': settings.RTS_UPLOAD_GROUP, 'caption': escape_markdown(message), 'parse_mode': 'MarkdownV2'}
        response = requests.post(url, files=files, data=data)
        return response.status_code == 200
    except Exception as e:
        print(f"Failed to send to Telegram: {str(e)}")
        return False
