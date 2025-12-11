import time
import os
import json
import re
from datetime import datetime, timedelta, timezone
from openai import OpenAI
from ddgs import DDGS
from urllib.request import Request, urlopen, ProxyHandler, build_opener
import socket
import ssl
from urllib.parse import urlsplit
from concurrent.futures import ThreadPoolExecutor, as_completed

# ==========================================
# CONFIGURATION
# ==========================================
# Determine paths relative to this script to ensure consistency
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(SCRIPT_DIR) # Go up one level to 'L'

PROCESSED_LOG_FILE = os.path.join(PROJECT_ROOT, "processed_posts.json")
ALERTS_FILE = os.path.join(PROJECT_ROOT, "market_alerts.json")

# SiliconFlow API Configuration
SILICONFLOW_API_KEY = os.getenv("SILICONFLOW_API_KEY") 
BASE_URL = "https://api.siliconflow.cn/v1"
 

HUGGINGFACE_API_KEY = os.getenv("HUGGINGFACE_API_KEY")
HUGGINGFACE_IMAGE_MODEL = os.getenv("HUGGINGFACE_IMAGE_MODEL", "Salesforce/blip-image-captioning-large")
HF_API_URL = "https://api-inference.huggingface.co/models"
SOCKS5_COUNTRY = "US"
PROXY_POOL_TTL = 300
PROXY_POOL = {"healthy": [], "last_refresh": 0}

# Apify Configuration
TRUTH_ACCOUNT_ID = os.getenv("TRUTH_ACCOUNT_ID", "107780257626128497")
TRUTH_COOKIE = os.getenv("TRUTH_COOKIE", "")
TRUTH_USERNAME = os.getenv("TRUTH_USERNAME", "realDonaldTrump")

 

# Basic stop words to filter out common noise
STOP_WORDS = {
    "a", "an", "the", "and", "or", "but", "is", "are", "was", "were", "be", "been", "being",
    "in", "on", "at", "to", "for", "from", "with", "by", "about", "of", "that", "this", "these", "those",
    "it", "he", "she", "they", "we", "i", "you", "me", "him", "her", "us", "them",
    "what", "which", "who", "whom", "whose", "when", "where", "why", "how",
    "will", "would", "shall", "should", "can", "could", "may", "might", "must",
    "has", "have", "had", "do", "does", "did",
    "very", "really", "just", "so", "too", "quite", "rather",
    "donald", "trump", "realdonaldtrump", "truth", "social" # Filter self-references for search
}


# ==========================================
# AI ANALYSIS FUNCTIONS
# ==========================================

def extract_keywords(text):
    """
    Extracts key terms from the post content for better search queries.
    Prioritizes capitalized words (Named Entities) and non-stop words.
    """
    # Remove URLs
    text = re.sub(r'http\S+', '', text)
    # Remove special chars but keep spaces
    text = re.sub(r'[^\w\s]', ' ', text)
    
    words = text.split()
    
    # Identify potential entities (capitalized words not at start of sentence)
    important_words = []
    for w in words:
        clean_w = w.lower()
        if clean_w not in STOP_WORDS and len(clean_w) > 2:
            important_words.append(w)
            
    # Return top 6 most interesting words
    return " ".join(important_words[:6])

def fetch_external_context(query_text):
    """
    Fetches external context (news/search results) using DuckDuckGo.
    Uses extracted keywords to find relevant market news and discussions.
    """
    try:
        keywords = extract_keywords(query_text)
        if not keywords:
            keywords = query_text[:50]
        q_news = re.sub(r"\s+", " ", f"Donald Trump {keywords} market news").strip()
        q_market = re.sub(r"\s+", " ", f"Donald Trump {keywords} stock market reaction").strip()
        q_news = re.sub(r"[\)\:]+$", "", q_news)
        q_market = re.sub(r"[\)\:]+$", "", q_market)
        results = []
        with DDGS() as ddgs:
            try:
                news_gen = ddgs.news(q_news, max_results=2)
                if news_gen:
                    results.extend([f"[News] {r['title']}: {r['body']}" for r in news_gen])
            except Exception:
                pass
            try:
                web_gen = ddgs.text(q_market, max_results=2)
                if web_gen:
                    results.extend([f"[Market Discussion] {r['title']}: {r['body']}" for r in web_gen])
            except Exception:
                pass
        return "\n".join(results) if results else "No related external news found."
    except Exception:
        return "No related external news found."

