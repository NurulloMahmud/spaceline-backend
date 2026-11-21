"""Sending loads to RTS Financial as one purchase.

RTS takes a purchase over FTPS: one PDF per load (our invoice page, then the POD/BOL, other files
and the rate con, as the old billing upload did) and one CSV listing every load, which is what
makes the purchase. The CSV goes last, so RTS has no purchase until every PDF is up.

A purchase is all or nothing. Only when the CSV is in do the invoices move to Invoiced and the
loads to the Invoiced load status; any failure leaves every invoice as it was and says why.
"""
import csv
import logging
import os
import socket
import ssl
from datetime import timedelta
from ftplib import FTP, FTP_TLS, all_errors
from io import BytesIO, StringIO

import requests
from django.conf import settings
from django.core.files.base import ContentFile
from django.db import transaction
from django.utils import timezone
from PIL import Image
from pypdf import PdfReader, PdfWriter

from billing.models import LoadStatus
from users.models import Company

from .models import Invoice, RtsPurchase, RtsPurchaseLine
from .services import is_dead_load, log_invoice, set_load_status

logger = logging.getLogger(__name__)

try:  # phone photos
    from pillow_heif import register_heif_opener

    register_heif_opener()
except Exception:  # noqa: BLE001 - HEIC support is optional
    pass

TRIES = 3
STALE_AFTER = timedelta(minutes=15)
CSV_NAME = 'rts_invoices.csv'  # the name the old billing upload used with RTS
IMAGE_EDGE = 1700  # longest side of a photo page, about 200 dpi on letter paper


class BadFile(Exception):
    """A load's file can't be read or is missing: the purchase stops before anything is sent."""

    def __init__(self, invoice, label):
        super().__init__(f'{label} can\'t be read')
        self.invoice = invoice
        self.label = label


class Unreachable(Exception):
    """RTS did not take the connection or the login."""


class UploadFailed(Exception):
    def __init__(self, message, unsure=False):
        super().__init__(message)
        self.unsure = unsure


# --- what a load needs ---------------------------------------------------------------------------

def file_kind(name):
    """rc, pod (POD or BOL), invoice (an old generated invoice, not sent again) or other."""
    n = str(name or '').strip().lower()
    if n in ('rc', 'ratecon', 'rate con', 'signed_rate_con') or 'rate' in n:
        return 'rc'
    if 'pod' in n or 'bol' in n:
        return 'pod'
    if 'invoice' in n:
        return 'invoice'
    return 'other'


def blockers(invoice):
    """Why this invoice can't go to RTS yet; empty when it can. The window shows the same list."""
    load = invoice.load
    out = []
    if is_dead_load(load):
        out.append('Rejected load')
    if not (load.load_number or '').strip():
        out.append('No ref #')
    if not (invoice.amount and invoice.amount > 0):
        out.append('No amount')
    if not load.broker_id:
        out.append('No debtor')
    kinds = {file_kind(f.name) for f in load.loadfile_set.all() if f.file or f.rc_url}
    if 'rc' not in kinds:
        out.append('No rate con')
    if 'pod' not in kinds:
        out.append('No POD/BOL')
    return out


def account(account_type):
    a = settings.RTS_ACCOUNTS.get(account_type) or {}
    return a if a.get('login') and a.get('password') else None


def account_types():
    note = {RtsPurchase.EXPRESS: 'Waiting on RTS: how Express purchases are sent'}
    return [{'key': k, 'label': l, 'enabled': bool(account(k)), 'note': '' if account(k) else note.get(k, 'Not set up')} for k, l in RtsPurchase.ACCOUNT_TYPES]


def sent_today():
    start = timezone.localtime().replace(hour=0, minute=0, second=0, microsecond=0)
    return RtsPurchase.objects.filter(status=RtsPurchase.SUBMITTED, submitted_at__gte=start).count()


def mark_stale():
    """A job that died mid-way (a restart) must not look like it is still sending."""
    cut = timezone.now() - STALE_AFTER
    RtsPurchase.objects.filter(status=RtsPurchase.SENDING, updated_at__lt=cut).update(
        status=RtsPurchase.FAILED, error_kind='stale',
        error='Sending stopped partway (the server restarted). RTS may have part of it. Check RTS Pro before sending these loads again.',
    )


# --- the PDF for one load --------------------------------------------------------------------------

