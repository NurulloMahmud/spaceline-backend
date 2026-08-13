import boto3
from django.conf import settings
from rest_framework import generics, viewsets, status, views
from rest_framework.response import Response
from django.utils.dateparse import parse_date
from django.db.models import F, Value, DecimalField, ExpressionWrapper, Q, Count
from django.db.models.functions import Coalesce
from django.db import transaction
from rest_framework.exceptions import ValidationError
import pandas as pd
from datetime import date, datetime, time
from django.db.models import Count, Sum, Q
from django.utils.dateparse import parse_date
from django_filters.rest_framework import DjangoFilterBackend
from rest_framework.filters import OrderingFilter, SearchFilter
from rest_framework.permissions import IsAuthenticated
from users.serializers import UserListSerializer
from django.db.models import Q
from users.models import CustomUser
from .models import (Broker, LoadStatus, Load, LoadHistory, LoadFile, LoadStop, 
                     Batch, BatchLoad, PaymentType, Tag, LoadTag
                     )
from .serializers import (BrokersSerializer, BatchUseSerializer,
                          LoadStopViewSerializer, LoadStatusesSerializer, 
                          LoadsViewSerializer, LoadByDriverSerializer,
                          LoadUseSerializer, LoadStopWriteSerializer, BatchMultipleLoadSerializer,
                          LoadsWriteSerializer, LoadHistoryViewSerializer, 
                          LoadFilesViewSerializer, LoadFilesWriteSerializer, 
                          BrokersUseSerializer, PaymentTypeSerializer, LoadTagWriteSerializer, LoadTagViewSerializer,
                          BatchViewSerializer, BatchWriteSerializer, BatchLoadViewSerializer, BatchLoadWriteSerializer, TagSerializer,
                          )
from users.permissions import IsAdminUser, IsBilling, IsDispatch, IsDispatchManager, IsUpdater, IsPayroll
from hiring.views import CustomPagination
from .utils import debounce_recalculate_miles
import logging

logger = logging.getLogger(__name__)


class BrokersViewSet(viewsets.ModelViewSet):
    queryset = Broker.objects.all()
    serializer_class = BrokersSerializer
    pagination_class = CustomPagination
    filter_backends = [DjangoFilterBackend, SearchFilter, OrderingFilter]
    filterset_fields = ['state']
    search_fields = ['name', 'address', 'city', 'email', 'phone_number', 'mc']

    def get_permissions(self):
        if self.request.method == 'GET':
            permission_classes = [IsAuthenticated]
        else:
            permission_classes = [IsAdminUser | IsDispatch | IsDispatchManager | IsUpdater | IsBilling | IsPayroll]
        return [permission() for permission in permission_classes]


class BrokerListView(generics.ListAPIView):
    queryset = Broker.objects.all()
    serializer_class = BrokersUseSerializer