def hf_caption_image(image_url, timeout=15):
    try:
        if not HUGGINGFACE_API_KEY:
            return ""
        if not image_url:
            return ""
        with urlopen(Request(image_url, headers={"User-Agent":"Mozilla/5.0"}), timeout=timeout) as r:
            img_bytes = r.read()
        req = Request(f"{HF_API_URL}/{HUGGINGFACE_IMAGE_MODEL}", data=img_bytes, headers={
            "Authorization": f"Bearer {HUGGINGFACE_API_KEY}",
            "Content-Type": "application/octet-stream",
            "Accept": "application/json"
        })
        with urlopen(req, timeout=timeout) as resp:
            body = resp.read()
        try:
            data = json.loads(body)
        except Exception:
            return ""
        if isinstance(data, list) and data:
            if isinstance(data[0], dict) and "generated_text" in data[0]:
                return str(data[0].get("generated_text") or "").strip()
            labels = []
            for it in data[:3]:
                lbl = str(it.get("label") or "").strip()
                if lbl:
                    labels.append(lbl)
            return ", ".join(labels)
        if isinstance(data, dict):
            txt = str(data.get("generated_text") or "").strip()
            if txt:
                return txt
        return ""
    except Exception:
        return ""

def get_recent_posts_context(limit=3):
    """Retrieves the last few posts to provide trend context for AI analysis."""
    try:
        if os.path.exists(ALERTS_FILE):
            with open(ALERTS_FILE, "r") as f:
                alerts = json.load(f)
                # Get the content of the most recent posts
                recent = [f"- {a['content']}" for a in alerts[:limit]]
                return "\n".join(recent) if recent else "No recent posts available."
    except Exception:
        return "No recent posts available."
    return "No recent posts available."

