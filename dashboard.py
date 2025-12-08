import streamlit as st
import json
import time
import os
import pandas as pd
import re
from datetime import datetime, timezone

# ==========================================
# 1. PAGE CONFIGURATION
# ==========================================
st.set_page_config(
    page_title="Trump Truth Social Monitor",
    page_icon="🦅",
    layout="wide",
    initial_sidebar_state="collapsed"
)

# ==========================================
# 2. CUSTOM CSS STYLING
# ==========================================
st.markdown("""
<style>
    html, body, [class*="css"] {
        font-family: system-ui, -apple-system, "Segoe UI", Roboto, Ubuntu, Cantarell, "Noto Sans", Helvetica, Arial, sans-serif;
    }
    
    /* Reduce top padding to minimize empty space */
    .block-container {
        padding-top: 3.5rem !important;
        padding-bottom: 0rem !important;
        max-width: 95% !important;
    }
    
    /* Compact header */
    h1 {
        padding-top: 0rem !important;
        margin-bottom: 0.5rem !important;
    }
    
    h1, h2, h3 {
        font-weight: 600;
        color: #1E293B;
    }

    hr {
        margin: 6px 0 !important;
        border-top: 1px solid #E2E8F0 !important;
    }

    /* Metric Cards */
    .metric-container {
        background-color: #FFFFFF;
        border: 1px solid #E2E8F0;
        border-radius: 8px;
        padding: 16px;
        text-align: center;
        box-shadow: 0 1px 2px rgba(0,0,0,0.05);
    }
    .metric-value {
        font-size: 24px;
        font-weight: 700;
        color: #0F172A;
    }
    .metric-label {
        font-size: 12px;
        color: #64748B;
        text-transform: uppercase;
        letter-spacing: 0.5px;
    }

    /* Hero Card (Latest Post) */
    .hero-card {
        background: linear-gradient(135deg, #F8FAFC 0%, #EFF6FF 100%);
        border: 1px solid #CBD5E1;
        border-radius: 12px;
        padding: 24px;
        margin-bottom: 24px;
        box-shadow: 0 4px 6px -1px rgba(0, 0, 0, 0.1);
    }
    .hero-alert-high {
        border-left: 6px solid #EF4444;
    }
    .hero-alert-low {
        border-left: 6px solid #10B981;
    }
    
    .post-content {
        font-family: 'Georgia', serif;
        font-size: 20px;
        line-height: 1.5;
        color: #334155;
        margin-bottom: 16px;
    }
    
    /* Feed Item */
    .feed-item {
        background-color: white;
        border-radius: 8px;
        padding: 16px;
        margin-bottom: 12px;
        border: 1px solid #F1F5F9;
        transition: transform 0.2s;
    }
    .feed-item:hover {
        background-color: #F8FAFC;
        border-color: #E2E8F0;
    }
    
    /* Tags */
    .tag {
        display: inline-block;
        padding: 2px 8px;
        border-radius: 4px;
        font-size: 11px;
        font-weight: 600;
        margin-right: 6px;
    }
    .tag-red { background-color: #FEF2F2; color: #DC2626; border: 1px solid #FECACA; }
    .tag-green { background-color: #ECFDF5; color: #059669; border: 1px solid #A7F3D0; }
    .tag-gray { background-color: #F1F5F9; color: #475569; border: 1px solid #E2E8F0; }

    /* Stock Tooltip */
    .stock-tooltip {
        position: relative;
        display: inline-block;
        border-bottom: 2px dashed #F59E0B;
        cursor: help;
        font-weight: bold;
    }
    
    .stock-tooltip .tooltip-content {
        visibility: hidden;
        width: 500px;
        height: auto;
        background-color: #ffffff;
        text-align: center;
        border-radius: 8px;
        padding: 8px;
        position: absolute;
        z-index: 99999;
        top: 130%;
        left: 50%;
        margin-left: -250px;
        opacity: 0;
        transition: opacity 0.2s;
        box-shadow: 0 10px 15px -3px rgba(0, 0, 0, 0.1), 0 4px 6px -2px rgba(0, 0, 0, 0.05);
        border: 1px solid #E2E8F0;
    }
    
    .stock-tooltip:hover .tooltip-content {
        visibility: visible;
        opacity: 1;
    }
    
    .tooltip-image {
        width: 100%;
        height: auto;
        border-radius: 4px;
    }
    
    .stock-tooltip .tooltip-arrow {
        position: absolute;
        bottom: 100%;
        left: 50%;
        margin-left: -5px;
        border-width: 5px;
        border-style: solid;
        border-color: transparent transparent #E2E8F0 transparent;
    }

</style>
""", unsafe_allow_html=True)