class LoadsViewSet(viewsets.ModelViewSet):
    pagination_class = CustomPagination
    filter_backends = [DjangoFilterBackend, SearchFilter, OrderingFilter]
    filterset_fields = ['company', 'broker', 'booked_by', 'status', 'driver', 'payment_type', 'updated_by']
    search_fields = ['load_number', 'shipment', 'loadstop__trailer_info', 'load__driver__full_name']

    def get_queryset(self):
        if self.request.user.department.name.lower() in ['management', 'billing', 'payroll']:
            queryset = Load.objects.all()
        else:
            queryset = Load.objects.filter(company=self.request.user.company)
        search_term = self.request.query_params.get('search', None)
        tag_ids = self.request.query_params.get('tags', None)
        except_tags = self.request.query_params.get('except', 'false').lower() == 'true'
        start_date = self.request.query_params.get('start_date')
        end_date = self.request.query_params.get('end_date')
        date_type = self.request.query_params.get('date_type', 'pickup_date')
        
        if search_term:
            queryset = queryset.filter(
                Q(load_number__icontains=search_term) |
                Q(shipment__icontains=search_term) |
                Q(loadstop__trailer_info__icontains=search_term) |
                Q(driver__full_name__icontains=search_term)
            ).distinct()
        if tag_ids:
            try:
                tag_id_list = [int(i) for i in tag_ids.split(',')]
            except ValueError:
                raise ValidationError({"error": "Invalid tag IDs. Must be comma-separated integers."})
            
            if except_tags:
                queryset = queryset.exclude(loadtag__tag_id__in=tag_id_list).distinct()
            else:
                queryset = queryset.filter(loadtag__tag_id__in=tag_id_list).distinct()

        if date_type not in ['pickup_date', 'drop_date']:
            raise ValidationError({"error": "Invalid date_type. Must be 'pickup_date' or 'drop_date'."})
        if start_date:
            try:
                start_date_parsed = parse_date(start_date)
                if not start_date_parsed:
                    raise ValueError
                queryset = queryset.filter(**{f"{date_type}__date__gte": start_date_parsed})
            except ValueError:
                raise ValidationError({"error": "Invalid start_date format. Use YYYY-MM-DD."})
        if end_date:
            try:
                end_date_parsed = parse_date(end_date)
                if not end_date_parsed:
                    raise ValueError
                queryset = queryset.filter(**{f"{date_type}__date__lte": end_date_parsed})
            except ValueError:
                raise ValidationError({"error": "Invalid end_date format. Use YYYY-MM-DD."})
        return queryset.order_by('-created_at')

    def get_serializer_class(self):
        if self.request.method in ['POST', 'PUT', 'PATCH']:
            return LoadsWriteSerializer
        return LoadsViewSerializer

    def get_permissions(self):
        if self.request.method == 'GET':
            permission_classes = [IsAuthenticated]
        else:
            permission_classes = [IsAdminUser | IsDispatch | IsDispatchManager | IsUpdater | IsBilling | IsPayroll]
        return [permission() for permission in permission_classes]

    def perform_create(self, serializer):
        load = serializer.save()
        user = self.request.user
        pickup_date = load.pickup_date.strftime('%Y-%m-%d %H:%M:%S') if load.pickup_date else "N/A"
        drop_date = load.drop_date.strftime('%Y-%m-%d %H:%M:%S') if load.drop_date else "N/A"
        shipment = load.shipment if load.shipment else "N/A"
        carrier_name = load.company.name if load.company else "N/A"
        load_number = load.load_number
        carrier_pay = load.carrier_pay
        driver_pay = load.driver_pay
        stops_count = load.loadstop_set.count()
        description = (
            f"Load Created: |"
            f"Pick up Date: {pickup_date} | "
            f"Drop Date: {drop_date} | "
            f"Load Number: {load_number} | "
            f"Shipment: {shipment} | "
            f"Company: {carrier_name} | "
            f"Carrier Pay: {carrier_pay} | "
            f"Driver Pay: {driver_pay} | "
            f"Stops Count: {stops_count}"
        )
        LoadHistory.objects.create(
            load=load,
            changed_by=user,
            description=description,
            created_at=load.created_at
        )

    def perform_update(self, serializer):
        from .tasks import notify_drivers_async, notify_group_async
        from rest_framework.exceptions import ValidationError
        from payroll.models import StatementLoad
        from django.utils import timezone
        load = self.get_object()
        old_load_number = load.load_number
        original_status = load.status.name
        new_status = serializer.validated_data.get('status')
        new_load_number = serializer.validated_data.get('load_number')
        if new_load_number and new_load_number != old_load_number:
            if Load.objects.filter(load_number=new_load_number).exists():
                raise ValidationError({"error": "Load number already exists."})
        is_assigned_to_statement = StatementLoad.objects.filter(load=load.id).exists()
        if is_assigned_to_statement:
            raise ValidationError({"error": "This load is already assigned to a statement and cannot be updated."})
        rate_fields = {'driver_pay', 'carrier_pay'}
        incoming_rate_changes = {
            field for field in rate_fields
            if field in serializer.validated_data and serializer.validated_data[field] != getattr(load, field)
        }
        if incoming_rate_changes and load.main_load is not None:
            raise ValidationError({
                "error": "This load is a split load. Rates (driver pay, carrier pay) cannot be updated on split loads."
            })
        if new_status and new_status.name.lower() in ('factored', 'invoiced'):
            restricted_departments = {'dispatch', 'updater', 'dispatch manager'}
            if self.request.user.department and self.request.user.department.name.lower() in restricted_departments:
                raise ValidationError({
                    "error": f"Users in the '{self.request.user.department.name}' department cannot set the status to '{new_status.name}'."
                })
        original_data = LoadUseSerializer(load).data
        old_carrier_pay = load.carrier_pay
        updated_load = serializer.save()
        if new_status and new_status.name.lower() == 'delivered':
            updated_load.delivered_at = timezone.now()
            updated_load.save(update_fields=['delivered_at'])
        elif (original_status and original_status.lower() == 'delivered' and
            new_status and new_status.name.lower() != 'invoiced'):
            updated_load.delivered_at = None
            updated_load.save(update_fields=['delivered_at'])
        new_carrier_pay = updated_load.carrier_pay
        if old_carrier_pay != new_carrier_pay:
            BatchLoad.objects.filter(load=updated_load).delete()
        ignore_fields = {'created_at', 'last_updated'}
        changes = []
        field_mappings = {
            'company': lambda obj: obj.company.name if obj.company else None,
            'broker': lambda obj: obj.broker.name if obj.broker else None,
            'process': lambda obj: obj.process.name if obj.process else None,
            'status': lambda obj: obj.status.name if obj.status else None,
        }
        old_values = {}
        for field in original_data:
            if field in field_mappings:
                old_values[field] = field_mappings[field](load)
            else:
                old_values[field] = getattr(load, field)

        for field, old_value in old_values.items():
            if field in ignore_fields:
                continue
            if field in field_mappings:
                new_value = field_mappings[field](updated_load)
            else:
                new_value = getattr(updated_load, field)
            if str(old_value) != str(new_value):
                changes.append(f"{field.replace('_', ' ').title()}: {old_value} -> {new_value}")

        description = "Load updated | " + " | ".join(changes) if changes else "No significant changes made."
        LoadHistory.objects.create(
            load=load,
            changed_by=self.request.user,
            description=description
        )
        if (original_status and
            original_status.lower() != 'dispatched' and
            new_status and
            new_status.name.lower() == 'dispatched'):
            notify_group_async(updated_load.id)
            notify_drivers_async(updated_load.id)

    def destroy(self, request, *args, **kwargs):
        instance = self.get_object()
        main_load = instance.main_load
        with transaction.atomic():
            if main_load:
                main_load.driver_pay = (main_load.driver_pay or 0) + (instance.driver_pay or 0)
                main_load.save()
            instance.delete()
        return Response({"message": "Load deleted successfully"}, status=status.HTTP_204_NO_CONTENT)


