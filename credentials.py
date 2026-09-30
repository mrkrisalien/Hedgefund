"""In-memory broker keys. Source of truth is the dashboard Settings page."""
from __future__ import annotations

import base64
import json
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import config

IST = ZoneInfo("Asia/Kolkata")

FIELDS = (
    "DHAN_CLIENT_ID",
    "DHAN_ACCESS_TOKEN",
    "GROQ_API_KEY",
    "DEEPSEEK_API_KEY",
    "DELTA_API_KEY",
    "DELTA_API_SECRET",
)


def jwt_expiry_unix(token):
    text = str(token or "").strip()
    parts = text.split(".")
    if len(parts) < 2:
        return None
    payload = parts[1] + "=" * (-len(parts[1]) % 4)
    try:
        data = json.loads(base64.urlsafe_b64decode(payload.encode("ascii")))
    except (ValueError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    try:
        exp = int(data.get("exp") or 0)
    except (TypeError, ValueError):
        return None
    return exp or None


def _mask(value):
    text = str(value or "").strip()
    if not text:
        return ""
    if len(text) <= 8:
        return text[:2] + "••••"
    return text[:4] + "••••" + text[-4:]


def _iso(unix):
    if not unix:
        return None
    stamp = datetime.fromtimestamp(int(unix), tz=timezone.utc).astimezone(IST)
    return stamp.isoformat()


def apply_keys(payload):
    data = payload or {}
    for field in FIELDS:
        key = field.lower()
        if key not in data and field not in data:
            continue
        raw = data.get(key, data.get(field))
        if raw is None:
            continue
        setattr(config, field, str(raw).strip())
    token = str(config.DHAN_ACCESS_TOKEN or "").strip()
    exp = jwt_expiry_unix(token)
    if token and exp is not None and exp <= datetime.now(timezone.utc).timestamp():
        raise ValueError("Dhan access token has expired. Generate a new token in Dhan.")
    from broker import reset_client

    reset_client()
    return public_status()


def clear_keys():
    for field in FIELDS:
        setattr(config, field, "")
    from broker import reset_client

    reset_client()
    return public_status()


def dhan_ready():
    client_id = str(config.DHAN_CLIENT_ID or "").strip()
    token = str(config.DHAN_ACCESS_TOKEN or "").strip()
    if not client_id or not token:
        return False
    exp = jwt_expiry_unix(token)
    if exp is not None and exp <= datetime.now(timezone.utc).timestamp():
        return False
    return True


def public_status():
    token = str(config.DHAN_ACCESS_TOKEN or "").strip()
    exp = jwt_expiry_unix(token)
    now = datetime.now(timezone.utc).timestamp()
    expired = bool(token and exp is not None and exp <= now)
    remaining = int(exp - now) if exp else None
    return {
        "dhan_configured": bool(str(config.DHAN_CLIENT_ID or "").strip() and token),
        "dhan_ready": dhan_ready(),
        "dhan_client_id_masked": _mask(config.DHAN_CLIENT_ID),
        "dhan_token_masked": _mask(token),
        "dhan_expires_at": _iso(exp),
        "dhan_expires_unix": exp,
        "dhan_expired": expired,
        "dhan_expires_in_seconds": remaining if remaining is not None and remaining > 0 else (0 if expired else None),
        "deepseek_configured": bool(str(config.DEEPSEEK_API_KEY or "").strip()),
        "groq_configured": bool(str(config.GROQ_API_KEY or "").strip()),
        "delta_configured": bool(
            str(config.DELTA_API_KEY or "").strip()
            and str(config.DELTA_API_SECRET or "").strip()
        ),
        "source": "browser-settings",
    }
