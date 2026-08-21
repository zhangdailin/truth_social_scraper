import json
import os
import re
import time
from datetime import datetime, timezone
from urllib.request import Request, urlopen, ProxyHandler, build_opener, install_opener
import socks
import socket

# Paths shared across scripts
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
# PROJECT_ROOT 直接指向项目根目录（utils.py 所在的目录）
PROJECT_ROOT = SCRIPT_DIR
ALERTS_FILE = os.path.join(PROJECT_ROOT, "market_alerts.json")

# 媒体文件推文ID映射文件（记录 post_id -> 本地文件路径列表的映射）
MEDIA_MAPPING_FILE = os.path.join(PROJECT_ROOT, "media_mapping.json")
DASHBOARD_JSON_FILE = os.path.join(PROJECT_ROOT, "dashboard_data.json")
MEDIA_DIR = os.path.join(PROJECT_ROOT, "media")


def local_media_api_path(path):
    """Return a safe /api/media path for an existing file inside MEDIA_DIR."""
    if not path:
        return ""
    try:
        media_root = os.path.realpath(MEDIA_DIR)
        real_path = os.path.realpath(os.fspath(path))
        if not os.path.isfile(real_path) or os.path.commonpath([media_root, real_path]) != media_root:
            return ""
        rel = os.path.relpath(real_path, media_root).replace("\\", "/")
        return f"/api/media/{rel.lstrip('/')}"
    except (OSError, TypeError, ValueError):
        return ""


def media_type_for_path(path):
    lower = str(path or "").lower()
    return "video" if any(lower.endswith(ext) for ext in (".mp4", ".webm", ".mov", ".avi", ".mkv", ".gif")) else "image"


def serialize_local_media(media_list, local_paths):
    """Replace attachment URLs with safe local URLs while preserving metadata."""
    attachments = media_list or []
    valid = [local_media_api_path(p) for p in (local_paths or [])]
    valid = [p for p in valid if p]
    if not valid:
        return attachments
    converted = []
    for index, item in enumerate(attachments):
        new_item = dict(item) if isinstance(item, dict) else {}
        if index < len(valid):
            api_path = valid[index]
            new_item["url"] = api_path
            new_item["preview_url"] = api_path
            new_item["type"] = media_type_for_path(local_paths[index])
            new_item.setdefault("original_url", (item.get("url") or item.get("preview_url")) if isinstance(item, dict) else "")
        converted.append(new_item)
    for index in range(len(attachments), len(valid)):
        api_path = valid[index]
        converted.append({"url": api_path, "preview_url": api_path, "type": media_type_for_path(local_paths[index])})
    return converted


def load_media_mapping():
    """
    加载媒体文件推文ID映射
    返回字典: {post_id: [local_filepath1, local_filepath2, ...]}
    """
    if not os.path.exists(MEDIA_MAPPING_FILE):
        return {}
    
    try:
        with open(MEDIA_MAPPING_FILE, "r", encoding='utf-8') as f:
            mapping = json.load(f)
            return mapping if isinstance(mapping, dict) else {}
    except Exception as e:
        print(f"[Utils] 加载媒体映射失败: {e}")
        return {}


def save_post_media_mapping(post_id, media_paths):
    """
    保存推文ID到媒体文件路径的映射
    post_id: 推文ID
    media_paths: 本地文件路径列表，例如: ["/path/to/video1.mp4", "/path/to/image1.jpg"]
    """
    if not post_id:
        return
    
    try:
        mapping = load_media_mapping()
        # 只保存存在的文件路径
        existing_paths = [p for p in media_paths if p and os.path.exists(p)]
        if existing_paths:
            mapping[str(post_id)] = existing_paths
            with open(MEDIA_MAPPING_FILE, "w", encoding='utf-8') as f:
                json.dump(mapping, f, indent=2, ensure_ascii=False)
            print(f"[Utils] ✅ 已保存推文 {post_id} 的媒体映射: {len(existing_paths)} 个文件")
        else:
            print(f"[Utils] ⚠️ 推文 {post_id} 没有有效的媒体文件，跳过映射")
    except Exception as e:
        print(f"[Utils] ❌ 保存媒体映射失败: {e}")


