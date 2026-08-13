from django.db import models
import uuid
from django.utils import timezone
from hiring.models import Driver

class DriverOTP(models.Model):
    METHOD_CHOICES = [('sms', 'SMS'), ('email', 'Email')]
    driver = models.ForeignKey(Driver, on_delete=models.CASCADE, related_name='otps')
    code = models.CharField(max_length=6)
    method = models.CharField(max_length=10, choices=METHOD_CHOICES)
    is_used = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()

    def is_valid(self):
        return not self.is_used and self.expires_at > timezone.now()

    class Meta:
        db_table = 'driver_otps'
        verbose_name = 'Driver OTP'
        verbose_name_plural = 'Driver OTPs'
        ordering = ['-created_at']


class DriverAuthToken(models.Model):
    driver = models.ForeignKey(Driver, on_delete=models.CASCADE, related_name='auth_tokens')
    token = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()
    is_active = models.BooleanField(default=True)

    def is_valid(self):
        return self.is_active and self.expires_at > timezone.now()

    class Meta:
        db_table = 'driver_auth_tokens'
        verbose_name = 'Driver Auth Token'
        verbose_name_plural = 'Driver Auth Tokens'
        ordering = ['-created_at']


class DriverLocation(models.Model):
    driver = models.ForeignKey(Driver, on_delete=models.CASCADE, related_name='locations')
    latitude = models.FloatField()
    longitude = models.FloatField()
    device = models.CharField(max_length=255)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'driver_locations'
        verbose_name = 'Driver Location'
        verbose_name_plural = 'Driver Locations'
        ordering = ['-created_at']
