from decimal import Decimal, InvalidOperation

from django.db import transaction
from django.db.models import Count, F, Prefetch, Q, Sum
from django.db.models.functions import Coalesce
from django.utils import timezone
from rest_framework import status, views
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.permissions import BasePermission, IsAuthenticated
from rest_framework.response import Response
from django.shortcuts import get_object_or_404

from billing.models import LoadStatus
from payroll.models import Deduction, DeductionHistory, DeductionType
from users.pagination import CustomPagination

from .models import (
    Invoice, InvoiceNote, Payout, PayoutExpense, PayoutNote, ZERO, annotate_debt, available_for, debt_of,
)
from .serializers import deduction_dict, invoice_dict, link_dict, payout_dict, user_dict
from .services import (
    drop_quick_pay_fee, log_invoice, log_payout, money, overpayment_expense, raise_driver_pay, set_load_status, take_expense_everywhere, take_quick_pay_fee,
)

ACCOUNTING = ('billing', 'payroll')
MANAGEMENT = ('management',)


def department(user):
    return (user.department.name if user.department else '').strip().lower()


class IsAccounting(BasePermission):
    """Management, Billing and Payroll run the Accounting page. Nobody else sees it."""
    def has_permission(self, request, view):
        return bool(request.user and request.user.is_authenticated and department(request.user) in ACCOUNTING + MANAGEMENT)


def is_management(user):
    return department(user) in MANAGEMENT


class AccountingPagination(CustomPagination):
    page_size = 40
    max_page_size = 200


def as_money(value, field='amount'):
    try:
        v = Decimal(str(value).replace(',', '').replace('$', '')).quantize(Decimal('0.01'))
    except (InvalidOperation, ValueError, TypeError):
        raise ValidationError({field: 'Must be a number.'})
    if v < 0:
        raise ValidationError({field: 'Cannot be negative.'})
    return v


def ids_param(params, name):
    raw = params.get(name, '')
    out = []
    for part in str(raw).split(','):
        part = part.strip()
        if part:
            if not part.isdigit():
                raise ValidationError({name: 'Must be numbers.'})
            out.append(int(part))
    return out


def search_loads(qs, search, prefix):
    """Invoice numbers ("SH - 10005" or "10005"), load numbers, driver and broker names."""
    search = str(search or '').strip()
    if not search:
        return qs
    q = Q(**{f'{prefix}load_number__icontains': search}) | Q(**{f'{prefix}driver__full_name__icontains': search}) | Q(**{f'{prefix}broker__name__icontains': search})
    digits = search.upper().replace('SH', '').replace('-', '').strip()
    if digits.isdigit():
        q |= Q(**{f'{prefix}shipment': int(digits)})
    return qs.filter(q)


def paginate(view, request, qs, to_dict):
    paginator = AccountingPagination()
    page = paginator.paginate_queryset(qs, request, view=view)
    return paginator.get_paginated_response([to_dict(x) for x in page])


# --- Payouts -------------------------------------------------------------------------------------

PAYOUT_RELATED = ('load', 'load__status', 'load__driver', 'load__broker', 'load__invoice', 'confirmed_by', 'paid_by')


def payout_queryset():
    return Payout.objects.select_related(*PAYOUT_RELATED).prefetch_related(
        'load__loadfile_set',
        Prefetch('expenses', queryset=PayoutExpense.objects.select_related('deduction', 'deduction__type').prefetch_related('deduction__payout_links__payout'))
    ).annotate(notes_n=Count('notes', distinct=True))


def payout_filters(request, qs, with_status=True):
    p = request.query_params
    if with_status and p.get('status'):
        qs = qs.filter(status=p['status'])
    load_statuses = ids_param(p, 'load_status')
    if load_statuses:
        qs = qs.filter(load__status_id__in=load_statuses)
    if p.get('driver'):
        qs = qs.filter(load__driver_id=p['driver'])
    if p.get('payment_type'):
        qs = qs.filter(payment_type=p['payment_type'])
    if p.get('pickup_from'):
        qs = qs.filter(load__pickup_date__date__gte=p['pickup_from'])
    if p.get('pickup_to'):
        qs = qs.filter(load__pickup_date__date__lte=p['pickup_to'])
    return search_loads(qs, p.get('search'), 'load__')


