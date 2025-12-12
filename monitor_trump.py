import os
import json
import re
import time
from datetime import datetime, timezone
from openai import OpenAI
from urllib.request import Request, urlopen
from utils import (
    ALERTS_FILE,
    PROJECT_ROOT,
    derive_content,
    env_flag,
    extract_media,
    fetch_truth_posts,
    normalize_iso,
)

# ==========================================
# CONFIGURATION
# ==========================================
# Determine paths relative to repo root
PROCESSED_LOG_FILE = os.path.join(PROJECT_ROOT, "processed_posts.json")
# 保留的最大告警条数，None 表示不截断
MAX_ALERTS = None

# SiliconFlow API Configuration
SILICONFLOW_API_KEY = os.getenv("SILICONFLOW_API_KEY")
BASE_URL = "https://api.siliconflow.cn/v1"
 

HUGGINGFACE_API_KEY = os.getenv("HUGGINGFACE_API_KEY")
HUGGINGFACE_IMAGE_MODEL = os.getenv("HUGGINGFACE_IMAGE_MODEL", "Salesforce/blip-image-captioning-large")
HF_API_URL = "https://api-inference.huggingface.co/models"

# Truth Social configuration (cookie-based)
TRUTH_ACCOUNT_ID = os.getenv("TRUTH_ACCOUNT_ID", "107780257626128497")
TRUTH_COOKIE = os.getenv("TRUTH_COOKIE", "")
TRUTH_USERNAME = os.getenv("TRUTH_USERNAME", "realDonaldTrump")

# Feature toggles
ENABLE_AI_ANALYSIS = env_flag("ENABLE_AI_ANALYSIS", True)
ENABLE_REMOTE_FETCH = env_flag("ENABLE_REMOTE_FETCH", True)


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
    External context lookup via DuckDuckGo was removed; the feature is currently
    disabled. We still surface extracted keywords for transparency.
    """
    keywords = extract_keywords(query_text)
    if not keywords:
        keywords = query_text[:50]
    return f"External news lookup disabled. Extracted keywords: {keywords}"

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

def analyze_with_ai(post_content, media=None, retries=2, backoff=1.5):
    """
    Analyzes the post content using DeepSeek model via SiliconFlow API.
    Returns a dictionary with analysis results.
    """
    if not ENABLE_AI_ANALYSIS:
        return {
            "impact": False,
            "summary": "AI Analysis disabled via ENABLE_AI_ANALYSIS flag.",
            "recommendation": "None",
            "sentiment": "neutral",
            "affected_assets": [],
            "external_context_used": "Analysis disabled",
        }

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

    last_err = None
    for attempt in range(int(retries) + 1):
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
            last_err = e
            print(f"AI Analysis Failed attempt {attempt+1}/{int(retries)+1}: {e}")
            if attempt < int(retries):
                time.sleep(backoff * (attempt + 1))
                continue
            break

    return {
        "error": str(last_err),
        "impact": False,
        "summary": "AI Analysis Failed after retries."
    }

# ==========================================
# MONITORING FUNCTIONS
# ==========================================

def _alerts_file_empty():
    """Check whether the alerts store has any records."""
    try:
        if os.path.exists(ALERTS_FILE):
            with open(ALERTS_FILE, "r") as f:
                arr = json.load(f)
                return not bool(arr)
    except Exception:
        pass
    return True

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
    media = extract_media(atts)
    _content = derive_content(post, atts)

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
    
    # Keep only last N alerts if MAX_ALERTS is set
    if MAX_ALERTS:
        alerts = alerts[:int(MAX_ALERTS)]

    with open(ALERTS_FILE, "w") as f:
        json.dump(alerts, f, indent=2, ensure_ascii=False)
    
    print(f"Alert saved to {ALERTS_FILE}")

def run_fetch_recent(limit=20, fast_init=False):
    if not ENABLE_REMOTE_FETCH:
        print("Remote fetch disabled via ENABLE_REMOTE_FETCH flag.")
        return 0
    if not (TRUTH_COOKIE and TRUTH_ACCOUNT_ID and str(TRUTH_ACCOUNT_ID).isdigit()):
        return 0

    items = fetch_truth_posts(
        TRUTH_ACCOUNT_ID,
        TRUTH_USERNAME,
        TRUTH_COOKIE,
        limit=limit,
        fast_init=fast_init,
    )
    print(f"CookieAPI fetched items: {len(items) if isinstance(items, list) else 0}")
    
    processed_ids = load_processed_posts()
    alerts_empty = _alerts_file_empty()
    new_posts_count = 0
    print(f"CookieAPI alerts_empty={alerts_empty} processed_ids={len(processed_ids)}")

    # 按创建时间倒序，确保先处理最新的帖子
    def _created_ts(p):
        ts = p.get("created_at") or p.get("createdAt") or ""
        try:
            return datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        except Exception:
            return datetime.min

    sorted_items = sorted(items or [], key=_created_ts, reverse=True)

    for post in sorted_items:
        post_id = str(post.get("id") or "").strip()
        media_atts = post.get("media_attachments", [])
        media = extract_media(media_atts)
        content = derive_content(post, media_atts)
        keywords = extract_keywords(content)
        ai_result = analyze_with_ai(content, media=media_atts)
        created_iso = normalize_iso(post.get("created_at"))
        url = post.get("url") or "https://truthsocial.com/@realDonaldTrump"

        if alerts_empty or (post_id and post_id not in processed_ids):
            save_alert(
                {
                    "id": post_id or f"api_{int(datetime.now(timezone.utc).timestamp())}",
                    "content": content,
                    "created_at": created_iso,
                    "url": url,
                    "media_attachments": media_atts
                },
                keywords,
                ai_result,
                source="real"
            )
            if post_id:
                processed_ids.add(post_id)
            new_posts_count += 1

    save_processed_posts(processed_ids)
    print(f"CookieAPI wrote alerts: {new_posts_count}")
    return new_posts_count

 
