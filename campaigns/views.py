from __future__ import annotations

import json

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
    landing_rate = int((totals["landing"] / total_count) * 100)
    cta_rate = int((totals["cta"] / total_count) * 100)
    report_rate = int((totals["reported"] / total_count) * 100)
    chart_payload = {
        "labels": list(criticality_counts.keys()),
        "counts": list(criticality_counts.values()),
        "funnel_labels": [
            "Enviados",
            "Abiertos",
            "Landing",
            "CTA",
            "Reportados",
            "Rebotados",
        ],
        "funnel_counts": [
            totals["sent"],
            totals["opened"],
            totals["landing"],
            totals["cta"],
            totals["reported"],
            totals["bounced"],
        ],
        "rates": [open_rate, landing_rate, cta_rate, report_rate],
    }
    metric_tiles = []
    def _format_datetime(value):
        return value.strftime("%d/%m/%Y, %H:%M") if value else "--"

    body = f"""
    <html lang="es">
      <head>
        <meta charset="utf-8" />
        <title>Dashboard v3</title>
        <style>
          body {{
            margin: 0;
            font-family: "Inter", "Segoe UI", sans-serif;
            background: #f5f7fb;
            color: #2b2f33;
          }}
          .page {{
            max-width: 1200px;
            margin: 0 auto;
            padding: 24px 32px 48px;
          }}
          .headline {{
            font-size: 26px;
            margin: 0;
          }}
          .subhead {{
            margin: 6px 0 20px;
            color: #6b7280;
            font-size: 13px;
          }}
          .top-tabs {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(120px, 1fr));
            gap: 12px;
            margin-bottom: 16px;
          }}
          .top-tab {{
            background: #ffffff;
            border: 1px solid #e5e7eb;
            border-radius: 8px;
            padding: 12px 10px;
            text-align: center;
            font-weight: 600;
            color: #4b5563;
          }}
          .top-tab span {{
            display: block;
            font-size: 12px;
            color: #6b7280;
            margin-top: 4px;
            font-weight: 500;
          }}
          .top-metrics {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(120px, 1fr));
            gap: 8px;
            margin-bottom: 18px;
          }}
          .top-metric {{
            background: #ffffff;
            border: 1px solid #e5e7eb;
            border-radius: 8px;
            padding: 10px 8px;
            text-align: center;
            color: #4f46e5;
            font-weight: 700;
          }}
          .grid {{
            display: grid;
            grid-template-columns: repeat(12, 1fr);
            gap: 16px;
          }}
          .card {{
            background: #ffffff;
            border: 1px solid #e5e7eb;
            border-radius: 12px;
            padding: 16px;
          }}
          .card h3 {{
            margin: 0 0 12px;
            font-size: 14px;
            color: #4f46e5;
            text-transform: uppercase;
            letter-spacing: 0.4px;
          }}
          .ring {{
            width: 120px;
            height: 120px;
            margin: 0 auto 12px;
          }}
          .ring-label {{
            text-align: center;
            font-weight: 600;
          }}
          .bar-row {{
            display: flex;
            align-items: center;
            gap: 12px;
            margin-bottom: 10px;
          }}
          .bar {{
            flex: 1;
            height: 10px;
            border-radius: 999px;
            background: #e5e7eb;
            overflow: hidden;
          }}
          .bar span {{
            display: block;
            height: 100%;
            background: linear-gradient(90deg, #6366f1, #a5b4fc);
          }}
          .avatar-list {{
            display: grid;
            gap: 12px;
          }}
          .avatar-item {{
            display: flex;
            gap: 12px;
            align-items: center;
          }}
          .avatar {{
            width: 36px;
            height: 36px;
            border-radius: 999px;
            background: #c7d2fe;
            display: inline-flex;
            align-items: center;
            justify-content: center;
            font-weight: 700;
            color: #4338ca;
          }}
          .muted {{
            color: #6b7280;
            font-size: 12px;
          }}
        </style>
      </head>
      <body>
        <div class="page">
          <h1 class="headline">Cyber Phishing Dashboard</h1>
          <p class="subhead">Resumen de campañas, riesgos y curvas de mejora con el estilo de referencia.</p>
          <div class="top-tabs">
            <div class="top-tab">Campaigns<span>{len(campaigns)}</span></div>
            <div class="top-tab">Recipients<span>{totals["count"]}</span></div>
            <div class="top-tab">Delivered<span>{totals["sent"]}</span></div>
            <div class="top-tab">Opened<span>{totals["opened"]}</span></div>
            <div class="top-tab">Clicked<span>{totals["cta"]}</span></div>
            <div class="top-tab">Reported<span>{totals["reported"]}</span></div>
            <div class="top-tab">Bounced<span>{totals["bounced"]}</span></div>
          </div>
          <div class="top-metrics">
            <div class="top-metric">{totals["sent"]}</div>
            <div class="top-metric">{totals["opened"]}</div>
            <div class="top-metric">{totals["reported"]}</div>
            <div class="top-metric">{totals["count"]}</div>
            <div class="top-metric">{totals["landing"]}</div>
            <div class="top-metric">{totals["cta"]}</div>
            <div class="top-metric">{totals["bounced"]}</div>
          </div>
          <div class="grid">
            <div class="card" style="grid-column: span 6;">
              <h3>Organisation Health Risk</h3>
              <canvas class="ring" id="riskChart"></canvas>
              <div class="ring-label">Phishing Campaigns · {open_rate}%</div>
            </div>
            <div class="card" style="grid-column: span 6;">
              <h3>Attack Vectors</h3>
              <div class="bar-row">
                <span>Phishing</span>
                <div class="bar"><span style="width: 100%;"></span></div>
                <strong>100%</strong>
              </div>
              <div class="bar-row">
                <span>Smishing</span>
                <div class="bar"><span style="width: 0%;"></span></div>
                <strong>0%</strong>
              </div>
              <div class="bar-row">
                <span>Vishing</span>
                <div class="bar"><span style="width: 0%;"></span></div>
                <strong>0%</strong>
              </div>
              <div class="bar-row">
                <span>Ransomware</span>
                <div class="bar"><span style="width: 0%;"></span></div>
                <strong>0%</strong>
              </div>
            </div>
            <div class="card" style="grid-column: span 6;">
              <h3>Overall Risk Review</h3>
              <canvas id="riskBarChart" height="160"></canvas>
            </div>
            <div class="card" style="grid-column: span 6;">
              <h3>Improvement Curve</h3>
              <canvas id="improvementChart" height="160"></canvas>
            </div>
            <div class="card" style="grid-column: span 4;">
              <h3>Recent Activity</h3>
              <div class="avatar-list">
                {"".join([
                    f'''
                    <div class="avatar-item">
                      <div class="avatar">{escape((item.recipient.full_name or item.recipient.email)[:1].upper())}</div>
                      <div>
                        <div>{escape(item.recipient.full_name or item.recipient.email)}</div>
                        <div class="muted">{escape(_format_datetime(item.created_at))}</div>
                      </div>
                    </div>
                    '''
                    for item in recipients[:6]
                ])}
              </div>
            </div>
          </div>
        </div>
        <script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js"></script>
        <script>
          const payload = {json.dumps(chart_payload)};
          const riskCtx = document.getElementById("riskChart");
          new Chart(riskCtx, {{
            type: "doughnut",
            data: {{
              labels: ["Score", "Restante"],
              datasets: [{{ data: [{open_rate}, {100 - open_rate}], backgroundColor: ["#6366f1", "#e5e7eb"] }}],
            }},
            options: {{ plugins: {{ legend: {{ display: false }} }}, cutout: "70%" }},
          }});
          const riskBarCtx = document.getElementById("riskBarChart");
          new Chart(riskBarCtx, {{
            type: "bar",
            data: {{
              labels: payload.funnel_labels,
              datasets: [{{ data: payload.funnel_counts, backgroundColor: "#a5b4fc" }}],
            }},
            options: {{ plugins: {{ legend: {{ display: false }} }} }},
          }});
          const improvementCtx = document.getElementById("improvementChart");
          new Chart(improvementCtx, {{
            type: "line",
            data: {{
              labels: ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul"],
              datasets: [{{ data: payload.rates, borderColor: "#6366f1", backgroundColor: "rgba(99,102,241,0.2)" }}],
            }},
            options: {{ plugins: {{ legend: {{ display: false }} }} }},
          }});
        </script>
      </body>
    </html>
    """
    body_v1 = body
    body_v2 = body
    body_v3 = body
    return HttpResponse(body, content_type="text/html")
