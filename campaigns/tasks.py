from __future__ import annotations

from celery import shared_task
from django.db import transaction
from django.utils import timezone

from auditing.models import AuditLog
from campaigns.models import Campaign, CampaignRecipient
from campaigns.services import send_campaign_email


@shared_task
def process_campaigns() -> int:
    now = timezone.now()
    processed = 0
    campaigns = Campaign.objects.filter(start_at__lte=now, end_at__gte=now)
    for campaign in campaigns:
        pending = list(
            campaign.recipients.select_related("recipient").filter(
                status=CampaignRecipient.Status.PENDING
            )[: campaign.throttle_per_minute]
        )
        for campaign_recipient in pending:
            try:
                with transaction.atomic():
                    send_campaign_email(campaign_recipient)
                    campaign_recipient.status = CampaignRecipient.Status.SENT
                    campaign_recipient.sent_at = timezone.now()
                    campaign_recipient.save(update_fields=["status", "sent_at"])
                    AuditLog.objects.create(
                        action="campaign_email_sent",
                        actor=campaign.created_by,
                        metadata={
                            "campaign_id": campaign.id,
                            "recipient_id": campaign_recipient.recipient_id,
                            "campaign_recipient_id": campaign_recipient.id,
                        },
                    )
                    processed += 1
            except Exception as exc:  # noqa: BLE001 - log and continue
                campaign_recipient.status = CampaignRecipient.Status.BOUNCED
                campaign_recipient.save(update_fields=["status"])
                AuditLog.objects.create(
                    action="campaign_email_failed",
                    actor=campaign.created_by,
                    metadata={
                        "campaign_id": campaign.id,
                        "recipient_id": campaign_recipient.recipient_id,
                        "campaign_recipient_id": campaign_recipient.id,
                        "error": str(exc),
                    },
                )
    return processed
