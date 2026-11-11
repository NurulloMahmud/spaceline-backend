from rest_framework import serializers
from django.db.models import Sum
from decimal import Decimal
from django.utils.timezone import now
from users.models import Company
from .models import Deduction, DeductionType, StatementDeduction, StatementStatus, Statement, StatementLoad


class StatementStatusSerializer(serializers.ModelSerializer):

    class Meta:
        model = StatementStatus
        fields = '__all__'


class StatementDeductionUseSerializer(serializers.ModelSerializer):
    deduction = serializers.SerializerMethodField()

    class Meta:
        model = StatementDeduction
        fields = ['id', 'deduction', 'created_at', 'last_updated']
        ordering = ['-created_at']

    def get_deduction(self, obj):
        return {
            "id": obj.deduction.id,
            "amount": obj.deduction.amount,
            "date": obj.deduction.date,
            "notes": obj.deduction.note,
            "fee": obj.deduction.fee,
            "type": obj.deduction.type.name if obj.deduction.type else None,
        }


class StatementLoadUseSerializer(serializers.ModelSerializer):
    load = serializers.SerializerMethodField()

    class Meta:
        model = StatementLoad
        fields = ['id', 'load', 'created_at', 'last_updated']

    def get_load(self, obj):
        if obj.load:
            return {
                "id": obj.load.id,
                "load_number": obj.load.load_number,
                "shipment": f"SH - {obj.load.shipment}" if obj.load.shipment else None,
                "driver_pay": obj.load.driver_pay,
                "carrier_pay": obj.load.carrier_pay,
                "pickup_date": obj.load.pickup_date,
                "drop_date": obj.load.drop_date,
                "name": obj.load.name,
                "payment_type": obj.load.payment_type,
            }
        return None


class DeductionTypeSerializer(serializers.ModelSerializer):
    deduction_sum = serializers.SerializerMethodField(read_only=True)

    class Meta:
        model = DeductionType
        fields = '__all__'

    def get_deduction_sum(self, obj):
        return float(obj.deductions.aggregate(total=Sum('amount'))['total'] or 0)