# ==========================================
# 3. DATA LOADING
# ==========================================
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(SCRIPT_DIR)
ALERTS_FILE = os.path.join(PROJECT_ROOT, "market_alerts.json")

# Base64 encoded alarm sound (short beep)
ALARM_AUDIO_BASE64 = "data:audio/mpeg;base64,/+MYxAAEaAIEeUAQAgBgNgP/////KQQ/////Lvrg+lcWYHgtjadzsbTq+yREu495tq9c6v/7vt/of7mna9v6/btUnU17Jun9/+MYxCkT26KW+YGBAj9v6vUh+zab//v/96C3/pu6H+pv//r/ycIIP4pcWWTRBBBAMXgNdbRaABQAAABRWKwgjQVX0ECmrb///+MYxBQSM0sWWYI4A++Z/////////////0rOZ3MP//7H44QEgxgdvRVMXHZseL//540B4JAvMPEgaA4/0nHjxLhRgAoAYAgA/+MYxAYIAAJfGYEQAMAJAIAQMAwX936/q/tWtv/2f/+v//6v/+7qTEFNRTMuOTkuNVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV"

def get_chart_image_html(symbol):
    """Returns the HTML string for a Finviz chart image (Static, reliable in tooltips)."""
    # Handle Crypto for Finviz (needs USD suffix usually)
    check_symbol = symbol.upper()
    if check_symbol in ['BTC', 'ETH', 'DOGE', 'SOL', 'XRP', 'LTC']:
        check_symbol += "USD"
    
    # Finviz Chart URL
    # t=Symbol, ty=c (Candle), ta=0 (No TA), p=d (Daily), s=m (Medium size)
    image_url = f"https://finviz.com/chart.ashx?t={check_symbol}&ty=c&ta=0&p=d&s=m"
    
    return f"""<div style="background-color: white; padding: 4px;"><div style="font-size: 10px; color: #64748B; margin-bottom: 4px; text-align: left;">📊 {symbol} Daily Trend</div><img src="{image_url}" class="tooltip-image" alt="{symbol} Chart" onerror="this.style.display='none'; this.parentElement.innerHTML='Chart unavailable';"/></div>"""

def inject_stock_tooltips(text, assets):
    if not text or not assets:
        return text
    uniq = []
    seen = set()
    for a in assets:
        s = str(a).strip().upper()
        if s and s not in seen:
            seen.add(s)
            uniq.append(s)
    if not uniq:
        return text
    uniq.sort(key=len, reverse=True)
    pattern = r"\b(" + "|".join(map(re.escape, uniq)) + r")\b"
    def _repl(m):
        sym = m.group(1)
        return f"<span class=\"stock-tooltip\">{sym}<div class=\"tooltip-content\">{get_chart_image_html(sym)}</div><div class=\"tooltip-arrow\"></div></span>"
    return re.sub(pattern, _repl, text, flags=re.IGNORECASE)

def load_alerts():
    if not os.path.exists(ALERTS_FILE):
        return []
    try:
        with open(ALERTS_FILE, "r") as f:
            data = json.load(f)
            def _parse_ts(s):
                try:
                    s2 = (s or '').replace('Z', '+00:00')
                    dt = datetime.fromisoformat(s2)
                    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
                except Exception:
                    return datetime.min.replace(tzinfo=timezone.utc)
            def _pick_ts(a):
                return a.get('created_at') or a.get('createdAt') or a.get('detected_at') or ''
            data.sort(key=lambda x: _parse_ts(_pick_ts(x)), reverse=True)
            def _norm_content(a):
                c = (a.get('content', '') or '')
                c = re.sub(r'http\S+', '', c)
                c = re.sub(r'\s+', ' ', c).strip().lower()
                return c
            seen = set()
            deduped = []
            for a in data:
                key = _norm_content(a)
                if key and key in seen:
                    continue
                seen.add(key)
                deduped.append(a)
            return deduped
    except Exception:
        return []

