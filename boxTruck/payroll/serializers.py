from rest_framework import serializers
from django.db.models import Sum
from decimal import Decimal
from django.utils.timezone import now
from hiring.models import Driver
from .models import Deduction, DeductionHistory, DeductionType, StatementDeduction, StatementStatus, Statement, StatementLoad


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
                "company": obj.load.company.name,
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


class StatementLoadViewSerializer(serializers.ModelSerializer):
    statement = serializers.SerializerMethodField()
    load = serializers.SerializerMethodField()

    class Meta:
        model = StatementLoad
        fields = '__all__'

    def get_statement(self, obj):
        if obj.statement:
            return {
                'id': obj.statement.id,
                'start_date': obj.statement.start_date,
                'end_date': obj.statement.end_date,
                'created_by': obj.statement.created_by.username if obj.statement.created_by else None
            }
        return None

    def get_load(self, obj):
        if obj.load:
            return {
                "id": obj.load.id,
                "company": obj.load.company.name,
                "load_number": obj.load.load_number,
                "shipment": f"SH - {obj.load.shipment}" if obj.load.shipment else None,
                "pickup_date": obj.load.pickup_date,
                "drop_date": obj.load.drop_date,
                "driver_pay": obj.load.driver_pay,
                "carrier_pay": obj.load.carrier_pay,
            }
        return None


class StatementLoadWriteSerializer(serializers.ModelSerializer):

    class Meta:
        model = StatementLoad
        fields = '__all__'


class StatementViewSerializer(serializers.ModelSerializer):
    status = StatementStatusSerializer()
    created_by = serializers.SerializerMethodField()
    driver = serializers.SerializerMethodField()
    company = serializers.SerializerMethodField()
    loads = StatementLoadUseSerializer(source="statementload_set", many=True, read_only=True)
    deductions = StatementDeductionUseSerializer(source="statementdeduction_set", many=True, read_only=True)
    total_amount = serializers.SerializerMethodField()

    class Meta:
        model = Statement
        fields = '__all__'

    def get_created_by(self, obj):
        cb = getattr(obj, 'created_by', None)
        if cb:
            return {
                'id': cb.id,
                'username': cb.username,
                'first_name': cb.first_name,
                'last_name': cb.last_name
            }
        return None

    def get_driver(self, obj):
        d = getattr(obj, 'driver', None)
        if d:
            return {'id': d.id, 'name': d.full_name}
        return None

    def get_company(self, obj):
        c = getattr(obj, 'company', None)
        if c:
            return {'id': c.id, 'name': c.name}
        return None
    
    def get_total_amount(self, obj):
        total_deductions = Decimal("0.00")
        for sd in obj.statementdeduction_set.all():
            deduction = sd.deduction
            amount = deduction.amount or Decimal("0.00")
            fee = deduction.fee or Decimal("0.00")
            total_deductions += amount + fee
        total_amount = obj.gross_amount - total_deductions
        return total_amount


class StatementWriteSerializer(serializers.ModelSerializer):

    class Meta:
        model = Statement
        fields = '__all__'


class DriverDropdownSerializer(serializers.ModelSerializer):
    company = serializers.SerializerMethodField()
    status = serializers.SerializerMethodField()

    class Meta:
        model = Driver
        fields = ['id', 'company', 'full_name', 'status']

    def get_company(self, obj):
        if obj.company:
            return {
                'id': obj.company.id,
                'name': obj.company.name
            }
        return None
    
    def get_status(self, obj):
        if obj.status:
            return {
                'id': obj.status.id,
                'name': obj.status.name
            }
        return None


