import logging

from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import AIConversation, AIMessage
from users.permissions import IsAdminUser, IsDispatch, IsDispatchManager, IsBilling, IsPayroll
from .serializers import (
    AIConversationListSerializer,
    AIConversationSerializer,
    AskQuestionSerializer,
)
from .services import ask_ai_analyst

logger = logging.getLogger(__name__)


class AskQuestionView(APIView):
    def get_permissions(self):
        if self.request.method == 'GET':
            permission_classes = [IsAuthenticated]
        else:
            permission_classes = [IsAdminUser | IsDispatch | IsDispatchManager | IsBilling | IsPayroll]
        return [permission() for permission in permission_classes]

    def post(self, request):
        serializer = AskQuestionSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        question = serializer.validated_data['question']
        conversation_id = serializer.validated_data.get('conversation_id')
        user = request.user
        company = user.company
        ALL_DATA_DEPARTMENTS = ['Management', 'Billing', 'Payroll']
        department_name = user.department.name if user.department else None
        has_global_access = (
            user.is_superuser
            or user.is_staff
            or department_name in ALL_DATA_DEPARTMENTS
        )

        if not has_global_access and not company:
            return Response(
                {"error": "Your account is not linked to a company."},
                status=status.HTTP_400_BAD_REQUEST
            )
        company_id_for_query = None if has_global_access else company.id

        conversation = None
        if conversation_id:
            try:
                qs_filter = {"id": conversation_id, "user": user}
                if not has_global_access:
                    qs_filter["company"] = company
                conversation = AIConversation.objects.get(**qs_filter)
            except AIConversation.DoesNotExist:
                return Response(
                    {"error": "Conversation not found."},
                    status=status.HTTP_404_NOT_FOUND
                )

        if not conversation:
            title = question[:80] + ('...' if len(question) > 80 else '')
            conversation = AIConversation.objects.create(
                user=user,
                company=company,
                title=title
            )

        previous_messages = conversation.messages.order_by('created_at')
        history = []
        for msg in previous_messages:
            content = msg.content
            if msg.role == 'assistant' and msg.sql_query:
                content = f"[Previous SQL used: {msg.sql_query}]\n{msg.content}"
            history.append({"role": msg.role, "content": content})

        if company_id_for_query:
            history.insert(0, {
                "role": "system",
                "content": (
                    f"REMINDER: Every SQL query MUST include `loads.company_id = {company_id_for_query}` "
                    f"in the WHERE clause. No exceptions. Never filter by username alone without company_id."
                )
            })

        AIMessage.objects.create(
            conversation=conversation,
            role='user',
            content=question
        )
        result = ask_ai_analyst(
            question=question,
            company_id=company_id_for_query,
            conversation_history=history
        )

        if result.get('error') and not result.get('answer'):
            return Response(
                {"error": result['error']},
                status=status.HTTP_503_SERVICE_UNAVAILABLE
            )

        assistant_message = AIMessage.objects.create(
            conversation=conversation,
            role='assistant',
            content=result['answer'] or '',
            sql_query=result.get('sql'),
            query_result=result.get('query_result'),
            chart_config=result.get('chart')
        )

        return Response({
            "conversation_id": conversation.id,
            "message_id": assistant_message.id,
            "answer": result['answer'],
            "sql": result.get('sql'),
            "query_result": result.get('query_result'),
            "chart": result.get('chart'),
        }, status=status.HTTP_200_OK)


class ConversationListView(APIView):
    def get_permissions(self):
        if self.request.method == 'GET':
            permission_classes = [IsAuthenticated]
        else:
            permission_classes = [IsAdminUser | IsDispatch | IsDispatchManager | IsBilling | IsPayroll]
        return [permission() for permission in permission_classes]

    def get(self, request):
        conversations = AIConversation.objects.filter(
            user=request.user,
            company=request.user.company
        ).order_by('-last_updated')

        serializer = AIConversationListSerializer(conversations, many=True)
        return Response(serializer.data, status=status.HTTP_200_OK)


class ConversationDetailView(APIView):
    def get_permissions(self):
        if self.request.method == 'GET':
            permission_classes = [IsAuthenticated]
        else:
            permission_classes = [IsAdminUser | IsDispatch | IsDispatchManager | IsBilling | IsPayroll]
        return [permission() for permission in permission_classes]

    def _get_conversation(self, request, pk):
        try:
            return AIConversation.objects.get(
                id=pk,
                user=request.user,
                company=request.user.company
            )
        except AIConversation.DoesNotExist:
            return None

    def get(self, request, pk):
        """
        GET /api/ai-analyst/conversations/<id>/

        Response:
        {
            "id": 12,
            "title": "...",
            "user_full_name": "John Doe",
            "created_at": "...",
            "last_updated": "...",
            "messages": [
                {
                    "id": 1,
                    "role": "user",
                    "content": "Give me top 5 dispatchers",
                    "sql_query": null,
                    "query_result": null,
                    "chart_config": null,
                    "created_at": "..."
                },
                {
                    "id": 2,
                    "role": "assistant",
                    "content": "Here are the top 5 dispatchers...",
                    "sql_query": "SELECT ...",
                    "query_result": [{...}],
                    "chart_config": {...},
                    "created_at": "..."
                }
            ]
        }
        """
        conversation = self._get_conversation(request, pk)
        if not conversation:
            return Response({"error": "Conversation not found."}, status=status.HTTP_404_NOT_FOUND)

        serializer = AIConversationSerializer(conversation)
        return Response(serializer.data, status=status.HTTP_200_OK)

    def delete(self, request, pk):
        conversation = self._get_conversation(request, pk)
        if not conversation:
            return Response({"error": "Conversation not found."}, status=status.HTTP_404_NOT_FOUND)

        conversation.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class ConversationMessagesView(APIView):
    def get_permissions(self):
        if self.request.method == 'GET':
            permission_classes = [IsAuthenticated]
        else:
            permission_classes = [IsAdminUser | IsDispatch | IsDispatchManager | IsBilling | IsPayroll]
        return [permission() for permission in permission_classes]

    def get(self, request, pk):
        try:
            conversation = AIConversation.objects.get(
                id=pk,
                user=request.user,
                company=request.user.company
            )
        except AIConversation.DoesNotExist:
            return Response({"error": "Conversation not found."}, status=status.HTTP_404_NOT_FOUND)

        messages = conversation.messages.order_by('created_at')
        from .serializers import AIMessageSerializer
        serializer = AIMessageSerializer(messages, many=True)
        return Response(serializer.data, status=status.HTTP_200_OK)


class ConversationDeleteView(APIView):
    permission_classes = [IsAuthenticated]

    def delete(self, request, pk):
        try:
            conversation = AIConversation.objects.get(
                id=pk,
                user=request.user,
                company=request.user.company
            )
        except AIConversation.DoesNotExist:
            return Response({"error": "Conversation not found."}, status=status.HTTP_404_NOT_FOUND)
        
        owner = conversation.user
        if request.user != owner:
            return Response({"error": "You don't have permission to delete this conversation."}, status=status.HTTP_403_FORBIDDEN)
        conversation.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)