class PayoutListView(views.APIView):
    permission_classes = [IsAuthenticated, IsAccounting]

    def get(self, request):
        qs = payout_filters(request, payout_queryset()).order_by(F('load__pickup_date').desc(nulls_last=True), '-id')
        return paginate(self, request, qs, payout_dict)


class PayoutSummaryView(views.APIView):
    """The band and the tab counts, under the list's filters except status."""
    permission_classes = [IsAuthenticated, IsAccounting]

    def get(self, request):
        base = payout_filters(request, Payout.objects.all(), with_status=False)
        # amounts are gross minus the links, so the sums come from the two aggregates
        def totals(q):
            rows = q.annotate(applied=Coalesce(Sum('expenses__amount'), ZERO)).values_list('gross', 'applied')
            return sum((max(ZERO, g - a) for g, a in rows), ZERO), q.count()
        pending, pending_n = totals(base.filter(status=Payout.PENDING))
        confirmed, confirmed_n = totals(base.filter(status=Payout.CONFIRMED))
        paid_n = base.filter(status=Payout.PAID).count()
        week = timezone.now() - timezone.timedelta(days=7)
        paid_week, paid_week_n = totals(base.filter(status=Payout.PAID, paid_at__gte=week))
        month = timezone.now() - timezone.timedelta(days=30)
        deducted = PayoutExpense.objects.filter(payout__in=base.filter(load__pickup_date__gte=month)).aggregate(s=Coalesce(Sum('amount'), ZERO))['s']
        return Response({
            'pending': {'count': pending_n, 'total': pending}, 'confirmed': {'count': confirmed_n, 'total': confirmed},
            'paid': {'count': paid_n}, 'paid_week': {'count': paid_week_n, 'total': paid_week},
            'deducted_30d': deducted, 'total': base.count(),
        })


