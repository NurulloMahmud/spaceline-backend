from rest_framework import serializers
from django.db import transaction

from payroll.models import StatementLoad
from .models import (Broker,
                     LoadStatus, Load, LoadHistory, LoadFile, LoadStop,
                     Batch, BatchLoad, PaymentType, Tag, LoadTag
                     )


class BrokersSerializer(serializers.ModelSerializer):
    
    class Meta:
        model = Broker
        fields = '__all__'


class BrokersUseSerializer(serializers.ModelSerializer):

    class Meta:
        model = Broker
        fields = ['id', 'name', 'address']


class LoadStatusesSerializer(serializers.ModelSerializer):

    class Meta:
        model = LoadStatus
        fields = '__all__'


class LoadHistoryViewSerializer(serializers.ModelSerializer):
    load = serializers.SerializerMethodField(method_name='get_load')
    changed_by = serializers.SerializerMethodField(method_name='get_changed_by')

    class Meta:
        model = LoadHistory
        fields = '__all__'

    def get_load(self, obj):
        if obj.load:
            return {
                "id": obj.load.id,
                "company": obj.load.company.name,
            }
        return None

    def get_changed_by(self, obj):
        if obj.changed_by:
            return {
                "id": obj.changed_by.id,
                "username": obj.changed_by.username,
                "first_name": obj.changed_by.first_name,
                "last_name": obj.changed_by.last_name
            }
        return None


class LoadHistoryWriteSerializer(serializers.ModelSerializer):
    class Meta:
        model = LoadHistory
        fields = '__all__'


class LoadStopViewSerializer(serializers.ModelSerializer):
    load = serializers.SerializerMethodField(method_name='get_load')

    class Meta:
        model = LoadStop
        fields = '__all__'

    def get_load(self, obj):
        if obj.load:
            return {
                "id": obj.load.id,
                "company": obj.load.company.name,
                "load_number": obj.load.load_number
            }
        return None


class LoadStopWriteSerializer(serializers.ModelSerializer):
    class Meta:
        model = LoadStop
        fields = '__all__'


class BatchLoadViewSerializer(serializers.ModelSerializer):
    load = serializers.SerializerMethodField(method_name='get_load')
    batch = serializers.SerializerMethodField(method_name='get_batch')
    created_by = serializers.SerializerMethodField(method_name='get_created_by')

    class Meta:
        model = BatchLoad
        fields = '__all__'

    def get_load(self, obj):
        if obj.load:
            return {
                "id": obj.load.id,
                "load_number": obj.load.load_number,
                "company": obj.load.company.name,
                "shipment": f"SH - {obj.load.shipment}" if obj.load.shipment else None,
                "carrier_pay": obj.load.carrier_pay,
                "driver_pay": obj.load.driver_pay,
                "status": obj.load.status.name if obj.load.status else None
            }
        return None

    def get_batch(self, obj):
        if obj.batch:
            return {
                "id": obj.batch.id,
                "name": obj.batch.name,
                "date": obj.batch.date
            }
        return None

    def get_created_by(self, obj):
        if obj.created_by:
            return {
                "id": obj.created_by.id,
                "username": obj.created_by.username,
                "first_name": obj.created_by.first_name,
                "last_name": obj.created_by.last_name
            }
        return None


class BatchLoadWriteSerializer(serializers.ModelSerializer):

    class Meta:
        model = BatchLoad
        fields = '__all__'


class BatchViewSerializer(serializers.ModelSerializer):
    loads_count = serializers.SerializerMethodField()
    total_carrier_pay = serializers.SerializerMethodField()

    class Meta:
        model = Batch
        fields = '__all__'

    def get_loads_count(self, obj):
        from django.db.models import Count
        status_counts = obj.batchload_set.values('status').annotate(count=Count('id'))
        result = {
            'In Review': 0,
            'Completed': 0,
            'Total': 0
        }
        total = 0
        for entry in status_counts:
            status = entry['status']
            count = entry['count']
            if status in ['In Review', 'Completed']:
                result[status] = count
            total += count
        result['Total'] = total
        return result

    def get_total_carrier_pay(self, obj):
        from django.db.models import Sum
        total = obj.batchload_set.aggregate(
            total_pay=Sum('load__carrier_pay')
        )['total_pay']
        return total or 0


class BatchWriteSerializer(serializers.ModelSerializer):

    class Meta:
        model = Batch
        fields = '__all__'