def get_media_paths_by_post_id(post_id):
    """
    根据推文ID获取本地媒体文件路径列表
    返回文件路径列表（只返回存在的文件），如果不存在则返回空列表
    """
    if not post_id:
        return []
    
    try:
        # 优先从 alerts 文件中读取嵌入字段 local_media_paths（统一到单文件）
        if os.path.exists(ALERTS_FILE):
            try:
                with open(ALERTS_FILE, "r", encoding="utf-8") as f:
                    alerts_data = json.load(f)
                if isinstance(alerts_data, dict):
                    alerts = alerts_data.get("alerts") or []
                else:
                    alerts = alerts_data if isinstance(alerts_data, list) else []
                for a in alerts:
                    if str(a.get("id") or "") == str(post_id):
                        paths = a.get("local_media_paths") or []
                        if paths:
                            existing = [p for p in paths if p and os.path.exists(p)]
                            if existing:
                                return existing
            except Exception:
                pass
        # 回退到原 media_mapping.json（兼容旧数据）
        mapping = load_media_mapping()
        paths = mapping.get(str(post_id), [])
        # 只返回存在的文件
        existing_paths = [p for p in paths if p and os.path.exists(p)]
        return existing_paths
    except Exception as e:
        print(f"[Utils] 获取媒体映射失败: {e}")
        return []


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
                # 如果类型为空，尝试从URL判断
                if not mt:
                    mu_lower = str(mu).lower()
                    # 检查URL中是否包含视频相关的关键词或扩展名
                    if any(ext in mu_lower for ext in ['.mp4', '.webm', '.mov', '.avi', '.mkv', 'video', '/videos/']):
                        mt = "video"
                    else:
                        mt = "image"
                
                media.append(
                    {
                        "url": mu,
                        "preview_url": m.get("preview_url") or mu,
                        "description": m.get("description") or "",
                        "type": mt,
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
        if not count:
            return ""
        
        # 统计视频和图片数量
        video_count = 0
        image_count = 0
        for m in media_atts or []:
            mt = str(m.get("type", "")).lower()
            # 检查类型或URL来判断是否为视频
            if mt in ("video", "gifv"):
                video_count += 1
            else:
                # 如果类型不明确，检查URL
                mu = m.get("url") or m.get("preview_url") or ""
                mu_lower = str(mu).lower()
                if any(ext in mu_lower for ext in ['.mp4', '.webm', '.mov', '.avi', '.mkv', 'video', '/videos/']):
                    video_count += 1
                else:
                    image_count += 1
        
        # 根据类型返回不同的描述
        if video_count > 0 and image_count > 0:
            return f"[视频] {video_count} 个 [图片] {image_count} 张"
        elif video_count > 0:
            return f"[视频] {video_count} 个"
        else:
            return f"[图片] {image_count} 张"
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


# 全局变量存储原始 socket
_original_socket = None
_proxy_enabled = False


def _setup_proxy():
    """设置 SOCKS 代理（如果配置了环境变量）"""
    global _original_socket, _proxy_enabled
    
    proxy_str = os.getenv("SOCKS_PROXY", "").strip()
    if not proxy_str:
        # 如果之前启用了代理，现在要禁用，则恢复原始 socket
        if _proxy_enabled and _original_socket:
            socket.socket = _original_socket
            _proxy_enabled = False
        return False
    
    # 如果已经设置过相同的代理，直接返回
    if _proxy_enabled:
        return True
    
    try:
        # 保存原始 socket（如果还没保存）
        if _original_socket is None:
            _original_socket = socket.socket
        
        # 解析代理地址，格式: 127.0.0.1:7890 或 socks5://127.0.0.1:7890
        if proxy_str.startswith("socks5://") or proxy_str.startswith("socks4://"):
            proxy_str = proxy_str.split("://")[1]
        
        if ":" in proxy_str:
            host, port = proxy_str.rsplit(":", 1)
            port = int(port)
        else:
            host = proxy_str
            port = 1080  # 默认端口
        
        # 设置 SOCKS 代理
        socks.set_default_proxy(socks.SOCKS5, host, port)
        socket.socket = socks.socksocket
        _proxy_enabled = True
        print(f"[Proxy] SOCKS5 代理已启用: {host}:{port}")
        return True
    except Exception as e:
        print(f"[Proxy] 代理配置错误: {e}")
        return False


def fetch_json_with_retries(url, headers, timeout=15, retries=3, backoff=2):
    """HTTP GET JSON with retry/backoff semantics."""
    # 设置代理（如果配置了）
    _setup_proxy()
    
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