class PayoutDetailView(views.APIView):
    permission_classes = [IsAuthenticated, IsAccounting]

    def get_payout(self, pk):
        return get_object_or_404(payout_queryset().prefetch_related('notes__user', 'history__changed_by'), pk=pk)

    def get(self, request, pk):
        return Response(payout_dict(self.get_payout(pk), detail=True))

    @transaction.atomic
    def patch(self, request, pk):
        p = get_object_or_404(Payout.objects.select_related('load', 'load__driver', 'load__status'), pk=pk)
        user = request.user
        data = request.data
        changed = False

        if 'status' in data:
            new = data['status']
            if new not in dict(Payout.STATUSES):
                raise ValidationError({'status': 'Unknown status.'})
            if new != p.status:
                if (new == Payout.PAID or p.status == Payout.PAID) and not is_management(user):
                    raise PermissionDenied('Only management can mark a payout paid or reopen a paid one.')
                was = p.get_status_display()
                if p.status == Payout.PAID:
                    p.reopened_at = timezone.now()
                    p.paid_at = None
                    p.paid_by = None
                if new == Payout.CONFIRMED:
                    p.confirmed_at = timezone.now()
                    p.confirmed_by = user
                if new == Payout.PENDING:
                    p.confirmed_at = None
                    p.confirmed_by = None
                if new == Payout.PAID:
                    if not p.confirmed_at:
                        p.confirmed_at = timezone.now()
                        p.confirmed_by = user
                    p.paid_at = timezone.now()
                    p.paid_by = user
                p.status = new
                log_payout(p, user, f'{was} to {p.get_status_display()}' + (' (reopened by management)' if p.reopened_at and new != Payout.PAID and was == 'Paid' else ''))
                changed = True

        if 'payment_type' in data:
            if data['payment_type'] not in Payout.PAYMENT_TYPES:
                raise ValidationError({'payment_type': f'One of {", ".join(Payout.PAYMENT_TYPES)}.'})
            if p.status == Payout.PAID:
                raise ValidationError({'payment_type': 'The payout is paid; only management can reopen it.'})
            if data['payment_type'] != p.payment_type:
                log_payout(p, user, f'Payment type {p.payment_type} to {data["payment_type"]}')
                p.payment_type = data['payment_type']
                if p.payment_type == Payout.QUICK_PAY:
                    take_quick_pay_fee(p, user)
                else:
                    drop_quick_pay_fee(p, user)
                changed = True

        # A payout that was already Quick Pay before the fee existed, or booked as Quick Pay.
        if data.get('take_quick_pay_fee'):
            if p.status == Payout.PAID:
                raise ValidationError({'take_quick_pay_fee': 'The payout is paid; only management can reopen it.'})
            if p.payment_type != Payout.QUICK_PAY:
                raise ValidationError({'take_quick_pay_fee': 'Only a Quick Pay payout has the fee.'})
            if p.quick_pay_fee <= 0:
                take_quick_pay_fee(p, user)
                changed = True

        if 'amount' in data:
            if p.status == Payout.PAID:
                raise ValidationError({'amount': 'The payout is paid; only management can reopen it.'})
            amount = as_money(data['amount'])
            applied = p.applied_total
            new_gross = amount + applied
            driver_pay = p.load.driver_pay or ZERO
            over = new_gross - driver_pay
            before = p.amount
            if over > 0:
                how = data.get('over_pay')
                if how not in ('load', 'expense'):
                    return Response({'code': 'over_pay', 'over': over, 'driver_pay': driver_pay, 'applied': applied,
                                     'detail': f'{money(over)} more than the driver pay on the load. Send over_pay as "load" or "expense".'},
                                    status=status.HTTP_409_CONFLICT)
                p.gross = new_gross
                if how == 'load':
                    raise_driver_pay(p.load, new_gross, user, p)
                    log_payout(p, user, f'Payout changed {money(before)} to {money(amount)}; {money(over)} over the driver pay, driver pay on the load raised to {money(new_gross)}')
                else:
                    overpayment_expense(p.load, over, user, p)
                    log_payout(p, user, f'Payout changed {money(before)} to {money(amount)}; {money(over)} over the driver pay, {money(over)} expense created for the driver')
            else:
                p.gross = new_gross
                log_payout(p, user, f'Payout changed {money(before)} to {money(amount)}')
            changed = True

        if changed:
            p.save()
        return Response(payout_dict(self.get_payout(pk), detail=True))


class PayoutNotesView(views.APIView):
    permission_classes = [IsAuthenticated, IsAccounting]

    def post(self, request, pk):
        p = get_object_or_404(Payout, pk=pk)
        text = str(request.data.get('text', '')).strip()
        if not text:
            raise ValidationError({'text': 'Write something.'})
        n = PayoutNote.objects.create(payout=p, user=request.user, text=text)
        return Response({'id': n.id, 'user': user_dict(n.user), 'text': n.text, 'created_at': n.created_at}, status=status.HTTP_201_CREATED)