class BatchMultipleLoadSerializer(serializers.Serializer):
    batch = serializers.IntegerField()
    loads = serializers.ListField(child=serializers.IntegerField(), allow_empty=False)


class BatchFileUploadSerializer(serializers.Serializer):
    batch = serializers.PrimaryKeyRelatedField(queryset=Batch.objects.all())
    file = serializers.FileField()


class BatchUseSerializer(serializers.ModelSerializer):

    class Meta:
        model = Batch
        fields = ['id', 'name', 'date']


class LoadFilesViewSerializer(serializers.ModelSerializer):
    load = serializers.SerializerMethodField(method_name='get_load')
    file = serializers.SerializerMethodField(method_name='get_file')

    class Meta:
        model = LoadFile
        fields = ['id', 'load', 'file', 'name', 'created_at', 'last_updated']

    def get_load(self, obj):
        if obj.load:
            return {
                "id": obj.load.id,
                "company": obj.load.company.name
            }
        return None
    
    def get_file(self, obj):
        if obj.file:
            return obj.file.url
        return obj.rc_url


class LoadFilesWriteSerializer(serializers.ModelSerializer):
    class Meta:
        model = LoadFile
        fields = ['name', 'file', 'load', 'rc_url']

    def create(self, validated_data):
        return LoadFile.objects.create(**validated_data)


class LoadsWriteSerializer(serializers.ModelSerializer):
    stops = serializers.ListField(write_only=True, required=False)
    split = serializers.BooleanField(write_only=True, required=False, default=False)
    main_load = serializers.PrimaryKeyRelatedField(
        queryset=Load.objects.all(),
        required=False,
        write_only=True,
        allow_null=True
    )
    driver_payment = serializers.DecimalField(
        max_digits=10,
        decimal_places=2,
        required=False,
        write_only=True
    )
    split_pickup_date = serializers.DateTimeField(write_only=True, required=False)
    split_drop_date = serializers.DateTimeField(write_only=True, required=False)

    class Meta:
        model = Load
        fields = '__all__'

    def validate(self, attrs):
        split = attrs.get('split', False)
        main_load = attrs.get('main_load')
        split_pickup_date = attrs.get('split_pickup_date')
        split_drop_date = attrs.get('split_drop_date')
        driver_payment = attrs.get('driver_payment')
        request = self.context.get('request')
        user = request.user if request else None
        user_dept = getattr(getattr(user, 'department', None), 'name', '').lower()

        if request and request.method != 'POST':
            return attrs

        if split:
            if not main_load:
                raise serializers.ValidationError({"main_load": "This field is required when split=True."})
            if not split_pickup_date:
                raise serializers.ValidationError({"split_pickup_date": "This field is required when split=True."})
            if not split_drop_date:
                raise serializers.ValidationError({"split_drop_date": "This field is required when split=True."})

            if StatementLoad.objects.filter(load=main_load).exists():
                statement = StatementLoad.objects.select_related('statement__driver').get(load=main_load).statement
                raise serializers.ValidationError({
                    "detail": (
                        f"This load is part of driver statement: "
                        f"{statement.driver.name} ({statement.start_date} - {statement.end_date}). "
                        f"Remove it before splitting."
                    )
                })

            if driver_payment is not None:
                if main_load.driver_pay is None or main_load.driver_pay < driver_payment:
                    raise serializers.ValidationError({
                        "driver_payment": "Driver pay cannot be greater than the main load's driver pay."
                    })

        else:
            load_number = attrs.get('load_number')
            if load_number and Load.objects.filter(load_number=load_number).exists():
                raise serializers.ValidationError({
                    "load_number": f"Load with load_number '{load_number}' already exists."
                })

            if user_dept in ['management', 'billing', 'payroll'] and not attrs.get('company'):
                raise serializers.ValidationError({
                    "company": "Company is required for users in management, billing, or payroll."
                })
        return attrs

    def create(self, validated_data):
        split = validated_data.pop('split', False)
        main_load = validated_data.pop('main_load', None)
        driver_payment = validated_data.pop('driver_payment', None)
        split_pickup_date = validated_data.pop('split_pickup_date', None)
        split_drop_date = validated_data.pop('split_drop_date', None)
        stops_data = validated_data.pop('stops', [])
        with transaction.atomic():
            if split:
                load = self._create_split_load(
                    validated_data=validated_data,
                    main_load=main_load,
                    split_pickup_date=split_pickup_date,
                    split_drop_date=split_drop_date,
                    driver_payment=driver_payment
                )

                self._copy_stops_from_main_load(load, main_load)
                if driver_payment is not None:
                    main_load.driver_pay -= driver_payment
                    main_load.save(update_fields=['driver_pay'])

            else:
                validated_data['created_by'] = self.context['request'].user
                load = Load.objects.create(**validated_data)
                self._create_stops(load, stops_data)
            self.update_load_name(load)
            self._pass_stops(load)
            return load

    def _create_split_load(self, validated_data, main_load, split_pickup_date, split_drop_date, driver_payment):
        validated_data.update({
            'company': main_load.company,
            'broker': main_load.broker,
            'booked_by': main_load.booked_by,
            'loaded_miles': main_load.loaded_miles,
            'empty_miles': main_load.empty_miles,
            'status': main_load.status,
            'main_load': main_load,
            'pickup_date': split_pickup_date,
            'drop_date': split_drop_date,
            'created_by': self.context['request'].user,
            'driver': main_load.driver,
            'load_number': self._generate_split_load_number(main_load),
        })
        if driver_payment is not None:
            validated_data['driver_pay'] = driver_payment
        return Load.objects.create(**validated_data)

    def _copy_stops_from_main_load(self, load, main_load):
        main_stops = main_load.loadstop_set.all().order_by('order')
        for stop in main_stops:
            LoadStop.objects.create(
                load=load,
                address=stop.address,
                city=stop.city,
                state=stop.state,
                zipcode=stop.zipcode,
                order=stop.order,
                trailer_pickup=stop.trailer_pickup,
                trailer_drop=stop.trailer_drop,
                last_location=stop.last_location,
                partial=stop.partial,
                load_pickup=stop.load_pickup,
                load_drop=stop.load_drop,
                trailer_info=stop.trailer_info,
                requirements=stop.requirements,
                note=stop.note,
            )

    def _create_stops(self, load, stops_data):
        for stop in stops_data:
            LoadStop.objects.create(
                load=load,
                address=stop.get('address'),
                city=stop.get('city'),
                state=stop.get('state'),
                zipcode=stop.get('zipcode'),
                order=stop.get('order'),
                trailer_pickup=stop.get('trailer_pickup', False),
                trailer_drop=stop.get('trailer_drop', False),
                last_location=stop.get('last_location', False),
                partial=stop.get('partial', False),
                load_pickup=stop.get('load_pickup', False),
                load_drop=stop.get('load_drop', False),
                trailer_info=stop.get('trailer_info'),
                requirements=stop.get('requirements'),
                note=stop.get('note'),
            )

    def _generate_split_load_number(self, main_load):
        base_number = f"{main_load.load_number}-SPLIT"
        return f"{base_number}"

    def _pass_stops(self, load):
        from .mile import calculate_loaded_miles, calculate_empty_miles_for_load
        calculate_empty_miles_for_load(load)
        calculate_loaded_miles(load)

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