# Helper: convert ISO timestamp to local browser-like timezone string
def to_local_str(iso_str):
    try:
        if not iso_str:
            return ""
        s = iso_str.replace('Z', '+00:00')
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone().strftime('%Y-%m-%d %H:%M')
    except Exception:
        return (iso_str or '')[:16].replace('T', ' ')

def local_tz_label():
    _now_local = datetime.now(timezone.utc).astimezone()
    return _now_local.strftime('UTC%z')

def pick_ts_str(alert):
    return alert.get('created_at') or alert.get('createdAt') or alert.get('detected_at') or ''

alerts = load_alerts()
try:
    from monitor_trump import purge_simulated_alerts
    removed = purge_simulated_alerts()
    if removed:
        alerts = load_alerts()
except Exception:
    pass
if not alerts:
    try:
        from monitor_trump import run_fetch_recent
        run_fetch_recent(limit=20)
        alerts = load_alerts()
    except Exception:
        alerts = []

if 'last_api_check' not in st.session_state:
    st.session_state['last_api_check'] = time.time()
if time.time() - float(st.session_state['last_api_check']) >= float(st.session_state.get('check_interval_seconds', 900)):
    try:
        from monitor_trump import run_one_check
        run_one_check()
    except Exception:
        pass
    finally:
        st.session_state['last_api_check'] = time.time()
        alerts = load_alerts()

# Initialize session state for audio alerts
if 'last_played_alert_id' not in st.session_state:
    st.session_state['last_played_alert_id'] = None

# Check for high impact alerts and play sound
if alerts:
    latest_alert = alerts[0]
    is_high_impact = latest_alert.get('ai_analysis', {}).get('impact', False)
    
    # Play sound if high impact and not yet played for this specific alert
    if is_high_impact and st.session_state['last_played_alert_id'] != latest_alert['id']:
        st.markdown(f"""
            <audio autoplay>
                <source src="{ALARM_AUDIO_BASE64}" type="audio/mpeg">
            </audio>
            """, unsafe_allow_html=True)
        st.toast("🚨 High Market Impact Alert Detected!", icon="🔊")
        st.session_state['last_played_alert_id'] = latest_alert['id']

# ==========================================
# 4. DASHBOARD HEADER & CONTROLS
# ==========================================

# Top Layout: Title (Left) + Controls (Right)
c_header, c_control = st.columns([0.75, 0.25])

with c_header:
    st.markdown("# 🦅 Trump Truth Social Monitor")
    st.markdown("Real-time surveillance of Truth Social posts with **AI-driven market impact analysis**.")
    st.caption("Powered by **DeepSeek-V3** via SiliconFlow")

with c_control:
    st.markdown("**⚙️ System Control**")
    refresh_rate = st.slider("Auto-refresh (sec)", 5, 60, 10)
    fetch_interval_min = st.slider("Fetch interval (min)", 5, 120, 30)
    st.session_state['check_interval_seconds'] = int(fetch_interval_min * 60)
    _now_local = datetime.now(timezone.utc).astimezone()
    _tz_label = local_tz_label()
    st.success(f"● Online | {_now_local.strftime('%H:%M:%S')} ({_tz_label})")

st.markdown("---")