class PayoutExpensesView(views.APIView):
    """What a payout takes from the driver's expenses: take one, change the portion, or leave it for later."""
    permission_classes = [IsAuthenticated, IsAccounting]

    def _payout(self, pk):
        p = get_object_or_404(Payout.objects.select_related('load', 'load__driver'), pk=pk)
        if p.status == Payout.PAID:
            raise ValidationError({'detail': 'The payout is paid; only management can reopen it.'})
        return p

    def _deduction(self, p, deduction_id):
        d = get_object_or_404(Deduction.objects.select_related('type'), pk=deduction_id, is_deleted=False)
        if d.driver_id != p.load.driver_id:
            raise ValidationError({'deduction': 'That expense belongs to another driver.'})
        return d

    @transaction.atomic
    def post(self, request, pk):
        p = self._payout(pk)
        d = self._deduction(p, request.data.get('deduction'))
        if p.expenses.filter(deduction=d).exists():
            raise ValidationError({'deduction': 'Already on this payout.'})
        cap = min(available_for(d, p), p.amount)
        amount = as_money(request.data['amount']) if 'amount' in request.data else cap
        if amount <= 0 or amount > cap:
            raise ValidationError({'amount': f'Up to {money(cap)} can come off this payout.'})
        link = PayoutExpense.objects.create(payout=p, deduction=d, amount=amount)
        _, _, owed = debt_of(d)
        log_payout(p, request.user, f'{d.type.name if d.type else "Expense"}: took {money(amount)} of {money(d.amount)} on this payout' + (f'; {money(owed - amount)} stays open' if owed - amount > 0 else ''))
        return Response(link_dict(link, p.status), status=status.HTTP_201_CREATED)

    @transaction.atomic
    def patch(self, request, pk, deduction_id):
        p = self._payout(pk)
        link = get_object_or_404(PayoutExpense.objects.select_related('deduction', 'deduction__type'), payout=p, deduction_id=deduction_id)
        d = link.deduction
        amount = as_money(request.data.get('amount'))
        cap = min(available_for(d, p), p.gross)
        if amount <= 0 or amount > cap:
            raise ValidationError({'amount': f'Up to {money(cap)} can come off this payout.'})
        was = link.amount
        link.amount = amount
        link.save(update_fields=['amount'])
        _, held, owed = debt_of(d)
        log_payout(p, request.user, f'{d.type.name if d.type else "Expense"}: takes {money(amount)} instead of {money(was)} on this payout; {money(max(ZERO, owed - held))} stays open')
        return Response(link_dict(link, p.status))

    @transaction.atomic
    def delete(self, request, pk, deduction_id):
        p = self._payout(pk)
        link = get_object_or_404(PayoutExpense.objects.select_related('deduction', 'deduction__type'), payout=p, deduction_id=deduction_id)
        d = link.deduction
        link.delete()
        _, held, owed = debt_of(d)
        log_payout(p, request.user, f'{d.type.name if d.type else "Expense"}: removed from this payout, {money(max(ZERO, owed - held))} stays open for the next one')
        return Response(status=status.HTTP_204_NO_CONTENT)


class PayoutOpenExpensesView(views.APIView):
    """The driver's expenses with something left that are not on this payout."""
    permission_classes = [IsAuthenticated, IsAccounting]

    def get(self, request, pk):
        p = get_object_or_404(Payout.objects.select_related('load'), pk=pk)
        if not p.load.driver_id:
            return Response([])
        on_this = set(p.expenses.values_list('deduction_id', flat=True))
        out = []
        for d in annotate_debt(Deduction.objects.filter(driver_id=p.load.driver_id, is_deleted=False)).select_related('type', 'driver', 'created_by').order_by('date', 'id'):
            if d.id in on_this:
                continue
            available = available_for(d, p)
            if available <= 0:
                continue
            row = deduction_dict(d)
            row['available'] = available
            row['added_after'] = bool(d.date and p.created_at and d.date > p.created_at.date())
            out.append(row)
        return Response(out)


class LoadStatusView(views.APIView):
    """Load status changed from either tab; it changes the load itself."""
    permission_classes = [IsAuthenticated, IsAccounting]

    @transaction.atomic
    def post(self, request, pk):
        p = get_object_or_404(Payout.objects.select_related('load', 'load__status'), pk=pk)
        st = get_object_or_404(LoadStatus, pk=request.data.get('status'))
        if p.load.status_id != st.id:
            set_load_status(p.load, st, request.user)
        return Response({'id': p.load.id, 'status': {'id': st.id, 'name': st.name}})


# --- Invoices ------------------------------------------------------------------------------------

INVOICE_RELATED = ('load', 'load__status', 'load__driver', 'load__broker', 'load__payout')


def invoice_queryset():
    return Invoice.objects.select_related(*INVOICE_RELATED).prefetch_related('load__loadfile_set').annotate(notes_n=Count('notes', distinct=True))


