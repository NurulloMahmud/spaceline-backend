from rest_framework import viewsets, generics, views, status
from rest_framework.response import Response
from django.core.exceptions import ValidationError
from django.db import transaction
from decimal import Decimal
import pandas as pd
import numpy as np
from config import settings
from hiring.models import Driver
from users.models import Company, CustomUser
from rest_framework.filters import OrderingFilter, SearchFilter
from django_filters.rest_framework import DjangoFilterBackend
from rest_framework.permissions import IsAuthenticated
from hiring.views import CustomPagination
from users.permissions import IsAdminUser, IsUpdater, IsPayroll, IsBilling, IsDispatch, IsDispatchManager
from .models import (Deduction, DeductionHistory, DeductionType, StatementDeduction, StatementStatus, Statement, StatementLoad)
from .serializers import (StatementStatusSerializer, StatementWriteSerializer, StatementViewSerializer, StatementLoadViewSerializer, 
                          StatementLoadWriteSerializer, DriverDropdownSerializer, CreateDeductionWithStatementIdSerializer,
                          DeductionTypeSerializer, DeductionHistoryViewSerializer, DeductionWriteSerializer,
                          DeductionViewSerializer, StatementDeductionViewSerializer, StatementDeductionWriteSerializer,
                          )


class StatementStatusViewSet(viewsets.ModelViewSet):
    queryset = StatementStatus.objects.all().order_by('id')
    serializer_class = StatementStatusSerializer

    def get_permissions(self):
        if self.request.method == 'GET':
            permission_classes = [IsAuthenticated]
        else:
            permission_classes = [IsAdminUser]
        return [permission() for permission in permission_classes]


class StatementLoadsViewSet(viewsets.ModelViewSet):
    queryset = StatementLoad.objects.select_related('statement', 'load', 'statement__driver', 'load__driver').all()

    def get_serializer_class(self):
        if self.request.method in ['POST', 'PUT', 'PATCH']:
            return StatementLoadWriteSerializer
        return StatementLoadViewSerializer

    def get_permissions(self):
        if self.request.method == 'GET':
            permission_classes = [IsAuthenticated]
        else:
            permission_classes = [IsAdminUser | IsDispatch | IsDispatchManager | IsUpdater | IsBilling | IsPayroll]
        return [permission() for permission in permission_classes]

    @transaction.atomic
    def perform_create(self, serializer):
        from billing.models import LoadHistory
        load = serializer.validated_data.get('load')
        statement = serializer.validated_data.get('statement')
        if not load:
            raise ValidationError({"load": "Load is required."})
        if not statement:
            raise ValidationError({"statement": "Statement is required."})

        if StatementLoad.objects.filter(statement=statement, load=load).exists():
            raise ValidationError({
                "detail": "This load is already added to this statement."
            })

        if StatementLoad.objects.filter(load=load).exists():
            raise ValidationError({
                "detail": "This load is already assigned to another statement."
            })

        if statement.driver and load.driver and statement.driver != load.driver:
            raise ValidationError({
                "detail": "This load belongs to a different driver and cannot be added to this statement."
            })

        instance = serializer.save()
        load_driver_pay = load.driver_pay or Decimal('0')
        statement.gross_amount = (statement.gross_amount or Decimal('0')) + load_driver_pay
        waiting_approval = StatementStatus.objects.filter(name='Waiting Approval').first()
        if waiting_approval:
            statement.status = waiting_approval

        statement.save(update_fields=['gross_amount', 'status'])
        LoadHistory.objects.create(
            load=load,
            changed_by=self.request.user,
            description=(
                f"Load has been added to statement "
                f"{statement.driver.full_name if statement.driver else 'No Driver'} "
                f"for period {statement.start_date} - {statement.end_date}"
            )
        )

    @transaction.atomic
    def destroy(self, request, *args, **kwargs):
        from billing.models import LoadHistory
        instance = self.get_object()
        statement = instance.statement
        load = instance.load
        load_driver_pay = load.driver_pay or Decimal('0')
        LoadHistory.objects.create(
            load=load,
            changed_by=request.user,
            description=(
                f"Load has been removed from statement "
                f"{statement.driver.full_name if statement.driver else 'No Driver'} "
                f"for period {statement.start_date} - {statement.end_date}"
            )
        )
        self.perform_destroy(instance)
        if statement:
            current_gross = statement.gross_amount or Decimal('0')
            new_gross = current_gross - load_driver_pay
            if new_gross < 0:
                new_gross = Decimal('0')

            statement.gross_amount = new_gross
            waiting_approval = StatementStatus.objects.filter(name='Waiting Approval').first()
            if waiting_approval:
                statement.status = waiting_approval

            statement.save(update_fields=['gross_amount', 'status'])
        return Response(status=status.HTTP_204_NO_CONTENT)


