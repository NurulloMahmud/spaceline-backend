import uuid
from django.db import models
from django.contrib.auth.models import AbstractUser
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError


class Department(models.Model):
    name = models.CharField(max_length=100)

    def __str__(self):
        return f"{self.id}. {self.name}"

    class Meta:
        verbose_name_plural = "Departments"
        verbose_name = "Department"
        db_table = "departments"
        ordering = ['id']


class Company(models.Model):
    name = models.CharField(max_length=100)
    shipment_number = models.CharField(max_length=255, null=True, blank=True)
    address = models.CharField(max_length=255, null=True, blank=True)
    description = models.TextField(null=True, blank=True)
    email = models.EmailField(null=True, blank=True)
    mc = models.CharField(max_length=255, null=True, blank=True)
    website = models.CharField(max_length=255, null=True, blank=True)
    phone_number = models.CharField(max_length=255, null=True, blank=True)
    logo = models.ImageField(upload_to='companies/logos/', null=True, blank=True)
    bid_validity_minutes = models.PositiveIntegerField(default=15)
    contract_template_text = models.TextField(null=True, blank=True)
    contract_signer_name = models.CharField(max_length=255, null=True, blank=True)
    contract_signer_title = models.CharField(max_length=255, null=True, blank=True)

    @classmethod
    def current(cls):
        """The one company this deployment belongs to."""
        return cls.objects.order_by('id').first() or cls.objects.create(name='Company')

    def save(self, *args, **kwargs):
        if self.pk is None and Company.objects.exists():
            raise ValidationError('This deployment already has its company.')
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.id}. {self.name}"

    class Meta:
        verbose_name_plural = "Companies"
        verbose_name = "Company"
        db_table = "companies"
        ordering = ['id']


class Team(models.Model):
    """
    A dispatch team: a group of dispatchers and the drivers they run.

    Team membership is nullable on both sides. Existing users and drivers
    predate teams, and forcing the column would have refused to migrate a
    live database — assignment is done through the endpoints instead, and
    the unassigned lists show what is still outstanding.
    """
    name = models.CharField(max_length=100, unique=True)
    lead = models.ForeignKey(
        'CustomUser', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='teams_led',
    )
    description = models.TextField(null=True, blank=True)
    is_active = models.BooleanField(default=True)
    created_by = models.ForeignKey(
        'CustomUser', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='teams_created',
    )
    created_at = models.DateTimeField(auto_now_add=True)
    last_updated = models.DateTimeField(auto_now=True)

    def __str__(self):
        return self.name

    class Meta:
        verbose_name = "Team"
        verbose_name_plural = "Teams"
        db_table = "teams"
        ordering = ['name']


class CustomUser(AbstractUser):
    first_name = models.CharField(max_length=100, null=False, blank=False)
    last_name = models.CharField(max_length=100, null=False, blank=False)
    department = models.ForeignKey(Department, on_delete=models.CASCADE, null=True, blank=True)
    team = models.ForeignKey(
        Team, on_delete=models.SET_NULL, null=True, blank=True, related_name='members'
    )
    started_working_date = models.DateField(null=True, blank=True)
    is_active = models.BooleanField(default=False)

    def __str__(self):
        return self.username

    class Meta:
        verbose_name_plural = "CustomUsers"
        verbose_name = "CustomUser"
        db_table = "users"
        ordering = ['id']


class PasswordReset(models.Model):
    username = models.ForeignKey(get_user_model(), on_delete=models.CASCADE)
    created_at = models.DateTimeField(auto_now_add=True)
    clicked = models.BooleanField(default=False)

    def __str__(self):
        return f"{self.username}"

    class Meta:
        verbose_name_plural = "Reset Passwords"
        verbose_name = "Reset Password"
        db_table = "reset_passwords"
        ordering = ['-created_at']


class UserInvite(models.Model):
    username = models.CharField(max_length=150)
    department = models.ForeignKey('Department', on_delete=models.CASCADE)
    invited_by = models.ForeignKey(
        get_user_model(),
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='sent_invites'
    )
    token = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    is_used = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return self.username


class Page(models.Model):
    key = models.SlugField(max_length=100, unique=True)
    name = models.CharField(max_length=100)
    path = models.CharField(max_length=255, null=True, blank=True)
    is_active = models.BooleanField(default=True)

    def __str__(self):
        return self.name

    class Meta:
        verbose_name_plural = "Pages"
        verbose_name = "Page"
        db_table = "pages"
        ordering = ['id']


class UserPageAccess(models.Model):
    user = models.ForeignKey(get_user_model(), on_delete=models.CASCADE, related_name='page_access')
    page = models.ForeignKey(Page, on_delete=models.CASCADE, related_name='user_access')
    can_view = models.BooleanField(default=True)
    can_create = models.BooleanField(default=False)
    can_edit = models.BooleanField(default=False)
    can_delete = models.BooleanField(default=False)

    def __str__(self):
        return f"{self.user} - {self.page}"

    class Meta:
        verbose_name_plural = "User Page Access"
        verbose_name = "User Page Access"
        db_table = "user_page_access"
        unique_together = ('user', 'page')
        ordering = ['id']