def invoice_filters(request, qs, with_status=True):
    p = request.query_params
    if with_status and p.get('status'):
        if p['status'] == 'short':
            qs = qs.filter(amount__lt=F('original'))
        else:
            qs = qs.filter(status=p['status'])
    load_statuses = ids_param(p, 'load_status')
    if load_statuses:
        qs = qs.filter(load__status_id__in=load_statuses)
    if p.get('broker'):
        qs = qs.filter(load__broker_id=p['broker'])
    if p.get('short') in ('1', 'true'):
        qs = qs.filter(amount__lt=F('original'))
    if p.get('pickup_from'):
        qs = qs.filter(load__pickup_date__date__gte=p['pickup_from'])
    if p.get('pickup_to'):
        qs = qs.filter(load__pickup_date__date__lte=p['pickup_to'])
    return search_loads(qs, p.get('search'), 'load__')


class InvoiceListView(views.APIView):
    permission_classes = [IsAuthenticated, IsAccounting]

    def get(self, request):
        qs = invoice_filters(request, invoice_queryset())
        order = request.query_params.get('ordering', 'status')
        if order == 'status':
            from django.db.models import Case, When, IntegerField
            qs = qs.annotate(st_order=Case(When(status=Invoice.GOOD, then=0), When(status=Invoice.INVOICED, then=1), default=2, output_field=IntegerField())).order_by('st_order', F('load__pickup_date').desc(nulls_last=True), '-id')
        else:
            qs = qs.order_by(F('load__pickup_date').desc(nulls_last=True), '-id')
        return paginate(self, request, qs, invoice_dict)


class InvoiceSummaryView(views.APIView):
    permission_classes = [IsAuthenticated, IsAccounting]

    def get(self, request):
        base = invoice_filters(request, Invoice.objects.all(), with_status=False)
        def agg(q):
            r = q.aggregate(n=Count('id'), s=Coalesce(Sum('amount'), ZERO))
            return {'count': r['n'], 'total': r['s']}
        good = agg(base.filter(status=Invoice.GOOD))
        invoiced_q = base.filter(status=Invoice.INVOICED)
        invoiced = agg(invoiced_q)
        ages = [(timezone.now() - f.invoiced_at).days for f in invoiced_q.only('invoiced_at') if f.invoiced_at]
        invoiced['avg_age_days'] = round(sum(ages) / len(ages)) if ages else 0
        paid = agg(base.filter(status=Invoice.PAID))
        paid_month = agg(base.filter(status=Invoice.PAID, paid_at__gte=timezone.now() - timezone.timedelta(days=30)))
        short_q = base.filter(amount__lt=F('original'))
        short = {'count': short_q.count(), 'total': short_q.aggregate(s=Coalesce(Sum(F('amount') - F('original')), ZERO))['s']}
        return Response({'good': good, 'invoiced': invoiced, 'paid': paid, 'paid_month': paid_month, 'short': short, 'total': base.count()})


class InvoiceDetailView(views.APIView):
    permission_classes = [IsAuthenticated, IsAccounting]

    def get_invoice(self, pk):
        return get_object_or_404(invoice_queryset().prefetch_related('notes__user', 'history__changed_by'), pk=pk)

    def get(self, request, pk):
        return Response(invoice_dict(self.get_invoice(pk), detail=True))

    @transaction.atomic
    def patch(self, request, pk):
        f = get_object_or_404(Invoice.objects.select_related('load'), pk=pk)
        user = request.user
        data = request.data
        changed = False
        if 'amount' in data:
            amount = as_money(data['amount'])
            if amount != f.amount:
                was = f.amount
                f.amount = amount
                diff = amount - f.original
                tail = f'; short pay {money(diff)}' if diff < 0 else '; no longer short' if was < f.original else ''
                log_invoice(f, user, f'Amount {money(was)} to {money(amount)}{tail}')
                changed = True
            note = str(data.get('note', '')).strip()
            if note:
                InvoiceNote.objects.create(invoice=f, user=user, text=note)
        if 'status' in data:
            new = data['status']
            if new not in dict(Invoice.STATUSES):
                raise ValidationError({'status': 'Unknown status.'})
            if new != f.status:
                was = f.get_status_display()
                f.status = new
                if new == Invoice.INVOICED and not f.invoiced_at:
                    f.invoiced_at = timezone.now()
                if new == Invoice.PAID:
                    f.paid_at = timezone.now()
                else:
                    f.paid_at = None
                if new == Invoice.GOOD:
                    f.invoiced_at = None
                log_invoice(f, user, f'{was} to {f.get_status_display()}')
                changed = True
        if 'load_status' in data:
            st = get_object_or_404(LoadStatus, pk=data['load_status'])
            if f.load.status_id != st.id:
                set_load_status(f.load, st, user)
        if changed:
            f.save()
        return Response(invoice_dict(self.get_invoice(pk), detail=True))


