import uuid

from django.conf import settings
from django.db import models
from django.utils import timezone


class Campaign(models.Model):
    name = models.CharField(max_length=255)
    description = models.TextField(blank=True)
    start_at = models.DateTimeField()
    end_at = models.DateTimeField()
    email_template = models.TextField()
    landing_slug = models.SlugField(max_length=120)
    throttle_per_minute = models.PositiveIntegerField(default=60)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="created_campaigns",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self) -> str:
        return self.name


class Recipient(models.Model):
    email = models.EmailField(unique=True)
    full_name = models.CharField(max_length=255, blank=True)
    department = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self) -> str:
        return self.email


class CampaignRecipient(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        SENT = "sent", "Sent"
        BOUNCED = "bounced", "Bounced"

    campaign = models.ForeignKey(Campaign, on_delete=models.CASCADE, related_name="recipients")
    recipient = models.ForeignKey(Recipient, on_delete=models.CASCADE, related_name="campaigns")
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING)
    tracking_token = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    opened_at = models.DateTimeField(null=True, blank=True)
    clicked_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ("campaign", "recipient")

    def __str__(self) -> str:
        return f"{self.campaign} -> {self.recipient}"


class EmailEvent(models.Model):
    class EventType(models.TextChoices):
        OPEN = "open", "Open"
        CLICK = "click", "Click"

    recipient = models.ForeignKey(CampaignRecipient, on_delete=models.CASCADE, related_name="events")
    event_type = models.CharField(max_length=16, choices=EventType.choices)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.TextField(blank=True)
    referer = models.TextField(blank=True)
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(default=timezone.now)

    def __str__(self) -> str:
        return f"{self.event_type} - {self.recipient}"