class LoadStatusesViewSet(viewsets.ModelViewSet):
    queryset = LoadStatus.objects.all().order_by('id')
    serializer_class = LoadStatusesSerializer

    def get_permissions(self):
        if self.request.method == 'GET':
            permission_classes = [IsAuthenticated]
        else:
            permission_classes = [IsAdminUser]
        return [permission() for permission in permission_classes]


class LoadHistoryByIdAPIView(generics.ListAPIView):
    permission_classes = [IsAuthenticated]
    serializer_class = LoadHistoryViewSerializer

    def get_queryset(self):
        load = self.kwargs.get('pk')
        return LoadHistory.objects.filter(load=load)


class LoadFilesAPIView(views.APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, *args, **kwargs):
        from .bot import send_rate_con_message
        load_id = request.data.get('load')
        rc_url = request.data.get('rc_url', None)
        if not load_id:
            return Response(
                {"error": "Load ID is missing"},
                status=status.HTTP_400_BAD_REQUEST
            )
        files_data = []
        i = 0
        while f'files[{i}][name]' in request.data:
            file_name = request.data.get(f'files[{i}][name]')
            file_content = request.FILES.get(f'files[{i}][file]')
            if file_name and file_content:
                files_data.append({
                    'name': file_name,
                    'file': file_content,
                    'load': load_id
                })
            i += 1
        if rc_url:
            files_data.append({
                'name': 'RateCon',
                'file': None,
                'rc_url': rc_url,
                'load': load_id
            })
        if not files_data:
            return Response(
                {"error": "Either files or rc_url must be provided"},
                status=status.HTTP_400_BAD_REQUEST
            )
        s3_client = boto3.client(
            's3',
            aws_access_key_id=settings.AWS_ACCESS_KEY_ID,
            aws_secret_access_key=settings.AWS_SECRET_ACCESS_KEY,
            region_name=settings.AWS_S3_REGION_NAME,
        )
        try:
            with transaction.atomic():
                for file_data in files_data:
                    serializer = LoadFilesWriteSerializer(data=file_data)
                    if serializer.is_valid():
                        logger.info("Attempting to save file: %s", file_data.get('name'))
                        try:
                            instance = serializer.save()
                            logger.info("Serializer saved | instance.id=%s | file.name=%s", instance.id, instance.file.name if instance.file else 'NO FILE')
                        except Exception as save_err:
                            logger.error("Serializer save failed | error=%s", str(save_err))
                            raise ValueError(f"Save failed: {save_err}")
                        load = Load.objects.get(id=load_id)
                        if file_data.get('file') and instance.file:
                            s3_key = instance.file.name
                            try:
                                response = s3_client.head_object(
                                    Bucket=settings.AWS_STORAGE_BUCKET_NAME,
                                    Key=s3_key
                                )
                                logger.info(
                                    "S3 upload verified | key=%s | status=%s | size=%s | etag=%s | modified=%s",
                                    s3_key,
                                    response['ResponseMetadata']['HTTPStatusCode'],
                                    response.get('ContentLength'),
                                    response.get('ETag'),
                                    response.get('LastModified'),
                                )
                            except Exception as s3_err:
                                logger.error(
                                    "S3 verification failed | key=%s | error=%s",
                                    s3_key,
                                    str(s3_err),
                                )
                            send_rate_con_message(file_data, load)
                    else:
                        raise ValueError(serializer.errors)
            return Response(
                {"message": "Files uploaded successfully"},
                status=status.HTTP_201_CREATED
            )
        except Exception as e:
            return Response(
                {"error": "File upload failed", "details": str(e)},
                status=status.HTTP_400_BAD_REQUEST
            )

    def get(self, request, load_id=None, file_id=None):
        if file_id:
            try:
                file = LoadFile.objects.get(id=file_id)
                serializer = LoadFilesViewSerializer(file)
                return Response(serializer.data, status=status.HTTP_200_OK)
            except LoadFile.DoesNotExist:
                return Response({"error": "File not found"}, status=status.HTTP_404_NOT_FOUND)
        elif load_id:
            files = LoadFile.objects.filter(load_id=load_id)
            serializer = LoadFilesViewSerializer(files, many=True)
            return Response(serializer.data, status=status.HTTP_200_OK)
        else:
            return Response({"error": "Load ID required"}, status=status.HTTP_400_BAD_REQUEST)

    def put(self, request, file_id=None):
        if not file_id:
            return Response({"error": "File ID is required for update"}, status=status.HTTP_400_BAD_REQUEST)
        try:
            file_instance = LoadFile.objects.get(id=file_id)
            serializer = LoadFilesWriteSerializer(file_instance, data=request.data, partial=True)
            if serializer.is_valid():
                serializer.save()
                return Response(serializer.data, status=status.HTTP_200_OK)
            else:
                return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
        except LoadFile.DoesNotExist:
            return Response({"error": "File not found"}, status=status.HTTP_404_NOT_FOUND)

    def delete(self, request,  *args, **kwargs):
        file_id = kwargs.get("file_id")
        if not file_id:
            return Response({"error": "File ID is required for deletion"}, status=status.HTTP_400_BAD_REQUEST)
        try:
            file_instance = LoadFile.objects.get(id=file_id)
            if file_instance.file:
                file_name = file_instance.file.name
                storage = file_instance.file.storage
                if storage.exists(file_name):
                    storage.delete(file_name)
            file_instance.delete()
            return Response({"message": "File deleted successfully"}, status=status.HTTP_204_NO_CONTENT)
        except LoadFile.DoesNotExist:
            return Response({"error": "File not found"}, status=status.HTTP_404_NOT_FOUND)
        except Exception as e:
            return Response({"error": "File deletion failed", "details": str(e)}, status=status.HTTP_400_BAD_REQUEST)