class StatementViewForPDFSerializer(serializers.ModelSerializer):
    status = StatementStatusSerializer()
    created_by = serializers.SerializerMethodField()
    driver = serializers.SerializerMethodField()
    company = serializers.SerializerMethodField()
    loads = StatementLoadUseSerializer(source="statementload_set", many=True, read_only=True)
    deductions = StatementDeductionUseSerializer(source="statementdeduction_set", many=True, read_only=True)
    settlement = serializers.SerializerMethodField()
    total_amount = serializers.SerializerMethodField()
    total_deduction = serializers.SerializerMethodField()
    total_advances = serializers.SerializerMethodField()

    class Meta:
        model = Statement
        fields = '__all__'

    def get_settlement(self, obj):
        if not obj.driver:
            return None
        
        current_year = now().year
        driver_statements = Statement.objects.filter(driver=obj.driver, end_date__year=current_year)
        ytd_earnings = driver_statements.aggregate(total=Sum('gross_amount'))['total'] or Decimal("0.00")
        all_deductions = Deduction.objects.filter(
            statementdeduction__statement__driver=obj.driver,
            statementdeduction__statement__end_date__year=current_year
        ).distinct()
        ytd_deductions = Decimal("0.00")
        ytd_advances = Decimal("0.00")
        for deduction in all_deductions:
            amount = deduction.amount or Decimal("0.00")
            fee = deduction.fee or Decimal("0.00")
            total_deduction = amount + fee
            ytd_deductions += total_deduction
            if deduction.type and "advance" in deduction.type.name.lower():
                ytd_advances += total_deduction
        ytd_total = ytd_earnings - ytd_deductions
        return {
            'ytd_earnings': ytd_earnings,
            'ytd_total': ytd_total,
            'ytd_deductions': ytd_deductions,
            'ytd_advances': ytd_advances
        }
    
    def get_total_advances(self, obj):
        total = Decimal("0.00")
        advances = obj.statementdeduction_set.filter(
            deduction__type__name__icontains='advance'
        ).select_related('deduction')
        for sd in advances:
            deduction = sd.deduction
            amount = deduction.amount or Decimal("0.00")
            fee = deduction.fee or Decimal("0.00")
            total += amount + fee
        return total
    
    def get_total_deduction(self, obj):
        total = Decimal("0.00")
        deductions = obj.statementdeduction_set.select_related('deduction')
        for sd in deductions:
            deduction = sd.deduction
            amount = deduction.amount or Decimal("0.00")
            fee = deduction.fee or Decimal("0.00")
            total += amount + fee
        return total

    def get_created_by(self, obj):
        if obj.created_by:
            return {
                'id': obj.created_by.id,
                'username': obj.created_by.username,
                'first_name': obj.created_by.first_name,
                'last_name': obj.created_by.last_name
            }
        return None

    def get_driver(self, obj):
        if obj.driver:
            return {
                'id': obj.driver.id,
                'name': obj.driver.full_name,
                'company': obj.driver.company.name,
            }
        return None

    def get_company(self, obj):
        if obj.company:
            return {
                'id': obj.company.id,
                'name': obj.company.name,
                'address': obj.company.address if obj.company.address else "123 Road Street Address"
            }
        return None
    
    def get_total_amount(self, obj):
        total_deductions = Decimal("0.00")
        for sd in obj.statementdeduction_set.all():
            deduction = sd.deduction
            amount = deduction.amount or Decimal("0.00")
            fee = deduction.fee or Decimal("0.00")
            total_deductions += amount + fee
        total_amount = obj.gross_amount - total_deductions
        return total_amount


class DeductionTypeSerializer(serializers.ModelSerializer):
    deduction_sum = serializers.SerializerMethodField(read_only=True)

    class Meta:
        model = DeductionType
        fields = '__all__'

    def get_deduction_sum(self, obj):
        return float(obj.deductions.aggregate(total=Sum('amount'))['total'] or 0)


class DeductionWriteSerializer(serializers.ModelSerializer):

    class Meta:
        model = Deduction
        fields = '__all__'


class DeductionViewSerializer(serializers.ModelSerializer):
    driver = serializers.SerializerMethodField()
    created_by = serializers.SerializerMethodField()
    type = DeductionTypeSerializer()

    class Meta:
        model = Deduction
        fields = '__all__'

    def get_created_by(self, obj):
        if obj.created_by:
            return {
                "id": obj.created_by.id,
                "username": obj.created_by.username,
                'first_name': obj.created_by.first_name,
                'last_name': obj.created_by.last_name
            }

    def get_driver(self, obj):
        if obj.driver:
            return {
                "id": obj.driver.id,
                "name": obj.driver.full_name
            }


class DeductionHistoryViewSerializer(serializers.ModelSerializer):
    deduction = serializers.SerializerMethodField()
    changed_by = serializers.SerializerMethodField()

    class Meta:
        model = DeductionHistory
        fields = '__all__'

    def get_deduction(self, obj):
        if obj.deduction:
            return {
                'id': obj.deduction.id,
                'type': obj.deduction.type.name,
                'amount': obj.deduction.amount
            }
        return None

    def get_changed_by(self, obj):
        if obj.changed_by:
            return {
                'id': obj.changed_by.id,
                'username': obj.changed_by.username,
                'first_name': obj.changed_by.first_name,
                'last_name': obj.changed_by.last_name
            }
        return None


class StatementDeductionViewSerializer(serializers.ModelSerializer):
    deduction = serializers.SerializerMethodField()
    statement = serializers.SerializerMethodField()

    class Meta:
        model = StatementDeduction
        fields = '__all__'

    def get_deduction(self, obj):
        if obj.deduction:
            return {
                'id': obj.deduction.id,
                'driver': obj.deduction.driver.full_name,
                'amount': obj.deduction.amount,
                'type': obj.deduction.type.name if obj.deduction.type else None,
                'date': obj.deduction.date
            }

    def get_statement(self, obj):
        if obj.statement:
            return {
                'id': obj.statement.id,
                'start_date': obj.statement.start_date,
                'end_date': obj.statement.end_date,
                'created_by': obj.statement.created_by.username
            }
        return None


class StatementDeductionWriteSerializer(serializers.ModelSerializer):

    class Meta:
        model = StatementDeduction
        fields = '__all__'


class CreateDeductionWithStatementIdSerializer(serializers.ModelSerializer):
    date = serializers.DateField(read_only=True)

    class Meta:
        model = Deduction
        fields = '__all__'

    def create(self, validated_data):
        from datetime import timedelta
        request = self.context.get("request")
        user_id = request.user
        statement_id = self.context['statement_id']
        statement = Statement.objects.get(id=statement_id)
        if not statement:
            raise serializers.ValidationError({"statement_id": "Invalid statement ID"})

        deduction_date = statement.start_date + timedelta(days=1)
        deduction = Deduction.objects.create(**validated_data, date=deduction_date, created_by=user_id)
        DeductionHistory.objects.create(
            deduction=deduction,
            changed_by=user_id,
            description="Deduction Created"
        )
        return deduction
