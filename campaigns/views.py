from __future__ import annotations

import json
from urllib.parse import urlencode

from django.db import models
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.utils import timezone
from django.utils.html import escape
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST

from campaigns.models import Campaign, CampaignRecipient, EmailEvent
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
    updates = {"cta_click_count": campaign_recipient.cta_click_count + 1}
    if campaign_recipient.cta_clicked_at is None:
        updates["cta_clicked_at"] = now
    CampaignRecipient.objects.filter(pk=campaign_recipient.pk).update(**updates)
    landing_path = reverse(
        "campaigns:landing",
        kwargs={"landing_slug": campaign_recipient.campaign.landing_slug},
    )
    return redirect(landing_path)


@csrf_exempt
@require_POST
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
    accept_header = request.headers.get("Accept", "")
    if "application/json" in accept_header:
        return JsonResponse({"status": "ok", "message": "Gracias"})
    body = """
    <html>
      <body>
        <p>Gracias por tu confirmación.</p>
      </body>
    </html>
    """
    return HttpResponse(body, content_type="text/html")


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
    tracking_pixel = ""
    submit_form = ""
    if token:
        tracking_url = reverse("campaigns:track-landing", kwargs={"token": token})
        tracking_pixel = f'<img src="{tracking_url}" alt="." width="1" height="1" style="opacity:0; position:absolute; left:-9999px; top:-9999px;" />'
        submit_url = reverse("campaigns:track-submit", kwargs={"token": token})
        submit_form = f"""
        <form method="post" action="{submit_url}">
          <button type="submit">Confirmar</button>
        </form>
        """
    body = f"""
    <html>
      <body>
        <p>Gracias por visitar la campaña {landing_slug}.</p>
        {submit_form}
        {tracking_pixel}
      </body>
    </html>
    """
    if not body.strip():
        body = "<html><body><p>Dashboard v3</p></body></html>"
    body_v1 = body
    body_v2 = body
    body_v3 = body
    return HttpResponse(body, content_type="text/html")


