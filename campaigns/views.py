from __future__ import annotations

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
    if recipient.cta_click_count or recipient.click_count:
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
        ("Clic", recipient.click_count > 0, recipient.clicked_at),
        ("Landing", recipient.landing_view_count > 0, recipient.landing_viewed_at),
        ("CTA", recipient.cta_click_count > 0, recipient.cta_clicked_at),
        ("Intento", recipient.submit_attempted, recipient.submit_attempt_at),
        ("Reportó", recipient.reported_at is not None, recipient.reported_at),
    ]


@require_GET
def dashboard(request):
    campaigns = Campaign.objects.order_by("-start_at")
    selected_campaign = request.GET.get("campaign")
    selected_department = request.GET.get("department", "")
    selected_status = request.GET.get("status", "")
    selected_criticality = request.GET.get("criticality", "")

    recipients = CampaignRecipient.objects.select_related("campaign", "recipient").order_by("-created_at")
    if selected_campaign:
        recipients = recipients.filter(campaign_id=selected_campaign)
    if selected_department:
        recipients = recipients.filter(recipient__department=selected_department)
    if selected_status:
        recipients = recipients.filter(status=selected_status)

    all_departments = (
        CampaignRecipient.objects.select_related("recipient")
        .exclude(recipient__department="")
        .values_list("recipient__department", flat=True)
        .distinct()
        .order_by("recipient__department")
    )

    rows = []
    totals = {"count": 0, "opened": 0, "clicked": 0, "reported": 0}
    for item in recipients:
        criticality = _criticality_label(item)
        if selected_criticality and selected_criticality != criticality:
            continue
        totals["count"] += 1
        if item.opened_at or item.open_seen_at:
            totals["opened"] += 1
        if item.click_count:
            totals["clicked"] += 1
        if item.reported_at:
            totals["reported"] += 1
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
        rows.append(
            f"""
            <div class="flow-row">
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
                <div>Clicks: {item.click_count} · CTA: {item.cta_click_count} · Landing: {item.landing_view_count}</div>
              </div>
            </div>
            """
        )

    total_count = totals["count"] or 1
    open_rate = int((totals["opened"] / total_count) * 100)
    click_rate = int((totals["clicked"] / total_count) * 100)
    report_rate = int((totals["reported"] / total_count) * 100)

    body = f"""
    <html>
      <head>
        <title>Dashboard de interacción</title>
        <style>
          body {{
            margin: 0;
            font-family: "Inter", "Segoe UI", sans-serif;
            background: radial-gradient(circle at top, #0c1b2a 0%, #070b12 45%, #05070c 100%);
            color: #e6f1ff;
          }}
          header {{
            padding: 32px 48px 12px;
          }}
          header h1 {{
            font-size: 28px;
            margin: 0;
          }}
          header p {{
            margin: 8px 0 0;
            color: #9cb4d3;
          }}
          .filters {{
            padding: 0 48px 16px;
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
            gap: 16px;
          }}
          .filters select {{
            width: 100%;
            padding: 10px 12px;
            border-radius: 10px;
            border: 1px solid #203246;
            background: #0c1624;
            color: #e6f1ff;
          }}
          .summary {{
            padding: 0 48px 24px;
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
            gap: 16px;
          }}
          .summary-card {{
            background: #0c1624;
            border: 1px solid #1f2c3e;
            border-radius: 16px;
            padding: 16px 20px;
          }}
          .summary-card h2 {{
            margin: 0;
            font-size: 22px;
          }}
          .summary-card span {{
            color: #9cb4d3;
            font-size: 13px;
          }}
          .flows {{
            padding: 0 48px 48px;
            display: grid;
            gap: 16px;
          }}
          .flow-row {{
            background: #0c1624;
            border: 1px solid #1f2c3e;
            border-radius: 18px;
            padding: 20px;
            box-shadow: 0 10px 24px rgba(1, 6, 14, 0.3);
          }}
          .flow-header {{
            display: flex;
            justify-content: space-between;
            align-items: flex-start;
            gap: 16px;
          }}
          .flow-header h3 {{
            margin: 0;
            font-size: 18px;
          }}
          .flow-header p {{
            margin: 6px 0 0;
            color: #9cb4d3;
            font-size: 13px;
          }}
          .flow-meta {{
            display: flex;
            gap: 10px;
            align-items: center;
          }}
          .status-pill {{
            padding: 6px 10px;
            border-radius: 999px;
            background: #142236;
            font-size: 12px;
            color: #b6c9e8;
            border: 1px solid #22384d;
          }}
          .badge {{
            padding: 6px 12px;
            border-radius: 999px;
            font-size: 12px;
            font-weight: 600;
          }}
          .badge-critical {{ background: rgba(255, 89, 89, 0.18); color: #ff8b8b; border: 1px solid #ff5b5b; }}
          .badge-high {{ background: rgba(255, 153, 51, 0.15); color: #ffb57a; border: 1px solid #ff9933; }}
          .badge-medium {{ background: rgba(77, 160, 255, 0.15); color: #7bb7ff; border: 1px solid #4da0ff; }}
          .badge-low {{ background: rgba(48, 220, 174, 0.15); color: #6ff3cb; border: 1px solid #30dcae; }}
          .badge-none {{ background: rgba(108, 118, 140, 0.2); color: #c0c7d6; border: 1px solid #4d596f; }}
          .flow-steps {{
            margin-top: 16px;
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(120px, 1fr));
            gap: 10px;
          }}
          .flow-step {{
            background: #111d2c;
            border-radius: 12px;
            padding: 10px;
            border: 1px dashed #223447;
            display: flex;
            flex-direction: column;
            gap: 4px;
            color: #9cb4d3;
            min-height: 56px;
          }}
          .flow-step.active {{
            border: 1px solid #28d3ff;
            color: #e6f1ff;
            background: linear-gradient(135deg, rgba(40, 211, 255, 0.2), rgba(13, 32, 50, 0.9));
          }}
          .flow-step span {{
            font-size: 13px;
            font-weight: 600;
          }}
          .flow-step small {{
            font-size: 11px;
          }}
          .flow-footer {{
            margin-top: 12px;
            display: flex;
            justify-content: space-between;
            color: #9cb4d3;
            font-size: 12px;
          }}
          .empty {{
            background: #0c1624;
            border: 1px dashed #22384d;
            border-radius: 16px;
            padding: 32px;
            text-align: center;
            color: #9cb4d3;
          }}
          .filters form {{
            display: contents;
          }}
          .filters button {{
            padding: 10px 16px;
            border-radius: 10px;
            background: #28d3ff;
            color: #05101c;
            border: none;
            font-weight: 600;
            cursor: pointer;
          }}
        </style>
      </head>
      <body>
        <header>
          <h1>Dashboard de interacción con campañas</h1>
          <p>Visualiza el flujo de cada usuario frente a la campaña, criticidad y puntos de contacto.</p>
        </header>
        <section class="filters">
          <form method="get">
            <select name="campaign">
              <option value="">Todas las campañas</option>
              {"".join([f'<option value="{c.id}" {"selected" if str(c.id) == selected_campaign else ""}>{escape(c.name)}</option>' for c in campaigns])}
            </select>
            <select name="department">
              <option value="">Todas las áreas</option>
              {"".join([f'<option value="{escape(dep)}" {"selected" if dep == selected_department else ""}>{escape(dep)}</option>' for dep in all_departments])}
            </select>
            <select name="status">
              <option value="">Todos los estados</option>
              {"".join([f'<option value="{choice}" {"selected" if choice == selected_status else ""}>{label}</option>' for choice, label in CampaignRecipient.Status.choices])}
            </select>
            <select name="criticality">
              <option value="">Todas las criticidades</option>
              {"".join([f'<option value="{label}" {"selected" if label == selected_criticality else ""}>{label}</option>' for label in ["Crítica", "Alta", "Media", "Baja", "Sin señales"]])}
            </select>
            <button type="submit">Filtrar</button>
          </form>
        </section>
        <section class="summary">
          <div class="summary-card">
            <span>Usuarios filtrados</span>
            <h2>{totals["count"]}</h2>
          </div>
          <div class="summary-card">
            <span>Tasa de apertura</span>
            <h2>{open_rate}%</h2>
          </div>
          <div class="summary-card">
            <span>Tasa de clics</span>
            <h2>{click_rate}%</h2>
          </div>
          <div class="summary-card">
            <span>Tasa de reporte</span>
            <h2>{report_rate}%</h2>
          </div>
        </section>
        <section class="flows">
          {''.join(rows) if rows else '<div class="empty">No hay resultados con estos filtros.</div>'}
        </section>
      </body>
    </html>
    """
    return HttpResponse(body, content_type="text/html")
