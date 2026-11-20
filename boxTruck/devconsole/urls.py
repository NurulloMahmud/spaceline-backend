from django.urls import path

from .views import ErrorLogsView

urlpatterns = [
    path('error-logs/', ErrorLogsView.as_view(), name='dev-error-logs'),
]