class StatementViewSet(viewsets.ModelViewSet):
    queryset = Statement.objects.all()
    pagination_class = CustomPagination
    filter_backends = [DjangoFilterBackend, SearchFilter, OrderingFilter]
    filterset_fields = ['company', 'driver', 'status', 'final', 'created_by']
    search_fields = ['driver__full_name']

    def get_serializer_class(self):
        if self.request.method in ['POST', 'PUT', 'PATCH']:
            return StatementWriteSerializer
        return StatementViewSerializer

    def get_permissions(self):
        if self.request.method == 'GET':
            permission_classes = [IsAuthenticated]
        else:
            permission_classes = [IsAdminUser | IsDispatch | IsDispatchManager | IsUpdater | IsBilling | IsPayroll]
        return [permission() for permission in permission_classes]

    def get_queryset(self):
        user = self.request.user
        queryset = Statement.objects.select_related(
            'status',
            'driver',
            'driver__company',
            'company',
            'created_by',
        ).prefetch_related(
            'statementload_set',
        )
        if self.request.method == 'GET':
            start_date = self.request.query_params.get('start_date')
            end_date = self.request.query_params.get('end_date')
            if not start_date or not end_date:
                return Statement.objects.none()
            queryset = Statement.objects.filter(start_date__gte=start_date, end_date__lte=end_date)
            if user.department.name.lower() not in ['management', 'billing', 'payroll', 'accounting', 'audit']:
                queryset = queryset.filter(company=user.company).exclude(status__name='In Process')
        return queryset.order_by('driver__full_name')

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        return self.perform_create(serializer)

    @transaction.atomic
    def perform_create(self, serializer):
        from billing.bot import send_telegram_message
        from decimal import Decimal
        from django.db.models import Q, Avg
        from rest_framework.response import Response
        from billing.models import Load
        from datetime import datetime
        import threading
        from .threading import async_generate_pdfs
        try:
            request_data = self.request.data
            start_date = request_data.get("start_date")
            end_date = request_data.get("end_date")
            company_id = request_data.get("company")
            payment_type = request_data.get("payment_type")
            provided_driver_ids = request_data.get("driver_ids", [])
            if not provided_driver_ids:
                return Response({"message": "No driver IDs provided."}, status=400)
            driver_statuses = Driver.objects.filter(id__in=provided_driver_ids)
            created_statements = []
            skipped_drivers = []
            pdf_update_statement_ids = []
            skipped_reasons = {}
            status_obj, _ = StatementStatus.objects.get_or_create(name='In Process')
            start_date_obj = datetime.strptime(start_date, "%Y-%m-%d").date()
            end_date_obj = datetime.strptime(end_date, "%Y-%m-%d").date()
            week_number = start_date_obj.isocalendar()[1]
            for driver_status in driver_statuses:
                last_statement = Statement.objects.filter(
                    driver=driver_status.id
                ).order_by('-end_date').first()
                if last_statement:
                    if last_statement.status.name.lower() != 'closed':
                        skipped_drivers.append(driver_status.name)
                        skipped_reasons[driver_status.name] = "Previous statement is not closed"
                        continue
                
                assigned_load_ids = StatementLoad.objects.values_list('load_id', flat=True)
                loads = (Load.objects.filter(
                    driver=driver_status,
                    drop_date__date__lt=end_date_obj,
                    batchload__isnull=False
                )
                .exclude(status__name__in=['Cancelled', 'Rejected'])
                .exclude(id__in=assigned_load_ids))
                if payment_type and payment_type.lower() != 'all':
                    loads = loads.filter(payment_type=payment_type)
                loads = loads.values('id', 'driver_pay')
                if not loads.exists():
                    skipped_drivers.append(driver_status.full_name)
                    skipped_reasons[driver_status.full_name] = "No valid loads found for selected period"
                    continue
                else:
                    load_objects = Load.objects.filter(id__in=[load['id'] for load in loads])
                    load_data = []
                    total_driver_pay = Decimal('0')
                    for load in load_objects:
                        if not load.driver_pay or load.driver_pay <= 0:
                            continue
                        driver_pay = Decimal(str(load.driver_pay or 0))
                        load_data.append({
                            'id': load.id,
                            'driver_pay': float(driver_pay)
                        })
                        total_driver_pay += driver_pay
                gross_amount = total_driver_pay
                existing_deductions = Deduction.objects.filter(
                    Q(driver=driver_status) &
                    Q(paid=False) &
                    (Q(date__range=[start_date_obj, end_date_obj]) | Q(date__lt=end_date_obj)) &
                    ~Q(statementdeduction__isnull=False)
                )
                deductions = list(existing_deductions)
                if payment_type and payment_type.lower() == 'quick pay':
                    quick_pay_fee_type, _ = DeductionType.objects.get_or_create(name='Quick Pay Fee')
                    advance_deduction = next(
                        (d for d in deductions if d.type and d.type.name.lower() == 'advance'),
                        None
                    )
                    if advance_deduction:
                        advance_total = advance_deduction.amount
                        net = total_driver_pay - advance_total
                        fee_amount = (net * Decimal('0.05')).quantize(Decimal('0.01'))
                        fee_date = advance_deduction.date
                    else:
                        fee_amount = (total_driver_pay * Decimal('0.05')).quantize(Decimal('0.01'))
                        fee_date = start_date_obj

                    quick_pay_fee_deduction = Deduction.objects.create(
                        driver=driver_status,
                        amount=fee_amount,
                        type=quick_pay_fee_type,
                        date=fee_date,
                        fee=Decimal('0.00'),
                        paid=False,
                        created_by=self.request.user
                    )
                    DeductionHistory.objects.create(
                        deduction=quick_pay_fee_deduction,
                        changed_by=self.request.user,
                        description=f"Quick Pay Fee auto-created | Driver: {driver_status.full_name} | Period: {start_date_obj} - {end_date_obj} | Fee Amount: {fee_amount}"
                    )
                    deductions.append(quick_pay_fee_deduction)
                statement_data = {
                    "start_date": start_date_obj,
                    "end_date": end_date_obj,
                    "company": company_id,
                    "driver": driver_status.id,
                    "created_by": self.request.user.id,
                    "gross_amount": gross_amount,
                    "status": status_obj.id,
                    "week_number": week_number
                }
                statement_serializer = StatementWriteSerializer(data=statement_data)
                if statement_serializer.is_valid():
                    statement = statement_serializer.save()
                    created_statements.append(statement)
                    statement_loads = [
                        StatementLoad(statement=statement, load_id=item['id'])
                        for item in load_data
                    ]
                    StatementLoad.objects.bulk_create(statement_loads)
                    for deduction in deductions:
                        StatementDeduction.objects.create(
                            deduction=deduction,
                            statement=statement
                        )
                        if deduction in existing_deductions:
                            deduction.paid = False
                            deduction.save()
                else:
                    skipped_drivers.append(driver_status.name)
                    skipped_reasons[driver_status.name] = f"Validation error: {statement_serializer.errors}"
            if pdf_update_statement_ids:
                threading.Thread(target=async_generate_pdfs, args=(pdf_update_statement_ids,)).start()
                send_telegram_message(f"📎 PDF update statement IDs: {pdf_update_statement_ids}")
            return Response({
                "message": "Statements processing completed",
                "created_statements_count": len(created_statements),
                "skipped_drivers_count": len(skipped_drivers),
                "skipped_drivers": skipped_reasons
            }, status=201)
        except Exception as e:
            transaction.set_rollback(True)
            return Response({
                "error": "An error occurred while creating statements",
                "details": str(e)
            }, status=400)

    @transaction.atomic
    def perform_update(self, serializer):
        from .utils import generate_statement_pdf
        from billing.bot import send_telegram_message, send_statement_closed_message
        instance = serializer.instance
        previous_status = instance.status.name
        updated_fields = serializer.validated_data.keys()
        is_only_note_update = (len(updated_fields) == 1 and 'note' in updated_fields)
        statement = serializer.save()
        if not is_only_note_update:
            if previous_status != "Approved" and statement.status.name == "Approved":
                try:
                    generate_statement_pdf(statement)
                except Exception as e:
                    error_message = f"🚨 *Error generating PDF* for statement *{statement.driver.full_name}*:\n```{str(e)}```"
                    send_telegram_message(error_message)

            if statement.status.name.lower() == 'closed':
                Deduction.objects.filter(statementdeduction__statement=statement).update(paid=True)
                send_statement_closed_message(statement)
                try:
                    generate_statement_pdf(statement)
                except Exception as e:
                    error_message = f"🚨 *Error generating PDF* for statement *{statement.driver.full_name}*:\n```{str(e)}```"
                    send_telegram_message(error_message)
            if previous_status.lower() == "closed" and statement.status.name.lower() != "closed":
                Deduction.objects.filter(statementdeduction__statement=statement).update(paid=False)
                deductions = Deduction.objects.filter(statementdeduction__statement=statement)
                for deduction in deductions:
                    DeductionHistory.objects.create(
                        deduction=deduction,
                        changed_by=self.request.user,
                        description=f"Statement status changed from Closed to {statement.status.name} | Driver: {statement.driver.full_name} | Dates: {statement.start_date} - {statement.end_date}"
                    )

            if previous_status in ["Approved", "Closed"] and 'status' not in updated_fields:
                statement.status = StatementStatus.objects.get(name="Waiting Approval")
                statement.save(update_fields=["status"])
        return


