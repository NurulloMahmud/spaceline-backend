from rest_framework import serializers

from users.models import Company, CustomUser
from .models import (Driver, DriverHistory, DriverStatus, Vehicle, VehicleEquipment,
                     DriverCompany, DriverFile, CompanyFile, VehicleFile,
                     CompanyHistory, VehicleHistory, Deposit, DepositHistory
                     )
from users.serializers import CompanySerializer


class DriverStatusSerializer(serializers.ModelSerializer):

    class Meta:
        model = DriverStatus
        fields = '__all__'


class CompanyFileSerializer(serializers.ModelSerializer):

    class Meta:
        model = CompanyFile
        fields = '__all__'


class DriverFileSerializer(serializers.ModelSerializer):

    class Meta:
        model = DriverFile
        fields = '__all__'


class VehicleFileSerializer(serializers.ModelSerializer):

    class Meta:
        model = VehicleFile
        fields = '__all__'


class DriverWriteSerializer(serializers.ModelSerializer):

    class Meta:
        model = Driver
        fields = '__all__'

    def validate_phone_number(self, value):
        if not value:
            return value
        qs = Driver.objects.filter(phone_number=value)
        if self.instance:
            qs = qs.exclude(pk=self.instance.pk)
        if qs.exists():
            raise serializers.ValidationError('A driver with this phone number already exists.')
        return value


class DriverViewSerializer(serializers.ModelSerializer):
    company = CompanySerializer()
    status = DriverStatusSerializer()
    manager = serializers.SerializerMethodField()
    referral_by = serializers.SerializerMethodField()
    documents = DriverFileSerializer(many=True, source='driverfile_set')
    vehicle = serializers.SerializerMethodField()
    team = serializers.SerializerMethodField()
    dispatcher = serializers.SerializerMethodField()
    deposit = serializers.SerializerMethodField()

    class Meta:
        model = Driver
        fields = '__all__'
    
    def get_manager(self, obj):
        if obj.manager:
            return {
                    "id": obj.manager.id,
                    "first_name": obj.manager.first_name,
                    "last_name": obj.manager.last_name,
                    "username": obj.manager.username,
                }
        return None
    
    def get_team(self, obj):
        if obj.team:
            return {"id": obj.team.id, "name": obj.team.name}
        return None

    def get_dispatcher(self, obj):
        if obj.dispatcher:
            return {
                "id": obj.dispatcher.id,
                "first_name": obj.dispatcher.first_name,
                "last_name": obj.dispatcher.last_name,
                "username": obj.dispatcher.username,
            }
        return None

    def get_referral_by(self, obj):
        if obj.referral_by:
            return {
                    "id": obj.referral_by.id,
                    "first_name": obj.referral_by.first_name,
                    "last_name": obj.referral_by.last_name,
                    "username": obj.referral_by.username,
                }
        return None
    
    def get_vehicle(self, obj):
        vehicle = Vehicle.objects.filter(driver=obj).first()
        if vehicle:
            return {
                'id': vehicle.id,
                'vehicle_type': vehicle.vehicle_type,
                'make': vehicle.make,
                'model': vehicle.model,
                'year': vehicle.year,
                'registration_plate': vehicle.registration_plate,
                'vin': vehicle.vin,
                # Cargo box and equipment: quoted to brokers on every bid.
                'length': vehicle.length,
                'width': vehicle.width,
                'height': vehicle.height,
                'payload': vehicle.payload,
                'ramps': vehicle.ramps,
                'dock_height': vehicle.dock_height,
                'equipments': VehicleEquipmentSerializer(
                    vehicle.vehicleequipment_set.all(), many=True
                ).data,
            }
        return None

    def get_deposit(self, obj):
        deposit = getattr(obj, 'deposit', None)
        if deposit:
            return DepositViewSerializer(deposit).data
        return None


class VehicleEquipmentSerializer(serializers.ModelSerializer):

    class Meta:
        model = VehicleEquipment
        fields = '__all__'


