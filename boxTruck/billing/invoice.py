from io import BytesIO
from datetime import datetime
from reportlab.lib.pagesizes import letter
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Image, Table, TableStyle
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import inch
from reportlab.lib import colors
from reportlab.lib.enums import TA_RIGHT, TA_CENTER
import os
from config import settings

def get_normalized_carrier_name(name):
    name = name.upper()
    if name.startswith("PRIORITY"):
        return "PRIORITY FREIGHT LLC"
    elif name.startswith("SHIPLUXE"):
        return "SHIPLUXE LLC"
    elif name.startswith("ROADPULSE LOGISTICS"):
        return "ROADPULSE LOGISTICS LLC"
    return name


def generate_rts_invoice(load):
    buffer = BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=letter,
        rightMargin=0.75 * inch,
        leftMargin=0.75 * inch,
        topMargin=0.75 * inch,
        bottomMargin=0.75 * inch
    )

    styles = getSampleStyleSheet()
    styles.add(ParagraphStyle(
        name='RightAlign',
        parent=styles['Normal'],
        alignment=TA_RIGHT,
        fontSize=12
    ))
    styles.add(ParagraphStyle(
        name='CenterAlign',
        parent=styles['Normal'],
        alignment=TA_CENTER,
        fontSize=12
    ))
    styles.add(ParagraphStyle(
        name='BoldHeader',
        parent=styles['Normal'],
        fontSize=14,
        spaceAfter=6,
        fontName='Helvetica-Bold'
    ))
    styles.add(ParagraphStyle(
        name='SectionHeader',
        parent=styles['Normal'],
        fontSize=11,
        fontName='Helvetica-Bold',
        spaceAfter=3
    ))

    def wrap_text(text, max_width=15):
        if not text or len(str(text)) <= max_width:
            return str(text)

        text = str(text)
        wrapped_lines = []
        while len(text) > max_width:
            break_point = max_width
            for i in range(max_width, 0, -1):
                if text[i] in [' ', '-', '_']:
                    break_point = i + 1
                    break

            wrapped_lines.append(text[:break_point].rstrip())
            text = text[break_point:].lstrip()

        if text:
            wrapped_lines.append(text)

        return "<br/>".join(wrapped_lines)

    elements = []
    header_data = [
        [
            "",
            Paragraph(
                f"<font size=24><b>${load.carrier_pay:.2f}</b></font>" if load.carrier_pay else "<font size=24><b>$0.00</b></font>",
                styles['RightAlign'])
        ]
    ]
    header_data[0][0] = Paragraph("<b>PAYABLE TO:</b>", styles['BoldHeader'])
    header_data[0][1] = Paragraph("<b>RTS FINANCIAL</b>", styles['BoldHeader'])
    header_table = Table(header_data, colWidths=[4 * inch, 2.5 * inch])
    header_table.setStyle(TableStyle([
        ('ALIGN', (0, 0), (0, 0), 'LEFT'),
        ('ALIGN', (1, 0), (1, 0), 'RIGHT'),
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ('TOPPADDING', (0, 0), (-1, -1), 0),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 20),
    ]))
    elements.append(header_table)

    invoice_data = [
        [Paragraph("<b>Invoice #</b>", styles['SectionHeader']),
         Paragraph(wrap_text(load.shipment or "94252"), styles['Normal'])],
        [Paragraph("<b>Invoice Date</b>", styles['SectionHeader']),
         Paragraph(datetime.now().strftime('%m/%d/%Y'), styles['Normal'])],
        [Paragraph("<b>Reference(Load or W/O)</b>", styles['SectionHeader']),
         Paragraph(wrap_text(load.load_number or "903-0058-0425"), styles['Normal'])],
        [Paragraph("<b>Terms</b>", styles['SectionHeader']),
         Paragraph("30 days", styles['Normal'])],
    ]

    invoice_table = Table(invoice_data, colWidths=[2.2 * inch, 2.5 * inch])
    invoice_table.setStyle(TableStyle([
        ('ALIGN', (0, 0), (-1, -1), 'LEFT'),
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ('FONTSIZE', (0, 0), (-1, -1), 10),
        ('TOPPADDING', (0, 0), (-1, -1), 8),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 8),
        ('LEFTPADDING', (0, 0), (-1, -1), 8),
        ('RIGHTPADDING', (0, 0), (-1, -1), 8),
        ('GRID', (0, 0), (-1, -1), 0.5, colors.black),
        ('BACKGROUND', (0, 0), (0, -1), colors.lightgrey),
    ]))
    elements.append(invoice_table)
    elements.append(Spacer(1, 0.4 * inch))
    company_info_data = [
        [
            [
                Paragraph("<b>Assigned For:</b>", styles['SectionHeader']),
                Paragraph(get_normalized_carrier_name(load.company.name) if load.company else "N/A", styles['Normal']),
                Paragraph(load.company.address if (load.company and load.company.address) else "N/A", styles['Normal'])
            ],
            [
                Paragraph("<b>BILL TO:</b>", styles['SectionHeader']),
                Paragraph(load.broker.name if load.broker else "N/A", styles['Normal']),
                Paragraph(load.broker.address if (load.broker and load.broker.address) else "N/A", styles['Normal'])
            ]
        ]
    ]

    assigned_for_content = [
        [Paragraph("<b>Assigned For:</b>", styles['SectionHeader'])],
        [Paragraph(get_normalized_carrier_name(load.company.name) if load.company else "N/A", styles['Normal'])],
        [Paragraph(load.company.address if (load.company and load.company.address) else "N/A", styles['Normal'])]
    ]

    bill_to_address = ""
    if load.broker:
        if load.broker.address:
            bill_to_address = load.broker.address
            address_parts = []
            if hasattr(load.broker, 'city') and load.broker.city:
                address_parts.append(load.broker.city)
            if hasattr(load.broker, 'state') and load.broker.state:
                address_parts.append(load.broker.state)
            if hasattr(load.broker, 'zipcode') and load.broker.zipcode:
                address_parts.append(load.broker.zipcode)
            if address_parts:
                bill_to_address += "<br/>" + ", ".join(address_parts)
        else:
            address_parts = []
            if hasattr(load.broker, 'city') and load.broker.city:
                address_parts.append(load.broker.city)
            if hasattr(load.broker, 'state') and load.broker.state:
                address_parts.append(load.broker.state)
            if hasattr(load.broker, 'zipcode') and load.broker.zipcode:
                address_parts.append(load.broker.zipcode)

            if address_parts:
                bill_to_address = ", ".join(address_parts)
    bill_to_content = [
        [Paragraph("<b>BILL TO:</b>", styles['SectionHeader'])],
        [Paragraph(load.broker.name if load.broker else "N/A", styles['Normal'])],
        [Paragraph(bill_to_address or "N/A", styles['Normal'])]
    ]
    assigned_table = Table(assigned_for_content, colWidths=[3 * inch])
    assigned_table.setStyle(TableStyle([
        ('ALIGN', (0, 0), (-1, -1), 'LEFT'),
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ('FONTSIZE', (0, 0), (-1, -1), 10),
        ('TOPPADDING', (0, 0), (-1, -1), 4),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
        ('LEFTPADDING', (0, 0), (-1, -1), 0),
    ]))
    bill_table = Table(bill_to_content, colWidths=[3 * inch])
    bill_table.setStyle(TableStyle([
        ('ALIGN', (0, 0), (-1, -1), 'LEFT'),
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ('FONTSIZE', (0, 0), (-1, -1), 10),
        ('TOPPADDING', (0, 0), (-1, -1), 4),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
        ('LEFTPADDING', (0, 0), (-1, -1), 0),
    ]))
    company_table = Table([[assigned_table, bill_table]], colWidths=[3.25 * inch, 3.25 * inch])
    company_table.setStyle(TableStyle([
        ('ALIGN', (0, 0), (-1, -1), 'LEFT'),
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ('TOPPADDING', (0, 0), (-1, -1), 0),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 0),
    ]))
    elements.append(company_table)
    elements.append(Spacer(1, 0.4 * inch))
    elements.append(Paragraph("<b>RATES AND CHARGES</b>", styles['SectionHeader']))
    elements.append(Spacer(1, 0.15 * inch))

    rate_data = [
        [Paragraph("<b>(USD) Total Rate</b>", styles['Normal']),
         Paragraph(f"<b>${load.carrier_pay:.2f}</b>" if load.carrier_pay else "N/A", styles['RightAlign'])]
    ]

    rate_table = Table(rate_data, colWidths=[4 * inch, 2.5 * inch])
    rate_table.setStyle(TableStyle([
        ('ALIGN', (0, 0), (0, 0), 'LEFT'),
        ('ALIGN', (1, 0), (1, 0), 'RIGHT'),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('FONTSIZE', (0, 0), (-1, -1), 11),
        ('TOPPADDING', (0, 0), (-1, -1), 10),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 10),
        ('LEFTPADDING', (0, 0), (-1, -1), 8),
        ('RIGHTPADDING', (0, 0), (-1, -1), 8),
        ('GRID', (0, 0), (-1, -1), 0.5, colors.black),
    ]))
    elements.append(rate_table)
    elements.append(Spacer(1, 0.3 * inch))
    elements.append(Paragraph("<b>Notes:</b>", styles['SectionHeader']))
    elements.append(Spacer(1, 0.5 * inch))
    stamp_path = os.path.join(settings.BASE_DIR, 'media', 'logos', 'RTS.jpg')
    if os.path.exists(stamp_path):
        try:
            stamp = Image(stamp_path, width=4.5 * inch, height=1.8 * inch, kind='proportional')
            elements.append(stamp)
            elements.append(Spacer(1, 0.2 * inch))
        except:
            elements.append(Spacer(1, 1 * inch))
    else:
        elements.append(Spacer(1, 1 * inch))
    doc.build(elements)
    buffer.seek(0)
    return buffer
