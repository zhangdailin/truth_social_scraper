import json
import os
import random
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from urllib.parse import urlparse
from urllib.request import Request, urlopen, build_opener, ProxyHandler, BaseHandler
from urllib.error import URLError

try:
    import socks  # type: ignore
    _SOCKS_AVAILABLE = True
except ImportError:
    socks = None
    _SOCKS_AVAILABLE = False


class SocksiPyHandler(BaseHandler):
    """Custom SOCKS proxy handler for urllib using PySocks."""
    
    def __init__(self, proxy_type, host, port, username=None, password=None):
        if not _SOCKS_AVAILABLE:
            raise ImportError("PySocks is required for SOCKS proxy support")
        self.proxy_type = proxy_type
        self.host = host
        self.port = port
        self.username = username
        self.password = password
    
    def http_open(self, req):
        return self._proxy_open(req, False)
    
    def https_open(self, req):
        return self._proxy_open(req, True)
    
    def _proxy_open(self, req, is_https):
        import socket as std_socket
        from http.client import HTTPConnection, HTTPSConnection
        from io import BytesIO
        
        # Parse the target URL
        url = req.full_url
        parsed = urlparse(url)
        target_host = parsed.hostname
        target_port = parsed.port or (443 if is_https else 80)
        
        # Create a SOCKS socket factory function
        def create_socks_connection(host, port, timeout=None, source_address=None):
            """Create a connection function that uses SOCKS proxy."""
            sock = socks.socksocket()
            sock.set_proxy(
                self.proxy_type,
                self.host,
                self.port,
                username=self.username if self.username else None,
                password=self.password if self.password else None,
            )
            if timeout is not None:
                sock.settimeout(timeout)
            sock.connect((host, port))
            return sock
        
        # Temporarily monkey-patch socket.create_connection
        original_create_connection = std_socket.create_connection
        std_socket.create_connection = create_socks_connection
        
        try:
            # Use http.client to make the request
            timeout = req.timeout or 30
            if is_https:
                conn = HTTPSConnection(target_host, target_port, timeout=timeout)
            else:
                conn = HTTPConnection(target_host, target_port, timeout=timeout)
            
            # Build the path with query string
            path = parsed.path or "/"
            if parsed.query:
                path += "?" + parsed.query
            
            # Send request
            conn.request(req.get_method(), path, body=req.data, headers=dict(req.headers))
            resp = conn.getresponse()
            
            # Create a file-like object for the response
            response_data = resp.read()
            resp_file = BytesIO(response_data)
            
            # Create urllib response
            from urllib.response import addinfourl
            return addinfourl(resp_file, resp.msg, req.full_url, resp.status)
        finally:
            # Restore original create_connection
            std_socket.create_connection = original_create_connection

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


def fetch_public_proxies(max_items=5):
    """
    Fetch a small list of public proxies from proxifly/free-proxy-list.
    Automatically detects protocol types (socks5, socks4, http) from the proxy URLs.
    
    Args:
        max_items: Maximum number of proxies to return
    """
    sources = [
        "https://raw.githubusercontent.com/proxifly/free-proxy-list/refs/heads/main/proxies/countries/US/data.txt",
        "https://raw.githubusercontent.com/proxifly/free-proxy-list/refs/heads/main/proxies/countries/PL/data.txt",
        "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/socks5.txt",
        "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/socks4.txt",
    ]
    
    proxies = []
    for src in sources:
        try:
            with urlopen(src, timeout=8) as resp:
                body = resp.read().decode("utf-8", errors="ignore")
        except Exception:
            continue
        
        for line in body.splitlines():
            line = line.strip()
            if not line:
                continue
            # Check if line already contains protocol prefix (socks5://, socks4://, http://)
            if "://" in line:
                # Already has protocol, check if it's HTTP and skip it
                parsed = urlparse(line)
                scheme = (parsed.scheme or "").lower()
                # Skip HTTP proxies, only keep SOCKS and HTTPS
                if scheme == "http":
                    continue
                # Keep SOCKS (socks4, socks4a, socks5, socks5h) and HTTPS
                proxies.append(line)
            elif ":" in line:
                # No protocol prefix, skip it (would be HTTP by default)
                # Only add proxies that explicitly specify a non-HTTP protocol
                continue
    
    # Dedup and shuffle so we do not keep testing the same top-of-file entries
    proxies = list(dict.fromkeys(proxies))
    random.shuffle(proxies)
    return proxies[:max_items]