class DriverCompanyViewSerializer(serializers.ModelSerializer):
    driver = serializers.SerializerMethodField()
    documents = CompanyFileSerializer(many=True, source='companyfile_set')

    class Meta:
        model = DriverCompany
        fields = '__all__'
    
    def get_driver(self, obj):
        if obj.driver:
            return {
                "id": obj.driver.id,
                "full_name": obj.driver.full_name,
                "phone_number": obj.driver.phone_number,
                "email": obj.driver.email if obj.driver.email else None,
                "emergency_phone_number": obj.driver.emergency_phone_number if obj.driver.emergency_phone_number else None,
            }
        return None


class DriverCompanyWriteSerializer(serializers.ModelSerializer):

    class Meta:
        model = DriverCompany
        fields = '__all__'


class VehicleViewSerializer(serializers.ModelSerializer):
    driver = serializers.SerializerMethodField()
    second_driver = serializers.SerializerMethodField()
    documents = VehicleFileSerializer(many=True, source='vehiclefile_set')
    equipments = VehicleEquipmentSerializer(many=True, source='vehicleequipment_set')

    class Meta:
        model = Vehicle
        fields = '__all__'

    def get_driver(self, obj):
        if obj.driver:
            return {
                "id": obj.driver.id,
                "full_name": obj.driver.full_name,
                "phone_number": obj.driver.phone_number,
                "email": obj.driver.email if obj.driver.email else None,
                "emergency_phone_number": obj.driver.emergency_phone_number if obj.driver.emergency_phone_number else None,
                "status": obj.driver.status.name,
                "current_address": obj.driver.current_address if obj.driver.current_address else None,
                "current_city": obj.driver.current_city if obj.driver.current_city else None,
                "current_state": obj.driver.current_state if obj.driver.current_state else None,
                "current_zip": obj.driver.current_zip if obj.driver.current_zip else None,
                "current_longitude": obj.driver.current_longitude if obj.driver.current_longitude else None,
                "current_latitude": obj.driver.current_latitude if obj.driver.current_latitude else None,
                "current_address": obj.driver.current_address if obj.driver.current_address else None,
            }
        return None

    def get_second_driver(self, obj):
        if obj.second_driver:
            return {
                "id": obj.second_driver.id,
                "full_name": obj.second_driver.full_name,
                "phone_number": obj.second_driver.phone_number,
                "email": obj.second_driver.email if obj.second_driver.email else None,
                "emergency_phone_number": obj.second_driver.emergency_phone_number if obj.second_driver.emergency_phone_number else None,
                "status": obj.second_driver.status.name
            }
        return None


class VehicleWriteSerializer(serializers.ModelSerializer):

    class Meta:
        model = Vehicle
        fields = '__all__'


class DepositWriteSerializer(serializers.ModelSerializer):

    class Meta:
        model = Deposit
        fields = '__all__'


class DepositViewSerializer(serializers.ModelSerializer):
    driver = serializers.SerializerMethodField()

    class Meta:
        model = Deposit
        fields = '__all__'

    def get_driver(self, obj):
        if obj.driver:
            return {
                "id": obj.driver.id,
                "full_name": obj.driver.full_name,
            }
        return None


class DepositHistoryWriteSerializer(serializers.ModelSerializer):

    class Meta:
        model = DepositHistory
        fields = '__all__'


class DepositHistoryViewSerializer(serializers.ModelSerializer):
    changed_by = serializers.SerializerMethodField()

    class Meta:
        model = DepositHistory
        fields = '__all__'

    def get_changed_by(self, obj):
        if obj.changed_by:
            return {
                    "id": obj.changed_by.id,
                    "first_name": obj.changed_by.first_name,
                    "last_name": obj.changed_by.last_name,
                    "username": obj.changed_by.username,
                }
        return None


class CompanyHistoryWriteSerializer(serializers.ModelSerializer):

    class Meta:
        model = CompanyHistory
        fields = '__all__'


class CompanyHistoryViewSerializer(serializers.ModelSerializer):
    changed_by = serializers.SerializerMethodField()

    class Meta:
        model = CompanyHistory
        fields = '__all__'

    def get_changed_by(self, obj):
        if obj.changed_by:
            return {
                    "id": obj.changed_by.id,
                    "first_name": obj.changed_by.first_name,
                    "last_name": obj.changed_by.last_name,
                    "username": obj.changed_by.username,
                }
        return None