class LoadStopsViewSet(viewsets.ModelViewSet):
    queryset = LoadStop.objects.all()

    def get_serializer_class(self):
        if self.request.method in ['POST', 'PUT', 'PATCH']:
            return LoadStopWriteSerializer
        return LoadStopViewSerializer

    def get_permissions(self):
        if self.request.method == 'GET':
            permission_classes = [IsAuthenticated]
        else:
            permission_classes = [IsAdminUser | IsDispatch | IsDispatchManager | IsUpdater | IsBilling | IsPayroll]
        return [permission() for permission in permission_classes]

    def create(self, request, *args, **kwargs):
        with transaction.atomic():
            load_id = request.data.get('load')
            order_number = request.data.get('order')

            if not load_id or order_number is None:
                return Response({"error": "Load and order fields are required"}, status=400)

            exclusive_flags = ['destination', 'load_pickup']
            for flag in exclusive_flags:
                if request.data.get(flag, False):
                    LoadStop.objects.filter(load_id=load_id, **{flag: True}).update(**{flag: False})

            response = super().create(request, *args, **kwargs)
            new_stop = LoadStop.objects.get(id=response.data['id'])
            self.update_load_name(new_stop.load)

        empty_triggers = ['last_location', 'trailer_pickup', 'load_pickup', 'load_drop', 'trailer_drop', '']
        loaded_triggers = ['load_pickup', 'load_drop']
        has_empty_trigger = any(request.data.get(flag, False) for flag in empty_triggers)
        has_loaded_trigger = any(request.data.get(flag, False) for flag in loaded_triggers)
        if has_empty_trigger or has_loaded_trigger:
            debounce_recalculate_miles(new_stop.load_id)
        return response

    def update(self, request, *args, **kwargs):
        with transaction.atomic():
            instance = self.get_object()
            old_order = instance.order
            exclusive_flags = ['destination', 'load_pickup']
            for flag in exclusive_flags:
                incoming_flag_value = request.data.get(flag, getattr(instance, flag, False))
                if incoming_flag_value:
                    LoadStop.objects.filter(load=instance.load, **{flag: True}).exclude(id=instance.id).update(**{flag: False})

            response = super().update(request, *args, **kwargs)
            instance.refresh_from_db()
            self.update_load_name(instance.load)
        empty_triggers = ['last_location', 'trailer_pickup', 'load_pickup', 'load_drop', 'trailer_drop']
        loaded_triggers = ['load_pickup', 'load_drop']
        new_order = request.data.get('order')
        order_changed = new_order is not None and int(new_order) != old_order
        has_empty_trigger = any(request.data.get(flag) is not None for flag in empty_triggers)
        has_loaded_trigger = any(request.data.get(flag) is not None for flag in loaded_triggers)
        if order_changed or has_empty_trigger or has_loaded_trigger:
            debounce_recalculate_miles(instance.load_id)
        return response
    
    def destroy(self, request, *args, **kwargs):
        from .tasks import calculate_loaded_miles_background, calculate_empty_miles_multi_background
        with transaction.atomic():
            instance = self.get_object()
            load_id = instance.load_id
            should_recalc_empty = any([
                instance.trailer_pickup,
                instance.load_pickup,
                instance.load_drop,
                instance.trailer_drop,
            ])
            should_recalc_loaded = any([
                instance.load_pickup,
                instance.load_drop,
            ])
            response = super().destroy(request, *args, **kwargs)
        if should_recalc_empty:
            calculate_empty_miles_multi_background(load_id)
        if should_recalc_loaded:
            calculate_loaded_miles_background(load_id)
        return response

    @staticmethod
    def update_load_name(load):
        pickup_stop = load.loadstop_set.filter(load_pickup=True).order_by('order').first()
        drop_stop = load.loadstop_set.filter(load_drop=True).order_by('order').last()
        if pickup_stop and drop_stop:
            load.name = (
                f"From {pickup_stop.city}, {pickup_stop.state} "
                f"To {drop_stop.city}, {drop_stop.state}"
            )
        elif pickup_stop:
            load.name = (
                f"From {pickup_stop.city}, {pickup_stop.state}"
            )
        elif drop_stop:
            load.name = (
                f"To {drop_stop.city}, {drop_stop.state}"
            )
        else:
            load.name = f"Load {load.load_number or load.id}"
        load.save(update_fields=['name'])


class BookedByListAPIView(generics.ListAPIView):
    permission_classes = [IsAuthenticated]
    serializer_class = UserListSerializer

    def get_queryset(self):
        user = self.request.user
        user_department = user.department.name.lower()
        if user_department == 'management':
            return CustomUser.objects.filter(department__name__iexact='dispatch')
        return CustomUser.objects.filter(
            department__name__iexact='dispatch',
            company=user.company
        )


