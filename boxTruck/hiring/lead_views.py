"""Lead team, lead settings and lead posts (see hiring/leads.py).

Admins (Management, Developer, Dispatch manager) run the team and the group chat.
The bot token and the post log are for developers only (the Dev console).
"""

from __future__ import annotations

from typing import Any

from django.utils import timezone
from rest_framework import status, views
from rest_framework.permissions import BasePermission, IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response

from users.models import CustomUser
from users.permissions import IsDeveloper

from . import leads
from .models import LeadPost, LeadSettings, LeadTeamMember


class IsLeadAdmin(BasePermission):
    message = "Only Management, Developers and Dispatch managers can do this."

    def has_permission(self, request: Request, view: Any) -> bool:
        return leads.is_admin(request.user)


def _detail(message: str, code: int = status.HTTP_400_BAD_REQUEST) -> Response:
    return Response({"detail": message}, status=code)


class LeadCountView(views.APIView):
    """GET /hiring/lead-count/: drivers in the Lead column for the Hiring menu badge."""

    permission_classes = [IsAuthenticated]

    def get(self, request: Request) -> Response:
        return Response(leads.lead_count(request.user))


class LeadTeamView(views.APIView):
    """GET /hiring/lead-team/: the team in turn order, who is next, the chat and the bot.
    POST {user_id, telegram_username?}: adds a user at the end of the line."""

    permission_classes = [IsLeadAdmin]

    def get(self, request: Request) -> Response:
        return Response(leads.team_json())

    def post(self, request: Request) -> Response:
        body = request.data if hasattr(request.data, "get") else {}
        try:
            user_id = int(body.get("user_id"))
        except (TypeError, ValueError):
            return _detail("user_id is required.")
        user = CustomUser.objects.select_related("department").filter(pk=user_id).first()
        if user is None:
            return _detail("No such user.")
        if not user.is_active:
            return _detail("This user's account is not active.")
        if LeadTeamMember.objects.filter(user=user).exists():
            return _detail("This user is already on the lead team.")
        try:
            username = leads.clean_username(body.get("telegram_username"))
        except ValueError as exc:
            return _detail(str(exc))
        member = leads.add_member(user, username, request.user)
        member = LeadTeamMember.objects.select_related("user", "user__department").get(pk=member.pk)
        team = leads.team_json()
        return Response(
            leads.member_json(member, next_user_id=team["next_user_id"]),
            status=status.HTTP_201_CREATED,
        )


class LeadTeamMemberView(views.APIView):
    """PATCH /hiring/lead-team/{user_id}/ {telegram_username}; DELETE takes the user off
    the team (their leads stay assigned to them)."""

    permission_classes = [IsLeadAdmin]

    def _member(self, user_id: int) -> LeadTeamMember | None:
        return (
            LeadTeamMember.objects.select_related("user", "user__department")
            .filter(user_id=user_id)
            .first()
        )

    def patch(self, request: Request, user_id: int) -> Response:
        member = self._member(user_id)
        if member is None:
            return _detail("This user is not on the lead team.", status.HTTP_404_NOT_FOUND)
        body = request.data if hasattr(request.data, "get") else {}
        if "telegram_username" not in body:
            return _detail("telegram_username is required ('' clears it).")
        try:
            member.telegram_username = leads.clean_username(body.get("telegram_username"))
        except ValueError as exc:
            return _detail(str(exc))
        member.save(update_fields=["telegram_username"])
        return Response(leads.member_json(member, next_user_id=leads.team_json()["next_user_id"]))

    def delete(self, request: Request, user_id: int) -> Response:
        member = self._member(user_id)
        if member is None:
            return _detail("This user is not on the lead team.", status.HTTP_404_NOT_FOUND)
        member.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class LeadChatView(views.APIView):
    """PUT /hiring/lead-settings/chat/ {chat_id}: the group new leads are posted to.
    With a bot token saved, Telegram is asked for the chat's title (an error is a 400).
    DELETE clears it, so leads stop going to Telegram."""

    permission_classes = [IsLeadAdmin]

    def put(self, request: Request) -> Response:
        from . import telegram

        body = request.data if hasattr(request.data, "get") else {}
        chat_id = str(body.get("chat_id") or "").strip()
        if not leads.CHAT_ID_RE.match(chat_id):
            return _detail(
                "A group chat ID is a number like -1001234567890. Add the bot to the group, then copy the ID from it."
            )
        row = LeadSettings.get()
        token = leads.bot_token(row)
        title = ""
        if token:
            try:
                title = str((telegram.get_chat(token, chat_id) or {}).get("title") or "")
            except telegram.TelegramError as exc:
                return _detail(str(exc) or "Telegram did not find this chat.")
        row.chat_id = chat_id
        row.chat_title = title
        row.save(update_fields=["chat_id", "chat_title"])
        return Response(leads.chat_json(row))

    def delete(self, request: Request) -> Response:
        row = LeadSettings.get()
        row.chat_id = ""
        row.chat_title = ""
        row.save(update_fields=["chat_id", "chat_title"])
        return Response(status=status.HTTP_204_NO_CONTENT)