class ActiveDriversList(generics.ListAPIView):
    serializer_class = DriverDropdownSerializer
    search_fields = ['full_name']

    def get_queryset(self):
        active_drivers = Driver.objects.exclude(status__name='Inactive')
        company_id = self.request.query_params.get('company_id')
        user = self.request.user
        if self.request.user.department.name.lower() in ['management', 'billing', 'payroll', 'accounting', 'audit']:
            if company_id:
                return active_drivers.filter(company=company_id).order_by('full_name')
            return active_drivers.all().order_by('full_name')
        elif self.request.user.department.name.lower() in ['dispatch manager', 'team lead', 'dispatch', 'updater']:
            if company_id:
                return active_drivers.filter(company=company_id).order_by('full_name')
            return active_drivers.filter(company=user.company).order_by('full_name')
        else:
            return active_drivers.filter(company=user.company).order_by('full_name')


class InactiveDriversList(generics.ListAPIView):
    serializer_class = DriverDropdownSerializer
    search_fields = ['full_name']

    def get_queryset(self):
        active_drivers = Driver.objects.filter(status__name='Inactive')
        company_id = self.request.query_params.get('company_id')
        user = self.request.user
        if self.request.user.department.name.lower() in ['management', 'billing', 'payroll', 'accounting', 'audit']:
            if company_id:
                return active_drivers.filter(company=company_id).order_by('full_name')
            return active_drivers.all().order_by('full_name')
        elif self.request.user.department.name.lower() in ['dispatch manager', 'team lead', 'dispatch', 'updater']:
            if company_id:
                return active_drivers.filter(company=company_id).order_by('full_name')
            return active_drivers.filter(company=user.company).order_by('full_name')
        else:
            return active_drivers.filter(company=user.company).order_by('full_name')


