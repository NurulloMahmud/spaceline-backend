from rest_framework import serializers
from .models import DispatchSalary, DispatchBonus


class DispatchSalaryViewSerializer(serializers.ModelSerializer):
    created_by = serializers.SerializerMethodField()
    updated_by = serializers.SerializerMethodField()
    company = serializers.SerializerMethodField()

    class Meta:
        model = DispatchSalary
        fields = '__all__'

    def get_created_by(self, obj):
        if obj.created_by:
            return {
                'id': obj.created_by.id,
                'username': obj.created_by.username,
                'first_name': obj.created_by.first_name,
                'last_name': obj.created_by.last_name,
            }
        return None

    def get_updated_by(self, obj):
        if obj.updated_by:
            return {
                'id': obj.updated_by.id,
                'username': obj.updated_by.username,
                'first_name': obj.updated_by.first_name,
                'last_name': obj.updated_by.last_name,
            }
        return None

    def get_company(self, obj):
        if obj.company:
            return {
                'id': obj.company.id,
                'name': obj.company.name,
            }
        return None


class DispatchSalaryWriteSerializer(serializers.ModelSerializer):
    
    class Meta:
        model = DispatchSalary
        fields = '__all__'


class DispatchBonusViewSerializer(serializers.ModelSerializer):
    created_by = serializers.SerializerMethodField()
    updated_by = serializers.SerializerMethodField()
    company = serializers.SerializerMethodField()

    class Meta:
        model = DispatchBonus
        fields = '__all__'

    def get_created_by(self, obj):
        if obj.created_by:
            return {
                'id': obj.created_by.id,
                'username': obj.created_by.username,
                'first_name': obj.created_by.first_name,
                'last_name': obj.created_by.last_name,
            }
        return None

    def get_updated_by(self, obj):
        if obj.updated_by:
            return {
                'id': obj.updated_by.id,
                'username': obj.updated_by.username,
                'first_name': obj.updated_by.first_name,
                'last_name': obj.updated_by.last_name,
            }
        return None

    def get_company(self, obj):
        if obj.company:
            return {
                'id': obj.company.id,
                'name': obj.company.name,
            }
        return None


class DispatchBonusWriteSerializer(serializers.ModelSerializer):
    
    class Meta:
        model = DispatchBonus
        fields = '__all__'