def invoice_page(invoice, company):
    """Our invoice, payable to RTS Financial, as the old billing upload drew it."""
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_RIGHT
    from reportlab.lib.pagesizes import letter
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import inch
    from reportlab.platypus import Image as RLImage
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
    from xml.sax.saxutils import escape

    load = invoice.load
    broker = load.broker
    buf = BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=letter, rightMargin=0.75 * inch, leftMargin=0.75 * inch, topMargin=0.75 * inch, bottomMargin=0.75 * inch)
    st = getSampleStyleSheet()
    right = ParagraphStyle('R', parent=st['Normal'], alignment=TA_RIGHT, fontSize=12)
    big = ParagraphStyle('B', parent=right, fontSize=24, leading=29, fontName='Helvetica-Bold')
    head = ParagraphStyle('H', parent=st['Normal'], fontSize=14, spaceAfter=6, fontName='Helvetica-Bold')
    sec = ParagraphStyle('S', parent=st['Normal'], fontSize=11, fontName='Helvetica-Bold', spaceAfter=3)
    body = st['Normal']
    amount = f'${invoice.amount:,.2f}'
    e = lambda v: escape(str(v)) if v not in (None, '') else 'N/A'  # noqa: E731

    top = Table([[Paragraph('<b>PAYABLE TO:</b>', head), Paragraph('<b>RTS FINANCIAL</b>', head)],
                 ['', Paragraph(amount, big)]], colWidths=[4 * inch, 2.5 * inch])
    top.setStyle(TableStyle([('ALIGN', (1, 0), (1, -1), 'RIGHT'), ('VALIGN', (0, 0), (-1, -1), 'TOP'), ('BOTTOMPADDING', (0, 0), (-1, -1), 10)]))
    grid = Table([
        [Paragraph('<b>Invoice #</b>', sec), Paragraph(e(load.shipment), body)],
        [Paragraph('<b>Invoice Date</b>', sec), Paragraph(timezone.localdate().strftime('%m/%d/%Y'), body)],
        [Paragraph('<b>Reference (Load or W/O)</b>', sec), Paragraph(e(load.load_number), body)],
        [Paragraph('<b>Terms</b>', sec), Paragraph('30 days', body)],
    ], colWidths=[2.2 * inch, 2.5 * inch])
    grid.setStyle(TableStyle([('GRID', (0, 0), (-1, -1), 0.5, colors.black), ('BACKGROUND', (0, 0), (0, -1), colors.lightgrey),
                              ('TOPPADDING', (0, 0), (-1, -1), 8), ('BOTTOMPADDING', (0, 0), (-1, -1), 8), ('LEFTPADDING', (0, 0), (-1, -1), 8)]))
    place = ', '.join(x for x in [broker.city, broker.state, broker.zipcode] if x) if broker else ''
    bill_to = '<br/>'.join(escape(x) for x in [broker.address if broker else '', place] if x) or 'N/A'
    parties = Table([[
        [Paragraph('<b>Assigned For:</b>', sec), Paragraph(e(company.name), body), Paragraph(e(company.address), body)],
        [Paragraph('<b>BILL TO:</b>', sec), Paragraph(e(broker.name if broker else ''), body), Paragraph(bill_to, body)],
    ]], colWidths=[3.25 * inch, 3.25 * inch])
    parties.setStyle(TableStyle([('VALIGN', (0, 0), (-1, -1), 'TOP'), ('LEFTPADDING', (0, 0), (-1, -1), 0)]))
    rate = Table([[Paragraph('<b>(USD) Total Rate</b>', body), Paragraph(f'<b>{amount}</b>', right)]], colWidths=[4 * inch, 2.5 * inch])
    rate.setStyle(TableStyle([('GRID', (0, 0), (-1, -1), 0.5, colors.black), ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
                              ('TOPPADDING', (0, 0), (-1, -1), 10), ('BOTTOMPADDING', (0, 0), (-1, -1), 10), ('LEFTPADDING', (0, 0), (-1, -1), 8), ('RIGHTPADDING', (0, 0), (-1, -1), 8)]))
    parts = [top, Spacer(1, 0.15 * inch), grid, Spacer(1, 0.4 * inch), parties, Spacer(1, 0.4 * inch), Paragraph('<b>RATES AND CHARGES</b>', sec), Spacer(1, 0.15 * inch), rate,
             Spacer(1, 0.3 * inch), Paragraph('<b>Notes:</b>', sec), Spacer(1, 0.5 * inch)]
    stamp = os.path.join(settings.BASE_DIR, 'media', 'logos', 'RTS.jpg')
    if os.path.exists(stamp):
        parts.append(RLImage(stamp, width=4.5 * inch, height=1.8 * inch, kind='proportional'))
    doc.build(parts)
    return buf.getvalue()


def read_file(load_file):
    if load_file.file:
        with load_file.file.open('rb') as fh:
            return fh.read()
    r = requests.get(load_file.rc_url, timeout=30)
    r.raise_for_status()
    return r.content


