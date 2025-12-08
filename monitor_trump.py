import time
import os
import json
import re
import random
from datetime import datetime, timedelta, timezone
from apify_client import ApifyClient
from apify_client.errors import ApifyApiError
from openai import OpenAI
from ddgs import DDGS

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

# Apify Configuration
APIFY_TOKEN = os.getenv("APIFY_TOKEN") 

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

# Templates for simulated posts (Rich Dataset)
POST_TEMPLATES = [
    # Market / Crypto / Economy (High Impact)
    {
        "content": "Bitcoin is going to the MOON! We will make America the Crypto Capital of the Planet. Digital Dollar is NOT happening!",
        "impact": True, "assets": ["BTC", "COIN", "MSTR"], "rec": "Buy BTC", "sentiment": "positive",
        "reasoning": "Strong endorsement of Bitcoin and rejection of CBDC suggests favorable regulatory environment for crypto assets."
    },
    {
        "content": "Big Oil is back! We are opening up the pipelines. Energy costs will drop by 50% in my first year. Drill, Baby, Drill!",
        "impact": True, "assets": ["XLE", "XOM", "CVX"], "rec": "Buy XLE", "sentiment": "positive",
        "reasoning": "Deregulation promises for oil sector likely to boost energy stocks and reduce operational costs for majors."
    },
    {
        "content": "Tariffs on foreign cars will be HUGE if they don't build plants here. protect our Auto Workers! America First!",
        "impact": True, "assets": ["TM", "HMC", "F", "GM"], "rec": "Sell TM", "sentiment": "negative",
        "reasoning": "Threat of tariffs on imported vehicles negatively impacts foreign automakers while potentially shielding domestic manufacturers."
    },
    {
        "content": "The Federal Reserve needs to lower rates NOW. Our businesses are dying with these high rates. Powell must act!",
        "impact": True, "assets": ["SPY", "TLT", "QQQ"], "rec": "Buy TLT", "sentiment": "positive",
        "reasoning": "Pressure on Fed for rate cuts typically boosts bond prices and equity valuations, signaling potential monetary easing."
    },
    {
        "content": "China trade deal phase 2 is looking very good. They want to buy our agricultural products like never before!",
        "impact": True, "assets": ["DE", "ADM", "Soybeans"], "rec": "Buy DE", "sentiment": "positive",
        "reasoning": "Potential trade deal focused on agriculture export would directly benefit farm equipment and ag-commodity sectors."
    },

    # Politics / Tech / General (Mixed Impact)
    {
        "content": "Social Media companies are censoring conservatives. We need to repeal Section 230 immediately! They are out of control.",
        "impact": True, "assets": ["META", "GOOGL", "SNAP"], "rec": "Sell META", "sentiment": "negative",
        "reasoning": "Legislative threat to Section 230 poses significant regulatory risk to ad-driven social media platforms."
    },
    {
        "content": "My poll numbers are higher than any President in history. The people know the truth. We are winning everywhere!",
        "impact": False, "assets": [], "rec": "None", "sentiment": "neutral",
        "reasoning": "Standard political rhetoric with no direct economic policy implication."
    },
    {
        "content": "Just landed in Florida. Beautiful crowd at the airport. Thank you for the support!",
        "impact": False, "assets": [], "rec": "None", "sentiment": "positive",
        "reasoning": "Personal update, no market relevance."
    },
    {
        "content": "The Border is a disaster. We need to close it down and finish the Wall. National Security is priority #1.",
        "impact": False, "assets": ["GEO", "CXW"], "rec": "None", "sentiment": "neutral",
        "reasoning": "Border policy reiteration; potential long-term impact on private prisons/defense but no immediate market trigger."
    },
    {
        "content": "Fake News CNN is at it again. Their ratings are in the toilet. Nobody watches them!",
        "impact": False, "assets": ["WBD"], "rec": "None", "sentiment": "negative",
        "reasoning": "Media criticism, typical behavior, negligible market impact."
    },
    
    # More Filler
    {"content": "Make America Great Again!", "impact": False},
    {"content": "The radical left is destroying our country.", "impact": False},
    {"content": "We will save the Auto Industry!", "impact": True, "assets": ["GM", "F"], "rec": "Buy GM", "reasoning": "General support for domestic auto industry."},
    {"content": "Thank you Iowa! A massive victory is coming.", "impact": False},
    {"content": "Inflation is killing the middle class. We will fix it.", "impact": False},
    {"content": "Peace through strength. No more endless wars.", "impact": True, "assets": ["LMT", "RTX"], "rec": "Hold LMT", "reasoning": "Shift in foreign policy may affect defense contracts volatility."},
    {"content": "Election integrity is vital.", "impact": False},
    {"content": "Meeting with world leaders next week. They respect America again.", "impact": False},
    {"content": "Taxes will be lower than ever. Corporate tax to 15%!", "impact": True, "assets": ["SPY", "IWM"], "rec": "Buy IWM", "reasoning": "Corporate tax cuts disproportionately benefit small caps (Russell 2000) and domestic earners."},
    {"content": "Have a great Sunday everyone!", "impact": False}
]

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