class DriverHistoryWriteSerializer(serializers.ModelSerializer):

    class Meta:
        model = DriverHistory
        fields = '__all__'


class DriverHistoryViewSerializer(serializers.ModelSerializer):
    changed_by = serializers.SerializerMethodField()

    class Meta:
        model = DriverHistory
        fields = '__all__'

    def get_changed_by(self, obj):
        if obj.changed_by:
            return {
                    "id": obj.changed_by.id,
                    "first_name": obj.changed_by.first_name,
                    "last_name": obj.changed_by.last_name,
                    "username": obj.changed_by.username,
                }
        return None


class VehicleHistoryWriteSerializer(serializers.ModelSerializer):

    class Meta:
        model = VehicleHistory
        fields = '__all__'


class VehicleHistoryViewSerializer(serializers.ModelSerializer):
    changed_by = serializers.SerializerMethodField()

    class Meta:
        model = VehicleHistory
        fields = '__all__'

    def get_changed_by(self, obj):
        if obj.changed_by:
            return {
                    "id": obj.changed_by.id,
                    "first_name": obj.changed_by.first_name,
                    "last_name": obj.changed_by.last_name,
                    "username": obj.changed_by.username,
                }
        return None


# atrek drivers page
class DriverListSerializer(serializers.ModelSerializer):
    status = DriverStatusSerializer()
    company_name = serializers.CharField(source='company.name')
    manager = serializers.SerializerMethodField()
    docs_count = serializers.SerializerMethodField()

    class Meta:
        model = Driver
        fields = [
            'id',
            'company_name',
            'full_name',
            'email',
            'phone_number',
            'hired_date',
            'created_at',
            'status',
            'manager',
            'docs_count'
        ]

    def get_manager(self, obj):
        if obj.manager:
            return {
                    "id": obj.manager.id,
                    "first_name": obj.manager.first_name,
                    "last_name": obj.manager.last_name,
                    "username": obj.manager.username,
                }
        return None
    
    def get_docs_count(self, obj):
        company = DriverCompany.objects.filter(driver=obj).first()
        docs_count = company.companyfile_set.count() if company else 0
        return docs_count


class DriverCompanyModalSerializer(serializers.ModelSerializer):
    driver = serializers.SerializerMethodField()
    documents = CompanyFileSerializer(many=True, source='companyfile_set')

    class Meta:
        model = DriverCompany
        fields = [
            'id',
            'name',     
            'employer_id',
            'driver',
            'phone_number',
            'address',
            'city',
            'state',
            'zipcode',
            'documents'
        ]

    def get_driver(self, obj):
        if obj.driver:
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
        return None
    
    def _get_vehicle(self, driver):
        vehicle = driver.vehicles.first()
        if not vehicle:
            return None
        return {
            'id': vehicle.id,
            'vehicle_type': vehicle.vehicle_type,
            'make': vehicle.make,
            'model': vehicle.model,
            'year': vehicle.year,
            'registration_plate': vehicle.registration_plate,
            'vin': vehicle.vin,
        }


