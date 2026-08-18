from django.db import models
from hiring.models import Driver
from users.models import Company, CustomUser

# Create your models here.
class Broker(models.Model):
    name = models.CharField(max_length=100)
    mc = models.CharField(max_length=100, unique=True)
    address = models.CharField(max_length=200, null=True, blank=True)
    city = models.CharField(max_length=100, null=True, blank=True)
    state = models.CharField(max_length=100, null=True, blank=True)
    zipcode = models.CharField(max_length=100, null=True, blank=True)
    phone_number = models.CharField(max_length=100, null=True, blank=True)
    email = models.EmailField(null=True, blank=True)
    billing_email = models.EmailField(null=True, blank=True)
    ai_type = models.CharField(max_length=100, default='GPT')
    note = models.TextField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    last_updated = models.DateTimeField(auto_now=True)

    def __str__(self):
        return self.name

    class Meta:
        db_table = 'brokers'
        verbose_name = 'Broker'
        verbose_name_plural = 'Brokers'


class BrokerStar(models.Model):
    broker = models.ForeignKey(Broker, on_delete=models.CASCADE, related_name='stars')
    company = models.ForeignKey(Company, on_delete=models.CASCADE, related_name='broker_stars')
    user = models.ForeignKey(CustomUser, on_delete=models.SET_NULL, null=True, blank=True, related_name='broker_stars')
    stars = models.PositiveSmallIntegerField()
    comment = models.TextField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.company} -> {self.broker} ({self.stars}★)"

    class Meta:
        db_table = 'broker_stars'
        verbose_name = 'Broker Star'
        verbose_name_plural = 'Broker Stars'


class LoadStatus(models.Model):
    name = models.CharField(max_length=100)
    created_at = models.DateTimeField(auto_now_add=True)
    last_updated = models.DateTimeField(auto_now=True)

    def __str__(self):
        return self.name

    class Meta:
        db_table = 'load_statuses'
        verbose_name = 'Load Status'
        verbose_name_plural = 'Load Statuses'