class RateConfirmationUploadView(views.APIView):
    from rest_framework.parsers import MultiPartParser
    parser_classes = [MultiPartParser]

    def post(self, request, *args, **kwargs):
        from .utils import parse_rate_confirmation
        from .gemini import parse_rate_confirmation_gemini
        uploaded_file = request.FILES.get('file')
        broker_id = request.data.get('broker')
        if not uploaded_file:
            return Response({"error": "No file provided"}, status=status.HTTP_400_BAD_REQUEST)
        if not uploaded_file.name.lower().endswith('.pdf'):
            return Response({"error": "Only PDF files are accepted"}, status=status.HTTP_400_BAD_REQUEST)

        broker = Broker.objects.filter(id=broker_id).first()
        if not broker:
            return Response({"error": "Broker not found"}, status=status.HTTP_400_BAD_REQUEST)
        try:
            if broker.ai_type == 'Gemini':
                parsed_data = parse_rate_confirmation_gemini(uploaded_file)
            else:
                parsed_data = parse_rate_confirmation(uploaded_file)
            return Response({
                "parsed_data": parsed_data
            }, status=200)
        except Exception as e:
            return Response({"error": f"Failed to process file: {str(e)}"}, status=status.HTTP_400_BAD_REQUEST)


class BatchViewSet(viewsets.ModelViewSet):
    queryset = Batch.objects.all().order_by('-date')
    pagination_class = CustomPagination
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        from django.db.models import Count, Case, When, BooleanField, Q, F
        base_queryset = Batch.objects.annotate(
            total_batchloads=Count('batchload'),
            completed_batchloads=Count(
                'batchload',
                filter=Q(batchload__status='Completed')
            ),
            all_completed=Case(
                When(
                    total_batchloads=F('completed_batchloads'),
                    then=True
                ),
                default=False,
                output_field=BooleanField()
            )
        )

        if self.request.user.username == 'bmfactoringuser@gmail.com':
            return base_queryset.order_by('-date', '-id')
        return base_queryset.order_by('all_completed', '-date', '-id')

    def get_serializer_class(self):
        if self.request.method in ['POST', 'PUT', 'PATCH']:
            return BatchWriteSerializer
        return BatchViewSerializer


class BatchLoadViewSet(viewsets.ModelViewSet):
    queryset = BatchLoad.objects.all()
    permission_classes = [IsAuthenticated]

    def get_serializer_class(self):
        if self.request.method in ['POST', 'PATCH', 'PUT']:
            return BatchLoadWriteSerializer
        return BatchLoadViewSerializer

    def perform_create(self, serializer):
        serializer.save(created_by=self.request.user, status='In Review')

    def perform_update(self, serializer):
        instance = self.get_object()
        old_status = instance.status
        updated_instance = serializer.save()
        if old_status != 'Completed' and updated_instance.status == 'Completed':
            invoiced = LoadStatus.objects.get(name__iexact='Invoiced')
            updated_instance.load.status = invoiced
            updated_instance.load.save()


class BatchLoadDropDown(generics.ListAPIView):
    serializer_class = LoadByDriverSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        assigned_load_ids = BatchLoad.objects.values_list('load_id', flat=True)
        queryset = Load.objects.filter(status__name__in=['Factored']).exclude(id__in=assigned_load_ids)
        company_id = self.request.query_params.get('carrier')
        if company_id:
            queryset = queryset.filter(company_id=company_id)
        return queryset.order_by('-id')


class MultipleBatchLoadCreateView(generics.CreateAPIView):
    serializer_class = BatchMultipleLoadSerializer
    permission_classes = [IsAuthenticated]

    @transaction.atomic
    def create(self, request, *args, **kwargs):
        from .tasks import async_generate_invoices
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        batch_id = serializer.validated_data['batch']
        load_ids = serializer.validated_data['loads']
        try:
            batch = Batch.objects.get(id=batch_id)
        except Batch.DoesNotExist:
            return Response({'error': 'Batch not found.'}, status=status.HTTP_404_NOT_FOUND)
        already_assigned_loads = BatchLoad.objects.filter(load_id__in=load_ids).values_list('load_id', flat=True)
        valid_load_ids = set(load_ids) - set(already_assigned_loads)
        if not valid_load_ids:
            return Response({'error': 'All selected loads are already assigned to batches.'}, status=status.HTTP_400_BAD_REQUEST)
        loads = Load.objects.filter(id__in=valid_load_ids)
        batch_loads = [
            BatchLoad(
                batch=batch,
                load=load,
                status='In Review',
                created_by=request.user
            ) for load in loads
        ]
        BatchLoad.objects.bulk_create(batch_loads)
        async_generate_invoices(batch_id, valid_load_ids, request.user.id)
        return Response({
            'message': f'{len(batch_loads)} BatchLoad records created successfully.',
            'skipped_loads': list(already_assigned_loads)
        }, status=status.HTTP_201_CREATED)


