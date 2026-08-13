from django.urls import path
from .views import (
    AskQuestionView,
    ConversationListView,
    ConversationDetailView,
    ConversationMessagesView,
    ConversationDeleteView
)

app_name = 'ai_analyst'

urlpatterns = [
    path('ask/', AskQuestionView.as_view(), name='ask'), # checked
    path('conversations/', ConversationListView.as_view(), name='conversation-list'), # checked
    path('conversations/<int:pk>/', ConversationDetailView.as_view(), name='conversation-detail'), # checked
    path('conversations/<int:pk>/messages/', ConversationMessagesView.as_view(), name='conversation-messages'), # checked
    path('conversations/<int:pk>/delete/', ConversationDeleteView.as_view(), name='conversation-delete'), # checked
]