def add_document(writer, data, name):
    """Append a PDF or a photo to the packet. Raises on anything unreadable."""
    ext = os.path.splitext(str(name).split('?')[0])[-1].lower()
    if data[:4] == b'%PDF' or ext == '.pdf':
        reader = PdfReader(BytesIO(data), strict=False)
        if not reader.pages:
            raise ValueError('empty PDF')
        for page in reader.pages:
            writer.add_page(page)
        return
    img = Image.open(BytesIO(data))
    img.load()
    img = img.convert('L')
    img.thumbnail((IMAGE_EDGE, IMAGE_EDGE))
    out = BytesIO()
    img.save(out, format='PDF', resolution=200.0)
    for page in PdfReader(BytesIO(out.getvalue())).pages:
        writer.add_page(page)


LABEL = {'rc': 'Rate con', 'pod': 'POD/BOL', 'other': 'File'}


def build_packet(invoice, company):
    """Our invoice page, then POD/BOL, other files, rate con: one PDF RTS files under the load."""
    writer = PdfWriter()
    for page in PdfReader(BytesIO(invoice_page(invoice, company))).pages:
        writer.add_page(page)
    files = [f for f in invoice.load.loadfile_set.all() if (f.file or f.rc_url) and file_kind(f.name) != 'invoice']
    order = {'pod': 0, 'other': 1, 'rc': 2}
    for f in sorted(files, key=lambda f: (order[file_kind(f.name)], f.id)):
        label = LABEL[file_kind(f.name)] if file_kind(f.name) != 'other' else f'{f.name or "File"} file'
        try:
            add_document(writer, read_file(f), f.file.name if f.file else f.rc_url)
        except Exception as exc:  # noqa: BLE001 - any unreadable file stops the purchase
            logger.warning('rts packet: %s for invoice %s unreadable: %s', label, invoice.id, exc)
            raise BadFile(invoice, label) from exc
    out = BytesIO()
    writer.write(out)
    return out.getvalue(), len(writer.pages)


def build_csv(purchase, login):
    out = StringIO()
    w = csv.writer(out)
    w.writerow(['Client', 'Invoice#', 'DebtorNo', 'Debtor Name', 'Load #', 'InvDate', 'InvAmt'])
    day = timezone.localdate().strftime('%m/%d/%Y')
    for line in purchase.lines.all():
        w.writerow([login, line.invoice_number, line.debtor, line.debtor, line.reference, day, f'{line.amount:.2f}'])
    return out.getvalue().encode('utf-8')


# --- the RTS file server -----------------------------------------------------------------------------

def connect(acct):
    """An open, logged-in session. Tests replace this."""
    try:
        if settings.RTS_FTP_TLS:
            ftp = FTP_TLS(timeout=30)
            ftp.connect(settings.RTS_HOST, settings.RTS_PORT)
            ftp.login(acct['login'], acct['password'])
            ftp.prot_p()
        else:
            ftp = FTP(timeout=30)
            ftp.connect(settings.RTS_HOST, settings.RTS_PORT)
            ftp.login(acct['login'], acct['password'])
        return ftp
    except (*all_errors, socket.timeout, ssl.SSLError) as exc:
        raise Unreachable(str(exc)) from exc


class Session:
    """Uploads with three tries each, reconnecting between tries."""

    def __init__(self, acct):
        self.acct = acct
        self.ftp = connect(acct)

    def put(self, name, data, last=False):
        err = None
        for attempt in range(TRIES):
            try:
                if self.ftp is None:
                    self.ftp = connect(self.acct)
                self.ftp.storbinary(f'STOR {name}', BytesIO(data))
                return
            except Unreachable as exc:
                err = exc
                self.ftp = None
            except (*all_errors, socket.timeout, ssl.SSLError) as exc:
                err = exc
                self.close()
            logger.warning('rts upload %s try %s failed: %s', name, attempt + 1, err)
        if last:
            raise UploadFailed(f'The purchase list did not upload: {err}', unsure=True)
        raise UploadFailed(f'{name} did not upload: {err}')

    def close(self):
        try:
            if self.ftp is not None:
                self.ftp.quit()
        except Exception:  # noqa: BLE001
            pass
        self.ftp = None


# --- the job ---------------------------------------------------------------------------------------

def set_step(purchase, step):
    purchase.step = step
    purchase.save(update_fields=['step', 'updated_at'])


def fail(purchase, kind, message, bad_invoice=None):
    purchase.status = RtsPurchase.FAILED
    purchase.error_kind = kind
    purchase.error = message
    purchase.bad_invoice = bad_invoice
    purchase.save(update_fields=['status', 'error_kind', 'error', 'bad_invoice', 'updated_at'])


