from django.db.models.signals import m2m_changed
from django.dispatch import receiver

from campaigns.models import Campaign, CampaignRecipient, Recipient


@receiver(m2m_changed, sender=Campaign.recipient_tags.through)
def sync_campaign_recipients(sender, instance, action, **kwargs):
    if action not in {"post_add", "post_remove", "post_clear"}:
        return

    tag_ids = instance.recipient_tags.values_list("id", flat=True)
    recipients = Recipient.objects.filter(tags__in=tag_ids).distinct()
    recipient_ids = list(recipients.values_list("id", flat=True))

    existing = CampaignRecipient.objects.filter(campaign=instance)
    existing.exclude(recipient_id__in=recipient_ids).delete()

    to_create = [
        CampaignRecipient(campaign=instance, recipient_id=recipient_id)
        for recipient_id in recipient_ids
    ]
    CampaignRecipient.objects.bulk_create(to_create, ignore_conflicts=True)
