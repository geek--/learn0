from __future__ import annotations

from typing import Any

from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.urls import reverse
from django.utils.html import strip_tags

from campaigns.models import CampaignRecipient


def _build_base_url() -> str:
    base = settings.SITE_BASE_URL.rstrip("/")
    return base


def build_tracking_urls(campaign_recipient: CampaignRecipient) -> dict[str, str]:
    base_url = _build_base_url()
    return {
        "tracking_pixel": f"{base_url}{reverse('campaigns:track-open', kwargs={'token': campaign_recipient.tracking_token})}",
        "click_url": f"{base_url}{reverse('campaigns:track-click', kwargs={'token': campaign_recipient.tracking_token})}",
        "cta_url": f"{base_url}{reverse('campaigns:track-cta', kwargs={'token': campaign_recipient.tracking_token})}",
        "submit_url": f"{base_url}{reverse('campaigns:track-submit', kwargs={'token': campaign_recipient.tracking_token})}",
        "report_url": f"{base_url}{reverse('campaigns:track-report', kwargs={'token': campaign_recipient.tracking_token})}",
        "landing_url": (
            f"{base_url}{reverse('campaigns:landing', kwargs={'landing_slug': campaign_recipient.campaign.landing_slug})}"
            f"?t={campaign_recipient.tracking_token}"
        ),
    }


def render_email_template(template: str, context: dict[str, Any]) -> str:
    rendered = template or ""
    if "{{ tracking_pixel }}" not in rendered:
        rendered = f"{rendered}<br>{context.get('tracking_pixel', '')}"
    for key, value in context.items():
        rendered = rendered.replace(f"{{{{ {key} }}}}", str(value))
    return rendered


def send_campaign_email(campaign_recipient: CampaignRecipient) -> None:
    campaign = campaign_recipient.campaign
    recipient = campaign_recipient.recipient
    urls = build_tracking_urls(campaign_recipient)
    context = {
        "tracking_pixel": f'<img src="{urls["tracking_pixel"]}" width="1" height="1" style="display:none;" alt="" />',
        "click_url": urls["click_url"],
        "landing_url": urls["landing_url"],
        "cta_url": urls["cta_url"],
        "submit_url": urls["submit_url"],
        "report_url": urls["report_url"],
        "recipient_email": recipient.email,
        "recipient_name": recipient.full_name or recipient.email,
        "campaign_name": campaign.name,
    }
    html_body = render_email_template(campaign.email_template, context)
    text_body = strip_tags(html_body)

    message = EmailMultiAlternatives(
        subject=campaign.name,
        body=text_body,
        from_email=settings.DEFAULT_FROM_EMAIL,
        to=[recipient.email],
    )
    message.attach_alternative(html_body, "text/html")
    message.send(fail_silently=False)
