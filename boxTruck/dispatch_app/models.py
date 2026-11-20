import uuid

from django.conf import settings
from django.db import models
from django.db.models import Q


class DispatchDevice(models.Model):
    """One phone linked to one BTM user through the BTM Dispatch hub.

    The hub holds the device token (only its SHA-256 is kept here) and trades
    it for a 10 minute access token whose `dsid` claim is this row's id. A
    user has at most one live (not revoked) device: linking a new phone
    revokes the old one at once.
    """

    REPLACED = "replaced"
    UNLINKED = "unlinked"
    HUB = "hub"
    USER_DELETED = "user_deleted"
    REASONS = [
        (REPLACED, "Linked on another phone"),
        (UNLINKED, "Unlinked from the web"),
        (HUB, "Removed in the app or by the hub"),
        (USER_DELETED, "User deleted"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    # Kept after the user is deleted, revoked with reason user_deleted.
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="dispatch_devices",
    )
    token_hash = models.CharField(max_length=64, unique=True)
    hub_workspace_id = models.CharField(max_length=64, blank=True, default="")
    platform = models.CharField(max_length=16, blank=True, default="")
    device_label = models.CharField(max_length=120, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    last_seen_at = models.DateTimeField(null=True, blank=True)
    revoked_at = models.DateTimeField(null=True, blank=True)
    revoked_reason = models.CharField(
        max_length=16, blank=True, default="", choices=REASONS
    )

    class Meta:
        db_table = "dispatch_devices"
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["user"],
                condition=Q(revoked_at__isnull=True),
                name="dispatch_one_live_device_per_user",
            )
        ]

    def __str__(self) -> str:
        state = "live" if self.is_live else f"revoked ({self.revoked_reason})"
        return f"{self.device_label or self.platform or 'phone'} of user {self.user_id}, {state}"

    @property
    def is_live(self) -> bool:
        return self.revoked_at is None and self.user_id is not None
