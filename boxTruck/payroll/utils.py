import os
import tempfile
from datetime import datetime, date
from django.core.files import File
from django.core.files.storage import default_storage
from decimal import Decimal, InvalidOperation

from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import inch
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, HRFlowable
from reportlab.platypus import Image
from reportlab.lib.enums import TA_LEFT, TA_RIGHT, TA_CENTER
from billing.bot import send_telegram_message
from config import settings
from .serializers import StatementViewForPDFSerializer


def format_date_for_display(date_value):
    if date_value is None:
        return 'N/A'
    if hasattr(date_value, 'strftime'):
        return date_value.strftime('%m/%d/%Y')
    if isinstance(date_value, str):
        for fmt in ('%Y-%m-%dT%H:%M:%S', '%Y-%m-%d'):
            try:
                return datetime.strptime(date_value[:len(fmt)], fmt).strftime('%m/%d/%Y')
            except ValueError:
                continue
    return str(date_value)


def format_currency(amount):
    if not amount:
        return "$0.00"
    try:
        d = Decimal(str(amount))
        formatted = f"${abs(d):,.2f}"
        return f"({formatted})" if d < 0 else formatted
    except (InvalidOperation, ValueError, TypeError):
        return "$0.00"


def get_company_logo_path(company_name):
    if not company_name:
        send_telegram_message("⚠️ *Company name is None or empty*")
        return None
    logos_dir = os.path.join(settings.BASE_DIR, 'media', 'logos')
    cleaned = company_name.split('/')[0].strip().lower()
    for filename in os.listdir(logos_dir):
        if cleaned in filename.lower():
            path = os.path.join(logos_dir, filename)
            send_telegram_message(f"✅ *Logo found:* {path}")
            return path
    send_telegram_message(f"⚠️ *No logo found* for company: {company_name}")
    return None


