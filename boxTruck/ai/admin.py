from django.contrib import admin
from .models import AIConversation, AIMessage


class AIMessageInline(admin.TabularInline):
    model = AIMessage
    fields = ('role', 'content', 'sql_query', 'created_at')
    readonly_fields = ('role', 'content', 'sql_query', 'query_result', 'chart_config', 'created_at')
    extra = 0
    can_delete = False


@admin.register(AIConversation)
class AIConversationAdmin(admin.ModelAdmin):
    list_display = ('id', 'user', 'company', 'title', 'created_at', 'last_updated')
    list_filter = ('company', 'created_at')
    search_fields = ('user__username', 'title')
    readonly_fields = ('created_at', 'last_updated')
    inlines = [AIMessageInline]


@admin.register(AIMessage)
class AIMessageAdmin(admin.ModelAdmin):
    list_display = ('id', 'conversation', 'role', 'short_content', 'created_at')
    list_filter = ('role', 'created_at')
    readonly_fields = ('conversation', 'role', 'content', 'sql_query', 'query_result', 'chart_config', 'created_at')

    def short_content(self, obj):
        return obj.content[:80] + ('...' if len(obj.content) > 80 else '')
    short_content.short_description = 'Content'