def analyze_with_ai(post_content, media=None):
    """
    Analyzes the post content using DeepSeek model via SiliconFlow API.
    Returns a dictionary with analysis results.
    """
    if not SILICONFLOW_API_KEY:
        return {
            "error": "Missing SILICONFLOW_API_KEY environment variable.",
            "impact": False,
            "summary": "AI Analysis disabled (No API Key)."
        }

    media_context = "No media attached."
    caption_text = ""
    try:
        arr = media or []
        if arr:
            lines = []
            caps = []
            for i, m in enumerate(arr[:3]):
                u = m.get("preview_url") or m.get("url") or ""
                t = (m.get("type") or "").lower()
                label = "video" if t in ("video", "gifv") else "image"
                cap = hf_caption_image(u) if u else ""
                if not cap:
                    d = (m.get("description") or "").strip()
                    cap = d if d else label
                lines.append(f"[{i+1}] ({label}) {cap} | {u}")
                if cap:
                    caps.append(cap)
            media_context = "\n".join(lines)
            caption_text = " ".join(caps)
    except Exception:
        media_context = "No media attached."
        caption_text = ""

    client = OpenAI(
        api_key=SILICONFLOW_API_KEY,
        base_url=BASE_URL
    )

    combined_text = (post_content or "").strip()
    if caption_text and combined_text:
        combined_text = combined_text + " [Media Interpretation] " + caption_text
    elif caption_text and not combined_text:
        combined_text = caption_text

    # Fetch external context (always call with the combined text)
    external_context = fetch_external_context(combined_text)
    
    # Fetch recent posts context (for trend analysis)
    recent_posts_context = get_recent_posts_context(limit=5)

    prompt = f"""
    You are a senior Wall Street financial analyst (Hedge Fund level). Analyze the following social media post by Donald Trump.
    
    **OBJECTIVES:**
    1. **Trend Analysis**: Use the "Recent Trump Posts" provided below to detect developing narratives (e.g., escalating attacks on a company, sustained crypto pumping).
    2. **Specific Actionable Alpha**: Do NOT limit recommendations to generic ETFs (like SPY/XLK). You MUST recommend **specific single-name stocks** (e.g., TSLA, DJT, NVDA, XOM, COIN, GEO) if there is a logical thesis.
    
    **INPUT DATA:**
    ---
    [Real-time External News/Context]
    {external_context}
    
    [Recent Trump Posts (For Trend Context)]
    {recent_posts_context}
    
    [Attached Media]
    {media_context}
    ---
    
    **CURRENT POST:**
    "{combined_text}"
    
    **RESPONSE FORMAT (JSON ONLY):**
    {{
        "impact": boolean, // true if it likely affects the market
        "reasoning": "string", // Concise thesis. Mention if this reinforces a recent trend from history. (max 50 words)
        "affected_assets": ["list", "of", "tickers"], // Mix of Stocks & ETFs. E.g. ["TSLA", "RIVN", "KARS"]
        "sentiment": "positive" | "negative" | "neutral",
        "recommendation": "string" // ACTIONABLE. E.g., "Buy TSLA", "Short F", "Buy COIN", "Sell DIS". "None" if no clear trade.
    }}
    """

    try:
        response = client.chat.completions.create(
            model="deepseek-ai/DeepSeek-V3", 
            messages=[
                {"role": "system", "content": "You are a helpful financial assistant. You output valid JSON only."},
                {"role": "user", "content": prompt}
            ],
            response_format={"type": "json_object"},
            temperature=0.3,
            max_tokens=500
        )
        
        result_text = response.choices[0].message.content
        result_json = json.loads(result_text)
        
        # Inject external context summary into the result for transparency
        context_preview = "No external news found."
        if "No related external news found" not in external_context:
            match = re.search(r'\[News\] (.*?):', external_context)
            if match:
                context_preview = f"News Context: {match.group(1)}..."
            else:
                context_preview = "External market data used."
        
        result_json['external_context_used'] = context_preview
        result_json['media_used'] = bool(media_context and media_context != "No media attached.")
        result_json['media_caption_used'] = bool(caption_text)
        
        return result_json

    except Exception as e:
        print(f"AI Analysis Failed: {e}")
        return {
            "error": str(e),
            "impact": False,
            "summary": "AI Analysis Failed."
        }

# ==========================================
# MONITORING FUNCTIONS
# ==========================================

def fetch_json_with_retries(url, headers, timeout=15, retries=3, backoff=2):
    last_err = None
    for i in range(int(retries)):
        try:
            req = Request(url, headers=headers)
            with urlopen(req, timeout=timeout) as resp:
                body = resp.read()
                return json.loads(body)
        except Exception as e:
            last_err = e
            time.sleep(backoff * (i + 1))
    raise last_err

def fetch_public_socks5(limit=12):
    items = []
    srcs = [
        "https://www.proxy-list.download/api/v1/get?type=socks5",
        "https://api.proxyscrape.com/v2/?request=getproxies&protocol=socks5&timeout=2000&country=all&simplified=true",
    ]
    for s in srcs:
        try:
            with urlopen(Request(s, headers={"User-Agent":"Mozilla/5.0"}), timeout=10) as r:
                body = r.read().decode("utf-8", errors="ignore")
            for line in body.splitlines():
                p = line.strip()
                if p and ":" in p:
                    items.append(p)
        except Exception:
            continue
    out = []
    seen = set()
    for p in items:
        if p not in seen:
            seen.add(p)
            out.append(p)
    return out[:int(limit)]

def socks5_connect(proxy, host, port, timeout=12, use_ssl=False):
    ph, pp = proxy.split(":")
    pp = int(pp)
    s = socket.create_connection((ph, pp), timeout=timeout)
    s.sendall(b"\x05\x01\x00")
    msel = s.recv(2)
    dest = host.encode("utf-8")
    req = b"\x05\x01\x00\x03" + bytes([len(dest)]) + dest + port.to_bytes(2, "big")
    s.sendall(req)
    resp = s.recv(10)
    if use_ssl:
        ctx = ssl.create_default_context()
        s = ctx.wrap_socket(s, server_hostname=host)
    return s