def fetch_json_with_retries(
    url,
    headers,
    timeout=15,
    retries=3,
    backoff=2,
    use_free_proxies=True,
    verbose_proxy=False,
    proxy_limit=100000,
    proxy_retries=1,
    proxy_backoff=0,
    probe_first=False,
    probe_timeout=6,
    probe_url=None,
    probe_concurrency=1,
):
    """HTTP GET JSON with retry/backoff semantics; can try free proxy list."""
    last_err = None
    proxy_candidates = []
    if use_free_proxies:
        proxy_candidates.extend(fetch_public_proxies(max_items=proxy_limit))
    # Always allow direct connection as last fallback
    proxy_candidates.append(None)

    def _build_opener(proxy_url):
        """Return an opener that supports http/https and socks proxies."""
        if not proxy_url:
            return build_opener()
        parsed = urlparse(proxy_url)
        scheme = (parsed.scheme or "").lower()
        if scheme in {"socks5", "socks5h", "socks4", "socks4a"}:
            if not _SOCKS_AVAILABLE:
                # Missing PySocks library, skip this proxy
                if verbose_proxy:
                    print(f"[proxy] PySocks not available, skip: {proxy_url}")
                return None
            try:
                # Determine proxy type: socks5/socks5h use SOCKS5, socks4/socks4a use SOCKS4
                is_socks5 = "5" in scheme
                proxy_type = (
                    socks.PROXY_TYPE_SOCKS5
                    if is_socks5
                    else socks.PROXY_TYPE_SOCKS4
                )
                host = parsed.hostname
                port = parsed.port or 1080
                if not host:
                    # Invalid proxy URL (no hostname)
                    if verbose_proxy:
                        print(f"[proxy] invalid proxy URL (no hostname): {proxy_url}")
                    return None
                # SOCKS4 does not support authentication, only SOCKS5 does
                username = parsed.username if is_socks5 else None
                password = parsed.password if is_socks5 else None
                handler = SocksiPyHandler(
                    proxy_type,
                    host,
                    port,
                    username=username,
                    password=password,
                )
                opener = build_opener(handler)
                if verbose_proxy:
                    print(f"[proxy] created SOCKS handler: {proxy_url} (type={proxy_type}, host={host}, port={port})")
                return opener
            except Exception as e:
                # If handler creation fails, return None to skip this proxy
                if verbose_proxy:
                    print(f"[proxy] failed to create handler for {proxy_url}: {type(e).__name__}: {e}")
                import traceback
                if verbose_proxy:
                    traceback.print_exc()
                return None
        # http/https proxies
        try:
            cfg = {"http": proxy_url, "https": proxy_url}
            opener = build_opener(ProxyHandler(cfg))
            if verbose_proxy:
                print(f"[proxy] created HTTP/HTTPS handler: {proxy_url}")
            return opener
        except Exception as e:
            # If handler creation fails, return None to skip this proxy
            if verbose_proxy:
                print(f"[proxy] failed to create handler for {proxy_url}: {type(e).__name__}: {e}")
            return None

    # Optional concurrent probe to reorder by success
    preprobed = False
    probe_target = probe_url or os.getenv("PROBE_URL") or url
    if probe_first and probe_concurrency > 1:
        preprobed = True
        successes = []
        proxy_list = [p for p in proxy_candidates if p is not None]
        direct_present = None in proxy_candidates
        batch_size = 50
        if verbose_proxy:
            print(f"[proxy] start probe total={len(proxy_list)} batch={batch_size} workers={probe_concurrency}")

        def _probe(p_url):
            label = p_url or "direct"
            opener = _build_opener(p_url)
            if opener is None:
                if verbose_proxy:
                    print(f"[proxy] skip unsupported handler: {label}")
                return False, label
            try:
                req = Request(probe_target, headers=headers)
                with opener.open(req, timeout=min(timeout, probe_timeout)) as resp:
                    resp.read(128)
                return True, label
            except Exception as e:  # noqa: BLE001
                if verbose_proxy:
                    print(f"[proxy] probe failed {label}: {e}")
                return False, label

        batch_index = 0
        while proxy_list:
            batch_index += 1
            batch = proxy_list[:batch_size]
            proxy_list = proxy_list[batch_size:]
            labels = [p or "direct" for p in batch]
            status = {lbl: "pending" for lbl in labels}
            max_workers = min(int(probe_concurrency), max(1, len(batch)))
            if verbose_proxy:
                print(f"[proxy] batch {batch_index} testing {len(batch)}: {', '.join(labels)}")
            with ThreadPoolExecutor(max_workers=max_workers) as pool:
                future_map = {pool.submit(_probe, p): p for p in batch}
                for fut in as_completed(future_map):
                    ok, label = fut.result()
                    status[label] = "ok" if ok else "fail"
                    if ok:
                        successes.append(future_map[fut])
                    if verbose_proxy:
                        print(f"[proxy] batch {batch_index} result {label}: {'ok' if ok else 'fail'}")
            if verbose_proxy:
                print(f"[proxy] batch {batch_index} done keep={sum(1 for s in status.values() if s == 'ok')} drop={sum(1 for s in status.values() if s == 'fail')}")
        # keep order: successful only, then direct if present
        proxy_candidates = successes + ([None] if direct_present else [])

    for proxy_url in proxy_candidates:
        proxy_label = proxy_url or "direct"
        if verbose_proxy:
            print(f"[proxy] testing: {proxy_label}")
        proxy_cfg = (
            {"http": proxy_url, "https": proxy_url} if proxy_url else None
        )
        opener = _build_opener(proxy_url)
        if opener is None:
            if verbose_proxy:
                print(f"[proxy] skip unsupported handler: {proxy_label}")
            continue

        # Optional quick probe using the same headers (includes cookie)
        if probe_first and not preprobed:
            try:
                req = Request(probe_target, headers=headers)
                with opener.open(req, timeout=min(timeout, probe_timeout)) as resp:
                    resp.read(128)
                if verbose_proxy:
                    print(f"[proxy] probe ok: {proxy_label}")
            except Exception as e:  # noqa: BLE001
                last_err = e
                if verbose_proxy:
                    print(f"[proxy] probe failed {proxy_label}: {e}")
                continue

        attempts = proxy_retries if proxy_url else retries
        delay = proxy_backoff if proxy_url else backoff
        for i in range(int(attempts)):
            try:
                req = Request(url, headers=headers)
                with opener.open(req, timeout=timeout) as resp:
                    body = resp.read()
                    return json.loads(body)
            except Exception as e:  # noqa: BLE001
                last_err = e
                if verbose_proxy:
                    print(
                        f"[proxy] failed {proxy_label} attempt {i + 1}/{attempts}: {e}"
                    )
                if delay:
                    time.sleep(delay * (i + 1))
        # move to next proxy candidate
    raise last_err


