import json
import os
import re
import time
from datetime import datetime, timezone
from urllib.request import Request, urlopen

# Paths shared across scripts
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(SCRIPT_DIR)
ALERTS_FILE = os.path.join(PROJECT_ROOT, "market_alerts.json")


def normalize_iso(ts_value):
    """Return ISO timestamp in UTC, tolerating malformed values."""
    if not ts_value:
        return datetime.now(timezone.utc).isoformat()
    try:
        s = str(ts_value).replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).isoformat()
    except Exception:
        return datetime.now(timezone.utc).isoformat()


def to_local_str(iso_str):
    """Render ISO string to local time; best-effort fallback on errors."""
    try:
        if not iso_str:
            return ""
        s = iso_str.replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone().strftime("%Y-%m-%d %H:%M")
    except Exception:
        return (iso_str or "")[:16].replace("T", " ")


def local_tz_label():
    """Return current local timezone label like UTC+0800."""
    return datetime.now(timezone.utc).astimezone().strftime("UTC%z")


def pick_ts(value):
    """Pick a timestamp field from an alert/post dict."""
    return (
        value.get("created_at")
        or value.get("createdAt")
        or value.get("detected_at")
        or ""
    )


def env_flag(name, default=True):
    """Read an env var and coerce common truthy/falsey strings into bool."""
    val = os.getenv(name)
    if val is None:
        return default
    return str(val).strip().lower() in ("1", "true", "yes", "on")


def extract_media(atts):
    """Normalize media attachments into a simple list used by both scripts."""
    media = []
    try:
        for m in atts or []:
            mt = str(m.get("type", "")).lower()
            mu = m.get("url") or m.get("remote_url") or m.get("preview_url")
            if mu and (not mt or mt in ("image", "gifv", "video")):
                media.append(
                    {
                        "url": mu,
                        "preview_url": m.get("preview_url") or mu,
                        "description": m.get("description") or "",
                        "type": mt or "image",
                    }
                )
    except Exception:
        return []
    return media


def describe_media(media_atts):
    """Return a short text summary of media when no post text exists."""
    try:
        descs = [
            str(m.get("description") or "").strip()
            for m in media_atts or []
            if str(m.get("description") or "").strip()
        ]
        if descs:
            return " ".join(descs)
        count = len(media_atts or [])
        return f"[图片] {count} 张" if count else ""
    except Exception:
        count = len(media_atts or [])
        return f"[图片] {count} 张" if count else ""


def derive_content(post, media_atts):
    """Clean HTML content and backfill from media when missing."""
    raw_html = post.get("content") or post.get("text") or ""
    content = re.sub(r"<[^>]+>", " ", raw_html)
    content = re.sub(r"\s+", " ", content).strip()
    if content:
        return content
    return describe_media(media_atts)


def fetch_json_with_retries(url, headers, timeout=15, retries=3, backoff=2):
    """HTTP GET JSON with retry/backoff semantics."""
    last_err = None
    for i in range(int(retries)):
        try:
            req = Request(url, headers=headers)
            with urlopen(req, timeout=timeout) as resp:
                body = resp.read()
                return json.loads(body)
        except Exception as e:  # noqa: BLE001
            last_err = e
            time.sleep(backoff * (i + 1))
    raise last_err


def fetch_truth_posts(account_id, username, cookie, limit=20, fast_init=False):
    """Fetch recent Truth Social posts for an account, with a fallback attempt."""
    base_url = (
        f"https://truthsocial.com/api/v1/accounts/{account_id}/statuses"
        f"?exclude_replies=true&with_muted=true&limit={int(limit)}"
    )
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36"
        ),
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Origin": "https://truthsocial.com",
        "Referer": f"https://truthsocial.com/@{username}",
        "Cookie": cookie,
    }

    timeout = 8 if fast_init else 15
    retries = 1 if fast_init else 2
    backoff = 1 if fast_init else 2

    try:
        return fetch_json_with_retries(
            base_url, headers, timeout=timeout, retries=retries, backoff=backoff
        )
    except Exception as e:  # noqa: BLE001
        print(f"CookieAPI primary failed: {e}")

    fallback_limit = min(5, int(limit))
    fallback_url = (
        f"https://truthsocial.com/api/v1/accounts/{account_id}/statuses"
        f"?exclude_replies=true&with_muted=true&limit={fallback_limit}"
    )
    try:
        return fetch_json_with_retries(
            fallback_url,
            headers,
            timeout=(12 if fast_init else 25),
            retries=retries,
            backoff=(2 if fast_init else 3),
        )
    except Exception as e2:  # noqa: BLE001
        print(f"CookieAPI fallback failed: {e2}")
        return []