class Load(models.Model):
    company = models.ForeignKey(Company, on_delete=models.SET_NULL, null=True, blank=True)
    driver = models.ForeignKey(Driver, on_delete=models.SET_NULL, null=True, blank=True)
    booked_by = models.ForeignKey(CustomUser, on_delete=models.SET_NULL, null=True, blank=True, related_name='booked_by')
    created_by = models.ForeignKey(CustomUser, on_delete=models.SET_NULL, null=True, blank=True)
    updated_by = models.ForeignKey(CustomUser, on_delete=models.SET_NULL, null=True, blank=True, related_name='updated_by')
    broker = models.ForeignKey(Broker, on_delete=models.CASCADE, null=True, blank=True)
    shipment = models.IntegerField(null=True, blank=True)
    load_number = models.CharField(max_length=100, null=True, blank=True)
    driver_pay = models.DecimalField(max_digits=20, decimal_places=2, null=True, blank=True)
    carrier_pay = models.DecimalField(max_digits=20, decimal_places=2, null=True, blank=True)
    pickup_date = models.DateTimeField(null=True, blank=True)
    drop_date = models.DateTimeField(null=True, blank=True)
    name = models.CharField(max_length=100, null=True, blank=True)
    loaded_miles = models.DecimalField(max_digits=20, decimal_places=2, null=True, blank=True)
    empty_miles = models.DecimalField(max_digits=20, decimal_places=2, default=0, null=True, blank=True)
    status = models.ForeignKey(LoadStatus, on_delete=models.CASCADE, null=True, blank=True)
    delivered_at = models.DateTimeField(null=True, blank=True)
    note = models.TextField(null=True, blank=True)
    dispatcher_note = models.TextField(null=True, blank=True)
    main_load = models.ForeignKey("self", on_delete=models.SET_NULL, null=True, blank=True)
    recovery = models.BooleanField(default=False)
    payment_type = models.CharField(max_length=100, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    last_updated = models.DateTimeField(auto_now=True)

    def save(self, *args, **kwargs):
        if not self.pk:
            last_load = Load.objects.filter(company=self.company).order_by('-shipment').first()
            if last_load and last_load.shipment is not None:
                self.shipment = last_load.shipment + 1
            else:
                self.shipment = int(self.company.shipment_number)
        super().save(*args, **kwargs)

    def __str__(self):
        return self.load_number

    class Meta:
        db_table = 'loads'
        verbose_name = 'Load'
        verbose_name_plural = 'Loads'


class LoadHistory(models.Model):
    load = models.ForeignKey(Load, on_delete=models.CASCADE)
    changed_by = models.ForeignKey(CustomUser, on_delete=models.CASCADE, null=True, blank=True)
    description = models.TextField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.load.load_number

    class Meta:
        db_table = 'load_history'
        verbose_name = 'Load History'
        verbose_name_plural = 'Load Histories'


class LoadFile(models.Model):
    name = models.CharField(max_length=100)
    load = models.ForeignKey(Load, on_delete=models.CASCADE, null=True, blank=True)
    file = models.FileField(upload_to='billing/loads/', null=True, blank=True)
    rc_url = models.CharField(max_length=255, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    last_updated = models.DateTimeField(auto_now=True)

    def delete(self, *args, **kwargs):
        if self.file:
            storage = self.file.storage
            file_name = self.file.name
            if storage.exists(file_name):
                storage.delete(file_name)
        super().delete(*args, **kwargs)

    def __str__(self):
        return self.load.load_number

    class Meta:
        db_table = 'load_files'
        verbose_name = 'Load File'
        verbose_name_plural = 'Load Files'


class LoadStop(models.Model):
    load = models.ForeignKey(Load, on_delete=models.CASCADE)
    address = models.CharField(max_length=255, null=True, blank=True)
    city = models.CharField(max_length=255, null=True, blank=True)
    state = models.CharField(max_length=2, null=True, blank=True)
    zipcode = models.CharField(max_length=255, null=True, blank=True)
    order = models.IntegerField()
    trailer_pickup = models.BooleanField(default=False)
    trailer_drop = models.BooleanField(default=False)
    last_location = models.BooleanField(default=False)
    partial = models.BooleanField(default=False)
    load_pickup = models.BooleanField(default=False)
    load_drop = models.BooleanField(default=False)
    trailer_info = models.CharField(max_length=255, null=True, blank=True)
    requirements = models.TextField(null=True, blank=True)
    note = models.TextField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    last_updated = models.DateTimeField(auto_now=True)

    def __str__(self):
        return self.load.load_number

    class Meta:
        db_table = 'load_stops'
        verbose_name = 'Load Stop'
        verbose_name_plural = 'Load Stops'


class Batch(models.Model):
    name = models.CharField(max_length=255, null=True, blank=True)
    date = models.DateField()
    note = models.TextField(null=True, blank=True)
    submitted = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.name

    class Meta:
        db_table = 'batches'
        verbose_name = 'Batch'
        verbose_name_plural = 'Batches'


class BatchLoad(models.Model):
    batch = models.ForeignKey(Batch, on_delete=models.CASCADE)
    load = models.ForeignKey(Load, on_delete=models.CASCADE)
    status = models.CharField(max_length=255, null=True, blank=True)
    created_by = models.ForeignKey(CustomUser, on_delete=models.CASCADE, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.batch.name

    class Meta:
        db_table = 'batch_loads'
        verbose_name = 'Batch Load'
        verbose_name_plural = 'Batch Loads'


class PaymentType(models.Model): 
    quick_pay = models.IntegerField()
    regular_pay = models.IntegerField()
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"Quick Pay: {self.quick_pay}%, Regular Pay: {self.regular_pay}%"
    
    class Meta:
        db_table = 'payment_types'
        verbose_name = 'Payment Type'
        verbose_name_plural = 'Payment Types'


class Tag(models.Model):
    name = models.CharField(max_length=255, null=True, blank=True)
    color = models.CharField(max_length=240, null=True, blank=True)  # Hex color code

    def __str__(self):
        return self.name

    class Meta:
        db_table = 'tags'
        verbose_name = 'Tag'
        verbose_name_plural = 'Tags'


class LoadTag(models.Model):
    load = models.ForeignKey(Load, on_delete=models.CASCADE)
    tag = models.ForeignKey(Tag, on_delete=models.CASCADE)

    def __str__(self):
        return f"{self.load.load_number} - {self.tag.name}"

    class Meta:
        db_table = 'load_tags'
        verbose_name = 'Load Tag'
        verbose_name_plural = 'Load Tags'