# Metrics Grid
if alerts:
    latest = alerts[0]
    high_impact_count = sum(1 for a in alerts if a.get('ai_analysis', {}).get('impact'))
    
    c1, c2, c3, c4 = st.columns(4)
    
    with c1:
        st.markdown(f"""
        <div class="metric-container">
            <div class="metric-label">Monitored Posts</div>
            <div class="metric-value">{len(alerts)}</div>
        </div>
        """, unsafe_allow_html=True)
        
    with c2:
        st.markdown(f"""
        <div class="metric-container">
            <div class="metric-label">High Impact Alerts</div>
            <div class="metric-value" style="color: {'#EF4444' if high_impact_count > 0 else '#0F172A'}">{high_impact_count}</div>
        </div>
        """, unsafe_allow_html=True)
        
    with c3:
        sentiment = latest.get('ai_analysis', {}).get('sentiment', 'N/A').upper()
        color = "#10B981" if sentiment == "POSITIVE" else "#EF4444" if sentiment == "NEGATIVE" else "#64748B"
        st.markdown(f"""
        <div class="metric-container">
            <div class="metric-label">Latest Sentiment</div>
            <div class="metric-value" style="color: {color}">{sentiment}</div>
        </div>
        """, unsafe_allow_html=True)
        
    with c4:
        try:
            _lts = pick_ts_str(latest)
            _ts = datetime.fromisoformat(_lts.replace('Z','+00:00'))
            if _ts.tzinfo is None:
                _ts = _ts.replace(tzinfo=timezone.utc)
            _age_min2 = int((datetime.now(timezone.utc) - _ts.astimezone(timezone.utc)).total_seconds() / 60)
        except Exception:
            _age_min2 = 0
        st.markdown(f"""
        <div class="metric-container">
            <div class="metric-label">Latest Post Age</div>
            <div class="metric-value">{_age_min2} min</div>
        </div>
        """, unsafe_allow_html=True)

    st.markdown("<br>", unsafe_allow_html=True)
    latest_time_str = pick_ts_str(latest)
    try:
        ts = datetime.fromisoformat(latest_time_str.replace('Z','+00:00'))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        age_min = int((datetime.now(timezone.utc) - ts.astimezone(timezone.utc)).total_seconds() / 60)
        if age_min > 60:
            st.warning(f"Data age {age_min} min")
    except Exception:
        pass

    # HERO SECTION (Latest Post)
    latest_ai = latest.get('ai_analysis', {})
    is_high_impact = latest_ai.get('impact', False)
    impact_class = "hero-alert-high" if is_high_impact else "hero-alert-low"
    
    # Recommendation Logic
    recommendation = latest_ai.get('recommendation', 'None')
    rec_html = ""
    if recommendation and recommendation != "None":
        # Inject tooltips into recommendation text
        assets = latest_ai.get('affected_assets', [])
        try:
            recommendation_with_tooltips = inject_stock_tooltips(recommendation, assets)
        except Exception:
            recommendation_with_tooltips = recommendation
        
        rec_html = f"""<div style="margin-top:12px; padding:12px; background-color:#FEF3C7; border-left:4px solid #F59E0B; border-radius:4px;">
<strong style="color:#B45309;">💰 Trading Recommendation:</strong> 
<span style="color:#92400E; font-weight:600;">{recommendation_with_tooltips}</span>
</div>"""
    
    st.markdown(f"""<div class="hero-card {impact_class}">
<div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:12px;">
<span class="tag {'tag-red' if is_high_impact else 'tag-green'}">
{'🚨 HIGH MARKET IMPACT' if is_high_impact else '✅ LOW IMPACT'}
</span>
<span class="tag tag-gray">{'REAL' if latest.get('source','real')=='real' else 'SIMULATED'}</span>
<span style="color:#64748B; font-size:12px;">{to_local_str(pick_ts_str(latest))} ({local_tz_label()})</span>
</div>
<div class="post-content">“{latest.get('content','')}”</div>
{rec_html}
<div style="margin-top:16px; padding-top:16px; border-top:1px solid #E2E8F0;">
<div style="font-weight:600; font-size:14px; color:#475569; margin-bottom:4px;">🤖 AI Analyst Notes:</div>
<div style="color:#334155; font-size:14px; margin-bottom:8px;">{latest_ai.get('reasoning', 'Analysis pending...')}</div>
<div style="font-size:12px; color:#64748B; background-color:#F8FAFC; padding:8px; border-radius:4px; border:1px solid #E2E8F0;">
<strong>🔍 Context Checked:</strong> {latest_ai.get('external_context_used', 'No external context data available.').replace('News Context:', '').strip()}
</div>
</div>
</div>""", unsafe_allow_html=True)
    
    # FEED SECTION
    c_feed_title, c_feed_sort = st.columns([0.8, 0.2])
    with c_feed_title:
        st.subheader("📜 Recent Posts")
    
    # List Layout
    for alert in alerts[1:6]:
        ai = alert.get('ai_analysis', {})
        is_high = ai.get('impact', False)
        impact_class = "hero-alert-high" if is_high else "hero-alert-low"
        ts_disp = to_local_str(pick_ts_str(alert))
        rec = ai.get('recommendation', 'None')
        assets = ai.get('affected_assets', [])
        rec_html = ""
        if rec and rec != "None":
            try:
                rec_with_tooltips = inject_stock_tooltips(rec, assets)
            except Exception:
                rec_with_tooltips = rec
            rec_html = f"""<div style=\"margin-top:12px; padding:12px; background-color:#FEF3C7; border-left:4px solid #F59E0B; border-radius:4px;\">
<strong style=\"color:#B45309;\">💰 Trading Recommendation:</strong>
<span style=\"color:#92400E; font-weight:600;\">{rec_with_tooltips}</span>
</div>"""

        st.markdown(f"""<div class=\"hero-card {impact_class}\">
<div style=\"display:flex; justify-content:space-between; align-items:center; margin-bottom:12px;\">
<span class=\"tag {'tag-red' if is_high else 'tag-green'}\">{'🚨 HIGH MARKET IMPACT' if is_high else '✅ LOW IMPACT'}</span>
<span class=\"tag tag-gray\">{'REAL' if alert.get('source','real')=='real' else 'SIMULATED'}</span>
<span style=\"color:#64748B; font-size:12px;\">{ts_disp} ({local_tz_label()})</span>
</div>
<div class=\"post-content\">“{alert.get('content','')}”</div>
{rec_html}
<div style=\"margin-top:16px; padding-top:16px; border-top:1px solid #E2E8F0;\">
<div style=\"font-weight:600; font-size:14px; color:#475569; margin-bottom:4px;\">🤖 AI Analyst Notes:</div>
<div style=\"color:#334155; font-size:14px; margin-bottom:8px;\">{ai.get('reasoning', 'Analysis pending...')}</div>
<div style=\"font-size:12px; color:#64748B; background-color:#F8FAFC; padding:8px; border-radius:4px; border:1px solid #E2E8F0;\">\n<strong>🔍 Context Checked:</strong> {(ai.get('external_context_used','No external context data available.')).replace('News Context:', '').strip()}\n</div>
</div>
</div>""", unsafe_allow_html=True)

        st.divider()

    # Historical Data
    if len(alerts) > 5:
        st.markdown("---")
        st.subheader("📚 Post Archive")
        with st.expander(f"View {len(alerts)-5} older posts", expanded=False):
            st.dataframe(
                pd.DataFrame([
                    {
                        "Date": to_local_str(pick_ts_str(a)),
                        "Content": a.get('content',''),
                        "Impact": "High" if a.get('ai_analysis', {}).get('impact') else "Low",
                        "Sentiment": a.get('ai_analysis', {}).get('sentiment', '-'),
                        "AI Reasoning": a.get('ai_analysis', {}).get('reasoning', '-'),
                        "Context Source": a.get('ai_analysis', {}).get('external_context_used', '-')
                    }
                    for a in alerts[5:]
                ]),
                width='stretch'
            )
else:
    st.info("System initializing... Waiting for first data fetch.")

# Footer
st.markdown("<br><br><br>", unsafe_allow_html=True)
st.markdown(
    """
    <div style="text-align: center; color: #94a3b8; font-size: 12px; padding: 20px;">
        © 2025 Trump Truth Social Monitor. All rights reserved. <br>
        Powered by DeepSeek-V3 & SiliconFlow
    </div>
    """,
    unsafe_allow_html=True
)

# Auto-refresh
time.sleep(refresh_rate)
st.rerun()
