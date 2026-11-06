from django.conf import settings
from django.db import models
from django.utils import timezone


class DriverBidDecision(models.Model):
    """What a dispatcher decided about one driver-app bid, kept here.

    The hub tells the driver; this is our own record, so the Load board's Bid
    history stops listing a declined bid as waiting on the dispatcher. Kept by
    the loads-service bid id: a new bid from the same driver on the same load
    is a new id, and so not declined.
    """

    DECLINED = "declined"
    STATUSES = [(DECLINED, "Declined")]

    bid_id = models.CharField(max_length=80, unique=True)
    load_id = models.CharField(max_length=80, db_index=True)
    driver = models.ForeignKey(
        "hiring.Driver",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="bid_decisions",
    )
    status = models.CharField(max_length=20, choices=STATUSES, default=DECLINED)
    note = models.TextField(blank=True, default="")
    # Null when the decision came from a service without a user.
    decided_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    decided_at = models.DateTimeField(default=timezone.now)

    def __str__(self) -> str:
        return f"{self.bid_id} {self.status}"

    class Meta:
        db_table = "driver_bid_decisions"
        ordering = ["-decided_at", "-id"]
        verbose_name = "Driver Bid Decision"
        verbose_name_plural = "Driver Bid Decisions"