class DeductionTypeViewSet(viewsets.ModelViewSet):
    queryset = DeductionType.objects.all().order_by('name')
    serializer_class = DeductionTypeSerializer

    def get_permissions(self):
        if self.request.method == 'GET':
            permission_classes = [IsAuthenticated]
        else:
            permission_classes = [IsAdminUser | IsBilling | IsPayroll]
        return [permission() for permission in permission_classes]


class DeductionHistoryByIDAPIView(generics.ListAPIView):
    permission_classes = [IsAuthenticated]
    serializer_class = DeductionHistoryViewSerializer

    def get_queryset(self):
        deduction = self.kwargs.get('pk')
        return DeductionHistory.objects.filter(deduction=deduction)


class DeductionViewSet(viewsets.ModelViewSet):
    queryset = Deduction.objects.all()
    pagination_class = CustomPagination
    filter_backends = [DjangoFilterBackend, SearchFilter, OrderingFilter]
    filterset_fields = ['driver', 'type', 'paid', 'driver__company']
    search_fields = ['note', 'driver__full_name', 'amount']

    def get_queryset(self):
        user = self.request.user
        if user.department.name.lower() in ['management', 'billing', 'payroll']:
            return Deduction.objects.filter(is_deleted=False).order_by('-id')
        company_drivers = Driver.objects.filter(company=user.company)
        return Deduction.objects.filter(driver__in=company_drivers, is_deleted=False).order_by('-id')

    def get_serializer_class(self):
        if self.request.method in ['POST', 'PUT', 'PATCH']:
            return DeductionWriteSerializer
        return DeductionViewSerializer

    def get_permissions(self):
        if self.request.method == 'GET':
            permission_classes = [IsAuthenticated]
        else:
            permission_classes = [IsAdminUser | IsDispatch | IsDispatchManager | IsUpdater | IsBilling | IsPayroll]
        return [permission() for permission in permission_classes]

    def perform_create(self, serializer):
        from datetime import timedelta
        from decimal import Decimal
        from rest_framework.exceptions import ValidationError
        split = self.request.data.get('split', 1)
        split = int(split) if str(split).isdigit() and int(split) > 0 else 1
        instance_data = serializer.validated_data.copy()
        base_amount = instance_data['amount']
        driver = instance_data['driver']
        has_final_statement = (
                Statement.objects.filter(driver=driver, final=True).exists()
        )
        if has_final_statement:
            raise ValidationError({
                "error": "This driver has a finalized statement. Deductions cannot be created."
            })
        base_fee = instance_data.get('fee', 0) or Decimal("0.00")
        start_date = instance_data['date']
        note = instance_data.get('note', '')
        split_amount = Decimal(base_amount) / split
        split_fee = Decimal(base_fee) / split if base_fee else None
        deductions = []
        with transaction.atomic():
            for i in range(split):
                instance_data['amount'] = split_amount
                instance_data['fee'] = split_fee if split_fee else None
                instance_data['date'] = start_date + timedelta(weeks=i)
                instance_data['created_by'] = self.request.user
                instance_data['note'] = f"{note}(split)" if split > 1 else note
                deduction = Deduction.objects.create(**instance_data)
                deductions.append(deduction)
                self.update_related_statement_status(deduction)
                DeductionHistory.objects.create(
                    deduction=deduction,
                    changed_by=self.request.user,
                    description="Deduction Created",
                )
        return Response(DeductionViewSerializer(deductions, many=True).data, status=status.HTTP_201_CREATED)

    def update_related_statement_status(self, deduction):
        incomplete_status, _ = StatementStatus.objects.get_or_create(name="Incomplete")
        closed_status = StatementStatus.objects.filter(name__iexact="Closed").first()
        statement = Statement.objects.filter(
            driver=deduction.driver.id,
            start_date__lte=deduction.date,
            end_date__gte=deduction.date
        ).exclude(status=closed_status).first()
        if statement:
            statement.status = incomplete_status
            statement.save(update_fields=["status"])

    def update(self, request, *args, **kwargs):
        from decimal import Decimal
        from datetime import timedelta
        from django.db.models import F
        from django.core.files.storage import default_storage
        instance = self.get_object()
        split = request.data.get("split", 1)
        try:
            split = int(split)
        except (TypeError, ValueError):
            split = 1

        if split and split > 1:
            has_final_statement = (
                    Statement.objects.filter(driver=instance.driver, final=True).exists()
            )
            if has_final_statement:
                return Response(
                    {"error": "This driver has a finalized statement. Split not allowed."},
                    status=status.HTTP_400_BAD_REQUEST
                )

            base_amount = Decimal(request.data.get("amount", instance.amount))
            base_fee = Decimal(request.data.get("fee", instance.fee or 0))
            start_date = request.data.get("date", instance.date)
            if not start_date:
                start_date = instance.date

            split_amount = base_amount / split
            split_fee = base_fee / split if base_fee else None
            note = request.data.get("note", instance.note or "")
            deductions = []

            with transaction.atomic():
                old_histories = DeductionHistory.objects.filter(deduction=instance).order_by("created_at")
                if old_histories.exists():
                    old_history_entry = old_histories.first()
                else:
                    old_history_entry = None
                linked_statements = Statement.objects.filter(statementdeduction__deduction=instance)
                for statement in linked_statements:
                    statement.total_amount = F("total_amount") + ((instance.amount or 0) + (instance.fee or 0))
                    statement.status = StatementStatus.objects.get(name="Incomplete")
                    if statement.pdf_file:
                        if default_storage.exists(statement.pdf_file.name):
                            default_storage.delete(statement.pdf_file.name)
                        statement.pdf_file = None
                    statement.save()
                instance.delete()
                for i in range(split):
                    new_data = {
                        "driver": instance.driver,
                        "amount": split_amount,
                        "fee": split_fee if split_fee else None,
                        "date": start_date + timedelta(weeks=i),
                        "created_by": request.user,
                        "note": f"{note}(split)" if split > 1 else note,
                        "type": instance.type,
                        "paid": instance.paid
                    }
                    deduction = Deduction.objects.create(**new_data)
                    deductions.append(deduction)
                    self.update_related_statement_status(deduction)
                    if old_history_entry:
                        history = DeductionHistory.objects.create(
                            deduction=deduction,
                            changed_by=old_history_entry.changed_by,
                            description=f"Original Deduction created"
                        )
                        history.save()
                        DeductionHistory.objects.filter(id=history.id).update(created_at=old_history_entry.created_at)
                    if i == 0:
                        for statement in linked_statements:
                            StatementDeduction.objects.create(statement=statement, deduction=deduction)
                            statement.total_amount = F("total_amount") - (
                                        (deduction.amount or 0) + (deduction.fee or 0))
                            statement.status = StatementStatus.objects.get(name="Incomplete")
                            if statement.pdf_file:
                                if default_storage.exists(statement.pdf_file.name):
                                    default_storage.delete(statement.pdf_file.name)
                                statement.pdf_file = None
                            statement.save()
                    DeductionHistory.objects.create(
                        deduction=deduction,
                        changed_by=request.user,
                        description="Deduction Split Created",
                    )
            return Response(
                {"message": f"Deduction split into {split} parts.", "count": len(deductions)},
                status=status.HTTP_200_OK
            )
        new_amount = request.data.get("amount")
        new_fee = request.data.get("fee")
        old_amount = instance.amount or Decimal("0")
        old_fee = instance.fee or Decimal("0")
        original_total = old_amount + old_fee
        new_amount_decimal = Decimal(new_amount) if new_amount is not None else old_amount
        new_fee_decimal = Decimal(new_fee) if new_fee is not None else old_fee
        new_total = new_amount_decimal + new_fee_decimal
        amount_changed = original_total != new_total
        original_data = {
            "amount": instance.amount,
            "driver": instance.driver.full_name if instance.driver else None,
            "type": instance.type.name if instance.type else None,
            "date": instance.date,
            "paid": instance.paid,
            "fee": instance.fee if instance.fee else None,
            "note": instance.note if instance.note else None
        }
        with transaction.atomic():
            if amount_changed:
                amount_difference = original_total - new_total
                assigned_statements = Statement.objects.filter(statementdeduction__deduction=instance).distinct()
                for statement in assigned_statements:
                    statement.total_amount = F('total_amount') + amount_difference
                    statement.status = StatementStatus.objects.get(name='Waiting Approval')
                    if statement.pdf_file:
                        statement.pdf_file.delete(save=False)
                    statement.save()
            serializer = self.get_serializer(instance, data=request.data, partial=True)
            serializer.is_valid(raise_exception=True)
            updated_instance = serializer.save()
            ignore_fields = {'last_updated'}
            changes = []
            field_mappings = {
                'driver': lambda obj: obj.driver.full_name if obj.driver else None,
                'type': lambda obj: obj.type.name if obj.type else None
            }
            for field in request.data:
                if field in ignore_fields:
                    continue

                old_value = original_data.get(field)
                if field in field_mappings:
                    new_value = field_mappings[field](updated_instance)
                else:
                    new_value = getattr(updated_instance, field, None)

                if isinstance(new_value, Decimal) and old_value is not None:
                    old_value = Decimal(str(old_value))

                if str(old_value) != str(new_value):
                    changes.append(f"{field.replace('_', ' ').title()}: {old_value} -> {new_value}")
            description = "Deduction Updated | " + " | ".join(changes) if changes else "No significant changes made."
            DeductionHistory.objects.create(
                deduction=instance,
                changed_by=request.user,
                description=description
            )
            if amount_changed:
                return Response(
                    {"message": "Deduction updated and statement adjusted."},
                    status=status.HTTP_200_OK
                )
            return Response(serializer.data, status=status.HTTP_200_OK)

    def destroy(self, request, *args, **kwargs):
        from django.db.models import F
        from django.core.files.storage import default_storage
        instance = self.get_object()
        deduction_amount = (instance.amount or Decimal('0')) + (instance.fee or Decimal('0'))
        with transaction.atomic():
            linked_statements = Statement.objects.filter(statementdeduction__deduction=instance)
            for statement in linked_statements:
                statement.total_amount = F("total_amount") + deduction_amount
                statement.status = StatementStatus.objects.get(name="Waiting Approval")
                if statement.pdf_file:
                    if default_storage.exists(statement.pdf_file.name):
                        default_storage.delete(statement.pdf_file.name)
                    statement.pdf_file = None
                statement.save()
            DeductionHistory.objects.create(
                deduction=instance,
                changed_by=request.user,
                description="Deduction Deleted",
            )
            instance.delete()
        return Response({"message": "Deduction deleted and statement adjusted."}, status=status.HTTP_204_NO_CONTENT)


