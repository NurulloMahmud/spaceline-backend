from django.contrib.auth import get_user_model
from rest_framework import serializers
from django.utils import timezone
from rest_framework.validators import UniqueValidator
from rest_framework_simplejwt.serializers import TokenObtainPairSerializer
from django.contrib.auth.password_validation import validate_password
from users.models import Company, CustomUser, Department, Team, UserInvite, Page, UserPageAccess


User = get_user_model()


class TeamSummarySerializer(serializers.ModelSerializer):
    """The compact form embedded in user and driver payloads."""

    class Meta:
        model = Team
        fields = ['id', 'name']


class TeamSerializer(serializers.ModelSerializer):
    lead_name = serializers.SerializerMethodField()
    company_name = serializers.CharField(source='company.name', read_only=True)
    member_count = serializers.SerializerMethodField()
    driver_count = serializers.SerializerMethodField()

    class Meta:
        model = Team
        fields = [
            'id', 'name', 'description', 'company', 'company_name',
            'lead', 'lead_name', 'is_active',
            'member_count', 'driver_count', 'created_at', 'last_updated',
        ]
        read_only_fields = ['created_at', 'last_updated']

    def get_lead_name(self, obj):
        if not obj.lead:
            return None
        return f"{obj.lead.first_name} {obj.lead.last_name}".strip() or obj.lead.username

    def get_member_count(self, obj):
        # Annotated by the viewset; falls back to a query for single reads.
        value = getattr(obj, 'members_total', None)
        return value if value is not None else obj.members.count()

    def get_driver_count(self, obj):
        value = getattr(obj, 'drivers_total', None)
        return value if value is not None else obj.drivers.count()

    def validate(self, attrs):
        """A team lead must belong to the same company as the team."""
        company = attrs.get('company') or getattr(self.instance, 'company', None)
        lead = attrs.get('lead') or getattr(self.instance, 'lead', None)
        if lead and company and lead.company_id != company.id:
            raise serializers.ValidationError(
                {"lead": "The team lead must belong to the same company as the team."}
            )
        return attrs


class TeamAssignUsersSerializer(serializers.Serializer):
    user_ids = serializers.ListField(
        child=serializers.IntegerField(), allow_empty=False,
        help_text="Users to move onto this team.",
    )


class TeamMemberSerializer(serializers.ModelSerializer):
    department = serializers.CharField(source='department.name', read_only=True)
    full_name = serializers.SerializerMethodField()

    class Meta:
        model = CustomUser
        fields = ['id', 'username', 'first_name', 'last_name', 'full_name',
                  'department', 'is_active']

    def get_full_name(self, obj):
        return f"{obj.first_name} {obj.last_name}".strip() or obj.username




class CompanySerializer(serializers.ModelSerializer):
    class Meta:
        model = Company
        fields = '__all__'


class PasswordResetRequestSerializer(serializers.Serializer):
    username = serializers.CharField(required=True)


class PasswordResetSerializer(serializers.Serializer):
    password = serializers.CharField(required=True)
    confirm_password = serializers.CharField(required=True)

    def validate(self, data):
        if data['password'] != data['confirm_password']:
            raise serializers.ValidationError("New password and confirm password should be similar")
        return data


class CustomTokenObtainPairSerializer(TokenObtainPairSerializer):
    @classmethod
    def get_token(cls, user):
        token = super().get_token(user)
        token["department"] = user.department.name if user.department else None
        token["company"] = user.company.name if user.company else None
        token["company_id"] = user.company.id if user.company else None
        return token

    def validate(self, attrs):
        data = super().validate(attrs)

        if not self.user.is_active:
            raise serializers.ValidationError("User is not active")

        data.update({
            'user_id': self.user.id,
            'username': self.user.username,
            'first_name': self.user.first_name,
            'last_name': self.user.last_name,
            'department': self.user.department.name if self.user.department else None,
            'company_id': self.user.company.id if self.user.company else None,
            'company': self.user.company.name if self.user.company else None
        })
        return data


class UserRegisterSerializer(serializers.ModelSerializer):
    password = serializers.CharField(write_only=True, validators=[validate_password])
    username = serializers.CharField(
        min_length=3,
        validators=[
            UniqueValidator(queryset=CustomUser.objects.all())
        ]
    )
    
    class Meta:
        model = CustomUser
        fields = ['id', 'username', 'password', 'first_name', 'last_name']

    def create(self, validated_data):
        user = CustomUser.objects.create_user(**validated_data)
        return user


class UserProfileUpdateSerializer(serializers.ModelSerializer):
    password = serializers.CharField(write_only=True, validators=[validate_password])
    
    class Meta:
        model = CustomUser
        fields = ['id', 'username', 'password', 'first_name', 'last_name', 'company', 'department', 'is_active']
        extra_kwargs = {
            'id': {'read_only': True},
            'company': {'read_only': True},
            'first_name': {'read_only': True},
            'last_name': {'read_only': True},
            'department': {'read_only': True},
            'is_active': {'read_only': True}
        }

    def update(self, instance, validated_data):
        password = validated_data.pop('password', None)
        for attr, value in validated_data.items():
            setattr(instance, attr, value)

        if password:
            instance.set_password(password)
        instance.save()
        return instance