class DriverBulkCreateSerializer(serializers.Serializer):
    full_name = serializers.CharField()
    ssn = serializers.CharField(required=False, allow_blank=True)
    status = serializers.PrimaryKeyRelatedField(queryset=DriverStatus.objects.all())
    company = serializers.PrimaryKeyRelatedField(queryset=Company.objects.all())

    def validate_phone_number(self, value):
        if not value:
            return value
        if Driver.objects.filter(phone_number=value).exists():
            raise serializers.ValidationError('A driver with this phone number already exists.')
        return value
    phone_number = serializers.CharField(required=False, allow_blank=True)
    emergency_phone_number = serializers.CharField(required=False, allow_blank=True)
    medical_exp_date = serializers.DateField(required=False, allow_null=True)
    email = serializers.EmailField(required=False, allow_blank=True)
    telegram_group_id = serializers.CharField(required=False, allow_blank=True)
    driver_id = serializers.CharField(required=False, allow_blank=True)
    address = serializers.CharField(required=False, allow_blank=True)
    unit_number = serializers.CharField(required=False, allow_blank=True)
    note = serializers.CharField(required=False, allow_blank=True)
    dob = serializers.DateField(required=False, allow_null=True)
    driver_type = serializers.CharField(required=False, allow_blank=True)
    contract = serializers.CharField(required=False, allow_blank=True)
    fein = serializers.CharField(required=False, allow_blank=True)
    city = serializers.CharField(required=False, allow_blank=True)
    state = serializers.CharField(required=False, allow_blank=True)
    zip_code = serializers.CharField(required=False, allow_blank=True)
    hired_date = serializers.DateField(required=False, allow_null=True)
    terminated_date = serializers.DateField(required=False, allow_null=True)
    cdl_number = serializers.CharField(required=False, allow_blank=True)
    cdl_state = serializers.CharField(required=False, allow_blank=True)
    cdl_issued = serializers.CharField(required=False, allow_blank=True)
    cdl_class = serializers.CharField(required=False, allow_blank=True)
    cdl_issue_date = serializers.DateField(required=False, allow_null=True)
    cdl_expiration = serializers.DateField(required=False, allow_null=True)
    cdl_endorsement = serializers.CharField(required=False, allow_blank=True)
    tax_exempt = serializers.BooleanField(required=False)
    payee_code = serializers.CharField(required=False, allow_blank=True)
    fatca_reporting_code = serializers.CharField(required=False, allow_blank=True)
    referral_by = serializers.PrimaryKeyRelatedField(queryset=CustomUser.objects.all(), required=False, allow_null=True)
    manager = serializers.PrimaryKeyRelatedField(queryset=CustomUser.objects.all(), required=False, allow_null=True)
    company__name = serializers.CharField()
    company__mc = serializers.CharField()
    company__employer_id = serializers.CharField()
    company__phone_number = serializers.CharField()
    company__business_as = serializers.CharField(required=False, allow_blank=True)
    company__business_type = serializers.CharField(required=False, allow_blank=True)
    company__zipcode = serializers.CharField(required=False, allow_blank=True)
    company__state = serializers.CharField(required=False, allow_blank=True)
    company__city = serializers.CharField(required=False, allow_blank=True)
    company__address = serializers.CharField(required=False, allow_blank=True)
    vehicle__vehicle_type = serializers.CharField()
    vehicle__make = serializers.CharField()
    vehicle__model = serializers.CharField()
    vehicle__year = serializers.IntegerField()
    vehicle__payload = serializers.IntegerField()
    vehicle__gvw = serializers.IntegerField()
    vehicle__second_driver = serializers.PrimaryKeyRelatedField(queryset=Driver.objects.all(), required=False, allow_null=True)
    vehicle__registration_plate = serializers.CharField(required=False, allow_blank=True)
    vehicle__registration_expiry_date = serializers.DateField(required=False, allow_null=True)
    vehicle__registration_state = serializers.CharField(required=False, allow_blank=True)
    vehicle__insurance_company = serializers.CharField(required=False, allow_blank=True)
    vehicle__insurance_expiry_date = serializers.DateField(required=False, allow_null=True)
    vehicle__policy_number = serializers.CharField(required=False, allow_blank=True)
    vehicle__vin = serializers.CharField(required=False, allow_blank=True)
    vehicle__length = serializers.IntegerField(required=False, allow_null=True)
    vehicle__width = serializers.IntegerField(required=False, allow_null=True)
    vehicle__height = serializers.IntegerField(required=False, allow_null=True)
    vehicle__door_open_width = serializers.IntegerField(required=False, allow_null=True)
    vehicle__door_open_height = serializers.IntegerField(required=False, allow_null=True)
    vehicle__dock_height = serializers.CharField(required=False, allow_blank=True)
    vehicle__ramps = serializers.CharField(required=False, allow_blank=True)
    deposit__company = serializers.CharField(required=False, allow_blank=True)
    deposit__city = serializers.CharField(required=False, allow_blank=True)
    deposit__state = serializers.CharField(required=False, allow_blank=True)
    deposit__zip_code = serializers.CharField(required=False, allow_blank=True)
    deposit__address = serializers.CharField(required=False, allow_blank=True)
    deposit__bank_name = serializers.CharField(required=False, allow_blank=True)
    deposit__routing_number = serializers.CharField(required=False, allow_blank=True)
    deposit__account_number = serializers.CharField(required=False, allow_blank=True)
    driver_files = serializers.ListField(child=serializers.FileField(), required=False)
    driver_file_names = serializers.ListField(child=serializers.CharField(), required=False)
    company_files = serializers.ListField(child=serializers.FileField(), required=False)
    company_file_names = serializers.ListField(child=serializers.CharField(), required=False)
    vehicle_files = serializers.ListField(child=serializers.FileField(), required=False)
    vehicle_file_names = serializers.ListField(child=serializers.CharField(), required=False)

    def validate(self, attrs):
        driver_files = attrs.get('driver_files', [])
        driver_file_names = attrs.get('driver_file_names', [])
        company_files = attrs.get('company_files', [])
        company_file_names = attrs.get('company_file_names', [])
        vehicle_files = attrs.get('vehicle_files', [])
        vehicle_file_names = attrs.get('vehicle_file_names', [])

        if len(driver_files) != len(driver_file_names):
            raise serializers.ValidationError('driver_files and driver_file_names must have the same count.')
        if len(company_files) != len(company_file_names):
            raise serializers.ValidationError('company_files and company_file_names must have the same count.')
        if len(vehicle_files) != len(vehicle_file_names):
            raise serializers.ValidationError('vehicle_files and vehicle_file_names must have the same count.')

        return attrs

    def create(self, validated_data):
        from django.db import transaction
        driver_files = validated_data.pop('driver_files', [])
        driver_file_names = validated_data.pop('driver_file_names', [])
        company_files = validated_data.pop('company_files', [])
        company_file_names = validated_data.pop('company_file_names', [])
        vehicle_files = validated_data.pop('vehicle_files', [])
        vehicle_file_names = validated_data.pop('vehicle_file_names', [])
        company_data = {
            k.replace('company__', ''): validated_data.pop(k)
            for k in list(validated_data.keys()) if k.startswith('company__')
        }
        vehicle_data = {
            k.replace('vehicle__', ''): validated_data.pop(k)
            for k in list(validated_data.keys()) if k.startswith('vehicle__')
        }
        deposit_data = {
            k.replace('deposit__', ''): validated_data.pop(k)
            for k in list(validated_data.keys()) if k.startswith('deposit__')
        }

        with transaction.atomic():
            driver = Driver.objects.create(**validated_data)
            driver_company = DriverCompany.objects.create(driver=driver, **company_data)
            vehicle = Vehicle.objects.create(driver=driver, **vehicle_data)
            Deposit.objects.create(driver=driver, **deposit_data)
            DriverFile.objects.bulk_create([
                DriverFile(driver=driver, name=name, document=file)
                for file, name in zip(driver_files, driver_file_names)
            ])

            CompanyFile.objects.bulk_create([
                CompanyFile(company=driver_company, name=name, document=file)
                for file, name in zip(company_files, company_file_names)
            ])

            VehicleFile.objects.bulk_create([
                VehicleFile(vehicle=vehicle, name=name, document=file)
                for file, name in zip(vehicle_files, vehicle_file_names)
            ])

        return driver


