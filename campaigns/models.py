import uuid

from django.conf import settings
from django.core.exceptions import ValidationError
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
        FAILED = "failed", "Failed"

    campaign = models.ForeignKey(Campaign, on_delete=models.CASCADE, related_name="recipients")
    recipient = models.ForeignKey(Recipient, on_delete=models.CASCADE, related_name="campaigns")
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING)
    tracking_token = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    sent_at = models.DateTimeField(null=True, blank=True)
    failed_at = models.DateTimeField(null=True, blank=True)
    fail_reason = models.TextField(blank=True)
    opened_at = models.DateTimeField(null=True, blank=True)
    open_seen_at = models.DateTimeField(null=True, blank=True)
    open_signal_quality = models.CharField(max_length=16, blank=True)
    clicked_at = models.DateTimeField(null=True, blank=True)
    click_count = models.PositiveIntegerField(default=0)
    landing_viewed_at = models.DateTimeField(null=True, blank=True)
    landing_view_count = models.PositiveIntegerField(default=0)
    cta_clicked_at = models.DateTimeField(null=True, blank=True)
    cta_click_count = models.PositiveIntegerField(default=0)
    submit_attempt_at = models.DateTimeField(null=True, blank=True)
    submit_attempted = models.BooleanField(default=False)
    reported_at = models.DateTimeField(null=True, blank=True)
    report_channel = models.CharField(max_length=32, blank=True)
    time_to_click_seconds = models.PositiveIntegerField(null=True, blank=True)
    time_to_report_seconds = models.PositiveIntegerField(null=True, blank=True)
    data_repaired = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ("campaign", "recipient")

    def __str__(self) -> str:
        return f"{self.campaign} -> {self.recipient}"

    def clean(self) -> None:
        if self.status == self.Status.SENT and self.sent_at is None:
            raise ValidationError("sent_at is required when status is SENT.")


class EmailEvent(models.Model):
    class EventType(models.TextChoices):
        OPEN = "open", "Open"
        CLICK = "click", "Click"
        LANDING_VIEW = "landing_view", "Landing view"
        CTA_CLICK = "cta_click", "CTA click"
        SUBMIT_ATTEMPT = "submit_attempt", "Submit attempt"
        REPORT = "report", "Report"

    recipient = models.ForeignKey(CampaignRecipient, on_delete=models.CASCADE, related_name="events")
    event_type = models.CharField(max_length=16, choices=EventType.choices)
    ip_address_truncated = models.CharField(max_length=64, blank=True)
    ip_hash = models.CharField(max_length=128, blank=True)
    user_agent = models.TextField(blank=True)
    referer = models.TextField(blank=True)
    device_type = models.CharField(max_length=16, blank=True)
    os_family = models.CharField(max_length=32, blank=True)
    browser_family = models.CharField(max_length=32, blank=True)
    email_client_hint = models.CharField(max_length=32, blank=True)
    message_provider_hint = models.CharField(max_length=32, blank=True)
    is_webview = models.BooleanField(default=False)
    language = models.CharField(max_length=64, blank=True)
    timezone_offset_minutes = models.IntegerField(null=True, blank=True)
    open_signal_quality = models.CharField(max_length=16, blank=True)
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(default=timezone.now)

    def __str__(self) -> str:
        return f"{self.event_type} - {self.recipient}"
