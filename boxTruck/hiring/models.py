import uuid
from django.db import models
from users.models import Company, CustomUser, Team


class DriverStatus(models.Model):
    name = models.CharField(max_length=255)

    def __str__(self):
        return self.name
    
    class Meta:
        verbose_name = 'Driver Status'
        verbose_name_plural = 'Driver Statuses'
        db_table = 'driver_statuses'


class Driver(models.Model):
    company = models.ForeignKey(Company, on_delete=models.CASCADE)
    full_name = models.CharField(max_length=255)
    ssn = models.CharField(max_length=255, null=True, blank=True)
    status = models.ForeignKey(DriverStatus, on_delete=models.CASCADE)
    phone_number = models.CharField(max_length=255, null=True, blank=True)
    emergency_phone_number = models.CharField(max_length=255, null=True, blank=True)
    medical_exp_date = models.DateField(null=True, blank=True)
    email = models.EmailField(null=True, blank=True)
    telegram_group_id = models.CharField(max_length=255, null=True, blank=True)
    telegram_user_id = models.CharField(max_length=255, null=True, blank=True)
    driver_id = models.CharField(max_length=255, null=True, blank=True)
    address = models.CharField(max_length=255, null=True, blank=True)
    unit_number = models.CharField(max_length=255, null=True, blank=True)
    note = models.TextField(null=True, blank=True)
    dob = models.DateField(null=True, blank=True)
    driver_type = models.CharField(max_length=255, null=True, blank=True)
    contract = models.CharField(max_length=255, null=True, blank=True)
    fein = models.CharField(max_length=255, null=True, blank=True)
    city = models.CharField(max_length=250, null=True, blank=True)
    state = models.CharField(max_length=55, null=True, blank=True)
    zip_code = models.CharField(max_length=20, null=True, blank=True)
    hired_date = models.DateField(null=True, blank=True)
    terminated_date = models.DateField(null=True, blank=True)
    cdl_number = models.CharField(max_length=255, null=True, blank=True)
    cdl_state = models.CharField(max_length=255, null=True, blank=True)
    cdl_issued = models.CharField(max_length=255, null=True, blank=True)
    cdl_class = models.CharField(max_length=33, null=True, blank=True)
    cdl_issue_date = models.DateField(null=True, blank=True)
    cdl_expiration = models.DateField(null=True, blank=True)
    cdl_endorsement = models.CharField(max_length=255, null=True, blank=True)
    tax_exempt = models.BooleanField(default=False)
    payee_code = models.CharField(max_length=255, null=True, blank=True)
    fatca_reporting_code = models.CharField(max_length=255, null=True, blank=True)
    referral_by = models.ForeignKey(CustomUser, on_delete=models.SET_NULL, null=True, blank=True, related_name='referral_by')
    manager = models.ForeignKey(CustomUser, on_delete=models.SET_NULL, null=True, blank=True, related_name='manager')
    team = models.ForeignKey(
        Team, on_delete=models.SET_NULL, null=True, blank=True, related_name='drivers'
    )
    dispatcher = models.ForeignKey(
        CustomUser, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='assigned_drivers',
    )
    current_state = models.CharField(max_length=255, null=True, blank=True)
    current_city = models.CharField(max_length=255, null=True, blank=True)
    current_zip = models.CharField(max_length=255, null=True, blank=True)
    current_longitude = models.FloatField(null=True, blank=True)
    current_latitude = models.FloatField(null=True, blank=True)
    current_address = models.CharField(max_length=255, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return self.full_name
    
    class Meta:
        verbose_name = 'Driver'
        verbose_name_plural = 'Drivers'
        db_table = 'drivers'


class DriverFile(models.Model):
    driver = models.ForeignKey(Driver, on_delete=models.CASCADE)
    name = models.CharField(max_length=255)
    document = models.FileField(upload_to='driver_files/')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return self.driver.full_name
    
    class Meta:
        verbose_name = 'Driver File'
        verbose_name_plural = 'Driver Files'
        db_table = 'driver_files'


class DriverCompany(models.Model):
    driver = models.ForeignKey(Driver, on_delete=models.CASCADE)
    mc = models.CharField(max_length=255)
    employer_id = models.CharField(max_length=255)
    name = models.CharField(max_length=255)
    phone_number = models.CharField(max_length=255)
    business_as = models.CharField(max_length=255)
    business_type = models.CharField(max_length=255)
    zipcode = models.CharField(max_length=255)
    state = models.CharField(max_length=255)
    city = models.CharField(max_length=255)
    address = models.CharField(max_length=255)
    email = models.EmailField(null=True, blank=True)
    applicant_first_name = models.CharField(max_length=255, null=True, blank=True)
    applicant_last_name = models.CharField(max_length=255, null=True, blank=True)

    def __str__(self):
        return self.name
    
    class Meta:
        verbose_name = 'Driver Company'
        verbose_name_plural = 'Driver Companies'
        db_table = 'driver_companies'


class CompanyFile(models.Model):
    company = models.ForeignKey(DriverCompany, on_delete=models.CASCADE)
    name = models.CharField(max_length=255)
    document = models.FileField(upload_to='driver_files/')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return self.company.name
    
    class Meta:
        verbose_name = 'Company File'
        verbose_name_plural = 'Company Files'
        db_table = 'company_files'


class Vehicle(models.Model):
    driver = models.ForeignKey(Driver, on_delete=models.CASCADE, related_name='vehicles')
    second_driver = models.ForeignKey(Driver, on_delete=models.SET_NULL, null=True, blank=True, related_name='second_driver')
    vehicle_type = models.CharField(max_length=255, null=True, blank=True)
    make = models.CharField(max_length=255)
    model = models.CharField(max_length=255)
    payload = models.IntegerField()
    gvw = models.IntegerField()
    year = models.IntegerField(null=True, blank=True)
    registration_plate = models.CharField(max_length=255, null=True, blank=True)
    registration_expiry_date = models.DateField(null=True, blank=True)
    registration_state = models.CharField(max_length=255, null=True, blank=True)
    insurance_company = models.CharField(max_length=255, null=True, blank=True)
    insurance_expiry_date = models.DateField(null=True, blank=True)
    dock_height = models.CharField(max_length=255, null=True, blank=True)
    policy_number = models.CharField(max_length=255, null=True, blank=True)
    vin = models.CharField(max_length=255, null=True, blank=True)
    length = models.IntegerField(null=True, blank=True)
    width = models.IntegerField(null=True, blank=True)
    height = models.IntegerField(null=True, blank=True)
    door_open_width = models.IntegerField(null=True, blank=True)
    door_open_height = models.IntegerField(null=True, blank=True)
    ramps = models.CharField(max_length=255, null=True, blank=True)
    note = models.TextField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return self.driver.full_name
    
    class Meta:
        verbose_name = 'Vehicle'
        verbose_name_plural = 'Vehicles'
        db_table = 'vehicles'


class VehicleEquipment(models.Model):
    vehicle = models.ForeignKey(Vehicle, on_delete=models.CASCADE)
    name = models.CharField(max_length=255)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return self.name
    
    class Meta:
        verbose_name = 'Vehicle Equipment'
        verbose_name_plural = 'Vehicle Equipments'
        db_table = 'vehicle_equipments'


class VehicleFile(models.Model):
    vehicle = models.ForeignKey(Vehicle, on_delete=models.CASCADE)
    name = models.CharField(max_length=255)
    document = models.FileField(upload_to='vehicle_files/')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return self.vehicle.driver.full_name
    
    class Meta:
        verbose_name = 'Vehicle File'
        verbose_name_plural = 'Vehicle Files'
        db_table = 'vehicle_files'


class CompanyHistory(models.Model): 
    company = models.ForeignKey(DriverCompany, on_delete=models.CASCADE)
    changed_by = models.ForeignKey(CustomUser, on_delete=models.CASCADE)
    description = models.TextField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.company.name
    
    class Meta:
        verbose_name = 'Company History'
        verbose_name_plural = 'Company Histories'
        db_table = 'company_histories'


class DriverInviteLink(models.Model):
    token = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    created_by = models.ForeignKey(CustomUser, on_delete=models.CASCADE)
    company = models.ForeignKey(Company, on_delete=models.CASCADE)
    manager = models.ForeignKey(
        CustomUser, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='managed_invite_links',
    )
    driver = models.ForeignKey(
        Driver, on_delete=models.CASCADE, null=True, blank=True,
        related_name='sign_links',
    )
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField(null=True, blank=True)

    def is_valid(self):
        from django.utils import timezone
        if not self.is_active:
            return False
        if self.expires_at and self.expires_at < timezone.now():
            return False
        return True

    class Meta:
        db_table = 'driver_invite_links'
        verbose_name = 'Driver Invite Link'
        verbose_name_plural = 'Driver Invite Links'


class DriverHistory(models.Model): 
    driver = models.ForeignKey(Driver, on_delete=models.CASCADE)
    changed_by = models.ForeignKey(CustomUser, on_delete=models.CASCADE)
    description = models.TextField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.driver.full_name
    
    class Meta:
        verbose_name = 'Driver History'
        verbose_name_plural = 'Driver Histories'
        db_table = 'driver_histories'


class VehicleHistory(models.Model): 
    vehicle = models.ForeignKey(Vehicle, on_delete=models.CASCADE)
    changed_by = models.ForeignKey(CustomUser, on_delete=models.CASCADE)
    description = models.TextField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.vehicle.vehicle_type
    
    class Meta:
        verbose_name = 'Vehicle History'
        verbose_name_plural = 'Vehicle Histories'
        db_table = 'vehicle_histories'


class Deposit(models.Model):
    driver = models.OneToOneField(Driver, on_delete=models.CASCADE)
    company = models.CharField(max_length=255, null=True, blank=True)
    city = models.CharField(max_length=255, null=True, blank=True)
    state = models.CharField(max_length=255, null=True, blank=True)
    zip_code = models.CharField(max_length=255, null=True, blank=True)
    address = models.CharField(max_length=255, null=True, blank=True)
    bank_name = models.CharField(max_length=255, null=True, blank=True)
    routing_number = models.CharField(max_length=255, null=True, blank=True)
    account_number = models.CharField(max_length=255, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"Deposit for {self.driver.full_name}"

    class Meta:
        verbose_name = 'Deposit'
        verbose_name_plural = 'Deposits'
        db_table = 'deposits'


class DepositHistory(models.Model):
    deposit = models.ForeignKey(Deposit, on_delete=models.CASCADE)
    changed_by = models.ForeignKey(CustomUser, on_delete=models.CASCADE)
    description = models.TextField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"Deposit history for {self.deposit.driver.full_name}"

    class Meta:
        verbose_name = 'Deposit History'
        verbose_name_plural = 'Deposit Histories'
        db_table = 'deposit_histories'

