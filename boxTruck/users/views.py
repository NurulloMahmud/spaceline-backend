from django.db.models import Count, Q
from django.utils import timezone
from django.contrib.auth import get_user_model
from django.utils.http import urlsafe_base64_encode, urlsafe_base64_decode
from rest_framework import status, generics, views
from rest_framework.generics import CreateAPIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated, AllowAny
from rest_framework import viewsets
from rest_framework.decorators import action
from rest_framework.views import APIView
from rest_framework_simplejwt.views import TokenObtainPairView
from .permissions import IsAdminUser, IsDispatchManager, IsDispatch, IsInternalService, IsUpdater
from .models import PasswordReset, Company, CustomUser, Department, Team, UserInvite, Page, UserPageAccess
from .serializers import (
    InviteUserSerializer, PasswordResetRequestSerializer, PasswordResetSerializer, CompanySerializer, UserProfileSerializer, RegisterWithInviteSerializer, UserRegisterSerializer, CustomTokenObtainPairSerializer,
    UserProfileUpdateSerializer, UserUpdateSerializer, UserListSerializer, DepartmentSerializer, UpdaterUsersSerializer,
    PageSerializer, UserPageAccessSerializer, MyPageAccessSerializer,
    TeamSerializer, TeamAssignUsersSerializer, TeamMemberSerializer
)

from hiring.models import Driver  # noqa: E402  (team deletion unassigns drivers)

User = get_user_model()


class CompanyProfileView(generics.RetrieveUpdateAPIView):
    """The one company this deployment belongs to. There is no list, create or delete."""
    serializer_class = CompanySerializer

    def get_permissions(self):
        if self.request.method == 'GET':
            return [IsAuthenticated()]
        return [IsAdminUser()]

    def get_object(self):
        return Company.current()


class RegisterAPIView(CreateAPIView):
    serializer_class = UserRegisterSerializer

    def post(self, request, *args, **kwargs):
        serializer = UserRegisterSerializer(data=request.data)
        if serializer.is_valid():
            serializer.save()
            context = {
                "success": True,
                "message": "User created successfully",
                "data": serializer.data
            }
            return Response(context, status=status.HTTP_201_CREATED)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


class UserProfileUpdateView(generics.UpdateAPIView):
    serializer_class = UserProfileUpdateSerializer
    permission_classes = [IsAuthenticated]

    def get_object(self):
        return self.request.user


class UserListAPIView(generics.ListAPIView):
    serializer_class = UserListSerializer

    def get_queryset(self):
        return CustomUser.objects.all()

    def get_permissions(self):
        if self.request.method == 'GET':
            permission_classes = [IsAuthenticated]
        else:
            permission_classes = [IsAuthenticated]
        return [permission() for permission in permission_classes]


class UserRetrieveAPIView(generics.ListAPIView):
    serializer_class = UserProfileSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        user = self.request.user
        queryset = CustomUser.objects.filter(id=user.id)
        return queryset


class UserUpdateView(generics.UpdateAPIView):
    serializer_class = UserUpdateSerializer
    queryset = CustomUser.objects.all()

    def get_permissions(self):
        if self.request.method == 'GET':
            permission_classes = [IsDispatchManager | IsAdminUser | IsUpdater]
        else:
            permission_classes = [IsDispatchManager | IsAdminUser | IsUpdater]
        return [permission() for permission in permission_classes]


class PasswordResetRequestView(APIView):

    def get_permissions(self):
        if self.request.method == 'GET':
            permission_classes = [IsDispatchManager | IsAdminUser | IsUpdater]
        else:
            permission_classes = [IsDispatchManager | IsAdminUser | IsUpdater]
        return [permission() for permission in permission_classes]

    def post(self, request):
        serializer = PasswordResetRequestSerializer(data=request.data)
        if serializer.is_valid():
            username = serializer.validated_data['username']
            try:
                user = User.objects.get(username=username)
                reset_request = PasswordReset.objects.filter(username=user, clicked=False).first()
                if reset_request:
                    encoded_username = urlsafe_base64_encode(username.encode())
                    reset_link = f"/password-reset/{encoded_username}/"
                    return Response({"reset_link": reset_link}, status=status.HTTP_200_OK)

                reset_request = PasswordReset.objects.create(username=user, clicked=False)
                encoded_username = urlsafe_base64_encode(username.encode())
                reset_link = f"/password-reset/{encoded_username}/"
                return Response({"reset_link": reset_link}, status=status.HTTP_200_OK)
            except User.DoesNotExist:
                return Response({"error": "User not found"}, status=status.HTTP_404_NOT_FOUND)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


