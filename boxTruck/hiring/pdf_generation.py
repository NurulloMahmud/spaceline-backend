import os
from io import BytesIO
from xml.sax.saxutils import escape as xml_escape

from pypdf import PdfReader, PdfWriter
from pypdf.generic import BooleanObject, NameObject
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import inch
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle

W9_TEMPLATE_PATH = os.path.join(os.path.dirname(__file__), 'pdf_templates', 'w9_template.pdf')

# W-9 tax classification checkboxes are literally named "1".."7" on the template,
# matching company_type 1:1 (confirmed against the real form's field names).
W9_TAX_CLASSIFICATION_BOXES = {str(i) for i in range(1, 8)}


def fill_w9(data):
    """Fill the IRS Form W-9 template with contractor/company info.

    `data` keys used: company_name, company_doing_business, company_address,
    company_city, company_state, company_zip, company_employer_id, company_type
    (string "1".."7"), payee_code, fatca_reporting_code.
    Returns PDF bytes. Signature/date are intentionally left blank here — those
    are filled during the later review-and-sign step, not at generation time.
    """
    reader = PdfReader(W9_TEMPLATE_PATH)
    writer = PdfWriter()
    writer.append(reader)

    ein = (data.get('company_employer_id') or '').replace('-', '').strip()
    city_state_zip = ', '.join(
        part for part in [data.get('company_city'), data.get('company_state')] if part
    )
    if data.get('company_zip'):
        city_state_zip = f"{city_state_zip} {data['company_zip']}".strip()

    # Every text field on the template is set explicitly, including ones we
    # don't use (blanked to ''), so nothing carries over a stale appearance
    # from whatever this template last had filled in.
    text_fields = {
        'company_name': data.get('company_name', ''),
        'company_doing_business': data.get('company_doing_business', ''),
        'other': '',
        'limited': '',
        'exempt_payee_code': data.get('payee_code') or '',
        'exempt_reporting_code': data.get('fatca_reporting_code') or '',
        'company_address': data.get('company_address', ''),
        'city_state_zip': city_state_zip,
        'account_number': '',
        'Request_name_and_address': '',
        'number_to_give_the_request': '',
        'employer_id1': ein[:2],
        'employer_id2': ein[2:9],
        'company_ssn1': '',
        'company_ssn2': '',
        'company_ssn3': '',
        # Signature date is filled during the later review-and-sign step, not here.
        'date': '',
    }

    company_type = str(data.get('company_type') or '')
    # Explicitly set every classification box, not just the selected one: the
    # template's checkbox appearance state can have stale "/On" values baked in
    # from whatever it last had filled, and those aren't cleared just by
    # leaving a field out of the update dict.
    checkbox_fields = {
        box: ('/On' if box == company_type else '/Off')
        for box in W9_TAX_CLASSIFICATION_BOXES
    }

    for page in writer.pages:
        writer.update_page_form_field_values(page, text_fields)
        writer.update_page_form_field_values(page, checkbox_fields)

    if writer._root_object.get('/AcroForm') is not None:
        writer._root_object['/AcroForm'][NameObject('/NeedAppearances')] = BooleanObject(True)

    buffer = BytesIO()
    writer.write(buffer)
    buffer.seek(0)
    return buffer


def generate_contract(company, context):
    """Render a company's contract_template_text (a str.format() template) to PDF.

    Raises ValueError if the company has no template configured yet, so callers
    fail loudly instead of silently producing a broken/empty contract. The
    template body should not include a signature block — one is always
    appended as a proper two-column table (plain-text spacing collapses in
    Paragraph flow and can't be relied on for alignment).
    """
    if not company.contract_template_text:
        raise ValueError(f"Company '{company.name}' has no contract_template_text configured.")

    # Values get interpolated into a reportlab Paragraph, which parses its
    # input as a small XML/HTML dialect, so raw '&', '<', '>' (e.g. "J&J")
    # must be escaped before substitution or they render mangled.
    full_context = {
        'company_name': company.name or '',
        'company_address': company.address or '',
        'contract_signer_name': company.contract_signer_name or '',
        'contract_signer_title': company.contract_signer_title or '',
        **context,
    }
    full_context = {k: xml_escape(str(v)) for k, v in full_context.items()}
    body = company.contract_template_text.format(**full_context)

    buffer = BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=letter,
        rightMargin=0.85 * inch,
        leftMargin=0.85 * inch,
        topMargin=0.85 * inch,
        bottomMargin=0.85 * inch,
    )

    styles = getSampleStyleSheet()
    styles.add(ParagraphStyle(name='ContractTitle', parent=styles['Normal'],
                               fontSize=14, fontName='Helvetica-Bold',
                               spaceAfter=10, alignment=1))
    styles.add(ParagraphStyle(name='ContractBody', parent=styles['Normal'],
                               fontSize=10, leading=14, spaceAfter=10))
    styles.add(ParagraphStyle(name='SignatureCell', parent=styles['Normal'],
                               fontSize=10, leading=13))

    elements = []
    for i, paragraph in enumerate(body.split('\n\n')):
        paragraph = paragraph.strip()
        if not paragraph:
            continue
        style = styles['ContractTitle'] if i == 0 else styles['ContractBody']
        elements.append(Paragraph(paragraph.replace('\n', '<br/>'), style))
        elements.append(Spacer(1, 0.05 * inch))

    elements.append(Spacer(1, 0.3 * inch))
    p = styles['SignatureCell']
    blank = '_' * 25
    signature_rows = [
        [Paragraph('<b>FOR COMPANY:</b>', p), Paragraph('<b>FOR CONTRACTOR:</b>', p)],
        [Paragraph(f"{full_context['contract_signer_name']}<br/>Sign Name", p),
         Paragraph(f"{blank}<br/>Sign Name", p)],
        [Paragraph(f"{full_context['contract_signer_name']}<br/>Print name", p),
         Paragraph(f"{blank}<br/>Print name", p)],
        [Paragraph(f"{full_context['contract_signer_title']}<br/>Title", p),
         Paragraph(f"{blank}<br/>Title", p)],
        [Paragraph(f"{blank}<br/>Date", p), Paragraph(f"{blank}<br/>Date", p)],
    ]
    signature_table = Table(signature_rows, colWidths=[3 * inch, 3 * inch])
    signature_table.setStyle(TableStyle([
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ('TOPPADDING', (0, 0), (-1, -1), 10),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 10),
        ('LEFTPADDING', (0, 0), (-1, -1), 0),
    ]))
    elements.append(signature_table)

    doc.build(elements)
    buffer.seek(0)
    return buffer