class InvoiceNotesView(views.APIView):
    permission_classes = [IsAuthenticated, IsAccounting]

    def post(self, request, pk):
        f = get_object_or_404(Invoice, pk=pk)
        text = str(request.data.get('text', '')).strip()
        if not text:
            raise ValidationError({'text': 'Write something.'})
        n = InvoiceNote.objects.create(invoice=f, user=request.user, text=text)
        return Response({'id': n.id, 'user': user_dict(n.user), 'text': n.text, 'created_at': n.created_at}, status=status.HTTP_201_CREATED)


# --- Expenses ------------------------------------------------------------------------------------

def expense_queryset():
    return annotate_debt(Deduction.objects.filter(is_deleted=False)).select_related('driver', 'type', 'created_by').prefetch_related('payout_links__payout')


def expense_filters(request, qs, with_state=True):
    p = request.query_params
    state = p.get('state') if with_state else None
    if state == 'open':
        qs = qs.filter(owed_sum__gt=0, held_sum__lte=0)
    elif state == 'settling':
        qs = qs.filter(owed_sum__gt=0, held_sum__gt=0)
    elif state == 'settled':
        qs = qs.filter(owed_sum__lte=0)
    if p.get('driver'):
        qs = qs.filter(driver_id=p['driver'])
    if p.get('type'):
        qs = qs.filter(type_id=p['type'])
    if p.get('date_from'):
        qs = qs.filter(date__gte=p['date_from'])
    if p.get('date_to'):
        qs = qs.filter(date__lte=p['date_to'])
    search = str(p.get('search', '')).strip()
    if search:
        qs = qs.filter(Q(driver__full_name__icontains=search) | Q(type__name__icontains=search) | Q(note__icontains=search))
    return qs


class ExpenseListView(views.APIView):
    permission_classes = [IsAuthenticated, IsAccounting]

    def get(self, request):
        qs = expense_filters(request, expense_queryset()).order_by(F('date').desc(nulls_last=True), '-id')
        return paginate(self, request, qs, deduction_dict)

    @transaction.atomic
    def post(self, request):
        """Driver, type, amount, optional fee, date, note. Nothing else: payouts collect it."""
        data = request.data
        from hiring.models import Driver
        driver = get_object_or_404(Driver, pk=data.get('driver'))
        kind = get_object_or_404(DeductionType, pk=data.get('type'))
        amount = as_money(data.get('amount'))
        fee = as_money(data.get('fee') or 0, 'fee')
        if amount + fee <= 0:
            raise ValidationError({'amount': 'Must be more than zero.'})
        date = data.get('date') or timezone.localdate()
        d = Deduction.objects.create(driver=driver, type=kind, amount=amount + fee, fee=fee or None, date=date, note=str(data.get('note', '')).strip(), created_by=request.user)
        DeductionHistory.objects.create(deduction=d, changed_by=request.user, description=f'Deduction Created: {money(d.amount)}' + (f' (incl. {money(fee)} fee)' if fee else ''))
        links = take_expense_everywhere(d, request.user)
        row = deduction_dict(expense_queryset().get(pk=d.pk))
        row['taken'] = [{'payout': l.payout_id, 'shipment': f'SH - {l.payout.load.shipment}' if l.payout.load.shipment else f'load {l.payout.load_id}', 'amount': l.amount} for l in PayoutExpense.objects.filter(id__in=[x.id for x in links]).select_related('payout__load')]
        return Response(row, status=status.HTTP_201_CREATED)


