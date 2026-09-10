from django.urls import path

from .views import (
    CardDefaultView,
    CardDeleteView,
    CardsView,
    CardSetupStatusView,
    CardSetupView,
    RecurringView,
    SummaryView,
    TransactionsView,
    UpcomingView,
)

urlpatterns = [
    path("summary/", SummaryView.as_view(), name="charge-summary"),
    path("upcoming/", UpcomingView.as_view(), name="charge-upcoming"),
    path("transactions/", TransactionsView.as_view(), name="charge-transactions"),
    path("recurring/", RecurringView.as_view(), name="charge-recurring"),
    path("cards/", CardsView.as_view(), name="charge-cards"),
    path("cards/setup/", CardSetupView.as_view(), name="charge-card-setup"),
    path("cards/setup/<uuid:session_id>/", CardSetupStatusView.as_view(), name="charge-card-setup-status"),
    path("cards/<uuid:card_id>/default/", CardDefaultView.as_view(), name="charge-card-default"),
    path("cards/<uuid:card_id>/", CardDeleteView.as_view(), name="charge-card-delete"),
]