class StatementDeductionViewSet(viewsets.ModelViewSet):
    queryset = StatementDeduction.objects.all()

    def get_serializer_class(self):
        if self.request.method in ['POST', 'PUT', 'PATCH']:
            return StatementDeductionWriteSerializer
        return StatementDeductionViewSerializer

    def get_permissions(self):
        if self.request.method == 'GET':
            permission_classes = [IsAuthenticated]
        else:
            permission_classes = [IsAdminUser | IsDispatch | IsDispatchManager | IsUpdater | IsBilling | IsPayroll]
        return [permission() for permission in permission_classes]

    def perform_create(self, serializer):
        statement = serializer.validated_data.get('statement')
        deduction = serializer.validated_data.get('deduction')
        if statement.driver != deduction.driver:
            raise ValidationError({
                "detail": "Cannot assign deduction to statement: Driver mismatch. "
                        f"Statement is for driver '{statement.driver}', "
                        f"but deduction belongs to driver '{deduction.driver}'."
            })
        serializer.save()
        statement.status = StatementStatus.objects.get(name='Waiting Approval')
        statement.save()

    def destroy(self, request, *args, **kwargs):
        instance = self.get_object()
        statement = instance.statement
        with transaction.atomic():
            instance.delete()
            statement.status = StatementStatus.objects.get(name='Waiting Approval')
            statement.save()
        return Response({"message": "Deduction removed and statement updated."}, status=status.HTTP_200_OK)