class NearbyDriverSerializer(serializers.ModelSerializer):
    status = serializers.SerializerMethodField()
    vehicle = serializers.SerializerMethodField()
    miles_out = serializers.SerializerMethodField()
    company = serializers.SerializerMethodField()

    class Meta:
        model = Driver
        fields = [
            'id',
            'full_name',
            'unit_number',
            'phone_number',
            'note',
            'telegram_group_id',
            'company',
            'current_latitude',
            'current_longitude',
            'current_city',
            'current_state',
            'current_zip',
            'current_address',
            'status',
            'vehicle',
            'miles_out',
        ]

    def get_status(self, obj):
        if obj.status:
            return {
                'id': obj.status.id,
                'name': obj.status.name,
            }
        return None

    def get_company(self, obj):
        if obj.company:
            return {
                'id': obj.company.id,
                'name': obj.company.name,
            }
        return None

    def get_vehicle(self, obj):
        vehicle = obj.vehicles.first()
        if not vehicle:
            return None
        return {
            'id': vehicle.id,
            'vehicle_type': vehicle.vehicle_type,
            'make': vehicle.make,
            'model': vehicle.model,
            'year': vehicle.year,
            'length': vehicle.length,
            'width': vehicle.width,
            'height': vehicle.height,
            'payload': vehicle.payload,
            'ramps': vehicle.ramps,
            'registration_plate': vehicle.registration_plate,
            'equipments': VehicleEquipmentSerializer(
                vehicle.vehicleequipment_set.all(), many=True
            ).data,
        }

    def get_miles_out(self, obj):
        return getattr(obj, '_miles_out', None)
    

