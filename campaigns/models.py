from django.conf import settings
from django.db import models


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
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ("campaign", "recipient")

    def __str__(self) -> str:
        return f"{self.campaign} -> {self.recipient}"
