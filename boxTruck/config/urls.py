from django.contrib import admin
from django.urls import path, include
from users.views import CustomTokenObtainPairView

# MEDIA & STATIC
from django.conf import settings
from django.conf.urls.static import static

# Simple JWT
from rest_framework_simplejwt.views import (
    TokenObtainPairView,
    TokenRefreshView,
)

# Swagger
from rest_framework import permissions
from drf_yasg.views import get_schema_view
from drf_yasg import openapi

schema_view = get_schema_view(
    openapi.Info(
        title="DRF project template",
        default_version='v1',
        description="Django REST Framework project template with Authorization (Simple JWT)",
        terms_of_service="https://www.google.com/policies/terms/",
        contact=openapi.Contact(email="contact@snippets.local"),
        license=openapi.License(name="No License"),
    ),
    public=True,
    permission_classes=(permissions.AllowAny,),
)

urlpatterns = [
    path('api/admin/', admin.site.urls),
    path('api/auth/', include('rest_framework.urls')),
    path('api/analytics/', include('analytics.urls')),
    path('api/users/', include('users.urls')),
    path('api/ai/', include('ai.urls')),
    path('api/hiring/', include('hiring.urls')),
    path('api/billing/', include('billing.urls')),
    path('api/payroll/', include('payroll.urls')),
    path('api/mobile/', include('mobile.urls')),
    path('api/dispatchers/', include('dispatchers.urls')),
    path('api/swagger<format>/', schema_view.without_ui(cache_timeout=0), name='schema-json'),
    path('api/swagger/', schema_view.with_ui('swagger', cache_timeout=0), name='schema-swagger-ui'),
    path('redoc/', schema_view.with_ui('redoc', cache_timeout=0), name='schema-redoc'),
    path('api/token/', CustomTokenObtainPairView.as_view(), name='token_obtain_pair'),
    path('api/token/refresh/', TokenRefreshView.as_view(), name='token_refresh'),
    path('', schema_view.with_ui('swagger', cache_timeout=0), name='schema-swagger-ui'),
]

if settings.LOCAL and settings.DEBUG:
    urlpatterns += static(settings.STATIC_URL, document_root=settings.STATIC_ROOT)
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