class LeadTestMessageView(views.APIView):
    """POST /hiring/lead-settings/test/: sends the test message to the group."""

    permission_classes = [IsLeadAdmin]

    def post(self, request: Request) -> Response:
        ok, detail = leads.send_test()
        if not ok:
            return _detail(detail)
        return Response({"ok": True})


class LeadBotView(views.APIView):
    """GET/PUT/DELETE /hiring/lead-settings/bot/ (developers). The raw token is never
    sent back: `masked` shows the bot id, the first 4 and the last 3 of the secret."""

    permission_classes = [IsDeveloper]

    def get(self, request: Request) -> Response:
        return Response(leads.bot_json(LeadSettings.get()))

    def put(self, request: Request) -> Response:
        from . import telegram

        body = request.data if hasattr(request.data, "get") else {}
        token = str(body.get("token") or "").strip()
        if not token or ":" not in token:
            return _detail("Paste the whole token BotFather gave, like 123456789:AAH...")
        try:
            username = telegram.check_token(token)
        except telegram.TelegramError as exc:
            return _detail(str(exc) or "Telegram did not accept this token.")
        row = LeadSettings.get()
        row.bot_token_enc = leads.encrypt_token(token)
        row.bot_username = str(username or "").lstrip("@")
        row.token_saved_at = timezone.now()
        row.token_saved_by = request.user
        fields = ["bot_token_enc", "bot_username", "token_saved_at", "token_saved_by"]
        if row.chat_id:
            # A chat saved before the bot existed gets its title now.
            try:
                row.chat_title = str((telegram.get_chat(token, row.chat_id) or {}).get("title") or "")
            except telegram.TelegramError:
                row.chat_title = ""
            fields.append("chat_title")
        row.save(update_fields=fields)
        return Response(leads.bot_json(row))

    def delete(self, request: Request) -> Response:
        row = LeadSettings.get()
        row.bot_token_enc = ""
        row.bot_username = ""
        row.token_saved_at = None
        row.token_saved_by = None
        row.save(update_fields=["bot_token_enc", "bot_username", "token_saved_at", "token_saved_by"])
        return Response(status=status.HTTP_204_NO_CONTENT)


class LeadPostListView(views.APIView):
    """GET /hiring/lead-posts/?limit=20 (developers): recent Telegram posts, newest first."""

    permission_classes = [IsDeveloper]

    def get(self, request: Request) -> Response:
        try:
            limit = int(request.query_params.get("limit") or 20)
        except ValueError:
            limit = 20
        limit = max(1, min(limit, 200))
        posts = LeadPost.objects.select_related("driver", "driver__assigned_to").order_by(
            "-created_at", "-id"
        )[:limit]
        return Response(
            [
                {
                    "id": p.id,
                    "at": p.created_at,
                    "kind": p.kind,
                    "status": p.status,
                    "error": p.error,
                    "photos": p.photos,
                    "attempts": p.attempts,
                    "driver": {"id": p.driver.id, "name": p.driver.full_name} if p.driver else None,
                    "assigned_to": leads.person(p.driver.assigned_to) if p.driver else None,
                }
                for p in posts
            ]
        )