class PasswordResetView(APIView):
    permission_classes = [AllowAny]

    def get(self, request, encoded_username):
        try:
            username = urlsafe_base64_decode(encoded_username).decode()
            reset_request = PasswordReset.objects.filter(username__username=username, clicked=False).first()
            if not reset_request:
                return Response({"status": "Invalid", "error": "Password reset request does not exist."}, status=status.HTTP_400_BAD_REQUEST)

            reset_request.clicked = True
            reset_request.save()
            return Response({"status": "Valid"}, status=status.HTTP_200_OK)
        except ValueError:
            return Response({"status": "Invalid", "error": "Invalid encoded username."}, status=status.HTTP_400_BAD_REQUEST)

    def post(self, request, encoded_username):
        serializer = PasswordResetSerializer(data=request.data)
        if serializer.is_valid():
            try:
                username = urlsafe_base64_decode(encoded_username).decode()
                reset_request = PasswordReset.objects.filter(username__username=username, clicked=True).first()
                if not reset_request:
                    return Response({"status": "Invalid", "error": "Password reset request does not exist or has already been used."}, status=status.HTTP_400_BAD_REQUEST)
                user = CustomUser.objects.get(username=username)
                user.set_password(serializer.validated_data['password'])
                user.save()
                reset_request.clicked = True
                reset_request.save()
                return Response({"status": "Password reset"}, status=status.HTTP_200_OK)
            except (CustomUser.DoesNotExist, ValueError):
                return Response({"error": "Invalid reset link"}, status=status.HTTP_400_BAD_REQUEST)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


class CustomTokenObtainPairView(TokenObtainPairView):
    serializer_class = CustomTokenObtainPairSerializer


class DepartmentViewSet(viewsets.ModelViewSet):
    serializer_class = DepartmentSerializer
    queryset = Department.objects.all()

    def get_permissions(self):
        if self.request.method == 'GET':
            permission_classes = [IsAuthenticated]
        else:
            permission_classes = [IsAdminUser]
        return [permission() for permission in permission_classes]


class PageViewSet(viewsets.ModelViewSet):
    queryset = Page.objects.all()
    serializer_class = PageSerializer

    def get_permissions(self):
        if self.request.method == 'GET':
            permission_classes = [IsAuthenticated]
        else:
            permission_classes = [IsAdminUser]
        return [permission() for permission in permission_classes]


class UserPageAccessViewSet(viewsets.ModelViewSet):
    serializer_class = UserPageAccessSerializer
    permission_classes = [IsAdminUser]

    def get_queryset(self):
        queryset = UserPageAccess.objects.select_related('user', 'page').all()
        user_id = self.request.query_params.get('user')
        if user_id:
            queryset = queryset.filter(user_id=user_id)
        return queryset


class MyPageAccessView(generics.ListAPIView):
    serializer_class = MyPageAccessSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        return UserPageAccess.objects.select_related('page').filter(
            user=self.request.user, can_view=True, page__is_active=True
        )


class DispatchUsers(generics.ListAPIView):
    serializer_class = UserListSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        return CustomUser.objects.filter(department__name__in=['Dispatch', 'Dispatch Manager'])


class UpdaterUsersAPIView(generics.ListAPIView):
    serializer_class = UpdaterUsersSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        return CustomUser.objects.filter(
            department__name__in=['Updater']
        ).annotate(
            active_loads_count=Count(
                'updated_by',
                filter=~Q(updated_by__status__name__in=['Delivered', 'Cancelled', 'Invoiced', 'Rejected', 'Factored'])
            )
        )


class CreateInviteView(generics.CreateAPIView):
    serializer_class = InviteUserSerializer
    permission_classes = [IsAuthenticated]

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        invite = serializer.save()
        invite_link = f"https://spaceline.boxtruckmanage.com/register/invite/{invite.token}/"
        return Response({
            "message": "Invite link created successfully.",
            "invite_link": invite_link,
            "token": str(invite.token)
        })