class LoadByDriverForStatementView(generics.ListAPIView):
    serializer_class = LoadByDriverSerializer

    def get_queryset(self):
        from django.utils.dateparse import parse_datetime
        from payroll.models import StatementLoad
        from django.db.models import Q
        start_date = self.request.query_params.get('start_date')
        end_date = self.request.query_params.get('end_date')
        driver_id = self.request.query_params.get('driver')
        if not (start_date and end_date and driver_id):
            return Response({'message': 'Provide start_date, end_date and driver'}, status=status.HTTP_400_BAD_REQUEST)

        try:
            start_date = parse_datetime(start_date)
            end_date = parse_datetime(end_date)
            driver_id = int(driver_id)
        except (ValueError, TypeError):
            return Response({'message': 'TypeError'}, status=status.HTTP_400_BAD_REQUEST)

        load_ids = Load.objects.filter(driver_id=driver_id).values_list('id', flat=True)
        batch_load_ids = BatchLoad.objects.filter(load_id__in=load_ids).values_list('load_id', flat=True)
        already_paid_load_ids = StatementLoad.objects.filter(
            statement__driver_id=driver_id
        ).values_list('load_id', flat=True)
        return Load.objects.filter(
            Q(id__in=load_ids) &
            Q(id__in=batch_load_ids) &
            Q(pickup_date__date__lte=end_date.date()) &
            Q(driver_pay__gt=0)
        ).exclude(
            Q(status__name__in=['Cancelled', 'Rejected']) |
            Q(id__in=already_paid_load_ids)
        )

    def list(self, request, *args, **kwargs):
        queryset = self.get_queryset()
        if not queryset.exists():
            return Response({"message": "No loads found for the given driver in the selected period."}, status=200)
        serializer = self.get_serializer(queryset, many=True)
        return Response(serializer.data, status=status.HTTP_200_OK)


class BatchLoadByIDListAPIView(generics.ListAPIView):
    permission_classes = [IsAuthenticated]
    pagination_class = CustomPagination
    serializer_class = BatchLoadViewSerializer
    filter_backends = [DjangoFilterBackend, SearchFilter, OrderingFilter]
    filterset_fields = ['load__company__name']
    search_fields = ['load__load_number', 'load__shipment']

    def get_queryset(self):
        batch = self.kwargs.get('pk')
        return BatchLoad.objects.filter(batch=batch).order_by('-status')


class LoadsCountByProcessView(views.APIView):
    def get(self, request):
        user = self.request.user
        queryset = Load.objects.all()
        start_date_str = self.request.query_params.get('start_date')
        end_date_str = self.request.query_params.get('end_date')
        delivery_date_str = self.request.query_params.get('delivery_date')
        start_date = parse_date(start_date_str) if start_date_str else None
        end_date = parse_date(end_date_str) if end_date_str else None
        delivery_date = parse_date(delivery_date_str) if delivery_date_str else None

        if start_date and end_date:
            queryset = queryset.filter(
                Q(pickup_date__gte=start_date),
                Q(drop_date__lte=end_date)
            )
        if delivery_date:
            queryset = queryset.filter(
                drop_date__lte=delivery_date
            )
        if user.department.name.lower() in ['dispatch', 'dispatch manager', 'team lead', 'updater']:
            queryset = queryset.filter(company=user.company)
        filter_params = {
            'company': request.query_params.get('carrier'),
            'broker': request.query_params.get('broker'),
            'booked_by': request.query_params.get('booked_by'),
            'from_facility': request.query_params.get('from_facility'),
            'status': request.query_params.get('status'),
        }
        filter_params = {k: v for k, v in filter_params.items() if v is not None}
        if filter_params:
            queryset = queryset.filter(**filter_params)

        search_term = request.query_params.get('search')
        if search_term:
            queryset = queryset.filter(
                Q(load_number__icontains=search_term) |
                Q(shipment__icontains=search_term)
            )

        status_counts = queryset.values(
            'status__id',
            'status__name'
        ).annotate(
            count=Count('id')
        )
        result = {
            'counts': list(status_counts),
            'total': queryset.count()
        }
        return Response(result)


class BatchListView(generics.ListAPIView):
    queryset = Batch.objects.all().order_by('-id')
    serializer_class = BatchUseSerializer


