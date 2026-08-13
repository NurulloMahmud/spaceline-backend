from rest_framework import serializers
from .models import AIConversation, AIMessage


class AIMessageSerializer(serializers.ModelSerializer):
    class Meta:
        model = AIMessage
        fields = [
            'id',
            'role',
            'content',
            'sql_query',
            'query_result',
            'chart_config',
            'created_at',
        ]
        read_only_fields = fields


class AIConversationSerializer(serializers.ModelSerializer):
    messages = AIMessageSerializer(many=True, read_only=True)
    user_full_name = serializers.SerializerMethodField()

    class Meta:
        model = AIConversation
        fields = [
            'id',
            'title',
            'user_full_name',
            'created_at',
            'last_updated',
            'messages',
        ]
        read_only_fields = fields

    def get_user_full_name(self, obj):
        return f"{obj.user.first_name} {obj.user.last_name}".strip()


class AIConversationListSerializer(serializers.ModelSerializer):
    user_full_name = serializers.SerializerMethodField()
    message_count = serializers.SerializerMethodField()

    class Meta:
        model = AIConversation
        fields = [
            'id',
            'title',
            'user_full_name',
            'message_count',
            'created_at',
            'last_updated',
        ]
        read_only_fields = fields

    def get_user_full_name(self, obj):
        return f"{obj.user.first_name} {obj.user.last_name}".strip()

    def get_message_count(self, obj):
        return obj.messages.count()


class AskQuestionSerializer(serializers.Serializer):
    question = serializers.CharField(
        max_length=2000,
        help_text="Natural language question about your data"
    )
    conversation_id = serializers.IntegerField(
        required=False,
        allow_null=True,
        help_text="Continue an existing conversation. Leave null to start a new one."
    )