def generate_statement_pdf(statement):
    import time
    with tempfile.NamedTemporaryFile(suffix='.pdf', delete=False) as temp_file:
        temp_path = temp_file.name

    directory_path = 'payroll/statement'
    timestamp = int(time.time())
    filename = f"{statement.driver.full_name} {statement.start_date}-{statement.end_date} {timestamp}.pdf"
    file_path = f"{directory_path}/{filename}"
    serializer = StatementViewForPDFSerializer(statement)
    data = serializer.data

    doc = SimpleDocTemplate(
        temp_path,
        pagesize=letter,
        rightMargin=36,
        leftMargin=36,
        topMargin=36,
        bottomMargin=36
    )
    styles = getSampleStyleSheet()
    title_style = ParagraphStyle(
        'CustomTitle',
        parent=styles['Heading2'],
        fontSize=11,
        leading=13,
        spaceAfter=6
    )

    elements = []
    carrier_info    = data.get('company', {})
    carrier_name    = carrier_info.get('name', 'N/A')
    carrier_address = str(carrier_info.get('address', 'N/A'))
    settlement_data = [
        [Paragraph(f"<b>Settlement ST-{statement.id}</b>", styles['Normal'])],
        [Paragraph(
            f"Date: {format_date_for_display(data.get('start_date'))} - {format_date_for_display(data.get('end_date'))}",
            styles['Normal']
        )],
    ]
    settlement_table = Table(settlement_data, colWidths=[2 * inch])
    settlement_table.setStyle(TableStyle([
        ('ALIGN',         (0, 0), (-1, -1), 'RIGHT'),
        ('FONTSIZE',      (0, 0), (-1, -1), 10),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 6),
    ]))

    logo_path = get_company_logo_path(carrier_name)
    logo_img  = None
    if logo_path and os.path.exists(logo_path):
        try:
            img = Image(os.path.abspath(logo_path))
            img.drawWidth  = 1 * inch
            img.drawHeight = 1 * inch
            img._hAlign    = 'LEFT'
            logo_img = img
        except Exception as e:
            send_telegram_message(f"🚨 *Error handling logo:* {str(e)}")
    else:
        send_telegram_message(f"⚠️ *Logo path invalid or file doesn't exist:* {logo_path}")

    carrier_data = [
        [Paragraph(f"<b>{carrier_name}</b>",    styles['Normal'])],
        [Paragraph(f"<b>{carrier_address}</b>", styles['Normal'])],
    ]
    carrier_table = Table(carrier_data, colWidths=[2 * inch])
    carrier_table.setStyle(TableStyle([
        ('ALIGN',         (0, 0), (-1, -1), 'LEFT'),
        ('FONTSIZE',      (0, 0), (-1, -1), 10),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 6),
    ]))

    left_elements = []
    if logo_img is not None:
        left_elements.append(logo_img)
        left_elements.append(Spacer(1, 0.05 * inch))
    left_elements.append(carrier_table)

    left_content = Table([[elem] for elem in left_elements], colWidths=[2 * inch])
    left_content.setStyle(TableStyle([
        ('ALIGN',  (0, 0), (0, -1), 'LEFT'),
        ('VALIGN', (0, 0), (0, -1), 'TOP'),
    ]))

    header_table = Table([[left_content, settlement_table]], colWidths=[4 * inch, 4 * inch])
    header_table.setStyle(TableStyle([
        ('ALIGN',  (0, 0), (0, 0), 'LEFT'),
        ('ALIGN',  (1, 0), (1, 0), 'RIGHT'),
        ('VALIGN', (0, 0), (1, 0), 'TOP'),
    ]))
    elements.append(header_table)
    elements.append(Spacer(1, 0.25 * inch))
    elements.append(Table(
        [[Paragraph(f"<b>{statement.driver.full_name}</b>", styles['Normal'])]],
        colWidths=[6.2 * inch]
    ))
    elements.append(Spacer(1, 8))
    settlement = data.get('settlement') or {}
    summary_data = [
        ["Earnings", format_currency(data.get('gross_amount', "0.00")),
        "YTD Earnings", format_currency(settlement.get('ytd_earnings', "0.00"))], 
        ["Advances", format_currency(data.get('total_advances', "0.00")),
        "YTD Advances", format_currency(settlement.get('ytd_advances', "0.00"))],
        ["Deductions", format_currency(data.get('total_deduction', "0.00")),
        "YTD Deductions", format_currency(settlement.get('ytd_deductions', "0.00"))],
        ["Total",    format_currency(data.get('total_amount', "0.00")),
        "YTD Total", format_currency(settlement.get('ytd_total', "0.00"))],        
    ]
    summary_table = Table(summary_data, colWidths=[1.5 * inch, 1.5 * inch, 1.5 * inch, 1.5 * inch])
    summary_table.setStyle(TableStyle([
        ('GRID',          (0, 0), (-1, -1), 0.5, colors.black),
        ('ALIGN',         (1, 0), (1, -1),  'RIGHT'),
        ('ALIGN',         (3, 0), (3, -1),  'RIGHT'),
        ('FONTSIZE',      (0, 0), (-1, -1), 10),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
        ('TOPPADDING',    (0, 0), (-1, -1), 4),
    ]))
    elements.append(summary_table)
    elements.append(Spacer(1, 12))
    elements.append(Paragraph(f"Earnings - {statement.driver.full_name}", title_style))
    driver_info = data.get('driver') or {}
    driver_name = driver_info.get('name', 'N/A')
    loads = data.get('loads', [])
    if loads:
        loads_data = [["Load #", "Shipment #", "Dates", "Address", "Payment"]]
        total_pay = Decimal('0')

        for item in loads:
            load_info   = item.get('load', {})
            pickup_raw  = load_info.get('pickup_date', '')
            drop_raw    = load_info.get('drop_date', '')
            pickup_date = pickup_raw.strftime('%m/%d/%Y') if hasattr(pickup_raw, 'strftime') else str(pickup_raw)
            drop_date   = drop_raw.strftime('%m/%d/%Y')   if hasattr(drop_raw,   'strftime') else str(drop_raw)

            pay = Decimal(str(item.get('load', {}).get('driver_pay') or 0))
            total_pay += pay

            loads_data.append([
                Paragraph(str(load_info.get('load_number', '')), styles['Normal']),
                load_info.get('shipment', ''),
                f"{pickup_date}\n{drop_date}",
                Paragraph(str(load_info.get('name') or ''), styles['Normal']),
                format_currency(pay),
            ])

        loads_data.append(["Total", "", "", "", format_currency(total_pay)])

        loads_table = Table(
            loads_data,
            colWidths=[1.0 * inch, 1.1 * inch, 1.2 * inch, 3.5 * inch, 1.2 * inch]
        )
        loads_table.setStyle(TableStyle([
            ('GRID',          (0, 0), (-1, -2), 0.5, colors.black),
            ('LINEABOVE',     (0, -1), (-1, -1), 0.5, colors.black),
            ('LINEBELOW',     (0, -1), (-1, -1), 0.5, colors.black),
            ('BACKGROUND',    (0, 0), (-1, 0),  colors.lightgrey),
            ('ALIGN',         (0, 0), (-1, 0),  'CENTER'),
            ('ALIGN',         (0, 1), (3, -1),  'LEFT'),
            ('ALIGN',         (4, 1), (4, -1),  'RIGHT'),
            ('FONTSIZE',      (0, 0), (-1, -1), 9),
            ('FONTNAME',      (0, -1), (-1, -1), 'Helvetica-Bold'),
            ('VALIGN',        (0, 0), (-1, -1), 'TOP'),
        ]))
        elements.append(loads_table)
    else:
        elements.append(Paragraph("No loads for this period.", styles['Normal']))

    elements.append(Spacer(1, 12))
    elements.append(Paragraph("Deductions", title_style))
    if data.get('deductions'):
        deduction_data = [["Driver", "Description", "Deduction Type", "Amount", "Fee"]]
        total_deductions = Decimal('0.00')
        total_fees = Decimal('0.00')
        for deduction in data['deductions']:
            deduction_info = deduction.get('deduction', {})
            amount = Decimal(str(deduction_info.get('amount', 0)))
            try:
                fee_value = deduction_info.get('fee')
                fee = Decimal(str(fee_value)) if fee_value is not None else Decimal('0.00')
            except (TypeError, InvalidOperation):
                fee = Decimal('0.00')
            total_deductions += amount
            total_fees += fee
            description = deduction_info.get('notes') or ''
            wrapped_description = Paragraph(description, styles["Normal"])
            deduction_data.append([
                f"{driver_name}",
                wrapped_description,
                deduction_info.get('type', ''),
                format_currency(Decimal(-amount)),
                format_currency(Decimal(-fee)) if fee else "$0.00"
            ])
        deduction_data.append([
            "", "", "", format_currency(-total_deductions), format_currency(-total_fees)
        ])
        deductions_table = Table(deduction_data,
                                 colWidths=[2 * inch, 2.4 * inch, 1.8 * inch, 1.1 * inch, 0.7 * inch])
        deductions_table.setStyle(TableStyle([
            ('GRID', (0, 0), (-1, -1), 0.5, colors.black),
            ('BACKGROUND', (0, 0), (-1, 0), colors.lightgrey),
            ('ALIGN', (0, 0), (-1, 0), 'CENTER'),
            ('ALIGN', (0, 1), (2, -1), 'LEFT'),
            ('ALIGN', (3, 1), (5, -1), 'RIGHT'),
            ('FONTSIZE', (0, 0), (-1, -1), 9),
            ('SPAN', (0, -1), (2, -1)),
        ]))
        elements.append(deductions_table)
    else:
        elements.append(Paragraph("No deductions for this period", styles['Normal']))

    elements.append(Spacer(1, 12))
    total_amount_data  = [["Total Amount:", format_currency(data.get('total_amount', '0.00'))]]
    total_amount_table = Table(total_amount_data, colWidths=[6 * inch, 2 * inch])
    total_amount_table.setStyle(TableStyle([
        ('ALIGN',    (0, 0), (0, 0), 'RIGHT'),
        ('ALIGN',    (1, 0), (1, 0), 'RIGHT'),
        ('FONTSIZE', (0, 0), (-1, -1), 11),
        ('FONTNAME', (0, 0), (1,  0),  'Helvetica-Bold'),
    ]))
    elements.append(total_amount_table)
    elements.append(Spacer(1, 24))
    elements.append(Paragraph(
        "Generated by BoxManage",
        ParagraphStyle('Footer', parent=styles['Normal'], alignment=1, fontSize=8, textColor=colors.gray)
    ))

    doc.build(elements)
    with open(temp_path, 'rb') as f:
        saved_path = default_storage.save(file_path, File(f))
    os.unlink(temp_path)

    statement.pdf_file = saved_path
    statement.save(update_fields=['pdf_file'])
    try:
        default_storage._get_storage().bucket.Object(saved_path).load()
    except Exception:
        pass

    return saved_path

def iso_year_date_range(iso_year: int):
    start = date.fromisocalendar(iso_year, 1, 1)
    try:
        end = date.fromisocalendar(iso_year, 53, 7)
    except ValueError:
        end = date.fromisocalendar(iso_year, 52, 7)
    return start, end