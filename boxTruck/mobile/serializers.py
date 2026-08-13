from billing.models import Load
from rest_framework import serializers
from billing.serializers import LoadStopViewSerializer
from users.models import Company
from .models import DriverLocation


class CompanyViewSerializer(serializers.ModelSerializer):
    
    class Meta:
        model = Company
        fields = ['id', 'name', 'description', 'email', 'phone_number', 'mc', 'website']


class DriverLocationViewSerializer(serializers.ModelSerializer):
    driver = serializers.SerializerMethodField()

    def get_driver(self, obj):
        return {
            "id": obj.driver.id,
            "full_name": obj.driver.full_name,
            "phone_number": obj.driver.phone_number,
            "email": obj.driver.email if obj.driver.email else None,
            "emergency_phone_number": obj.driver.emergency_phone_number if obj.driver.emergency_phone_number else None,
            "status": obj.driver.status.name,
            'vehicle': self._get_vehicle(obj.driver),
            "manager": obj.driver.manager.username + " - " + obj.driver.manager.first_name + " " + obj.driver.manager.last_name if obj.driver.manager else None
        }

    class Meta:
        model = DriverLocation
        fields = '__all__'


class DriverLocationWriteSerializer(serializers.ModelSerializer):

    class Meta:
        model = DriverLocation
        fields = '__all__'


class DriverLoadSerializer(serializers.ModelSerializer):
    status = serializers.SerializerMethodField()
    broker = serializers.SerializerMethodField()
    load_stops = serializers.SerializerMethodField()

    class Meta:
        model = Load
        fields = [
            'id',
            'shipment',
            'load_number',
            'driver_pay',
            'pickup_date',
            'drop_date',
            'status',
            'broker',
            'loaded_miles',
            'empty_miles',
            'note',
            'delivered_at',
            'payment_type',
            'created_at',
            'load_stops',
        ]

    def get_status(self, obj):
        if obj.status:
            return {
                'id': obj.status.id,
                'name': obj.status.name,
            }
        return None

    def get_broker(self, obj):
        if obj.broker:
            return {
                'id': obj.broker.id,
                'name': obj.broker.name,
                'mc': obj.broker.mc,
            }
        return None

    def get_load_stops(self, obj):
        load_stops = obj.loadstop_set.all().order_by('order')
        return LoadStopViewSerializer(load_stops, many=True).data
