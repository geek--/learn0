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
            background: #0c0d10;
            color: #e2e8f0;
          }}
          .shell {{
            min-height: 100vh;
            display: flex;
            border: 1px solid #2a2a2a;
            margin: 12px;
          }}
          .sidebar {{
            width: 56px;
            background: #0f0f10;
            border-right: 1px solid #2a2a2a;
            padding: 16px 10px;
            display: flex;
            flex-direction: column;
            align-items: center;
            gap: 16px;
          }}
          .brand {{
            width: 28px;
            height: 28px;
            border-radius: 50%;
            border: 1px solid #f8fafc;
            color: #f8fafc;
            font-weight: 700;
            display: flex;
            align-items: center;
            justify-content: center;
            font-size: 12px;
          }}
          .nav {{
            display: grid;
            gap: 12px;
          }}
          .nav-item {{
            width: 26px;
            height: 26px;
            border-radius: 50%;
            border: 1px solid #f8fafc;
            display: flex;
            align-items: center;
            justify-content: center;
            font-size: 16px;
            color: #f8fafc;
            background: transparent;
          }}
          .nav-item.active {{
            border-color: #2563eb;
          }}
          .nav-bottom {{
            margin-top: auto;
            display: grid;
            gap: 12px;
          }}
          .content {{
            flex: 1;
            padding: 22px 28px 32px;
          }}
          .page-header {{
            display: flex;
            align-items: center;
            justify-content: space-between;
            gap: 20px;
            margin-bottom: 16px;
          }}
          .page-header h1 {{
            margin: 0;
            font-size: 18px;
            color: #f8fafc;
            letter-spacing: 0.2px;
          }}
          .page-header p {{
            margin: 6px 0 0;
            font-size: 11px;
            color: #9ca3af;
          }}
          .header-actions {{
            display: flex;
            gap: 10px;
            align-items: center;
          }}
          .chip {{
            padding: 6px 12px;
            border-radius: 999px;
            background: #0f0f10;
            border: 1px solid #2a2a2a;
            color: #f8fafc;
            font-weight: 600;
            font-size: 11px;
          }}
          .chip.ghost {{
            background: transparent;
            border-color: #2a2a2a;
            color: #e5e7eb;
          }}
          .content-grid {{
            display: grid;
            grid-template-columns: 280px 1fr;
            gap: 18px;
          }}
          .panel {{
            background: #111214;
            border: 1px solid #2a2a2a;
            border-radius: 14px;
            padding: 16px;
            box-shadow: inset 0 0 0 1px rgba(255, 255, 255, 0.03);
          }}
          .campaigns-panel {{
            display: flex;
            flex-direction: column;
            gap: 14px;
          }}
          .panel-header {{
            font-size: 12px;
            font-weight: 600;
            color: #f8fafc;
          }}
          .search-input {{
            display: flex;
            align-items: center;
            gap: 8px;
            padding: 8px 10px;
            border-radius: 10px;
            border: 1px solid #2a2a2a;
            background: #0b0b0c;
          }}
          .search-input input {{
            border: none;
            outline: none;
            background: transparent;
            flex: 1;
            font-size: 12px;
            color: #f8fafc;
          }}
          .search-btn {{
            border: none;
            background: transparent;
            font-size: 16px;
            color: #f8fafc;
            cursor: pointer;
          }}
          .campaign-list {{
            display: flex;
            flex-direction: column;
            gap: 12px;
          }}
          .campaign-item {{
            display: flex;
            gap: 12px;
            padding: 10px;
            border-radius: 12px;
            border: 1px solid transparent;
            text-decoration: none;
            color: inherit;
            background: #0b0b0c;
          }}
          .campaign-item.active {{
            border-color: #2563eb;
            background: #12151b;
          }}
          .campaign-icon {{
            width: 32px;
            height: 32px;
            border-radius: 10px;
            background: #111318;
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
            color: #9ca3af;
          }}
          .detail-header {{
            display: flex;
            align-items: flex-start;
            justify-content: space-between;
            margin-bottom: 16px;
          }}
          .detail-title {{
            font-size: 16px;
            font-weight: 700;
            color: #f8fafc;
          }}
          .detail-sub {{
            font-size: 12px;
            color: #9ca3af;
          }}
          .detail-main {{
            display: grid;
            grid-template-columns: 2.2fr 1fr;
            gap: 16px;
            align-items: center;
            margin-bottom: 18px;
          }}
          .donut-card {{
            text-align: center;
            padding: 14px;
            border-radius: 16px;
            border: 1px solid #2a2a2a;
            background: #0b0b0c;
          }}
          .donut-title {{
            font-size: 11px;
            color: #e5e7eb;
            text-transform: uppercase;
            letter-spacing: 0.6px;
          }}
          .donut-number {{
            font-size: 20px;
            font-weight: 700;
            margin: 8px 0;
          }}
          .donut-label {{
            font-size: 11px;
            color: #9ca3af;
            margin-top: 6px;
          }}
          .donut-canvas {{
            width: 150px;
            height: 150px;
            margin: 0 auto;
          }}
          .mini-metrics {{
            display: grid;
            grid-template-columns: repeat(2, minmax(0, 1fr));
            gap: 12px;
          }}
          .mini-donut {{
            text-align: center;
            padding: 10px;
            border-radius: 12px;
            border: 1px solid #2a2a2a;
            background: #0b0b0c;
            text-decoration: none;
            color: inherit;
            transition: border-color 0.2s ease, transform 0.2s ease;
          }}
          .mini-donut.active {{
            border-color: #2563eb;
            transform: translateY(-1px);
          }}
          .mini-donut canvas {{
            width: 68px;
            height: 68px;
          }}
          .mini-label {{
            font-size: 11px;
            color: #f8fafc;
            margin-top: 4px;
            display: block;
          }}
          .mini-value {{
            font-size: 10px;
            color: #9ca3af;
          }}
          .detail-table h4 {{
            margin: 0 0 8px;
            font-size: 12px;
            color: #f8fafc;
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
            border-bottom: 1px solid #2a2a2a;
          }}
          .detail-table th {{
            color: #f8fafc;
            font-weight: 600;
            font-size: 10px;
            text-transform: uppercase;
            letter-spacing: 0.4px;
          }}
          .muted {{
            color: #9ca3af;
            font-size: 11px;
          }}
          .icon {{
            width: 18px;
            height: 18px;
            stroke: #f8fafc;
            stroke-width: 1.7;
            fill: none;
          }}
          .icon-muted {{
            stroke: #f8fafc;
          }}
          .nav-item.active .icon-muted {{
            stroke: #2563eb;
          }}
        </style>
      </head>
      <body>
        <div class="shell">
          <aside class="sidebar">
            <div class="brand">WT</div>
            <div class="nav">
              <div class="nav-item">
                <svg class="icon" viewBox="0 0 24 24" aria-hidden="true">
                  <circle cx="5" cy="12" r="1.5"></circle>
                  <circle cx="12" cy="12" r="1.5"></circle>
                  <circle cx="19" cy="12" r="1.5"></circle>
                </svg>
              </div>
              <div class="nav-item active">
                <svg class="icon icon-muted" viewBox="0 0 24 24" aria-hidden="true">
                  <rect x="4" y="4" width="7" height="7" rx="1.5"></rect>
                  <rect x="13" y="4" width="7" height="7" rx="1.5"></rect>
                  <rect x="4" y="13" width="7" height="7" rx="1.5"></rect>
                  <rect x="13" y="13" width="7" height="7" rx="1.5"></rect>
                </svg>
              </div>
              <div class="nav-item">
                <svg class="icon" viewBox="0 0 24 24" aria-hidden="true">
                  <path d="M4 6h16v12H4z"></path>
                  <path d="M4 7l8 6 8-6"></path>
                </svg>
              </div>
            </div>
            <div class="nav-bottom">
              <div class="nav-item">
                <svg class="icon" viewBox="0 0 24 24" aria-hidden="true">
                  <circle cx="12" cy="12" r="3.5"></circle>
                  <path d="M19 12l2-1-2-1-1-2 1-2-2-1-1-2-2 1-2-1-2 1-2-1-2 1 1 2-1 2 1 2-1 2 2 1 1 2 2-1 2 1 2-1 2 1 1-2 2-1-1-2 1-2z"></path>
                </svg>
              </div>
            </div>
          </aside>
          <main class="content">
            <header class="page-header">
              <div>
                <h1>Campañas de phishing</h1>
                <p>Administración de campañas y métricas clave en tiempo real.</p>
              </div>
              <div class="header-actions">
                <div class="chip">Resumen</div>
                <div class="chip ghost">{escape(selected_campaign_name)}</div>
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
                    <div class="detail-sub">Total enviados: {totals["sent"]}</div>
                  </div>
                  <div class="chip ghost">Por usuario</div>
                </div>
                <div class="detail-main">
                  <div class="donut-card">
                    <div class="donut-title">Total enviados</div>
                    <div class="donut-number">{totals["sent"]}</div>
                    <canvas id="openChart" class="donut-canvas"></canvas>
                    <div class="donut-label">{open_rate}% abrieron correo</div>
                  </div>
                  <div class="mini-metrics">
                    <a class="mini-donut {'active' if selected_metric == 'cta' else ''}"
                       href="?{escape(urlencode({'campaign': selected_campaign_id or '', 'q': search_term, 'metric': 'cta'}))}">
                      <canvas id="ctaChart"></canvas>
                      <span class="mini-label">{cta_rate}% Click CTA</span>
                      <span class="mini-value">{totals["cta"]} usuarios</span>
                    </a>
                    <a class="mini-donut {'active' if selected_metric == 'submit' else ''}"
                       href="?{escape(urlencode({'campaign': selected_campaign_id or '', 'q': search_term, 'metric': 'submit'}))}">
                      <canvas id="submitChart"></canvas>
                      <span class="mini-label">{submit_rate}% Submit data</span>
                      <span class="mini-value">{totals["submit"]} usuarios</span>
                    </a>
                    <a class="mini-donut {'active' if selected_metric == 'reported' else ''}"
                       href="?{escape(urlencode({'campaign': selected_campaign_id or '', 'q': search_term, 'metric': 'reported'}))}">
                      <canvas id="reportChart"></canvas>
                      <span class="mini-label">{report_rate}% Report</span>
                      <span class="mini-value">{totals["reported"]} usuarios</span>
                    </a>
                    <a class="mini-donut {'active' if selected_metric == 'opened' else ''}"
                       href="?{escape(urlencode({'campaign': selected_campaign_id or '', 'q': search_term, 'metric': 'opened'}))}">
                      <canvas id="openedChart"></canvas>
                      <span class="mini-label">{open_rate}% Open email</span>
                      <span class="mini-value">{totals["opened"]} usuarios</span>
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
              </section>
            </div>
          </main>
        </div>
        <script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js"></script>
        <script>
          const buildDonut = (canvasId, value, color) => {{
            const ctx = document.getElementById(canvasId);
            if (!ctx) return;
            new Chart(ctx, {{
              type: "doughnut",
              data: {{
                labels: ["Valor", "Restante"],
                datasets: [{{ data: [value, 100 - value], backgroundColor: [color, "#1f2937"] }}],
              }},
              options: {{ plugins: {{ legend: {{ display: false }} }}, cutout: "72%" }},
            }});
          }};
          buildDonut("openChart", {open_rate}, "#2563eb");
          buildDonut("ctaChart", {cta_rate}, "#2563eb");
          buildDonut("submitChart", {submit_rate}, "#2563eb");
          buildDonut("reportChart", {report_rate}, "#2563eb");
          buildDonut("openedChart", {open_rate}, "#2563eb");
        </script>
      </body>
    </html>
    """
    if not body_v3.strip():
        body_v3 = "<html><body><p>Dashboard v3</p></body></html>"
    body_v1 = body_v3
    body_v2 = body_v3
    body = body_v3
    return HttpResponse(body_v3, content_type="text/html")