class InviteDetailView(views.APIView):
    def get(self, request, token):
        try:
            invite = UserInvite.objects.select_related('department').get(token=token)
        except UserInvite.DoesNotExist:
            return Response({"detail": "Invalid invite link."}, status=status.HTTP_404_NOT_FOUND)

        if invite.is_used:
            return Response({"detail": "This invite has already been used."}, status=status.HTTP_400_BAD_REQUEST)

        if invite.expires_at and invite.expires_at < timezone.now():
            return Response({"detail": "This invite has expired."}, status=status.HTTP_400_BAD_REQUEST)

        return Response({
            "username": invite.username,
            "company": Company.current().name,
            "department": invite.department.name if invite.department else None,
        })
    

class RegisterWithInviteView(generics.CreateAPIView):
    serializer_class = RegisterWithInviteSerializer

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = serializer.save()
        return Response({
            "message": "User registered successfully.",
            "username": user.username
        })


class UserBulkView(views.APIView):
    permission_classes = [IsInternalService]

    def get(self, request):
        ids = request.query_params.get('ids', '')
        if not ids:
            return Response([])
        id_list = [i for i in ids.split(',') if i.strip().isdigit()]
        users = CustomUser.objects.filter(id__in=id_list).values(
            'id', 'username', 'first_name', 'last_name'
        )
        return Response(list(users))


class TeamViewSet(viewsets.ModelViewSet):
    """
    Dispatch teams.
    """
    serializer_class = TeamSerializer

    def get_permissions(self):
        if self.request.method in ('GET', 'HEAD', 'OPTIONS'):
            return [IsAuthenticated()]
        return [(IsAdminUser | IsDispatchManager)()]

    def get_queryset(self):
        queryset = (
            Team.objects
            .select_related('lead')
            .annotate(
                members_total=Count('members', distinct=True),
                drivers_total=Count('drivers', distinct=True),
            )
        )
        is_active = self.request.query_params.get('is_active')
        if is_active in ('true', 'false'):
            queryset = queryset.filter(is_active=is_active == 'true')

        search = self.request.query_params.get('search')
        if search:
            queryset = queryset.filter(name__icontains=search)

        return queryset

    def perform_create(self, serializer):
        serializer.save(created_by=self.request.user)

    def destroy(self, request, *args, **kwargs):
        """
        Deleting a team unassigns its members and drivers rather than
        cascading — losing driver records because a team was tidied up would
        be unrecoverable.
        """
        team = self.get_object()
        members = CustomUser.objects.filter(team=team).update(team=None)
        drivers = Driver.objects.filter(team=team).update(team=None)
        team.delete()
        return Response(
            {"detail": "Team deleted.", "users_unassigned": members,
             "drivers_unassigned": drivers},
            status=status.HTTP_200_OK,
        )

    @action(detail=True, methods=['get'])
    def members(self, request, pk=None):
        """The dispatchers on this team."""
        team = self.get_object()
        users = CustomUser.objects.filter(team=team).select_related('department')
        return Response(TeamMemberSerializer(users, many=True).data)

    @action(detail=True, methods=['post'], url_path='assign-users')
    def assign_users(self, request, pk=None):
        """Move users onto this team. Users already elsewhere are re-pointed."""
        team = self.get_object()
        serializer = TeamAssignUsersSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user_ids = serializer.validated_data['user_ids']

        eligible = CustomUser.objects.filter(id__in=user_ids)
        found = set(eligible.values_list('id', flat=True))
        rejected = [uid for uid in user_ids if uid not in found]

        updated = eligible.update(team=team)
        return Response({
            "assigned": updated,
            "rejected_user_ids": rejected,
            "detail": (
                "Users that do not exist were skipped."
                if rejected else "All users assigned."
            ),
        })

    @action(detail=True, methods=['post'], url_path='remove-users')
    def remove_users(self, request, pk=None):
        team = self.get_object()
        serializer = TeamAssignUsersSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        removed = CustomUser.objects.filter(
            id__in=serializer.validated_data['user_ids'], team=team
        ).update(team=None)
        return Response({"removed": removed})


class UnassignedUsersAPIView(generics.ListAPIView):
    """
    Dispatch users with no team yet — the worklist for getting everyone
    assigned, since the column is nullable for existing records.
    """
    serializer_class = TeamMemberSerializer
    permission_classes = [IsAdminUser | IsDispatchManager | IsUpdater]

    def get_queryset(self):
        queryset = CustomUser.objects.filter(team__isnull=True).select_related('department')
        only_dispatch = self.request.query_params.get('dispatch_only')
        if only_dispatch != 'false':
            queryset = queryset.filter(
                department__name__in=['Dispatch', 'Dispatch Manager', 'Updater']
            )
        return queryset