class LoadUseSerializer(serializers.ModelSerializer):
    booked_by = serializers.SerializerMethodField(method_name='get_booked_by')
    created_by = serializers.SerializerMethodField(method_name='get_created_by')
    updated_by = serializers.SerializerMethodField(method_name='get_updated_by')
    broker = serializers.SerializerMethodField(method_name='get_broker')
    status = serializers.SerializerMethodField()

    class Meta:
        model = Load
        fields = '__all__'

    def get_booked_by(self, obj):
        if obj.booked_by:
            return {
                "id": obj.booked_by.id,
                "username": obj.booked_by.username,
                "first_name": obj.booked_by.first_name,
                "last_name": obj.booked_by.last_name
            }
        return None
    
    def get_updated_by(self, obj):
        if obj.updated_by:
            return {
                "id": obj.updated_by.id,
                "username": obj.updated_by.username,
                "first_name": obj.updated_by.first_name,
                "last_name": obj.updated_by.last_name
            }
        return None

    def get_created_by(self, obj):
        if obj.created_by:
            return {
                "id": obj.created_by.id,
                "username": obj.created_by.username,
                "first_name": obj.created_by.first_name,
                "last_name": obj.created_by.last_name
            }
        return None

    def get_broker(self, obj):
        return obj.broker.name if obj.broker else None

    def get_status(self, obj):
        return obj.status.name if obj.status else None


class LoadByDriverSerializer(serializers.ModelSerializer):

    class Meta:
        model = Load
        fields = ['id', 'load_number', 'shipment', 'driver_pay', 'carrier_pay', 'pickup_date', 'drop_date']


class PaymentTypeSerializer(serializers.ModelSerializer):

    class Meta:
        model = PaymentType
        fields = '__all__'