class VehicleDropdownSerializer(serializers.ModelSerializer):
    driver = serializers.SerializerMethodField()
    second_driver = serializers.SerializerMethodField()

    class Meta:
        model = Vehicle
        fields = ['id', 'driver', 'second_driver', 'vin']

    def get_driver(self, obj):
        if obj.driver:
            return {
                "id": obj.driver.id,
                "full_name": obj.driver.full_name,
                "current_address": obj.driver.current_address if obj.driver.current_address else None,
                "current_city": obj.driver.current_city if obj.driver.current_city else None,
                "current_state": obj.driver.current_state if obj.driver.current_state else None,
                "current_zip": obj.driver.current_zip if obj.driver.current_zip else None,
                "current_longitude": obj.driver.current_longitude if obj.driver.current_longitude else None,
                "current_latitude": obj.driver.current_latitude if obj.driver.current_latitude else None,
                "company": obj.driver.company.name if obj.driver.company.name else None
            }
        return None
    
    def get_second_driver(self, obj):
        if obj.second_driver:
            return {
                "id": obj.second_driver.id,
                "full_name": obj.second_driver.full_name,
                "current_address": obj.second_driver.current_address if obj.second_driver.current_address else None,
                "current_city": obj.second_driver.current_city if obj.second_driver.current_city else None,
                "current_state": obj.second_driver.current_state if obj.second_driver.current_state else None,
                "current_zip": obj.second_driver.current_zip if obj.second_driver.current_zip else None,
                "current_longitude": obj.second_driver.current_longitude if obj.second_driver.current_longitude else None,
                "current_latitude": obj.second_driver.current_latitude if obj.second_driver.current_latitude else None,
                "company": obj.second_driver.company.name if obj.second_driver.company.name else None
            }
        return None


class UnassignedDriverSerializer(serializers.ModelSerializer):
    """The worklist row: who they are and which assignment is missing."""
    company_name = serializers.CharField(source='company.name', read_only=True)
    status_name = serializers.CharField(source='status.name', read_only=True)
    team = serializers.SerializerMethodField()
    dispatcher = serializers.SerializerMethodField()

    class Meta:
        model = Driver
        fields = ['id', 'full_name', 'phone_number', 'unit_number',
                  'company', 'company_name', 'status_name', 'team', 'dispatcher']

    def get_team(self, obj):
        return {"id": obj.team.id, "name": obj.team.name} if obj.team else None

    def get_dispatcher(self, obj):
        if not obj.dispatcher:
            return None
        name = f"{obj.dispatcher.first_name} {obj.dispatcher.last_name}".strip()
        return {"id": obj.dispatcher.id, "full_name": name or obj.dispatcher.username}


class DriverAssignSerializer(serializers.Serializer):
    """Bulk assignment of drivers to a team and/or a dispatcher."""
    driver_ids = serializers.ListField(
        child=serializers.IntegerField(), allow_empty=False
    )
    team = serializers.IntegerField(required=False, allow_null=True)
    dispatcher = serializers.IntegerField(required=False, allow_null=True)

    def validate(self, attrs):
        if 'team' not in attrs and 'dispatcher' not in attrs:
            raise serializers.ValidationError(
                "Provide team, dispatcher, or both. Send null to clear one."
            )
        return attrs