def http_get_via_socks5(url, headers, proxy, timeout=20):
    u = urlsplit(url)
    host = u.hostname
    port = u.port or (443 if u.scheme == "https" else 80)
    path = u.path or "/"
    if u.query:
        path = path + "?" + u.query
    use_ssl = u.scheme == "https"
    s = socks5_connect(proxy, host, port, timeout=timeout, use_ssl=use_ssl)
    req = f"GET {path} HTTP/1.1\r\nHost: {host}\r\nConnection: close\r\n" + "\r\n".join([f"{k}: {v}" for k,v in headers.items()]) + "\r\n\r\n"
    s.sendall(req.encode("utf-8"))
    chunks = []
    while True:
        data = s.recv(4096)
        if not data:
            break
        chunks.append(data)
    s.close()
    raw = b"".join(chunks)
    sep = raw.find(b"\r\n\r\n")
    body = raw[sep+4:] if sep != -1 else raw
    try:
        txt = body.decode("utf-8")
        if txt.startswith("0\r\n") or "\r\n" in txt.splitlines()[0]:
            pass
    except Exception:
        pass
    return json.loads(body)

def fetch_json_via_http_proxy(url, headers, proxy_url, timeout=15):
    ph = ProxyHandler({"http": proxy_url, "https": proxy_url})
    op = build_opener(ph)
    req = Request(url, headers=headers)
    with op.open(req, timeout=timeout) as resp:
        body = resp.read()
        return json.loads(body)

def http_proxy_check_country(proxy_url, expect=SOCKS5_COUNTRY, timeout=5):
    try:
        ph = ProxyHandler({"http": proxy_url, "https": proxy_url})
        op = build_opener(ph)
        req = Request("http://ip-api.com/json/?fields=status,countryCode", headers={"User-Agent":"Mozilla/5.0"})
        with op.open(req, timeout=timeout) as resp:
            data = json.loads(resp.read())
        cc = str(data.get("countryCode") or "").upper()
        ok = str(data.get("status") or "") == "success" and cc == expect
        if ok:
            return True
    except Exception:
        pass
    try:
        ph = ProxyHandler({"http": proxy_url, "https": proxy_url})
        op = build_opener(ph)
        req = Request("https://ipinfo.io/json", headers={"User-Agent":"Mozilla/5.0"})
        with op.open(req, timeout=timeout) as resp:
            data = json.loads(resp.read())
        cc = str(data.get("country") or "").upper()
        return cc == expect
    except Exception:
        return False

def fetch_public_http(limit=20):
    srcs = [
        "https://www.proxy-list.download/api/v1/get?type=http",
        "https://api.proxyscrape.com/?request=displayproxies&proxytype=http&timeout=2000&country=all&ssl=all&anonymity=all",
    ]
    items = []
    for s in srcs:
        try:
            with urlopen(Request(s, headers={"User-Agent":"Mozilla/5.0"}), timeout=10) as r:
                body = r.read().decode("utf-8", errors="ignore")
            for line in body.splitlines():
                p = line.strip()
                if p and ":" in p:
                    items.append(p)
        except Exception:
            continue
    out = []
    seen = set()
    for p in items:
        if p not in seen:
            seen.add(p)
            out.append(p)
    return out[:int(limit)]

def fetch_public_https(limit=20):
    srcs = [
        "https://www.proxy-list.download/api/v1/get?type=https",
    ]
    items = []
    for s in srcs:
        try:
            with urlopen(Request(s, headers={"User-Agent":"Mozilla/5.0"}), timeout=10) as r:
                body = r.read().decode("utf-8", errors="ignore")
            for line in body.splitlines():
                p = line.strip()
                if p and ":" in p:
                    items.append(p)
        except Exception:
            continue
    out = []
    seen = set()
    for p in items:
        if p not in seen:
            seen.add(p)
            out.append(p)
    return out[:int(limit)]

