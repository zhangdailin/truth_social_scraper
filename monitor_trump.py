import time
import os
import json
import re
from datetime import datetime, timedelta, timezone
from openai import OpenAI
from ddgs import DDGS
from urllib.request import Request, urlopen

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


def run_fetch_recent(limit=20):
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
                items = fetch_json_with_retries(url, headers, timeout=15, retries=2, backoff=2)
            except Exception as e:
                print(f"CookieAPI primary failed: {e}")
                try:
                    url2 = f"https://truthsocial.com/api/v1/accounts/{TRUTH_ACCOUNT_ID}/statuses?exclude_replies=true&with_muted=true&limit={min(5, int(limit))}"
                    print(f"CookieAPI retry request: {url2}")
                    items = fetch_json_with_retries(url2, headers, timeout=25, retries=2, backoff=3)
                except Exception as e2:
                    print(f"CookieAPI fallback failed: {e2}")
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

 