class TagSerializer(serializers.ModelSerializer):

    class Meta:
        model = Tag
        fields = '__all__'


class LoadTagViewSerializer(serializers.ModelSerializer):
    tag = serializers.SerializerMethodField(method_name='get_tag')

    class Meta:
        model = LoadTag
        fields = '__all__'

    def get_tag(self, obj):
        if obj.tag:
            return {
                "id": obj.tag.id,
                "name": obj.tag.name,
                "color": obj.tag.color if obj.tag.color else None
            }
        return None


class LoadTagWriteSerializer(serializers.ModelSerializer):

    class Meta:
        model = LoadTag
        fields = '__all__'


class LoadsViewSerializer(serializers.ModelSerializer):
    company = serializers.SerializerMethodField(method_name='get_company')
    booked_by = serializers.SerializerMethodField(method_name='get_booked_by')
    created_by = serializers.SerializerMethodField(method_name='get_created_by')
    updated_by = serializers.SerializerMethodField(method_name='get_updated_by')
    broker = serializers.SerializerMethodField(method_name='get_broker')
    driver = serializers.SerializerMethodField(method_name='get_driver')
    load_files = serializers.SerializerMethodField(method_name='get_load_files')
    status = serializers.SerializerMethodField(method_name='get_status')
    load_stops = serializers.SerializerMethodField(method_name='get_load_stops')
    shipment = serializers.SerializerMethodField(method_name='get_shipment')
    tags = serializers.SerializerMethodField(method_name='get_tags')
    profit = serializers.SerializerMethodField(method_name='get_profit')

    class Meta:
        model = Load
        fields = ['id', 'company', 'driver', 'booked_by', 'created_by', 'broker', 'shipment', 'load_number',
                  'driver_pay', 'carrier_pay', 'pickup_date', 'drop_date', 'status', 'created_at', 'payment_type',
                  'last_updated', 'load_files', 'load_stops', 'note', 'loaded_miles', 'empty_miles', 'main_load',
                  'delivered_at', 'updated_by', 'tags', 'dispatcher_note', 'profit'
                ]

    def get_load_files(self, obj):
        load_files = obj.loadfile_set.all()
        return LoadFilesViewSerializer(load_files, many=True).data

    def get_load_stops(self, obj):
        load_stops = obj.loadstop_set.all().order_by('order')
        return LoadStopViewSerializer(load_stops, many=True).data

    def get_booked_by(self, obj):
        if obj.booked_by:
            return {
                "id": obj.booked_by.id,
                "username": obj.booked_by.username,
                "first_name": obj.booked_by.first_name,
                "last_name": obj.booked_by.last_name
            }
        return None
    
    def get_updated_by(self, obj):
        if obj.updated_by:
            return {
                "id": obj.updated_by.id,
                "username": obj.updated_by.username,
                "first_name": obj.updated_by.first_name,
                "last_name": obj.updated_by.last_name
            }
        return None

    def get_company(self, obj):
        if obj.company:
            return {
                "id": obj.company.id,
                "name": obj.company.name
            }
        return None

    def get_status(self, obj):
        if obj.status:
            return {
                "id": obj.status.id,
                "name": obj.status.name
            }
        return None

    def get_created_by(self, obj):
        if obj.created_by:
            return {
                "id": obj.created_by.id,
                "username": obj.created_by.username,
                "first_name": obj.created_by.first_name,
                "last_name": obj.created_by.last_name
            }
        return None

    def get_broker(self, obj):
        if obj.broker:
            return {
                "id": obj.broker.id,
                "name": obj.broker.name,
                "mc": obj.broker.mc,
                "address": obj.broker.address if obj.broker.address else None,
                "city": obj.broker.city if obj.broker.city else None,
                "state": obj.broker.state if obj.broker.state else None
            }
        return None

    def get_driver(self, obj):
        if obj.driver:
            return {
                "id": obj.driver.id,
                "full_name": obj.driver.full_name,
                "unit_numer": obj.driver.unit_number if obj.driver.unit_number else None,
                "company": obj.driver.company.name if obj.driver.company.name else None
            }
        return None

    def get_shipment(self, obj):
        return f"SH - {obj.shipment}"
    
    def get_tags(self, obj):
        load_tags = obj.loadtag_set.all()
        return LoadTagViewSerializer(load_tags, many=True).data
    
    def get_profit(self, obj):
        profit = (obj.carrier_pay or 0) - (obj.driver_pay or 0)
        return profit

