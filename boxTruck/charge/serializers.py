from rest_framework import serializers


class CardSetupRequestSerializer(serializers.Serializer):
    """Body for POST /api/charge/cards/setup/.

    return_url is where Stripe sends the user back after entering the card. Its host must be in
    the billing project's allow-list (localhost is always allowed for dev). Defaults to
    FRONTEND_URL + /billing/cards.
    """

    return_url = serializers.URLField(required=False)
    client_reference = serializers.CharField(required=False, allow_blank=True, max_length=255)


class TransactionQuerySerializer(serializers.Serializer):
    type = serializers.ChoiceField(
        choices=["charge", "refund", "dispute", "dispute_reversal"], required=False
    )
    period = serializers.RegexField(r"^\d{4}-\d{2}$", required=False)
    limit = serializers.IntegerField(min_value=1, max_value=200, required=False, default=50)
    offset = serializers.IntegerField(min_value=0, required=False, default=0)
