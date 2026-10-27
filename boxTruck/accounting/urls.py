from django.urls import path

from .views import (
    ExpenseDetailView, ExpenseListView, ExpenseSummaryView,
    InvoiceDetailView, InvoiceListView, InvoiceNotesView, InvoiceSummaryView,
    LoadStatusView, PayoutDetailView, PayoutExpensesView, PayoutListView, PayoutNotesView, PayoutOpenExpensesView, PayoutSummaryView,
)

urlpatterns = [
    path('payouts/', PayoutListView.as_view(), name='accounting-payouts'),
    path('payouts/summary/', PayoutSummaryView.as_view(), name='accounting-payouts-summary'),
    path('payouts/<int:pk>/', PayoutDetailView.as_view(), name='accounting-payout'),
    path('payouts/<int:pk>/notes/', PayoutNotesView.as_view(), name='accounting-payout-notes'),
    path('payouts/<int:pk>/expenses/', PayoutExpensesView.as_view(), name='accounting-payout-expenses'),
    path('payouts/<int:pk>/expenses/<int:deduction_id>/', PayoutExpensesView.as_view(), name='accounting-payout-expense'),
    path('payouts/<int:pk>/open-expenses/', PayoutOpenExpensesView.as_view(), name='accounting-payout-open-expenses'),
    path('payouts/<int:pk>/load-status/', LoadStatusView.as_view(), name='accounting-payout-load-status'),
    path('invoices/', InvoiceListView.as_view(), name='accounting-invoices'),
    path('invoices/summary/', InvoiceSummaryView.as_view(), name='accounting-invoices-summary'),
    path('invoices/<int:pk>/', InvoiceDetailView.as_view(), name='accounting-invoice'),
    path('invoices/<int:pk>/notes/', InvoiceNotesView.as_view(), name='accounting-invoice-notes'),
    path('expenses/', ExpenseListView.as_view(), name='accounting-expenses'),
    path('expenses/summary/', ExpenseSummaryView.as_view(), name='accounting-expenses-summary'),
    path('expenses/<int:pk>/', ExpenseDetailView.as_view(), name='accounting-expense'),
]
