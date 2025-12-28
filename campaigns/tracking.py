from __future__ import annotations

import hashlib
import ipaddress
from dataclasses import dataclass

from django.conf import settings


@dataclass(frozen=True)
class ClientSignals:
    device_type: str
    os_family: str
    browser_family: str
    email_client_hint: str
    message_provider_hint: str
    is_webview: bool


def _normalize_contains(value: str, *needles: str) -> bool:
    lowered = value.lower()
    return any(needle in lowered for needle in needles)


def parse_user_agent(user_agent: str) -> ClientSignals:
    ua = user_agent or ""
    device_type = "unknown"
    if _normalize_contains(ua, "ipad", "tablet"):
        device_type = "tablet"
    elif _normalize_contains(ua, "iphone", "android", "mobile"):
        device_type = "mobile"
    elif ua:
        device_type = "desktop"

    os_family = "other"
    if _normalize_contains(ua, "windows"):
        os_family = "Windows"
    elif _normalize_contains(ua, "mac os x", "macintosh"):
        os_family = "macOS"
    elif _normalize_contains(ua, "iphone", "ipad", "ios"):
        os_family = "iOS"
    elif _normalize_contains(ua, "android"):
        os_family = "Android"
    elif _normalize_contains(ua, "linux"):
        os_family = "Linux"

    browser_family = "other"
    if _normalize_contains(ua, "edg/"):
        browser_family = "Edge"
    elif _normalize_contains(ua, "chrome/"):
        browser_family = "Chrome"
    elif _normalize_contains(ua, "safari/") and not _normalize_contains(ua, "chrome/"):
        browser_family = "Safari"
    elif _normalize_contains(ua, "firefox/"):
        browser_family = "Firefox"

    email_client_hint = "other"
    if _normalize_contains(ua, "outlook"):
        email_client_hint = "Outlook"
    elif _normalize_contains(ua, "owa", "outlook web"):
        email_client_hint = "OWA"
    elif _normalize_contains(ua, "gmail"):
        email_client_hint = "GmailWeb"
        if _normalize_contains(ua, "mobile", "iphone", "android"):
            email_client_hint = "GmailMobile"
    elif _normalize_contains(ua, "apple mail", "mail/"):
        email_client_hint = "AppleMail"
    elif _normalize_contains(ua, "thunderbird"):
        email_client_hint = "Thunderbird"

    message_provider_hint = "other"
    if _normalize_contains(ua, "outlook", "owa"):
        message_provider_hint = "m365"
    elif _normalize_contains(ua, "gmail"):
        message_provider_hint = "google"

    is_webview = _normalize_contains(ua, "wv", "webview", "line/") or (
        _normalize_contains(ua, "instagram", "fbav", "fb_iab")
    )

    return ClientSignals(
        device_type=device_type,
        os_family=os_family,
        browser_family=browser_family,
        email_client_hint=email_client_hint,
        message_provider_hint=message_provider_hint,
        is_webview=is_webview,
    )


def infer_open_signal_quality(user_agent: str) -> str:
    ua = user_agent or ""
    if _normalize_contains(ua, "applemail", "mail/") and _normalize_contains(ua, "mac os x", "iphone", "ipad"):
        return "low"
    if _normalize_contains(ua, "googleimageproxy", "gmail") and "google" in ua.lower():
        return "low"
    return "medium"


def truncate_ip(ip_value: str | None) -> str:
    if not ip_value:
        return ""
    try:
        ip_obj = ipaddress.ip_address(ip_value)
    except ValueError:
        return ""
    if ip_obj.version == 4:
        network = ipaddress.ip_network(f"{ip_obj}/24", strict=False)
    else:
        network = ipaddress.ip_network(f"{ip_obj}/48", strict=False)
    return str(network)


def hash_ip(ip_value: str | None) -> str:
    if not ip_value:
        return ""
    salt = settings.IP_HASH_SALT
    digest = hashlib.sha256(f"{salt}:{ip_value}".encode("utf-8")).hexdigest()
    return digest