def socks5_check_country(proxy, expect=SOCKS5_COUNTRY, timeout=4):
    try:
        data = http_get_via_socks5("http://ip-api.com/json/?fields=status,countryCode", {"User-Agent":"Mozilla/5.0"}, proxy, timeout=timeout)
        cc = str(data.get("countryCode") or "").upper()
        ok = str(data.get("status") or "") == "success" and cc == expect
        if ok:
            return True
    except Exception:
        pass
    try:
        data = http_get_via_socks5("https://ipinfo.io/json", {"User-Agent":"Mozilla/5.0"}, proxy, timeout=timeout)
        cc = str(data.get("country") or "").upper()
        return cc == expect
    except Exception:
        return False

def get_candidate_socks5(limit=6):
    cand = fetch_public_socks5(limit=limit*10)
    want = int(limit)
    out = []
    max_workers = min(len(cand), 32) or 1
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futs = {ex.submit(socks5_check_country, p, SOCKS5_COUNTRY): p for p in cand}
        for fut in as_completed(futs):
            p = futs[fut]
            ok = False
            try:
                ok = bool(fut.result())
            except Exception:
                ok = False
            if ok:
                out.append(p)
                if len(out) >= want:
                    break
    return out

def race_fetch_json_via_socks5(url, headers, proxies, timeout=16):
    if not proxies:
        return None
    res = [None]
    def attempt(px):
        try:
            data = http_get_via_socks5(url, headers, proxy=px, timeout=timeout)
            if isinstance(data, list) and not res[0]:
                res[0] = data
        except Exception:
            pass
    max_workers = min(len(proxies), 16) or 1
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futs = [ex.submit(attempt, p) for p in proxies]
        for _ in as_completed(futs):
            if res[0]:
                break
    return res[0]

def get_candidate_http(limit=6):
    cand = fetch_public_http(limit=limit*5)
    want = int(limit)
    out = []
    max_workers = min(len(cand), 24) or 1
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futs = {ex.submit(http_proxy_check_country, f"http://{p}", SOCKS5_COUNTRY): p for p in cand}
        for fut in as_completed(futs):
            p = futs[fut]
            ok = False
            try:
                ok = bool(fut.result())
            except Exception:
                ok = False
            if ok:
                out.append(f"http://{p}")
                if len(out) >= want:
                    break
    return out

def get_candidate_https(limit=6):
    cand = fetch_public_https(limit=limit*5)
    want = int(limit)
    out = []
    max_workers = min(len(cand), 24) or 1
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futs = {ex.submit(http_proxy_check_country, f"http://{p}", SOCKS5_COUNTRY): p for p in cand}
        for fut in as_completed(futs):
            p = futs[fut]
            ok = False
            try:
                ok = bool(fut.result())
            except Exception:
                ok = False
            if ok:
                out.append(f"http://{p}")
                if len(out) >= want:
                    break
    return out

def refresh_proxy_pool():
    now = int(time.time())
    if (now - int(PROXY_POOL.get("last_refresh") or 0)) < int(PROXY_POOL_TTL) and PROXY_POOL.get("healthy"):
        return
    http_list = get_candidate_http(limit=6)
    https_list = get_candidate_https(limit=6)
    socks_list = get_candidate_socks5(limit=6)
    healthy = []
    for px in http_list:
        healthy.append({"type": "http", "addr": px})
    for px in https_list:
        healthy.append({"type": "https", "addr": px})
    for px in socks_list:
        healthy.append({"type": "socks5", "addr": px})
    PROXY_POOL["healthy"] = healthy
    PROXY_POOL["last_refresh"] = now

def race_fetch_json_via_pool(url, headers, timeout=16):
    if not PROXY_POOL.get("healthy"):
        refresh_proxy_pool()
    proxies = list(PROXY_POOL.get("healthy") or [])
    if not proxies:
        return None
    res = [None]
    def attempt(p):
        try:
            if p["type"] in ("http", "https"):
                data = fetch_json_via_http_proxy(url, headers, p["addr"], timeout=timeout)
            else:
                data = http_get_via_socks5(url, headers, proxy=p["addr"], timeout=timeout)
            if isinstance(data, list) and not res[0]:
                res[0] = data
        except Exception:
            pass
    max_workers = min(len(proxies), 16) or 1
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futs = [ex.submit(attempt, p) for p in proxies]
        for _ in as_completed(futs):
            if res[0]:
                break
    return res[0]

