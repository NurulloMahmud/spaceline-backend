from decimal import Decimal

from rest_framework import serializers

from users.models import CustomUser

from .models import BonusRule


class SalaryItemSerializer(serializers.Serializer):
    user = serializers.PrimaryKeyRelatedField(queryset=CustomUser.objects.all())
    fixed_salary = serializers.DecimalField(max_digits=20, decimal_places=2, min_value=Decimal('0'), required=False, allow_null=True)
    profit_percentage = serializers.DecimalField(max_digits=5, decimal_places=2, min_value=Decimal('0'), max_value=Decimal('100'), required=False, allow_null=True)

    def validate(self, attrs):
        attrs['user'] = attrs['user'].id
        return attrs


class SaveSalariesSerializer(serializers.Serializer):
    items = SalaryItemSerializer(many=True)


class BonusRuleSerializer(serializers.ModelSerializer):
    # Declared by hand so the model's unique check stays out: posting a date again replaces that rule.
    effective_from = serializers.DateField()
    created_by_name = serializers.SerializerMethodField()

    class Meta:
        model = BonusRule
        fields = ['id', 'effective_from', 'profit_step', 'bonus_amount', 'created_by_name', 'created_at']
        read_only_fields = ['id', 'created_by_name', 'created_at']

    def get_created_by_name(self, obj):
        u = obj.created_by
        if u is None:
            return None
        return ' '.join(p for p in (u.first_name, u.last_name) if p) or u.username