def run(purchase_id):
    """Build, upload, submit. Runs in a background thread; never raises."""
    purchase = RtsPurchase.objects.select_related('created_by').get(pk=purchase_id)
    lines = list(purchase.lines.select_related('invoice__load__broker', 'invoice__load__status').prefetch_related('invoice__load__loadfile_set'))
    acct = account(purchase.account_type)
    if not acct:
        return fail(purchase, 'down', f'{purchase.get_account_type_display()} is not set up for RTS yet. Nothing was sent.')
    company = Company.current()
    packets = []
    try:
        set_step(purchase, RtsPurchase.PREPARING)
        for line in lines:
            data, pages = build_packet(line.invoice, company)
            packets.append((line, data, pages))
    except BadFile as exc:
        return fail(purchase, 'bad_file', f'{exc.label} can\'t be read', bad_invoice=exc.invoice)
    except Exception as exc:  # noqa: BLE001
        logger.exception('rts purchase %s: preparing failed', purchase.id)
        return fail(purchase, 'bad_file', f'The PDFs could not be made: {exc}')

    set_step(purchase, RtsPurchase.UPLOADING)
    session = None
    try:
        session = Session(acct)
        for line, data, _ in packets:
            session.put(f'{line.invoice_number}.pdf', data)
        set_step(purchase, RtsPurchase.SUBMITTING)
        sheet = build_csv(purchase, acct['login'])
        session.put(CSV_NAME, sheet, last=True)
    except Unreachable as exc:
        logger.warning('rts purchase %s: RTS unreachable: %s', purchase.id, exc)
        return fail(purchase, 'down', "Couldn't reach RTS. The purchase was not submitted and nothing changed.")
    except UploadFailed as exc:
        logger.warning('rts purchase %s: %s', purchase.id, exc)
        if exc.unsure:
            return fail(purchase, 'unsure', 'The purchase list did not finish uploading. RTS may or may not have it. Check RTS Pro before sending these loads again.')
        return fail(purchase, 'partial', 'Upload to RTS stopped partway. The purchase list was not sent, so RTS has no purchase. Try again.')
    finally:
        if session:
            session.close()

    submitted(purchase, packets, sheet)


def submitted(purchase, packets, sheet):
    """The purchase is in: keep what was sent, then move every load to Invoiced in one go."""
    now = timezone.now()
    label = purchase.get_account_type_display()
    user = purchase.created_by
    for line, data, pages in packets:
        try:
            line.packet.save(f'{purchase.id}/{line.invoice_number}.pdf', ContentFile(data), save=False)
        except Exception:  # noqa: BLE001 - a copy we keep, not what RTS needed
            logger.exception('rts purchase %s: keeping the packet for %s failed', purchase.id, line.invoice_number)
        line.pages = pages
        line.save(update_fields=['packet', 'pages'])
    try:
        purchase.csv_file.save(f'{purchase.id}/{CSV_NAME}', ContentFile(sheet), save=False)
    except Exception:  # noqa: BLE001
        logger.exception('rts purchase %s: keeping the CSV failed', purchase.id)
    invoiced_status = LoadStatus.objects.filter(name__iexact='invoiced').first()
    with transaction.atomic():
        for line, _, _ in packets:
            f = Invoice.objects.select_for_update(of=('self',)).select_related('load__status').get(pk=line.invoice_id)
            log_invoice(f, user, f'Sent to RTS in purchase #{purchase.id} ({label})')
            if f.status == Invoice.GOOD:
                was = f.get_status_display()
                f.status = Invoice.INVOICED
                f.invoiced_at = now
                f.save(update_fields=['status', 'invoiced_at', 'updated_at'])
                log_invoice(f, user, f'{was} to {f.get_status_display()}')
            if invoiced_status and f.load.status_id != invoiced_status.id:
                set_load_status(f.load, invoiced_status, user)
        purchase.status = RtsPurchase.SUBMITTED
        purchase.submitted_at = now
        purchase.error = ''
        purchase.error_kind = ''
        purchase.save(update_fields=['status', 'submitted_at', 'error', 'error_kind', 'csv_file', 'updated_at'])
    logger.info('rts purchase %s submitted: %s loads, %s', purchase.id, len(packets), purchase.total)


def safe_run(purchase_id):
    try:
        run(purchase_id)
    except Exception as exc:  # noqa: BLE001 - the request is long gone
        logger.exception('rts purchase %s crashed', purchase_id)
        RtsPurchase.objects.filter(pk=purchase_id, status=RtsPurchase.SENDING).update(
            status=RtsPurchase.FAILED, error_kind='unsure', error=f'Sending stopped with an error ({exc}). Check RTS Pro before sending these loads again.')