def load_processed_posts():
    """
    Loads the set of processed post IDs from the local JSON file.
    """
    if os.path.exists(PROCESSED_LOG_FILE):
        try:
            with open(PROCESSED_LOG_FILE, "r") as f:
                return set(json.load(f))
        except:
            return set()
    return set()

def save_processed_posts(processed_ids):
    with open(PROCESSED_LOG_FILE, "w") as f:
        json.dump(list(processed_ids), f)

def save_alert(post, keywords, ai_analysis=None, source=None):
    """
    Saves an alert to a JSON file for the dashboard to read.
    """
    created_raw = post.get("createdAt") or post.get("created_at")
    try:
        if created_raw:
            s = str(created_raw).replace('Z', '+00:00')
            dt = datetime.fromisoformat(s)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            created_iso = dt.astimezone(timezone.utc).isoformat()
        else:
            created_iso = datetime.now(timezone.utc).isoformat()
    except Exception:
        created_iso = datetime.now(timezone.utc).isoformat()

    atts = post.get("media_attachments") or post.get("media") or []
    media = []
    try:
        for m in atts:
            mt = str(m.get("type", "")).lower()
            mu = m.get("url") or m.get("remote_url") or m.get("preview_url")
            if mu and (not mt or mt in ("image", "gifv", "video")):
                media.append({
                    "url": mu,
                    "preview_url": m.get("preview_url") or mu,
                    "description": m.get("description") or "",
                    "type": mt or "image"
                })
    except Exception:
        media = []

    _content = post.get("content") or post.get("text", "") or ""
    if (not str(_content).strip()) and media:
        try:
            _descs = [d.get("description") for d in media if str(d.get("description", "")).strip()]
            if _descs:
                _content = " ".join(_descs)
            else:
                vc = sum(1 for x in media if (x.get("type") or "").lower() in ("video", "gifv"))
                ic = len(media) - vc
                if vc > 0:
                    _content = f"[视频] {vc} 个"
                else:
                    _content = f"[图片] {ic} 张"
        except Exception:
            vc = sum(1 for x in media if (x.get("type") or "").lower() in ("video", "gifv"))
            ic = len(media) - vc
            _content = f"[视频] {vc} 个" if vc > 0 else f"[图片] {ic} 张"

    alert_data = {
        "id": post.get("id"),
        "created_at": created_iso,
        "content": _content,
        "url": post.get("url", "https://truthsocial.com/@realDonaldTrump"),
        "media": media,
        "keywords": keywords,
        "ai_analysis": ai_analysis,
        "detected_at": datetime.now(timezone.utc).isoformat(),
        "source": source or ("simulated" if str(post.get("id", "")).startswith("simulated") else "real")
    }

    alerts = []
    if os.path.exists(ALERTS_FILE):
        try:
            with open(ALERTS_FILE, "r") as f:
                alerts = json.load(f)
        except:
            alerts = []
    
    # Add new alert to the beginning
    alerts.insert(0, alert_data)
    
    # Keep only last 100 alerts
    alerts = alerts[:100]

    with open(ALERTS_FILE, "w") as f:
        json.dump(alerts, f, indent=2, ensure_ascii=False)
    
    print(f"Alert saved to {ALERTS_FILE}")

 

def generate_simulated_post():
    return {
        "content": "",
        "id": f"simulated_{int(time.time())}",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "url": "https://truthsocial.com/@realDonaldTrump"
    }