class ExpenseSummaryView(views.APIView):
    permission_classes = [IsAuthenticated, IsAccounting]

    def get(self, request):
        base = expense_filters(request, expense_queryset(), with_state=False)
        rows = list(base.values_list('driver_id', 'owed_sum', 'held_sum', 'amount', 'date'))
        owed = [r for r in rows if r[1] > 0]
        month = timezone.localdate() - timezone.timedelta(days=30)
        settled_month = PayoutExpense.objects.filter(payout__status=Payout.PAID, payout__paid_at__gte=timezone.now() - timezone.timedelta(days=30)).aggregate(s=Coalesce(Sum('amount'), ZERO))['s']
        added = [r for r in rows if r[4] and r[4] >= month]
        return Response({
            'owed': {'count': len(owed), 'total': sum((r[1] for r in owed), ZERO), 'drivers': len({r[0] for r in owed})},
            'held': sum((r[2] for r in rows), ZERO),
            'settled_30d': settled_month,
            'added_30d': {'count': len(added), 'total': sum((r[3] for r in added), ZERO)},
            'open': len([r for r in rows if r[1] > 0 and r[2] <= 0]), 'settling': len([r for r in rows if r[1] > 0 and r[2] > 0]), 'settled': len([r for r in rows if r[1] <= 0]), 'total': len(rows),
        })


class ExpenseDetailView(views.APIView):
    permission_classes = [IsAuthenticated, IsAccounting]

    def get(self, request, pk):
        d = get_object_or_404(expense_queryset(), pk=pk)
        row = deduction_dict(d)
        row['collected_on'] = [{'payout': l.payout_id, 'load': l.payout.load_id, 'shipment': f'SH - {l.payout.load.shipment}' if l.payout.load.shipment else None,
                                'pickup_date': l.payout.load.pickup_date, 'amount': l.amount, 'status': l.payout.status, 'payout_amount': l.payout.amount}
                               for l in d.payout_links.select_related('payout__load').order_by('payout__load__pickup_date')]
        row['history'] = [{'id': h.id, 'changed_by': user_dict(h.changed_by), 'description': h.description, 'created_at': h.created_at} for h in DeductionHistory.objects.filter(deduction=d).select_related('changed_by').order_by('created_at', 'id')]
        return Response(row)

    @transaction.atomic
    def patch(self, request, pk):
        d = get_object_or_404(Deduction.objects.select_related('type'), pk=pk, is_deleted=False)
        data = request.data
        user = request.user
        if 'amount' in data:
            amount = as_money(data['amount'])
            settled, held, _ = debt_of(d)
            floor = settled + held
            if amount < floor:
                raise ValidationError({'amount': f'{money(floor)} is already settled or on a payout; the amount cannot go below that.'})
            if amount <= 0:
                raise ValidationError({'amount': 'Must be more than zero.'})
            if amount != d.amount:
                was = d.amount
                d.amount = amount
                d.save(update_fields=['amount', 'last_updated'])
                DeductionHistory.objects.create(deduction=d, changed_by=user, description=f'Amount {money(was)} to {money(amount)}')
                if amount > was:
                    take_expense_everywhere(d, user)
        for field in ('note', 'date', 'type'):
            if field in data:
                if field == 'type':
                    d.type = get_object_or_404(DeductionType, pk=data['type'])
                else:
                    setattr(d, field, data[field])
                d.save(update_fields=[field, 'last_updated'])
                DeductionHistory.objects.create(deduction=d, changed_by=user, description=f'{field.capitalize()} changed')
        return Response(deduction_dict(expense_queryset().get(pk=pk)))

    @transaction.atomic
    def delete(self, request, pk):
        d = get_object_or_404(Deduction, pk=pk, is_deleted=False)
        settled, held, _ = debt_of(d)
        if settled > 0 or held > 0:
            raise ValidationError({'detail': 'Part of it is already settled or on a payout; it cannot be deleted.'})
        d.is_deleted = True
        d.save(update_fields=['is_deleted', 'last_updated'])
        DeductionHistory.objects.create(deduction=d, changed_by=request.user, description='Deduction Deleted')
        return Response(status=status.HTTP_204_NO_CONTENT)
