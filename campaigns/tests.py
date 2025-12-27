from django.test import TestCase
from django.utils import timezone

from accounts.models import User
from campaigns.models import Campaign, CampaignRecipient, Recipient


class CampaignModelTests(TestCase):
    def setUp(self) -> None:
        self.user = User.objects.create_user(username="owner", password="pass")

    def test_campaign_recipient_unique(self) -> None:
        campaign = Campaign.objects.create(
            name="Test",
            description="",
            start_at=timezone.now(),
            end_at=timezone.now(),
            email_template="Hello",
            landing_slug="test",
            throttle_per_minute=30,
            created_by=self.user,
        )
        recipient = Recipient.objects.create(email="test@example.com")

        CampaignRecipient.objects.create(campaign=campaign, recipient=recipient)

        with self.assertRaises(Exception):
            CampaignRecipient.objects.create(campaign=campaign, recipient=recipient)