def run_fetch_recent(limit=20, fast_init=False, allow_proxy=True):
    if TRUTH_COOKIE and TRUTH_ACCOUNT_ID and str(TRUTH_ACCOUNT_ID).isdigit():
        try:
            url = f"https://truthsocial.com/api/v1/accounts/{TRUTH_ACCOUNT_ID}/statuses?exclude_replies=true&with_muted=true&limit={int(limit)}"
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
                "Accept": "application/json, text/plain, */*",
                "Accept-Language": "en-US,en;q=0.9",
                "Origin": "https://truthsocial.com",
                "Referer": f"https://truthsocial.com/@{TRUTH_USERNAME}",
                "Cookie": TRUTH_COOKIE,
            }
            print(f"CookieAPI request: {url}")
            try:
                items = fetch_json_with_retries(
                    url,
                    headers,
                    timeout=(8 if fast_init else 15),
                    retries=(1 if fast_init else 2),
                    backoff=(1 if fast_init else 2)
                )
            except Exception as e:
                print(f"CookieAPI primary failed: {e}")
                try:
                    url2 = f"https://truthsocial.com/api/v1/accounts/{TRUTH_ACCOUNT_ID}/statuses?exclude_replies=true&with_muted=true&limit={min(5, int(limit))}"
                    print(f"CookieAPI retry request: {url2}")
                    items = fetch_json_with_retries(
                        url2,
                        headers,
                        timeout=(12 if fast_init else 25),
                        retries=(1 if fast_init else 2),
                        backoff=(2 if fast_init else 3)
                    )
                except Exception as e2:
                    print(f"CookieAPI fallback failed: {e2}")
                    if allow_proxy:
                        try:
                            refresh_proxy_pool()
                            items = race_fetch_json_via_pool(url, headers, timeout=(10 if fast_init else 16))
                            if not isinstance(items, list):
                                return 0
                        except Exception as pe2:
                            print(f"Proxy pipeline error: {pe2}")
                            return 0
                    else:
                        return 0
            print(f"CookieAPI fetched items: {len(items) if isinstance(items, list) else 0}")
            processed_ids = load_processed_posts()
            new_posts_count = 0
            alerts_empty = True
            try:
                if os.path.exists(ALERTS_FILE):
                    with open(ALERTS_FILE, "r") as f:
                        arr = json.load(f)
                        alerts_empty = not bool(arr)
            except Exception:
                alerts_empty = True
            print(f"CookieAPI alerts_empty={alerts_empty} processed_ids={len(processed_ids)}")
            for post in items or []:
                post_id = str(post.get("id") or "").strip()
                content_html = post.get("content") or ""
                content = re.sub(r"<[^>]+>", " ", content_html)
                content = re.sub(r"\s+", " ", content).strip()
                media_atts = post.get("media_attachments", [])
                if not content and media_atts:
                    try:
                        descs = [str(m.get("description") or "").strip() for m in media_atts if str(m.get("description") or "").strip()]
                        if descs:
                            content = " ".join(descs)
                        else:
                            content = f"[图片] {len(media_atts)} 张"
                    except Exception:
                        content = f"[图片] {len(media_atts)} 张"
                keywords = extract_keywords(content)
                ai_result = analyze_with_ai(content, media=media_atts)
                created_iso = post.get("created_at") or datetime.now(timezone.utc).isoformat()
                url = post.get("url") or "https://truthsocial.com/@realDonaldTrump"
                if alerts_empty:
                    save_alert({"id": post_id or f"api_{int(time.time())}", "content": content, "created_at": created_iso, "url": url, "media_attachments": media_atts}, keywords, ai_result, source="real")
                    if post_id:
                        processed_ids.add(post_id)
                    new_posts_count += 1
                else:
                    if post_id and post_id not in processed_ids:
                        save_alert({"id": post_id, "content": content, "created_at": created_iso, "url": url, "media_attachments": media_atts}, keywords, ai_result, source="real")
                        processed_ids.add(post_id)
                        new_posts_count += 1
            save_processed_posts(processed_ids)
            print(f"CookieAPI wrote alerts: {new_posts_count}")
            return new_posts_count
        except Exception as e:
            print(f"CookieAPI error: {e}")
    return 0

def purge_simulated_alerts():
    if not os.path.exists(ALERTS_FILE):
        return 0
    try:
        with open(ALERTS_FILE, "r") as f:
            data = json.load(f)
        filtered = [a for a in data if str(a.get("source", "real")) != "simulated"]
        removed = len(data) - len(filtered)
        with open(ALERTS_FILE, "w") as f:
            json.dump(filtered, f, indent=2, ensure_ascii=False)
        return removed
    except Exception:
        return 0

 