def fetch_truth_posts(account_id, username, cookie, limit=20, fast_init=False):
    """Fetch recent Truth Social posts for an account, with a fallback attempt."""
    base_url = (
        f"https://truthsocial.com/api/v1/accounts/{account_id}/statuses"
        f"?exclude_replies=true&with_muted=true&limit={int(limit)}"
    )
    headers = _cookie_headers(cookie, username)

    timeout = 8 if fast_init else 15
    retries = 1 if fast_init else 2
    backoff = 1 if fast_init else 2

    try:
        return fetch_with_cookie_via_proxies(
            base_url,
            headers=headers,
            timeout=timeout,
            retries=retries,
            backoff=backoff,
            probe_timeout=5 if fast_init else 6,
            probe_concurrency=50,
        )
    except Exception as e:  # noqa: BLE001
        print(f"CookieAPI primary failed: {e}")

    fallback_limit = min(5, int(limit))
    fallback_url = (
        f"https://truthsocial.com/api/v1/accounts/{account_id}/statuses"
        f"?exclude_replies=true&with_muted=true&limit={fallback_limit}"
    )
    try:
        return fetch_with_cookie_via_proxies(
            fallback_url,
            headers=headers,
            timeout=(12 if fast_init else 25),
            retries=retries,
            backoff=(2 if fast_init else 3),
            probe_timeout=(6 if fast_init else 8),
            probe_concurrency=50,
        )
    except Exception as e2:  # noqa: BLE001
        print(f"CookieAPI fallback failed: {e2}")
        return []


def _cookie_headers(cookie, username=None):
    """Build headers for cookie-authenticated TruthSocial calls."""
    return {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36"
        ),
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Origin": "https://truthsocial.com",
        "Referer": f"https://truthsocial.com/@{username}" if username else "https://truthsocial.com",
        "Cookie": cookie,
    }


def fetch_with_cookie_via_proxies(
    url,
    headers,
    timeout=15,
    retries=2,
    backoff=2,
    proxy_retries=1,
    proxy_backoff=0,
    probe_timeout=6,
    probe_concurrency=50,
):
    """
    Uniform path for cookie + 50-proxy pipeline, so all cookie calls behave the same.
    """
    return fetch_json_with_retries(
        url,
        headers,
        timeout=timeout,
        retries=retries,
        backoff=backoff,
        verbose_proxy=True,
        proxy_retries=proxy_retries,
        proxy_backoff=proxy_backoff,
        probe_first=True,
        probe_timeout=probe_timeout,
        probe_url=os.getenv("PROBE_URL"),
        probe_concurrency=probe_concurrency,
    )