def analyze_with_ai(post_content):
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

    # Fetch external context
    external_context = fetch_external_context(post_content)
    
    # Fetch recent posts context (for trend analysis)
    recent_posts_context = get_recent_posts_context(limit=5)

    client = OpenAI(
        api_key=SILICONFLOW_API_KEY,
        base_url=BASE_URL
    )

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
    ---
    
    **CURRENT POST:**
    "{post_content}"
    
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
    alert_data = {
        "id": post.get("id"),
        "created_at": post.get("createdAt") or post.get("created_at") or datetime.now(timezone.utc).isoformat(),
        "content": post.get("content") or post.get("text", ""),
        "url": post.get("url", "https://truthsocial.com/@realDonaldTrump"),
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

def generate_history():
    return 0

def generate_simulated_post():
    return {
        "content": "",
        "id": f"simulated_{int(time.time())}",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "url": "https://truthsocial.com/@realDonaldTrump"
    }

def run_one_check():
    if not APIFY_TOKEN:
        return 0
    else:
        client = ApifyClient(APIFY_TOKEN)
        run_input = {"searchQueries": ["realDonaldTrump"], "resultsLimit": 5, "maxItems": 5}
        dataset_items = []
        try:
            run = client.actor("muhammetakkurtt/truth-social-scraper").call(run_input=run_input)
            if run:
                dataset_items = client.dataset(run["defaultDatasetId"]).iterate_items()
            else:
                dataset_items = [generate_simulated_post()]
        except ApifyApiError:
            dataset_items = []
        except Exception:
            dataset_items = []
    processed_ids = load_processed_posts()
    new_posts_count = 0
    for post in dataset_items:
        post_id = post.get("id")
        if post_id and post_id not in processed_ids:
            new_posts_count += 1
            content = post.get("content") or post.get("text", "")
            keywords = extract_keywords(content)
            ai_result = analyze_with_ai(content)
            save_alert(post, keywords, ai_result, source="real")
            processed_ids.add(post_id)
    save_processed_posts(processed_ids)
    return new_posts_count

def run_fetch_recent(limit=20):
    if not APIFY_TOKEN:
        return 0
    client = ApifyClient(APIFY_TOKEN)
    run_input = {"searchQueries": ["realDonaldTrump"], "resultsLimit": int(limit), "maxItems": int(limit)}
    dataset_items = []
    try:
        run = client.actor("muhammetakkurtt/truth-social-scraper").call(run_input=run_input)
        if run:
            dataset_items = client.dataset(run["defaultDatasetId"]).iterate_items()
    except ApifyApiError:
        dataset_items = []
    except Exception:
        dataset_items = []
    processed_ids = load_processed_posts()
    new_posts_count = 0
    for post in dataset_items:
        post_id = post.get("id")
        if post_id and post_id not in processed_ids:
            new_posts_count += 1
            content = post.get("content") or post.get("text", "")
            keywords = extract_keywords(content)
            ai_result = analyze_with_ai(content)
            save_alert(post, keywords, ai_result, source="real")
            processed_ids.add(post_id)
    save_processed_posts(processed_ids)
    return new_posts_count

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

def run_one_check_free():
    return 0
def run_monitoring_loop():
    return

if __name__ == "__main__":
    pass
