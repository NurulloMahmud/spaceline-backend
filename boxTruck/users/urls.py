from django.urls import path, include
from rest_framework.routers import SimpleRouter

from .views import (CompanyViewSet, RegisterAPIView, PasswordResetRequestView, PasswordResetView, DispatchUsers,
                    UserProfileUpdateView, UserUpdateView, UserListAPIView, UserRetrieveAPIView, DepartmentViewSet,
                    UpdaterUsersAPIView, CreateInviteView, InviteDetailView, RegisterWithInviteView, UserBulkView,
                    PageViewSet, UserPageAccessViewSet, MyPageAccessView,
                    TeamViewSet, UnassignedUsersAPIView
                    )

router = SimpleRouter()
router.register(r'companies', CompanyViewSet, basename='company') #checked
router.register(r'departments', DepartmentViewSet, basename='department') #checked
router.register(r'teams', TeamViewSet, basename='team')
router.register(r'pages', PageViewSet, basename='page')
router.register(r'user-page-access', UserPageAccessViewSet, basename='user-page-access')

urlpatterns = [
    path('', include(router.urls)),
    path('password-change/', UserProfileUpdateView.as_view(), name='password-change'), #checked
    path('list/', UserListAPIView.as_view(), name='list'), #checked
    path('update/<int:pk>/', UserUpdateView.as_view(), name='update'), #checked
    path('register/', RegisterAPIView.as_view(), name='register'), #checked
    path('password-reset-request/', PasswordResetRequestView.as_view(), name='password-reset-request'),
    path('password-reset/<str:encoded_username>/', PasswordResetView.as_view(), name='password-reset'),
    path('user-profile/', UserRetrieveAPIView.as_view(), name='user-profile'), #checked
    path('dispatch-users/', DispatchUsers.as_view(), name='dispatch-users'), #checked
    path('updater-users/', UpdaterUsersAPIView.as_view(), name='updater-users'), #checked
    path('users-bulk/', UserBulkView.as_view(), name='user-bulk'),
    path('unassigned/', UnassignedUsersAPIView.as_view(), name='unassigned-users'),
    path('my-pages/', MyPageAccessView.as_view(), name='my-pages'),

    path('invite/create/', CreateInviteView.as_view(), name='invite-create'),
    path('invite-detail/<uuid:token>/', InviteDetailView.as_view(), name='invite-detail'),
    path('register/invite/', RegisterWithInviteView.as_view(), name='register-with-invite'),
]