class GenerateCSVView(views.APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, *args, **kwargs):
        from django.utils import timezone
        from .tasks import process_batch_in_background
        batch_id = request.data.get('batch')
        if not batch_id:
            return Response({
                'message': 'Batch is required'
            }, status=400)
        try:
            batch = Batch.objects.get(id=batch_id)
            process_batch_in_background.delay(batch.id, request.user.id)
            batch.submitted = True
            batch.save(update_fields=['submitted'])
            return Response({
                'status': 'processing',
                'message': f'Batch "{batch.name}" is being processed in the background. '
                           f'You will receive Telegram notifications about the progress.',
                'batch_id': batch_id,
                'batch_name': batch.name,
                'start_time': timezone.now().isoformat()
            })
        except Batch.DoesNotExist:
            return Response({
                'status': 'error',
                'message': f'Batch with id {batch_id} not found'
            }, status=status.HTTP_404_NOT_FOUND)
        except Exception as e:
            return Response({
                'status': 'error',
                'message': f'An error occurred: {str(e)}',
                'batch_id': batch_id
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


class DailyReportByDispatchersAPIView(views.APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, *args, **kwargs):
        start_date_str = request.query_params.get('start_date')
        end_date_str = request.query_params.get('end_date')
        filter_type = request.query_params.get('type', 'drop_date')
        today = date.today()
        try:
            start_date = parse_date(start_date_str) if start_date_str else today
            end_date = parse_date(end_date_str) if end_date_str else today
            if not start_date or not end_date:
                raise ValueError
        except ValueError:
            return Response(
                {'message': 'Invalid date format. Use YYYY-MM-DD.'},
                status=status.HTTP_400_BAD_REQUEST
            )

        start_dt = datetime.combine(start_date, time.min)
        end_dt = datetime.combine(end_date, time.max)
        user_department = request.user.department
        privileged_departments = ['management', 'payroll', 'billing']
        is_privileged = user_department and user_department.name.lower() in privileged_departments
        loads = Load.objects.filter(
            booked_by__department__name__iexact='dispatch',
        ).exclude(
            status__name__in=['Cancelled', 'Rejected']
        )
        loads = loads.filter(pickup_date__range=(start_dt, end_dt))
        if not is_privileged:
            loads = loads.filter(booked_by__company=request.user.company)
        
        profit_expression = ExpressionWrapper(
            F('carrier_pay') - F('driver_pay'),
            output_field=DecimalField(max_digits=20, decimal_places=2)
        )
        report = loads.values(
            'booked_by__id',
            'booked_by__first_name',
            'booked_by__last_name',
            'booked_by__username',
            'booked_by__company__name',
        ).annotate(
            total_loads=Count('id'),
            total_driver_pay=Sum('driver_pay'),
            total_carrier_pay=Sum('carrier_pay'),
            total_profit=Sum(profit_expression)
        ).order_by('-total_profit')
        return Response(report, status=status.HTTP_200_OK)
    

class LoadsPaySummaryAPIView(views.APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, *args, **kwargs):
        user = request.user
        privileged_departments = ['management', 'billing', 'payroll']
        is_privileged = user.department and user.department.name.lower() in privileged_departments

        if is_privileged:
            queryset = Load.objects.all().exclude(status__name__in=['Cancelled', 'Rejected'])
        else:
            queryset = Load.objects.filter(company=user.company).exclude(status__name__in=['Cancelled', 'Rejected'])

        company = request.query_params.get('company')
        broker = request.query_params.get('broker')
        booked_by = request.query_params.get('booked_by')
        load_status = request.query_params.get('status')
        driver = request.query_params.get('driver')
        payment_type = request.query_params.get('payment_type')
        start_date = request.query_params.get('start_date')
        end_date = request.query_params.get('end_date')
        date_type = request.query_params.get('date_type', 'pickup_date')
        tag_ids = request.query_params.get('tags', None)
        except_tags = request.query_params.get('except', 'false').lower() == 'true'

        if company:
            queryset = queryset.filter(company=company)
        if broker:
            queryset = queryset.filter(broker=broker)
        if booked_by:
            queryset = queryset.filter(booked_by=booked_by)
        if load_status:
            queryset = queryset.filter(status=load_status)
        if driver:
            queryset = queryset.filter(driver=driver)
        if payment_type:
            queryset = queryset.filter(payment_type=payment_type)
        if tag_ids:
            try:
                tag_id_list = [int(i) for i in tag_ids.split(',')]
            except ValueError:
                return Response({"error": "Invalid tag IDs. Must be comma-separated integers."}, status=status.HTTP_400_BAD_REQUEST)
            
            if except_tags:
                queryset = queryset.exclude(loadtag__tag_id__in=tag_id_list).distinct()
            else:
                queryset = queryset.filter(loadtag__tag_id__in=tag_id_list).distinct()

        if not is_privileged:
            queryset = queryset.filter(driver__company=user.company)

        if date_type not in ['pickup_date', 'drop_date']:
            return Response({'message': 'Invalid date_type. Use "pickup_date" or "drop_date".'}, status=status.HTTP_400_BAD_REQUEST)

        if start_date and end_date:
            try:
                start_date_parsed = parse_date(start_date)
                end_date_parsed = parse_date(end_date)
                if not start_date_parsed or not end_date_parsed:
                    raise ValueError
                date_filter = {
                    f"{date_type}__date__gte": start_date_parsed,
                    f"{date_type}__date__lte": end_date_parsed
                }
                queryset = queryset.filter(**date_filter)
            except ValueError:
                return Response({'message': 'Invalid date format. Use YYYY-MM-DD.'}, status=status.HTTP_400_BAD_REQUEST)

        result = queryset.aggregate(
            total_loads=Count('id'),
            total_driver_pay=Sum('driver_pay'),
            total_carrier_pay=Sum('carrier_pay'),
        )
        return Response({
            'total_loads': result['total_loads'] or 0,
            'total_driver_pay': result['total_driver_pay'] or 0,
            'total_carrier_pay': result['total_carrier_pay'] or 0,
        }, status=status.HTTP_200_OK)


class PaymentTypeViewSet(viewsets.ModelViewSet):
    queryset = PaymentType.objects.all().order_by('id')
    serializer_class = PaymentTypeSerializer
    permission_classes = [IsAuthenticated]


class BrokerImportAPIView(views.APIView):
    permission_classes = [IsAuthenticated]

    def clean_value(self, value):
        if pd.isna(value):
            return None
        value = str(value).strip()
        return value if value else None

    def post(self, request, *args, **kwargs):
        excel_file = request.FILES.get("file")

        if not excel_file:
            return Response(
                {"detail": "No file uploaded. Use form-data with key 'file'."},
                status=status.HTTP_400_BAD_REQUEST
            )

        if not excel_file.name.endswith(".xlsx"):
            return Response(
                {"detail": "Only .xlsx files are allowed."},
                status=status.HTTP_400_BAD_REQUEST
            )

        try:
            raw_df = pd.read_excel(excel_file, engine="openpyxl", header=None)
        except Exception as e:
            return Response(
                {"detail": f"Failed to read Excel file: {str(e)}"},
                status=status.HTTP_400_BAD_REQUEST
            )

        if raw_df.empty:
            return Response(
                {"detail": "Excel file is empty."},
                status=status.HTTP_400_BAD_REQUEST
            )

        required_columns = {"Name", "MC", "Address", "City", "State", "Zip Code"}
        header_row_index = None
        for i, row in raw_df.iterrows():
            row_values = {str(cell).strip() for cell in row if pd.notna(cell)}

            if required_columns.issubset(row_values):
                header_row_index = i
                break

        if header_row_index is None:
            return Response(
                {
                    "detail": "Could not find the correct header row in the Excel file.",
                    "expected_columns": list(required_columns),
                    "preview_rows": raw_df.head(10).fillna("").values.tolist()
                },
                status=status.HTTP_400_BAD_REQUEST
            )

        excel_file.seek(0)
        df = pd.read_excel(excel_file, engine="openpyxl", header=header_row_index)
        df.columns = [str(col).strip() for col in df.columns]
        missing_columns = [col for col in required_columns if col not in df.columns]
        if missing_columns:
            return Response(
                {
                    "detail": "Missing required columns after detecting header row.",
                    "missing_columns": missing_columns,
                    "found_columns": df.columns.tolist(),
                    "header_row_index": header_row_index + 1
                },
                status=status.HTTP_400_BAD_REQUEST
            )

        created_count = 0
        skipped_count = 0
        skipped_mcs = []
        created_mcs = []
        errors = []

        for index, row in df.iterrows():
            try:
                name = self.clean_value(row.get("Name"))
                mc = self.clean_value(row.get("MC"))
                address = self.clean_value(row.get("Address"))
                city = self.clean_value(row.get("City"))
                state_value = self.clean_value(row.get("State"))
                zipcode = self.clean_value(row.get("Zip Code"))
                if not any([name, mc, address, city, state_value, zipcode]):
                    continue

                if name and name.lower().startswith("total:"):
                    continue

                if not mc:
                    errors.append({
                        "row": int(index) + header_row_index + 2,
                        "error": "MC is missing"
                    })
                    continue

                if mc.endswith(".0"):
                    mc = mc[:-2]

                if not name:
                    errors.append({
                        "row": int(index) + header_row_index + 2,
                        "mc": mc,
                        "error": "Name is missing"
                    })
                    continue

                if Broker.objects.filter(mc=mc).exists():
                    skipped_count += 1
                    skipped_mcs.append(mc)
                    continue

                Broker.objects.create(
                    name=name,
                    mc=mc,
                    address=address,
                    city=city,
                    state=state_value,
                    zipcode=zipcode,
                )

                created_count += 1
                created_mcs.append(mc)

            except Exception as e:
                errors.append({
                    "row": int(index) + header_row_index + 2,
                    "error": str(e)
                })

        return Response(
            {
                "message": "Broker import completed.",
                "header_row_detected": header_row_index + 1,
                "total_rows_in_excel": len(df),
                "created_count": created_count,
                "skipped_count": skipped_count,
                "created_mcs": created_mcs,
                "skipped_mcs": skipped_mcs,
                "errors": errors,
            },
            status=status.HTTP_200_OK
        )


class BestProfitLoadsAPIView(views.APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, *args, **kwargs):
        date_str = request.query_params.get("date")

        if not date_str:
            return Response(
                {"detail": "date is required. Format: YYYY-MM-DD"},
                status=status.HTTP_400_BAD_REQUEST
            )

        try:
            target_date = datetime.strptime(date_str, "%Y-%m-%d").date()
        except ValueError:
            return Response(
                {"detail": "Invalid date format. Use YYYY-MM-DD"},
                status=status.HTTP_400_BAD_REQUEST
            )

        loads = (
            Load.objects
            .select_related("company", "driver", "broker")
            .filter(drop_date__date=target_date)
            .annotate(
                profit=ExpressionWrapper(
                    Coalesce(F("carrier_pay"), Value(0)) - Coalesce(F("driver_pay"), Value(0)),
                    output_field=DecimalField(max_digits=20, decimal_places=2)
                )
            )
            .order_by("-profit")[:5]
        )
        data = []
        for load in loads:
            data.append({
                "id": load.id,
                "company_name": load.company.name if load.company else None,
                "shipment": f"SH - {load.shipment}" if load.shipment else None,
                "booked_by": load.booked_by.username if load.booked_by else None,
                "updated_by": load.updated_by.username if load.updated_by else None,
                "driver_name": load.driver.full_name if load.driver else None,
                "carrier_pay": load.carrier_pay,
                "driver_pay": load.driver_pay,
                "profit": load.profit,
                "pickup_date": load.pickup_date,
                "drop_date": load.drop_date,
            })
        return Response(
            {
                "date": date_str,
                "count": len(data),
                "results": data
            },
            status=status.HTTP_200_OK
        )


class TagViewSet(viewsets.ModelViewSet):
    permission_classes = [IsAuthenticated]
    serializer_class = TagSerializer
    queryset = Tag.objects.all()


class LoadTagViewSet(viewsets.ModelViewSet):
    permission_classes = [IsAuthenticated]
    queryset = LoadTag.objects.all()

    def get_serializer_class(self):
        if self.request.method in ['POST', 'PUT', 'PATCH']:
            return LoadTagWriteSerializer
        return LoadTagViewSerializer