@require_GET
def track_landing_view(request, token):
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
    response = HttpResponse(PIXEL_BYTES, content_type="image/gif")
    response["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    return response


def _criticality_label(recipient: CampaignRecipient) -> str:
    if recipient.reported_at:
        return "Crítica"
    if recipient.submit_attempted:
        return "Alta"
    if recipient.cta_click_count or recipient.landing_view_count:
        return "Media"
    if recipient.opened_at or recipient.open_seen_at:
        return "Baja"
    return "Sin señales"


def _criticality_badge_class(label: str) -> str:
    return {
        "Crítica": "badge-critical",
        "Alta": "badge-high",
        "Media": "badge-medium",
        "Baja": "badge-low",
        "Sin señales": "badge-none",
    }.get(label, "badge-none")


def _build_flow_steps(recipient: CampaignRecipient) -> list[tuple[str, bool, object]]:
    return [
        ("Enviado", recipient.sent_at is not None, recipient.sent_at),
        ("Abrió", recipient.opened_at is not None or recipient.open_seen_at is not None, recipient.opened_at),
        ("Landing", recipient.landing_view_count > 0, recipient.landing_viewed_at),
        ("CTA", recipient.cta_click_count > 0, recipient.cta_clicked_at),
        ("Intento", recipient.submit_attempted, recipient.submit_attempt_at),
        ("Reportó", recipient.reported_at is not None, recipient.reported_at),
    ]


@require_GET
def dashboard(request):
    body = ""
    campaigns = Campaign.objects.order_by("-start_at")
    selected_campaign = request.GET.get("campaign")
    selected_department = request.GET.get("department", "")
    selected_status = request.GET.get("status", "")
    selected_criticality = request.GET.get("criticality", "")
    selected_metric = request.GET.get("metric", "")
    search_term = request.GET.get("q", "").strip()

    recipients = CampaignRecipient.objects.select_related("campaign", "recipient").order_by("-created_at")
    if selected_campaign:
        recipients = recipients.filter(campaign_id=selected_campaign)
    if selected_department:
        recipients = recipients.filter(recipient__department=selected_department)
    if selected_status:
        recipients = recipients.filter(status=selected_status)
    if search_term:
        recipients = recipients.filter(
            models.Q(recipient__email__icontains=search_term)
            | models.Q(recipient__full_name__icontains=search_term)
        )

    all_departments = (
        CampaignRecipient.objects.select_related("recipient")
        .exclude(recipient__department="")
        .values_list("recipient__department", flat=True)
        .distinct()
        .order_by("recipient__department")
    )

    rows = []
    totals = {
        "count": 0,
        "sent": 0,
        "opened": 0,
        "landing": 0,
        "cta": 0,
        "submit": 0,
        "reported": 0,
        "bounced": 0,
    }
    criticality_counts = {
        "Crítica": 0,
        "Alta": 0,
        "Media": 0,
        "Baja": 0,
        "Sin señales": 0,
    }
    for item in recipients:
        criticality = _criticality_label(item)
        if selected_criticality and selected_criticality != criticality:
            continue
        totals["count"] += 1
        criticality_counts[criticality] += 1
        if item.sent_at or item.status == CampaignRecipient.Status.SENT:
            totals["sent"] += 1
        if item.opened_at or item.open_seen_at:
            totals["opened"] += 1
        if item.landing_view_count:
            totals["landing"] += 1
        if item.cta_click_count:
            totals["cta"] += 1
        if item.submit_attempted:
            totals["submit"] += 1
        if item.reported_at:
            totals["reported"] += 1
        if item.status == CampaignRecipient.Status.BOUNCED:
            totals["bounced"] += 1
        flow = []
        for step, is_active, timestamp in _build_flow_steps(item):
            time_label = timestamp.strftime("%d/%m %H:%M") if timestamp else "--"
            flow.append(
                f"""
                <div class="flow-step {'active' if is_active else ''}">
                  <span>{escape(step)}</span>
                  <small>{escape(time_label)}</small>
                </div>
                """
            )
        criticality_class = {
            "Crítica": "critical",
            "Alta": "high",
            "Media": "medium",
            "Baja": "low",
            "Sin señales": "none",
        }.get(criticality, "none")
        rows.append(
            f"""
            <div class="flow-row {criticality_class}">
              <div class="flow-header">
                <div>
                  <h3>{escape(item.recipient.full_name or item.recipient.email)}</h3>
                  <p>{escape(item.recipient.email)} · {escape(item.recipient.department or 'Sin área')}</p>
                </div>
                <div class="flow-meta">
                  <span class="status-pill">{escape(item.get_status_display())}</span>
                  <span class="badge {escape(_criticality_badge_class(criticality))}">{escape(criticality)}</span>
                </div>
              </div>
              <div class="flow-steps">
                {''.join(flow)}
              </div>
              <div class="flow-footer">
                <div>Campaña: <strong>{escape(item.campaign.name)}</strong></div>
                <div>Landing: {item.landing_view_count} · CTA: {item.cta_click_count} · Reportes: {1 if item.reported_at else 0}</div>
              </div>
            </div>
            """
        )

    total_count = totals["count"] or 1
    open_rate = int((totals["opened"] / total_count) * 100)
    cta_rate = int((totals["cta"] / total_count) * 100)
    submit_rate = int((totals["submit"] / total_count) * 100)
    report_rate = int((totals["reported"] / total_count) * 100)

    def _circumference(radius: int) -> float:
        return 2 * 3.141592653589793 * radius

    def _dash_offset(rate: int, radius: int) -> float:
        return _circumference(radius) * (1 - rate / 100)

    big_radius = 46
    small_radius = 18
    big_circ = _circumference(big_radius)
    small_circ = _circumference(small_radius)
    big_open_offset = _dash_offset(open_rate, big_radius)
    small_cta_offset = _dash_offset(cta_rate, small_radius)
    small_submit_offset = _dash_offset(submit_rate, small_radius)
    small_report_offset = _dash_offset(report_rate, small_radius)
    def _format_datetime(value):
        return value.strftime("%d/%m/%Y, %H:%M") if value else "--"

    selected_campaign_obj = None
    if selected_campaign:
        selected_campaign_obj = campaigns.filter(id=selected_campaign).first()
    if selected_campaign_obj is None:
        selected_campaign_obj = campaigns.first()
    selected_campaign_name = (
        selected_campaign_obj.name if selected_campaign_obj else "Campaña sin seleccionar"
    )
    selected_campaign_id = selected_campaign_obj.id if selected_campaign_obj else None

    campaign_items = []
    for campaign in campaigns:
        query = {"campaign": campaign.id}
        if search_term:
            query["q"] = search_term
        if selected_metric:
            query["metric"] = selected_metric
        date_range = f"{campaign.start_at:%d/%m/%Y} · {campaign.end_at:%d/%m/%Y}"
        campaign_items.append(
            f"""
            <a class="campaign-item {'active' if campaign.id == selected_campaign_id else ''}"
               href="?{escape(urlencode(query))}">
              <div class="campaign-icon">
                <svg class="icon icon-muted" viewBox="0 0 24 24" aria-hidden="true">
                  <path d="M4 10v4"></path>
                  <path d="M7 9v6"></path>
                  <path d="M10 7v10"></path>
                  <path d="M14 6l6 3v6l-6 3z"></path>
                </svg>
              </div>
              <div>
                <div class="campaign-name">{escape(campaign.name)}</div>
                <div class="campaign-meta">{escape(date_range)}</div>
              </div>
            </a>
            """
        )

    metric_filters = {
        "opened": models.Q(opened_at__isnull=False) | models.Q(open_seen_at__isnull=False),
        "cta": models.Q(cta_click_count__gt=0),
        "submit": models.Q(submit_attempted=True),
        "reported": models.Q(reported_at__isnull=False),
    }
    recipients_for_table = recipients
    if selected_metric in metric_filters:
        recipients_for_table = recipients.filter(metric_filters[selected_metric])

    recipient_rows = "".join(
        [
            f"""
            <tr>
              <td>{escape(item.recipient.full_name or item.recipient.email)}</td>
              <td>{escape(item.recipient.department or 'Sin área')}</td>
              <td>{escape(_format_datetime(item.created_at))}</td>
              <td>Email</td>
              <td>{escape(item.get_status_display())}</td>
            </tr>
            """
            for item in recipients_for_table[:6]
        ]
    )

    campaign_stats = (
        recipients.values("campaign_id", "campaign__name")
        .annotate(
            count=models.Count("id"),
            sent=models.Count(
                "id",
                filter=models.Q(sent_at__isnull=False) | models.Q(status=CampaignRecipient.Status.SENT),
            ),
            opened=models.Count(
                "id",
                filter=models.Q(opened_at__isnull=False) | models.Q(open_seen_at__isnull=False),
            ),
            landing=models.Count("id", filter=models.Q(landing_view_count__gt=0)),
            cta=models.Count("id", filter=models.Q(cta_click_count__gt=0)),
            reported=models.Count("id", filter=models.Q(reported_at__isnull=False)),
            bounced=models.Count("id", filter=models.Q(status=CampaignRecipient.Status.BOUNCED)),
        )
        .order_by("-count", "campaign__name")
    )

    body_v3 = f"""
    <html lang="es">
      <head>
        <meta charset="utf-8" />
        <title>Administración de campañas</title>
        <style>
          body {{
            margin: 0;
            font-family: "Inter", "Segoe UI", sans-serif;
            background: #f0f2f5;
            color: #1c1e21;
          }}
          .shell {{
            min-height: 100vh;
            display: flex;
            border: 1px solid #dfe3ee;
            margin: 12px 14px;
            background: #f0f2f5;
          }}
          .sidebar {{
            width: 64px;
            background: #ffffff;
            border-right: 1px solid #dfe3ee;
            padding: 16px 10px;
            display: flex;
            flex-direction: column;
            align-items: center;
            gap: 18px;
            position: fixed;
            top: 12px;
            bottom: 12px;
            left: 14px;
            z-index: 2;
            border-radius: 14px;
            box-shadow: 0 6px 16px rgba(16, 24, 40, 0.08);
          }}
          .brand {{
            width: 24px;
            height: 24px;
            border-radius: 50%;
            border: 2px solid #1877f2;
            color: #1877f2;
            font-weight: 700;
            display: flex;
            align-items: center;
            justify-content: center;
            font-size: 12px;
          }}
          .nav {{
            display: grid;
            gap: 14px;
          }}
          .nav-item {{
            width: 28px;
            height: 28px;
            border-radius: 50%;
            border: 1px solid #dfe3ee;
            display: flex;
            align-items: center;
            justify-content: center;
            font-size: 16px;
            color: #1c1e21;
            background: #ffffff;
          }}
          .nav-item.active {{
            border-color: #1877f2;
            background: #e7f3ff;
          }}
          .nav-bottom {{
            margin-top: auto;
            display: grid;
            gap: 12px;
          }}
          .nav-dot {{
            width: 16px;
            height: 16px;
            border-radius: 50%;
            display: inline-block;
            border: 1px solid #dfe3ee;
            background: #ffffff;
          }}
          .nav-dot.blue {{
            background: #e7f3ff;
            border-color: #1877f2;
          }}
          .nav-dot.yellow {{
            background: #fff4cc;
            border-color: #f7b928;
          }}
          .nav-dot.orange {{
            background: #ffe0b2;
            border-color: #f37f2e;
          }}
          .nav-dot.sky {{
            background: #dbeafe;
            border-color: #60a5fa;
          }}
          .content {{
            flex: 1;
            padding: 22px 26px 30px;
            margin-left: 84px;
          }}
          .page-header {{
            display: flex;
            align-items: center;
            justify-content: space-between;
            gap: 20px;
            margin-bottom: 14px;
          }}
          .page-header h1 {{
            margin: 0;
            font-size: 20px;
            color: #1c1e21;
            letter-spacing: 0.2px;
          }}
          .page-header p {{
            margin: 6px 0 0;
            font-size: 12px;
            color: #606770;
          }}
          .content-grid {{
            display: grid;
            grid-template-columns: 270px 1fr;
            gap: 14px;
          }}
          .panel {{
            background: #ffffff;
            border: 1px solid #dfe3ee;
            border-radius: 12px;
            padding: 16px;
            box-shadow: 0 1px 2px rgba(0, 0, 0, 0.05);
          }}
          .detail-panel {{
            padding: 0;
            display: flex;
            flex-direction: column;
            max-height: calc(100vh - 160px);
          }}
          .campaigns-panel {{
            display: flex;
            flex-direction: column;
            gap: 14px;
          }}
          .panel-header {{
            font-size: 12px;
            font-weight: 600;
            color: #1c1e21;
          }}
          .search-input {{
            display: flex;
            align-items: center;
            gap: 8px;
            padding: 8px 10px;
            border-radius: 10px;
            border: 1px solid #dfe3ee;
            background: #f5f6f7;
          }}
          .search-input input {{
            border: none;
            outline: none;
            background: transparent;
            flex: 1;
            font-size: 12px;
            color: #1c1e21;
          }}
          .search-btn {{
            border: none;
            background: transparent;
            font-size: 16px;
            color: #1c1e21;
            cursor: pointer;
          }}
          .campaign-list {{
            display: flex;
            flex-direction: column;
            gap: 12px;
          }}
          .campaign-item {{
            position: relative;
            display: flex;
            gap: 12px;
            padding: 10px;
            border-radius: 12px;
            border: 1px solid #dfe3ee;
            text-decoration: none;
            color: inherit;
            background: #ffffff;
          }}
          .campaign-item.active {{
            border-color: #1877f2;
            background: #e7f3ff;
          }}
          .campaign-item.active::before {{
            content: "";
            position: absolute;
            left: -2px;
            top: 10px;
            bottom: 10px;
            width: 6px;
            border-radius: 6px;
            background: #1877f2;
          }}
          .campaign-icon {{
            width: 32px;
            height: 32px;
            border-radius: 8px;
            border: 1px solid #dfe3ee;
            background: #f5f6f7;
            display: flex;
            align-items: center;
            justify-content: center;
            font-size: 18px;
          }}
          .campaign-name {{
            font-weight: 600;
            font-size: 12px;
          }}
          .campaign-meta {{
            font-size: 11px;
            color: #8d949e;
          }}
          .detail-header {{
            display: flex;
            align-items: flex-start;
            justify-content: space-between;
            margin-bottom: 16px;
            padding: 16px 16px 0;
          }}
          .detail-title {{
            font-size: 16px;
            font-weight: 700;
            color: #1c1e21;
          }}
          .detail-sub {{
            font-size: 12px;
            color: #606770;
          }}
          .detail-meta {{
            display: flex;
            align-items: center;
            gap: 10px;
            font-size: 12px;
            color: #606770;
          }}
          .detail-pill {{
            background: #e7f3ff;
            color: #1877f2;
            border-radius: 999px;
            padding: 4px 10px;
            font-weight: 600;
            font-size: 11px;
          }}
          .detail-tabs {{
            display: flex;
            gap: 16px;
            align-items: center;
            border-bottom: 1px solid #dfe3ee;
            padding-bottom: 6px;
            margin: 10px 0 12px;
            font-size: 11px;
            font-weight: 600;
            color: #1c1e21;
            padding: 8px 16px 12px;
            margin: 0;
            background: #ffffff;
            position: sticky;
            top: 0;
            z-index: 1;
          }}
          .detail-body {{
            padding: 0 16px 16px;
            overflow: auto;
          }}
          .detail-tabs .tab {{
            padding: 2px 12px;
            border-radius: 4px;
            border: 2px solid transparent;
          }}
          .detail-tabs .tab.active {{
            background: #e7f3ff;
            border-color: #1877f2;
          }}
          .detail-main {{
            display: grid;
            grid-template-columns: 1.4fr 1fr;
            gap: 16px;
            align-items: center;
            margin-bottom: 18px;
          }}
          .summary-card {{
            display: flex;
            align-items: center;
            gap: 18px;
            padding: 18px;
            border-radius: 16px;
            border: 1px solid #dfe3ee;
            background: #ffffff;
          }}
          .summary-meta {{
            display: grid;
            gap: 6px;
          }}
          .summary-rate {{
            font-size: 22px;
            font-weight: 700;
            color: #1c1e21;
          }}
          .summary-label {{
            font-size: 12px;
            color: #1c1e21;
          }}
          .summary-sub {{
            font-size: 12px;
            color: #606770;
          }}
          .ring-chart {{
            width: 140px;
            height: 140px;
          }}
          .ring-chart svg {{
            width: 100%;
            height: 100%;
          }}
          .ring-bg {{
            stroke: #1c1e21;
            stroke-width: 6;
            fill: none;
          }}
          .ring-inner {{
            stroke: #1c1e21;
            stroke-width: 3;
            fill: none;
          }}
          .ring-progress {{
            stroke: #1877f2;
            stroke-width: 10;
            fill: none;
            stroke-linecap: round;
          }}
          .mini-metrics {{
            display: grid;
            gap: 14px;
          }}
          .mini-donut {{
            display: flex;
            align-items: center;
            gap: 14px;
            padding: 10px 12px;
            border-radius: 12px;
            border: 1px solid #dfe3ee;
            background: #ffffff;
            text-decoration: none;
            color: inherit;
            transition: border-color 0.2s ease, transform 0.2s ease;
          }}
          .mini-donut.active {{
            border-color: #1877f2;
            transform: translateY(-1px);
          }}
          .mini-ring {{
            width: 54px;
            height: 54px;
          }}
          .mini-ring svg {{
            width: 100%;
            height: 100%;
          }}
          .mini-title {{
            font-size: 12px;
            color: #1c1e21;
            font-weight: 600;
          }}
          .mini-value {{
            font-size: 12px;
            color: #1c1e21;
            font-weight: 700;
            margin-right: 6px;
          }}
          .mini-sub {{
            font-size: 11px;
            color: #606770;
          }}
          .ring-progress.cta {{
            stroke: #ff5c8d;
          }}
          .ring-progress.submit {{
            stroke: #f7b928;
          }}
          .ring-progress.report {{
            stroke: #31a24c;
          }}
          .detail-table h4 {{
            margin: 0 0 8px;
            font-size: 12px;
            color: #1c1e21;
            text-transform: uppercase;
            letter-spacing: 0.4px;
          }}
          .detail-table table {{
            width: 100%;
            border-collapse: collapse;
            font-size: 11px;
          }}
          .detail-table th,
          .detail-table td {{
            text-align: left;
            padding: 8px 6px;
            border-bottom: 1px solid #dfe3ee;
          }}
          .detail-table th {{
            color: #1c1e21;
            font-weight: 600;
            font-size: 10px;
            text-transform: uppercase;
            letter-spacing: 0.4px;
          }}
          .muted {{
            color: #8d949e;
            font-size: 11px;
          }}
          .icon {{
            width: 18px;
            height: 18px;
            stroke: #1c1e21;
            stroke-width: 1.7;
            fill: none;
          }}
          .icon-muted {{
            stroke: #1c1e21;
          }}
          .nav-item.active .icon-muted {{
            stroke: #1877f2;
          }}
        </style>
      </head>
      <body>
        <div class="shell">
          <aside class="sidebar">
            <div class="nav">
              <div class="nav-item">
                <span class="nav-dot"></span>
              </div>
              <div class="nav-item active">
                <span class="nav-dot blue"></span>
              </div>
              <div class="nav-item">
                <span class="nav-dot yellow"></span>
              </div>
            </div>
            <div class="nav-bottom">
              <div class="nav-item">
                <span class="nav-dot orange"></span>
              </div>
              <div class="nav-item">
                <span class="nav-dot sky"></span>
              </div>
            </div>
          </aside>
          <main class="content">
            <header class="page-header">
              <div>
                <h1>Campañas de phishing</h1>
                <p>Administración de campañas y métricas clave en tiempo real.</p>
              </div>
            </header>
            <div class="content-grid">
              <section class="panel campaigns-panel">
                <div class="panel-header">Busca por nombre</div>
                <form class="search-input" method="get">
                  <input type="hidden" name="campaign" value="{selected_campaign_id or ''}" />
                  <input type="hidden" name="metric" value="{escape(selected_metric)}" />
                  <input type="text" name="q" value="{escape(search_term)}" placeholder="Buscar por nombre" />
                  <button class="search-btn" type="submit" aria-label="Buscar">
                    <svg class="icon icon-muted" viewBox="0 0 24 24" aria-hidden="true">
                      <circle cx="11" cy="11" r="6"></circle>
                      <path d="M20 20l-3.5-3.5"></path>
                    </svg>
                  </button>
                </form>
                <div class="campaign-list">
                  {"".join(campaign_items) if campaign_items else '<div class="muted">Sin campañas disponibles.</div>'}
                </div>
              </section>
              <section class="panel detail-panel">
                <div class="detail-header">
                  <div>
                    <div class="detail-title">{escape(selected_campaign_name)}</div>
                    <div class="detail-sub">Campañas en ejecución · Última actualización en tiempo real.</div>
                  </div>
                  <div class="detail-meta">
                    <span>Total enviados</span>
                    <span class="detail-pill">{totals["sent"]}</span>
                  </div>
                </div>
                <div class="detail-tabs">
                  <span class="tab active">Resumen</span>
                  <span class="tab">Por Usuario</span>
                  <span class="tab">Item2</span>
                  <span class="tab">Item2</span>
                  <span class="tab">Item3</span>
                </div>
                <div class="detail-body">
                  <div class="detail-main">
                    <div class="summary-card">
                      <div class="ring-chart" aria-hidden="true">
                        <svg viewBox="0 0 120 120">
                          <circle class="ring-bg" cx="60" cy="60" r="52"></circle>
                          <circle class="ring-inner" cx="60" cy="60" r="34"></circle>
                          <circle class="ring-progress"
                                  cx="60"
                                  cy="60"
                                  r="{big_radius}"
                                  stroke-dasharray="{big_circ:.2f}"
                                  stroke-dashoffset="{big_open_offset:.2f}"></circle>
                        </svg>
                      </div>
                      <div class="summary-meta">
                        <div class="summary-rate">{open_rate}%</div>
                        <div class="summary-label">Abrieron el correo</div>
                        <div class="summary-sub">Índice de apertura sobre enviados.</div>
                      </div>
                    </div>
                    <div class="mini-metrics">
                      <a class="mini-donut {'active' if selected_metric == 'cta' else ''}"
                         href="?{escape(urlencode({'campaign': selected_campaign_id or '', 'q': search_term, 'metric': 'cta'}))}">
                        <div class="mini-ring" aria-hidden="true">
                          <svg viewBox="0 0 60 60">
                            <circle class="ring-bg" cx="30" cy="30" r="24"></circle>
                            <circle class="ring-inner" cx="30" cy="30" r="16"></circle>
                            <circle class="ring-progress cta"
                                    cx="30"
                                    cy="30"
                                    r="{small_radius}"
                                    stroke-dasharray="{small_circ:.2f}"
                                    stroke-dashoffset="{small_cta_offset:.2f}"></circle>
                          </svg>
                        </div>
                        <div>
                          <span class="mini-title"><span class="mini-value">{cta_rate}%</span>Click CTA</span>
                          <div class="mini-sub">{totals["cta"]} usuarios</div>
                        </div>
                      </a>
                      <a class="mini-donut {'active' if selected_metric == 'submit' else ''}"
                         href="?{escape(urlencode({'campaign': selected_campaign_id or '', 'q': search_term, 'metric': 'submit'}))}">
                        <div class="mini-ring" aria-hidden="true">
                          <svg viewBox="0 0 60 60">
                            <circle class="ring-bg" cx="30" cy="30" r="24"></circle>
                            <circle class="ring-inner" cx="30" cy="30" r="16"></circle>
                            <circle class="ring-progress submit"
                                    cx="30"
                                    cy="30"
                                    r="{small_radius}"
                                    stroke-dasharray="{small_circ:.2f}"
                                    stroke-dashoffset="{small_submit_offset:.2f}"></circle>
                          </svg>
                        </div>
                        <div>
                          <span class="mini-title"><span class="mini-value">{submit_rate}%</span>Submited Data</span>
                          <div class="mini-sub">{totals["submit"]} usuarios</div>
                        </div>
                      </a>
                      <a class="mini-donut {'active' if selected_metric == 'reported' else ''}"
                         href="?{escape(urlencode({'campaign': selected_campaign_id or '', 'q': search_term, 'metric': 'reported'}))}">
                        <div class="mini-ring" aria-hidden="true">
                          <svg viewBox="0 0 60 60">
                            <circle class="ring-bg" cx="30" cy="30" r="24"></circle>
                            <circle class="ring-inner" cx="30" cy="30" r="16"></circle>
                            <circle class="ring-progress report"
                                    cx="30"
                                    cy="30"
                                    r="{small_radius}"
                                    stroke-dasharray="{small_circ:.2f}"
                                    stroke-dashoffset="{small_report_offset:.2f}"></circle>
                          </svg>
                        </div>
                        <div>
                          <span class="mini-title"><span class="mini-value">{report_rate}%</span>Report</span>
                          <div class="mini-sub">{totals["reported"]} usuarios</div>
                        </div>
                      </a>
                    </div>
                  </div>
                  <div class="detail-table">
                    <h4>Detalle por usuario</h4>
                    <table>
                      <thead>
                        <tr>
                          <th>Nombre</th>
                          <th>Área</th>
                          <th>Fecha</th>
                          <th>Dispositivo</th>
                          <th>Estado</th>
                        </tr>
                      </thead>
                      <tbody>
                        {recipient_rows if recipient_rows else '<tr><td colspan="5" class="muted">Sin registros para mostrar.</td></tr>'}
                      </tbody>
                    </table>
                  </div>
                </div>
              </section>
            </div>
          </main>
        </div>
      </body>
    </html>
    """
    if not body_v3.strip():
        body_v3 = "<html><body><p>Dashboard v3</p></body></html>"
    body_v1 = body_v3
    body_v2 = body_v3
    body = body_v3
    return HttpResponse(body_v3, content_type="text/html")


@require_GET
def dashboard_v2(request):
    body_v2 = """
    <html lang="es">
      <head>
        <meta charset="utf-8" />
        <title>Dashboard v2</title>
        <style>
          :root {
            color-scheme: light;
          }
          * {
            box-sizing: border-box;
          }
          body {
            margin: 0;
            font-family: "Inter", "Segoe UI", sans-serif;
            background: #f6f7fb;
            color: #1f2937;
          }
          .shell {
            min-height: 100vh;
            display: flex;
            gap: 0;
          }
          .sidebar {
            position: fixed;
            top: 0;
            bottom: 0;
            left: 0;
            width: 82px;
            background: #ffffff;
            border-right: 1px solid #e5e7eb;
            display: flex;
            flex-direction: column;
            padding: 18px 14px;
            gap: 18px;
            transition: width 0.2s ease;
            z-index: 2;
            overflow: hidden;
          }
          .sidebar.expanded {
            width: 230px;
          }
          .brand {
            display: flex;
            align-items: center;
            gap: 10px;
            font-weight: 700;
            color: #2563eb;
            font-size: 16px;
            width: 100%;
            justify-content: center;
          }
          .sidebar.expanded .brand {
            justify-content: flex-start;
          }
          .brand-icon {
            width: 34px;
            height: 34px;
            border-radius: 50%;
            border: 2px solid #2563eb;
            display: grid;
            place-items: center;
            font-size: 16px;
          }
          .footer-item {
            display: flex;
            align-items: center;
            gap: 10px;
            padding: 8px 10px;
            border-radius: 999px;
            border: 1px solid #e5e7eb;
            background: #ffffff;
            color: #6b7280;
            font-size: 12px;
            font-weight: 600;
            cursor: pointer;
            text-decoration: none;
          }
          .footer-item svg {
            width: 18px;
            height: 18px;
            stroke: currentColor;
            fill: none;
            stroke-width: 1.8;
          }
          .sidebar:not(.expanded) .footer-item {
            justify-content: center;
            padding: 8px 0;
            width: 32px;
            height: 32px;
          }
          .nav {
            display: flex;
            flex-direction: column;
            gap: 10px;
          }
          .nav-item {
            display: flex;
            align-items: center;
            gap: 12px;
            border-radius: 14px;
            padding: 10px 12px;
            color: #1f2937;
            text-decoration: none;
            font-size: 13px;
            font-weight: 600;
            border: 1px solid transparent;
          }
          .sidebar:not(.expanded) .nav-item {
            justify-content: center;
            padding: 10px 0;
          }
          .nav-item svg {
            width: 20px;
            height: 20px;
            stroke: currentColor;
            fill: none;
            stroke-width: 1.8;
          }
          .nav-item.active {
            background: #eaf1ff;
            border-color: #c7dcff;
            color: #1d4ed8;
          }
          .nav-label {
            white-space: nowrap;
            opacity: 0;
            transform: translateX(-6px);
            transition: opacity 0.2s ease, transform 0.2s ease;
          }
          .sidebar:not(.expanded) .nav-label,
          .sidebar:not(.expanded) .brand span {
            display: none;
          }
          .sidebar.expanded .nav-label,
          .sidebar.expanded .brand span {
            opacity: 1;
            transform: translateX(0);
          }
          .brand span {
            opacity: 0;
            transform: translateX(-6px);
            transition: opacity 0.2s ease, transform 0.2s ease;
          }
          .nav-footer {
            margin-top: auto;
            display: flex;
            flex-direction: column;
            gap: 12px;
          }
          .sidebar:not(.expanded) .nav-footer {
            align-items: center;
          }
          .footer-label {
            white-space: nowrap;
          }
          .sidebar:not(.expanded) .footer-label {
            display: none;
          }
          .main {
            flex: 1;
            margin-left: 82px;
            display: flex;
            flex-direction: column;
            min-height: 100vh;
          }
          .sidebar.expanded ~ .main {
            margin-left: 230px;
          }
          .header {
            position: sticky;
            top: 0;
            z-index: 1;
            background: #f6f7fb;
            padding: 18px 28px 12px;
            border-bottom: 1px solid #e5e7eb;
          }
          .header-row {
            display: flex;
            align-items: center;
            justify-content: space-between;
            gap: 18px;
          }
          .header-title {
            display: flex;
            align-items: center;
            gap: 12px;
          }
          .header h1 {
            margin: 0;
            font-size: 20px;
            font-weight: 700;
          }
          .header p {
            margin: 4px 0 0;
            font-size: 12px;
            color: #6b7280;
          }
          .header-actions {
            display: flex;
            align-items: center;
            gap: 10px;
            font-size: 12px;
            color: #4b5563;
          }
          .pill {
            display: inline-flex;
            align-items: center;
            gap: 6px;
            padding: 6px 12px;
            border-radius: 999px;
            background: #ffffff;
            border: 1px solid #e5e7eb;
            font-weight: 600;
            color: #111827;
          }
          .content {
            flex: 1;
            padding: 18px 28px 32px;
          }
          .canvas {
            background: #ffffff;
            border: 1px solid #e5e7eb;
            border-radius: 18px;
            min-height: 70vh;
            box-shadow: 0 10px 25px rgba(15, 23, 42, 0.06);
            position: relative;
            overflow: hidden;
          }
          .canvas::before {
            content: "";
            position: absolute;
            left: 0;
            top: 0;
            bottom: 0;
            width: 22%;
            background: radial-gradient(circle at 20% 20%, #eaf1ff 0, transparent 55%),
              radial-gradient(circle at 20% 80%, #e8f3ff 0, transparent 60%);
            opacity: 0.9;
          }
          .canvas-inner {
            position: relative;
            z-index: 1;
            padding: 28px;
          }
          .muted {
            color: #94a3b8;
            font-size: 13px;
          }
        </style>
      </head>
      <body>
        <div class="shell">
          <aside class="sidebar expanded" id="sidebar">
            <div class="brand">
              <div class="brand-icon">∞</div>
              <span>Security</span>
            </div>
            <nav class="nav">
              <a class="nav-item active" data-title="Campañas" href="#">
                <svg viewBox="0 0 24 24" aria-hidden="true">
                  <path d="M4 13h4v7H4z"></path>
                  <path d="M10 9h4v11h-4z"></path>
                  <path d="M16 5h4v15h-4z"></path>
                </svg>
                <span class="nav-label">Campañas</span>
              </a>
              <a class="nav-item" data-title="Resultados" href="#">
                <svg viewBox="0 0 24 24" aria-hidden="true">
                  <path d="M4 4h16v16H4z"></path>
                  <path d="M8 8h8v8H8z"></path>
                </svg>
                <span class="nav-label">Resultados</span>
              </a>
              <a class="nav-item" data-title="Contactos" href="#">
                <svg viewBox="0 0 24 24" aria-hidden="true">
                  <path d="M16 11a4 4 0 1 1-8 0 4 4 0 0 1 8 0z"></path>
                  <path d="M4 20a8 8 0 0 1 16 0"></path>
                </svg>
                <span class="nav-label">Contactos</span>
              </a>
              <a class="nav-item" data-title="Reportes" href="#">
                <svg viewBox="0 0 24 24" aria-hidden="true">
                  <path d="M5 4h10l4 4v12H5z"></path>
                  <path d="M9 14h6"></path>
                  <path d="M9 10h6"></path>
                </svg>
                <span class="nav-label">Reportes</span>
              </a>
            </nav>
            <div class="nav-footer">
              <button class="footer-item" id="toggle" aria-label="Expandir o contraer menú" type="button">
                <svg viewBox="0 0 24 24" aria-hidden="true" class="toggle-icon">
                  <path d="M9 6l6 6-6 6"></path>
                </svg>
                <span class="footer-label">Contraer</span>
              </button>
              <a class="footer-item" href="#">
                <svg viewBox="0 0 24 24" aria-hidden="true">
                  <path d="M12 3v2"></path>
                  <path d="M12 19v2"></path>
                  <path d="M4.9 4.9l1.4 1.4"></path>
                  <path d="M17.7 17.7l1.4 1.4"></path>
                  <path d="M3 12h2"></path>
                  <path d="M19 12h2"></path>
                  <path d="M4.9 19.1l1.4-1.4"></path>
                  <path d="M17.7 6.3l1.4-1.4"></path>
                  <circle cx="12" cy="12" r="3"></circle>
                </svg>
                <span class="footer-label">Configuración</span>
              </a>
              <a class="footer-item" href="#">
                <svg viewBox="0 0 24 24" aria-hidden="true">
                  <path d="M9 6v-1a2 2 0 0 1 2-2h6"></path>
                  <path d="M15 6h4v12h-4"></path>
                  <path d="M10 12h9"></path>
                  <path d="M7 9l-3 3 3 3"></path>
                </svg>
                <span class="footer-label">Cerrar sesión</span>
              </a>
            </div>
          </aside>

          <main class="main">
            <header class="header">
              <div class="header-row">
                <div class="header-title">
                  <div>
                    <h1 id="headerTitle">Campañas</h1>
                    <p>Review performance results and more.</p>
                  </div>
                </div>
                <div class="header-actions">
                  <span class="pill">● Título</span>
                  <span>Last 28 days: 27 Jul 2024 - 23 Aug 2024</span>
                </div>
              </div>
            </header>

            <section class="content">
              <div class="canvas">
                <div class="canvas-inner">
                  <div class="muted">Contenido flexible</div>
                </div>
              </div>
            </section>
          </main>
        </div>
        <script>
          const sidebar = document.getElementById("sidebar");
          const toggle = document.getElementById("toggle");
          const headerTitle = document.getElementById("headerTitle");
          const items = document.querySelectorAll(".nav-item");
          const toggleIcon = toggle.querySelector(".toggle-icon");
          const toggleLabel = toggle.querySelector(".footer-label");

          const setToggleState = (isExpanded) => {
            toggleIcon.innerHTML = isExpanded
              ? '<path d="M15 6l-6 6 6 6"></path>'
              : '<path d="M9 6l6 6-6 6"></path>';
            toggleLabel.textContent = isExpanded ? "Contraer" : "Expandir";
          };

          setToggleState(true);

          toggle.addEventListener("click", () => {
            const isExpanded = sidebar.classList.toggle("expanded");
            setToggleState(isExpanded);
          });

          items.forEach((item) => {
            item.addEventListener("click", (event) => {
              event.preventDefault();
              items.forEach((node) => node.classList.remove("active"));
              item.classList.add("active");
              headerTitle.textContent = item.dataset.title || "Campañas";
            });
          });
        </script>
      </body>
    </html>
    """
    return HttpResponse(body_v2, content_type="text/html")