class UserUpdateSerializer(serializers.ModelSerializer):
    class Meta:
        model = CustomUser
        fields = ['id', 'username', 'first_name', 'last_name', 'company', 'department', 'team', 'is_active', 'started_working_date']
        extra_kwargs = {
            'username': {'read_only': True},
            'password': {'read_only': True}
        }
    
    def validate(self, attrs):
        request = self.context.get('request')
        user = request.user
        new_department = attrs.get('department')
        if user.department and user.department.name.lower() == "dispatch manager":
            if new_department:
                restricted_departments = ["management", "billing", "payroll"]
                if new_department.name.lower() in restricted_departments:
                    raise serializers.ValidationError({
                        "department": "Dispatch Manager cannot assign users to Management, Billing, or Payroll."
                    })
        return attrs


class PageSerializer(serializers.ModelSerializer):
    class Meta:
        model = Page
        fields = ['id', 'key', 'name', 'path', 'is_active']


class UserPageAccessSerializer(serializers.ModelSerializer):
    username = serializers.CharField(source='user.username', read_only=True)
    page_key = serializers.CharField(source='page.key', read_only=True)
    page_name = serializers.CharField(source='page.name', read_only=True)

    class Meta:
        model = UserPageAccess
        fields = [
            'id', 'user', 'username', 'page', 'page_key', 'page_name',
            'can_view', 'can_create', 'can_edit', 'can_delete'
        ]


class UserListSerializer(serializers.ModelSerializer):
    company = CompanySerializer()
    team = TeamSummarySerializer(read_only=True)
    department = serializers.SerializerMethodField(method_name='get_department')
    page_access = UserPageAccessSerializer(many=True, read_only=True)

    def get_department(self, obj):
        return obj.department.name if obj.department else None

    class Meta:
        model = CustomUser
        fields = ['id', 'username', 'first_name', 'last_name', 'company', 'department', 'team', 'is_active', 'started_working_date', 'page_access']


class DepartmentSerializer(serializers.ModelSerializer):

    class Meta:
        model = Department
        fields = ['id', 'name']


class MyPageAccessSerializer(serializers.ModelSerializer):
    key = serializers.CharField(source='page.key')
    name = serializers.CharField(source='page.name')
    path = serializers.CharField(source='page.path')

    class Meta:
        model = UserPageAccess
        fields = ['key', 'name', 'path', 'can_view', 'can_create', 'can_edit', 'can_delete']


class UpdaterUsersSerializer(serializers.ModelSerializer):
    company = CompanySerializer()
    department = serializers.SerializerMethodField(method_name='get_department')
    active_loads_count = serializers.IntegerField(read_only=True)

    def get_department(self, obj):
        return obj.department.name if obj.department else None

    class Meta:
        model = CustomUser
        fields = [
            'id',
            'username',
            'first_name',
            'last_name',
            'company',
            'department',
            'is_active',
            'active_loads_count'
        ]


class InviteUserSerializer(serializers.ModelSerializer):
    department = serializers.PrimaryKeyRelatedField(queryset=Department.objects.all())

    class Meta:
        model = UserInvite
        fields = ['username', 'department']

    def validate_username(self, value):
        if User.objects.filter(username=value).exists():
            raise serializers.ValidationError("This username is already taken.")
        if UserInvite.objects.filter(username=value, is_used=False).exists():
            raise serializers.ValidationError("An active invite for this username already exists.")
        return value
    
    def validate_department(self, value):
        request = self.context.get('request')
        user = request.user
        if user.department and user.department.name.lower() == "dispatch manager":
            restricted_departments = ["management", "billing", "payroll"]
            if value.name.lower() in restricted_departments:
                raise serializers.ValidationError(
                    "Dispatch Manager cannot invite users to Management, Billing, or Payroll."
                )
        return value

    def create(self, validated_data):
        request = self.context['request']
        return UserInvite.objects.create(
            username=validated_data['username'],
            department=validated_data['department'],
            company=request.user.company,
            invited_by=request.user
        )


class RegisterWithInviteSerializer(serializers.Serializer):
    token = serializers.UUIDField()
    first_name = serializers.CharField(max_length=100)
    last_name = serializers.CharField(max_length=100)
    password = serializers.CharField(write_only=True, min_length=6)

    def validate(self, attrs):
        token = attrs.get("token")

        try:
            invite = UserInvite.objects.select_related('company', 'department').get(token=token)
        except UserInvite.DoesNotExist:
            raise serializers.ValidationError({"token": "Invalid invite token."})

        if invite.is_used:
            raise serializers.ValidationError({"token": "This invite has already been used."})

        if invite.expires_at and invite.expires_at < timezone.now():
            raise serializers.ValidationError({"token": "This invite has expired."})

        if User.objects.filter(username=invite.username).exists():
            raise serializers.ValidationError({"token": "This username is already taken."})

        attrs["invite"] = invite
        return attrs

    def create(self, validated_data):
        invite = validated_data["invite"]

        user = User.objects.create_user(
            username=invite.username,
            first_name=validated_data["first_name"],
            last_name=validated_data["last_name"],
            password=validated_data["password"],
            company=invite.company,
            department=invite.department,
            is_active=True
        )

        invite.is_used = True
        invite.save(update_fields=["is_used"])

        return user