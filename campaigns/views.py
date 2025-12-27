from __future__ import annotations

from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_GET

from campaigns.models import CampaignRecipient, EmailEvent
from campaigns.tracking import (
    ClientSignals,
    hash_ip,
    infer_open_signal_quality,
    parse_user_agent,
    truncate_ip,
)

PIXEL_BYTES = (
    b"GIF89a\x01\x00\x01\x00\x80\x00\x00\x00\x00\x00\xff\xff\xff!"
    b"\xf9\x04\x01\x00\x00\x00\x00,\x00\x00\x00\x00\x01\x00\x01\x00"
    b"\x00\x02\x02D\x01\x00;"
)


def _get_client_ip(request) -> str | None:
    forwarded = request.META.get("HTTP_X_FORWARDED_FOR")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.META.get("REMOTE_ADDR")


def _event_metadata(request) -> dict[str, str]:
    return {
        "user_agent": request.META.get("HTTP_USER_AGENT", ""),
        "referer": request.META.get("HTTP_REFERER", ""),
    }


def _parse_timezone_offset(request) -> int | None:
    raw = request.GET.get("tz")
    if raw is None:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _build_event_payload(request, *, signal_quality: str = "") -> dict[str, object]:
    user_agent = request.META.get("HTTP_USER_AGENT", "")
    signals: ClientSignals = parse_user_agent(user_agent)
    client_ip = _get_client_ip(request)
    return {
        "ip_address_truncated": truncate_ip(client_ip),
        "ip_hash": hash_ip(client_ip),
        "user_agent": user_agent,
        "referer": request.META.get("HTTP_REFERER", ""),
        "device_type": signals.device_type,
        "os_family": signals.os_family,
        "browser_family": signals.browser_family,
        "email_client_hint": signals.email_client_hint,
        "message_provider_hint": signals.message_provider_hint,
        "is_webview": signals.is_webview,
        "language": request.META.get("HTTP_ACCEPT_LANGUAGE", ""),
        "timezone_offset_minutes": _parse_timezone_offset(request),
        "open_signal_quality": signal_quality,
        "metadata": _event_metadata(request),
    }


@require_GET
def track_open(request, token):
    campaign_recipient = get_object_or_404(CampaignRecipient, tracking_token=token)
    signal_quality = infer_open_signal_quality(request.META.get("HTTP_USER_AGENT", ""))
    EmailEvent.objects.create(
        recipient=campaign_recipient,
        event_type=EmailEvent.EventType.OPEN,
        **_build_event_payload(request, signal_quality=signal_quality),
    )
    now = timezone.now()
    updates = {
        "open_seen_at": now,
        "open_signal_quality": signal_quality,
    }
    if campaign_recipient.opened_at is None:
        updates["opened_at"] = now
    CampaignRecipient.objects.filter(pk=campaign_recipient.pk).update(**updates)
    response = HttpResponse(PIXEL_BYTES, content_type="image/gif")
    response["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    return response


@require_GET
def track_click(request, token):
    campaign_recipient = get_object_or_404(CampaignRecipient, tracking_token=token)
    EmailEvent.objects.create(
        recipient=campaign_recipient,
        event_type=EmailEvent.EventType.CLICK,
        **_build_event_payload(request),
    )
    now = timezone.now()
    updates = {"click_count": campaign_recipient.click_count + 1}
    if campaign_recipient.clicked_at is None:
        updates["clicked_at"] = now
        if campaign_recipient.sent_at:
            updates["time_to_click_seconds"] = int((now - campaign_recipient.sent_at).total_seconds())
    CampaignRecipient.objects.filter(pk=campaign_recipient.pk).update(**updates)
    landing_path = reverse(
        "campaigns:landing",
        kwargs={"landing_slug": campaign_recipient.campaign.landing_slug},
    )
    return redirect(landing_path)


@require_GET
def track_cta(request, token):
    campaign_recipient = get_object_or_404(CampaignRecipient, tracking_token=token)
    EmailEvent.objects.create(
        recipient=campaign_recipient,
        event_type=EmailEvent.EventType.CTA_CLICK,
        **_build_event_payload(request),
    )
    now = timezone.now()
    if campaign_recipient.cta_clicked_at is None:
        CampaignRecipient.objects.filter(pk=campaign_recipient.pk).update(cta_clicked_at=now)
    landing_path = reverse(
        "campaigns:landing",
        kwargs={"landing_slug": campaign_recipient.campaign.landing_slug},
    )
    return redirect(landing_path)


@require_GET
def track_submit_attempt(request, token):
    campaign_recipient = get_object_or_404(CampaignRecipient, tracking_token=token)
    EmailEvent.objects.create(
        recipient=campaign_recipient,
        event_type=EmailEvent.EventType.SUBMIT_ATTEMPT,
        **_build_event_payload(request),
    )
    now = timezone.now()
    updates = {"submit_attempted": True}
    if campaign_recipient.submit_attempt_at is None:
        updates["submit_attempt_at"] = now
    CampaignRecipient.objects.filter(pk=campaign_recipient.pk).update(**updates)
    return JsonResponse({"status": "ok"})


@require_GET
def track_report(request, token):
    campaign_recipient = get_object_or_404(CampaignRecipient, tracking_token=token)
    channel = request.GET.get("channel", "other")
    payload = _build_event_payload(request)
    payload["metadata"] = {**payload.get("metadata", {}), "report_channel": channel}
    EmailEvent.objects.create(
        recipient=campaign_recipient,
        event_type=EmailEvent.EventType.REPORT,
        **payload,
    )
    now = timezone.now()
    updates = {"report_channel": channel}
    if campaign_recipient.reported_at is None:
        updates["reported_at"] = now
        if campaign_recipient.sent_at:
            updates["time_to_report_seconds"] = int((now - campaign_recipient.sent_at).total_seconds())
    CampaignRecipient.objects.filter(pk=campaign_recipient.pk).update(**updates)
    return JsonResponse({"status": "ok"})


@require_GET
def landing(request, landing_slug):
    token = request.GET.get("t")
    if token:
        campaign_recipient = get_object_or_404(CampaignRecipient, tracking_token=token)
        EmailEvent.objects.create(
            recipient=campaign_recipient,
            event_type=EmailEvent.EventType.LANDING_VIEW,
            **_build_event_payload(request),
        )
        now = timezone.now()
        updates = {"landing_view_count": campaign_recipient.landing_view_count + 1}
        if campaign_recipient.landing_viewed_at is None:
            updates["landing_viewed_at"] = now
        CampaignRecipient.objects.filter(pk=campaign_recipient.pk).update(**updates)
    return HttpResponse(f"Gracias por visitar la campaña {landing_slug}.", content_type="text/plain")
