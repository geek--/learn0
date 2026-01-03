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
        date_range = f"{campaign.start_at:%d/%m/%Y} · {campaign.end_at:%d/%m/%Y}"
        campaign_items.append(
            f"""
            <a class="campaign-item {'active' if campaign.id == selected_campaign_id else ''}"
               href="?{escape(urlencode(query))}">
              <div class="campaign-icon">📣</div>
              <div>
                <div class="campaign-name">{escape(campaign.name)}</div>
                <div class="campaign-meta">{escape(date_range)}</div>
              </div>
            </a>
            """
        )

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
            for item in recipients[:6]
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
            background: #f6f1ef;
            color: #3b2f33;
          }}
          .shell {{
            min-height: 100vh;
            display: flex;
          }}
          .sidebar {{
            width: 80px;
            background: #fdf9f7;
            border-right: 1px solid #e7d7d5;
            padding: 18px 12px;
            display: flex;
            flex-direction: column;
            align-items: center;
            gap: 18px;
          }}
          .brand {{
            width: 44px;
            height: 44px;
            border-radius: 50%;
            border: 2px solid #b45b69;
            color: #b45b69;
            font-weight: 700;
            display: flex;
            align-items: center;
            justify-content: center;
          }}
          .nav {{
            display: grid;
            gap: 16px;
          }}
          .nav-item {{
            width: 38px;
            height: 38px;
            border-radius: 12px;
            border: 1px solid #e7d7d5;
            display: flex;
            align-items: center;
            justify-content: center;
            font-size: 18px;
            color: #9b5661;
            background: #ffffff;
          }}
          .nav-item.active {{
            background: #f3dee1;
            border-color: #b45b69;
          }}
          .content {{
            flex: 1;
            padding: 28px 32px 40px;
          }}
          .page-header {{
            display: flex;
            align-items: center;
            justify-content: space-between;
            gap: 20px;
            margin-bottom: 20px;
          }}
          .page-header h1 {{
            margin: 0;
            font-size: 26px;
            color: #7b3e4a;
          }}
          .page-header p {{
            margin: 6px 0 0;
            font-size: 13px;
            color: #7a666b;
          }}
          .header-actions {{
            display: flex;
            gap: 10px;
            align-items: center;
          }}
          .chip {{
            padding: 8px 14px;
            border-radius: 999px;
            background: #f3dee1;
            border: 1px solid #d9b0b7;
            color: #7b3e4a;
            font-weight: 600;
            font-size: 12px;
          }}
          .chip.ghost {{
            background: #fff;
            border-color: #ead4d8;
          }}
          .content-grid {{
            display: grid;
            grid-template-columns: 320px 1fr;
            gap: 20px;
          }}
          .panel {{
            background: #ffffff;
            border: 1px solid #ead4d8;
            border-radius: 18px;
            padding: 18px;
            box-shadow: 0 2px 6px rgba(125, 71, 82, 0.05);
          }}
          .campaigns-panel {{
            display: flex;
            flex-direction: column;
            gap: 14px;
          }}
          .panel-header {{
            font-size: 13px;
            font-weight: 600;
            color: #9b5661;
          }}
          .search-input {{
            display: flex;
            align-items: center;
            gap: 8px;
            padding: 10px 12px;
            border-radius: 12px;
            border: 1px solid #ead4d8;
            background: #fdf9f7;
          }}
          .search-input input {{
            border: none;
            outline: none;
            background: transparent;
            flex: 1;
            font-size: 13px;
            color: #6f565c;
          }}
          .search-btn {{
            border: none;
            background: transparent;
            font-size: 16px;
            color: #9b5661;
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
            padding: 10px 12px;
            border-radius: 14px;
            border: 1px solid transparent;
            text-decoration: none;
            color: inherit;
            background: #fcf7f6;
          }}
          .campaign-item.active {{
            border-color: #c77b89;
            background: #f4e3e6;
          }}
          .campaign-icon {{
            width: 36px;
            height: 36px;
            border-radius: 12px;
            background: #f3dee1;
            display: flex;
            align-items: center;
            justify-content: center;
            font-size: 18px;
          }}
          .campaign-name {{
            font-weight: 600;
            font-size: 14px;
          }}
          .campaign-meta {{
            font-size: 12px;
            color: #846a70;
          }}
          .detail-header {{
            display: flex;
            align-items: flex-start;
            justify-content: space-between;
            margin-bottom: 16px;
          }}
          .detail-title {{
            font-size: 18px;
            font-weight: 700;
            color: #7b3e4a;
          }}
          .detail-sub {{
            font-size: 13px;
            color: #7a666b;
          }}
          .detail-main {{
            display: grid;
            grid-template-columns: 2fr 1fr 1fr;
            gap: 18px;
            align-items: center;
            margin-bottom: 18px;
          }}
          .donut-card {{
            text-align: center;
            padding: 14px;
            border-radius: 16px;
            border: 1px solid #f0d5da;
            background: #fff7f8;
          }}
          .donut-title {{
            font-size: 12px;
            color: #9b5661;
            text-transform: uppercase;
            letter-spacing: 0.6px;
          }}
          .donut-number {{
            font-size: 22px;
            font-weight: 700;
            margin: 8px 0;
          }}
          .donut-label {{
            font-size: 12px;
            color: #7a666b;
            margin-top: 6px;
          }}
          .donut-canvas {{
            width: 160px;
            height: 160px;
            margin: 0 auto;
          }}
          .mini-donut {{
            text-align: center;
            padding: 12px;
            border-radius: 14px;
            border: 1px solid #f0d5da;
            background: #fff;
          }}
          .mini-donut canvas {{
            width: 80px;
            height: 80px;
          }}
          .mini-label {{
            font-size: 12px;
            color: #7a666b;
            margin-top: 6px;
          }}
          .detail-table h4 {{
            margin: 0 0 8px;
            font-size: 13px;
            color: #9b5661;
            text-transform: uppercase;
            letter-spacing: 0.4px;
          }}
          .detail-table table {{
            width: 100%;
            border-collapse: collapse;
            font-size: 13px;
          }}
          .detail-table th,
          .detail-table td {{
            text-align: left;
            padding: 10px 6px;
            border-bottom: 1px solid #f1dfe2;
          }}
          .detail-table th {{
            color: #9b5661;
            font-weight: 600;
            font-size: 12px;
          }}
          .muted {{
            color: #9a8086;
            font-size: 12px;
          }}
        </style>
      </head>
      <body>
        <div class="shell">
          <aside class="sidebar">
            <div class="brand">WT</div>
            <div class="nav">
              <div class="nav-item">⋯</div>
              <div class="nav-item active">📊</div>
              <div class="nav-item">✉️</div>
            </div>
            <div class="nav-item">⚙️</div>
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
                  <input type="text" name="q" value="{escape(search_term)}" placeholder="Buscar por nombre" />
                  <button class="search-btn" type="submit">🔍</button>
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
                    <div class="donut-label">{open_rate}% open email</div>
                  </div>
                  <div class="mini-donut">
                    <canvas id="ctaChart"></canvas>
                    <div class="mini-label">{cta_rate}% click CTA</div>
                  </div>
                  <div class="mini-donut">
                    <canvas id="submitChart"></canvas>
                    <div class="mini-label">{submit_rate}% submit data</div>
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
                datasets: [{{ data: [value, 100 - value], backgroundColor: [color, "#f2e4e7"] }}],
              }},
              options: {{ plugins: {{ legend: {{ display: false }} }}, cutout: "72%" }},
            }});
          }};
          buildDonut("openChart", {open_rate}, "#b45b69");
          buildDonut("ctaChart", {cta_rate}, "#d3929e");
          buildDonut("submitChart", {submit_rate}, "#9b5661");
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