class StatementDeductionsDropDownView(generics.ListAPIView):
    serializer_class = DeductionViewSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        driver_id = self.request.query_params.get('driver')
        if not driver_id:
            return Deduction.objects.none()

        assigned_deduction_ids = StatementDeduction.objects.values_list('deduction_id', flat=True)
        return Deduction.objects.filter(
            driver_id=driver_id,
            paid=False,
            is_deleted=False
        ).exclude(id__in=assigned_deduction_ids)


class DeductionStatisticsView(generics.ListAPIView):
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        return Deduction.objects.none()

    def list(self, request, *args, **kwargs):
        from django.db.models import Sum
        user = request.user
        department_name = getattr(getattr(user, 'department', None), 'name', '').lower()
        allowed_departments = ['management', 'payroll', 'billing', 'accounting', 'audit']
        company_id = request.query_params.get('carrier')
        if department_name in allowed_departments:
            deductions = Deduction.objects.filter(is_deleted=False)
        else:
            deductions = Deduction.objects.filter(driver__company=user.company, is_deleted=False)

        if company_id:
            deductions = deductions.filter(driver__company=user.company, is_deleted=False)

        paid_deductions = deductions.filter(paid=True)
        unpaid_deductions = deductions.filter(paid=False)
        total_paid = paid_deductions.aggregate(total=Sum('amount'))['total'] or 0
        total_unpaid = unpaid_deductions.aggregate(total=Sum('amount'))['total'] or 0
        paid_drivers = (
            paid_deductions
            .values('driver__company__name')
            .annotate(total_paid=Sum('amount'))
            .order_by('driver__company__name')
        )
        unpaid_drivers = (
            unpaid_deductions
            .values('driver__company__name')
            .annotate(total_unpaid=Sum('amount'))
            .order_by('driver__company__name')
        )
        paid_types = (
            paid_deductions
            .values('type__name')
            .annotate(total_paid=Sum('amount'))
            .order_by('type__name')
        )
        unpaid_types = (
            unpaid_deductions
            .values('type__name')
            .annotate(total_unpaid=Sum('amount'))
            .order_by('type__name')
        )
        return Response({
            "total_paid_deductions": total_paid,
            "paid_companies": [
                {"company": item['driver__company__name'], "total_paid": item['total_paid']}
                for item in paid_drivers
            ],
            "paid_types": [
                {"type": item['type__name'], "total_paid": item['total_paid']}
                for item in paid_types
            ],
            "total_unpaid_deductions": total_unpaid,
            "unpaid_companies": [
                {"company": item['driver__company__name'], "total_unpaid": item['total_unpaid']}
                for item in unpaid_drivers
            ],
            "unpaid_types": [
                {"type": item['type__name'], "total_unpaid": item['total_unpaid']}
                for item in unpaid_types
            ],
        })


class CreateDeductionWithStatementIDView(views.APIView):
    def post(self, request):
        statement_id = request.query_params.get('statement_id')
        if not statement_id:
            return Response({"error": "statement_id is required"}, status=status.HTTP_400_BAD_REQUEST)
        serializer = CreateDeductionWithStatementIdSerializer(data=request.data, context={'statement_id': statement_id, 'request': request})
        if serializer.is_valid():
            deduction = serializer.save()
            return Response(CreateDeductionWithStatementIdSerializer(deduction).data, status=status.HTTP_201_CREATED)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

