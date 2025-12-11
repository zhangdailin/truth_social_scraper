import streamlit as st
import json
import time
import os
import pandas as pd
import re
from datetime import datetime, timezone
from utils import ALERTS_FILE, describe_media, local_tz_label, pick_ts, to_local_str

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

    /* Media grid */
    .media-grid {
        margin-top: 12px;
        display: grid;
        gap: 10px;
        grid-template-columns: repeat(auto-fit, minmax(var(--media-min, 180px), 1fr));
    }
    .media-item {
        position: relative;
        overflow: hidden;
        border-radius: 10px;
        border: 1px solid #E2E8F0;
        background: #0F172A;
        min-height: 140px;
    }
    .media-item img,
    .media-item video {
        width: 100%;
        height: 100%;
        object-fit: cover;
        display: block;
        background: #0F172A;
    }
    .media-item video {
        aspect-ratio: 16 / 9;
    }
    .media-item img {
        aspect-ratio: 4 / 3;
    }
    .media-more {
        display: flex;
        align-items: center;
        justify-content: center;
        color: #1E293B;
        background: linear-gradient(135deg, #E2E8F0, #CBD5E1);
        font-weight: 700;
        font-size: 18px;
        gap: 6px;
    }
    .media-more span {
        font-size: 12px;
        color: #475569;
        font-weight: 600;
        text-transform: uppercase;
        letter-spacing: 0.5px;
    }

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

st.markdown("""
<script>
(function(){
function fmt(iso){
  try{
    const d = new Date(iso);
    return d.toLocaleString([], {year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit'});
  }catch(e){return iso}
}
function tzLabel(){
  try{
    const tz = Intl.DateTimeFormat().resolvedOptions().timeZone || '';
    const offMin = new Date().getTimezoneOffset();
    const sign = offMin<=0?'+':'-';
    const pad = (n)=>String(Math.floor(Math.abs(n))).padStart(2,'0');
    const h = pad(offMin/60);
    const m = pad(offMin%60);
    return (tz?tz:'Local')+' UTC'+sign+h+':'+m;
  }catch(e){return 'Local'}
}
document.addEventListener('DOMContentLoaded', function(){
  document.querySelectorAll('.ts').forEach(function(el){
    const iso = el.getAttribute('data-iso');
    if(!iso) return;
    el.textContent = fmt(iso)+' ('+tzLabel()+')';
  });
});
})();
</script>
""", unsafe_allow_html=True)

# ==========================================
# 3. DATA LOADING
# ==========================================

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

def build_media_html(media, max_images=4, width=220):
    """Render media (images/videos) in a responsive grid with fallbacks."""
    try:
        if not media:
            return ""

        items = []
        max_images = int(max_images)
        min_col = max(140, int(width))

        video_onerror = 'onerror="this.closest(\'.media-item\').style.display=\'none\';"'
        img_onerror = video_onerror

        for m in media:
            if len(items) >= max_images:
                break
            t = (m.get("type") or "").lower()
            vsrc = m.get("url") or m.get("remote_url") or ""
            poster = m.get("preview_url") or ""
            is_video = t in ("video", "gifv") or (vsrc and (".mp4" in vsrc or "video" in vsrc))

            if is_video and vsrc:
                video_attrs = 'playsinline controls preload="metadata" controlsList="nodownload"'
                if poster:
                    video_attrs += f' poster="{poster}"'
                items.append(f'<div class="media-item"><video src="{vsrc}" {video_attrs} {video_onerror}></video></div>')
            else:
                src = poster or m.get("url") or ""
                if not src:
                    continue
                items.append(
                    '<div class="media-item">'
                    f'<img src="{src}" alt="" loading="lazy" decoding="async" {img_onerror} />'
                    '</div>'
                )

        if not items:
            return ""

        remaining = max(0, len(media) - max_images)
        if remaining:
            items.append(f'<div class="media-item media-more">+{remaining} <span>more</span></div>')

        return f'<div class="media-grid" style="--media-min:{min_col}px;">' + ''.join(items) + '</div>'
    except Exception:
        return ""

def display_text(item):
    try:
        c = (item.get('content') or '').strip()
        if c:
            return c
        arr = item.get('media') or []
        return describe_media(arr)
    except Exception:
        return item.get('content','')

def render_recommendation(ai_analysis):
    rec = ai_analysis.get('recommendation', 'None')
    if not rec or rec == "None":
        return ""
    assets = ai_analysis.get('affected_assets', [])
    try:
        rec_with_tooltips = inject_stock_tooltips(rec, assets)
    except Exception:
        rec_with_tooltips = rec
    return f"""<div style="margin-top:12px; padding:12px; background-color:#FEF3C7; border-left:4px solid #F59E0B; border-radius:4px;">
<strong style="color:#B45309;">💰 Trading Recommendation:</strong> 
<span style="color:#92400E; font-weight:600;">{rec_with_tooltips}</span>
</div>"""


def metric_card(label, value, color=None):
    """Lightweight metric card HTML to avoid repeated multiline snippets."""
    color_style = f' style="color: {color}"' if color else ""
    return (
        '<div class="metric-container">'
        f'<div class="metric-label">{label}</div>'
        f'<div class="metric-value"{color_style}>{value}</div>'
        "</div>"
    )

def render_alert_card(alert, latest=False):
    ai = alert.get('ai_analysis', {})
    is_high = ai.get('impact', False)
    impact_class = "hero-alert-high" if is_high else "hero-alert-low"
    impact_label = '🚨 HIGH MARKET IMPACT' if is_high else '✅ LOW IMPACT'
    tz_lbl = local_tz_label()
    ts_disp = to_local_str(pick_ts(alert))
    media_html = build_media_html(alert.get('media'), 4 if latest else 3, 220 if latest else 180)
    rec_html = render_recommendation(ai)
    return f"""<div class="hero-card {impact_class}">
<div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:12px;">
<span class="tag {'tag-red' if is_high else 'tag-green'}">{impact_label}</span>
<span class="tag tag-gray">{'REAL' if alert.get('source','real')=='real' else 'SIMULATED'}</span>
<span style="color:#64748B; font-size:12px;">{ts_disp} ({tz_lbl})</span>
</div>
<div class="post-content">“{display_text(alert)}”</div>
{media_html}
{rec_html}
<div style="margin-top:16px; padding-top:16px; border-top:1px solid #E2E8F0;">
<div style="font-weight:600; font-size:14px; color:#475569; margin-bottom:4px;">🤖 AI Analyst Notes:</div>
<div style="color:#334155; font-size:14px; margin-bottom:8px;">{ai.get('reasoning', 'Analysis pending...')}</div>
<div style="font-size:12px; color:#64748B; background-color:#F8FAFC; padding:8px; border-radius:4px; border:1px solid #E2E8F0;">
<strong>🔍 Context Checked:</strong> {(ai.get('external_context_used','No external context data available.')).replace('News Context:', '').strip()}
</div>
</div>
</div>"""

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
            data.sort(key=lambda x: _parse_ts(pick_ts(x)), reverse=True)
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

alerts = load_alerts()
if 'initial_fetch_done' not in st.session_state:
    st.session_state['initial_fetch_done'] = False
if not alerts and not st.session_state['initial_fetch_done']:
    try:
        from monitor_trump import run_fetch_recent
        cnt = run_fetch_recent(limit=10, fast_init=True)
        if not int(cnt or 0):
            cnt = 0
    except Exception:
        cnt = 0
    st.session_state['initial_fetch_done'] = True
    alerts = load_alerts()

if 'last_api_check' not in st.session_state:
    st.session_state['last_api_check'] = time.time()
if time.time() - float(st.session_state['last_api_check']) >= float(st.session_state.get('check_interval_seconds', 900)):
    try:
        from monitor_trump import run_fetch_recent
        run_fetch_recent(limit=1)
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
alerts_vis = [a for a in alerts if str(display_text(a) or '').strip()]
if alerts_vis:
    latest = alerts_vis[0]
    high_impact_count = sum(1 for a in alerts if a.get('ai_analysis', {}).get('impact'))
    
    c1, c2, c3, c4 = st.columns(4)
    sentiment = latest.get('ai_analysis', {}).get('sentiment', 'N/A').upper()
    sentiment_color = "#10B981" if sentiment == "POSITIVE" else "#EF4444" if sentiment == "NEGATIVE" else "#64748B"

    try:
        _lts = pick_ts(latest)
        _ts = datetime.fromisoformat(_lts.replace('Z','+00:00'))
        if _ts.tzinfo is None:
            _ts = _ts.replace(tzinfo=timezone.utc)
        _age_min2 = int((datetime.now(timezone.utc) - _ts.astimezone(timezone.utc)).total_seconds() / 60)
    except Exception:
        _age_min2 = 0

    metric_payloads = [
        ("Monitored Posts", len(alerts), None),
        ("High Impact Alerts", high_impact_count, "#EF4444" if high_impact_count > 0 else "#0F172A"),
        ("Latest Sentiment", sentiment, sentiment_color),
        ("Latest Post Age", f"{_age_min2} min", None),
    ]

    for col, payload in zip((c1, c2, c3, c4), metric_payloads):
        label, value, color = payload
        with col:
            st.markdown(metric_card(label, value, color), unsafe_allow_html=True)

    st.markdown("<br>", unsafe_allow_html=True)

    # HERO SECTION (Latest Post)
    latest_ai = latest.get('ai_analysis', {})
    st.markdown(render_alert_card(latest, latest=True), unsafe_allow_html=True)
    
    # FEED SECTION
    c_feed_title, c_feed_sort = st.columns([0.8, 0.2])
    with c_feed_title:
        st.subheader("📜 Recent Posts")
    
    # List Layout
    _feed = alerts_vis[1:6]
    for _i, alert in enumerate(_feed):
        st.markdown(render_alert_card(alert, latest=False), unsafe_allow_html=True)

        if _i < len(_feed) - 1:
            st.divider()

    # Historical Data (Paginated)
    if len(alerts_vis) > 6:
        st.markdown("---")
        st.subheader("📚 Post Archive")
        with st.expander(f"View {len(alerts_vis)-6} older posts", expanded=False):
            # Filter + view controls
            f1, f2, f3 = st.columns([0.5, 0.25, 0.25])
            search_text = f1.text_input(
                "Search text",
                key="archive_search",
                placeholder="Filter content, assets, reasoning…"
            )
            sentiment_filter = f2.selectbox("Sentiment", ["All", "Positive", "Neutral", "Negative"], index=0)
            impact_filter = f3.selectbox("Impact", ["All", "High", "Low"], index=0)

            v1, v2 = st.columns([0.5, 0.5])
            view_mode = v1.radio("View mode", ["Cards", "Table"], horizontal=True, key="archive_view")
            page_size = v2.selectbox("Rows per page", [10, 20, 50], index=1, key="archive_page_size")

            historical = alerts_vis[6:]

            def _match(alert):
                ai = alert.get('ai_analysis', {}) or {}
                sent = (ai.get('sentiment') or '-').lower()
                impact_val = "high" if ai.get('impact') else "low"
                if sentiment_filter != "All" and sentiment_filter.lower() not in sent:
                    return False
                if impact_filter != "All" and impact_filter.lower() != impact_val:
                    return False
                if search_text:
                    needle = search_text.lower().strip()
                    text_blob = " ".join([
                        (display_text(alert) or ""),
                        ai.get('reasoning', '') or "",
                        " ".join(map(str, ai.get('affected_assets', []) or []))
                    ]).lower()
                    if needle and needle not in text_blob:
                        return False
                return True

            filtered = [a for a in historical if _match(a)]
            total = len(filtered)

            if 'archive_page' not in st.session_state:
                st.session_state['archive_page'] = 1

            pages = max(1, (total + page_size - 1) // page_size)
            cur = min(max(int(st.session_state['archive_page']), 1), pages)

            cols_nav = st.columns([0.2, 0.6, 0.2])
            if cols_nav[0].button("◀ Prev", disabled=(cur <= 1)):
                cur = max(1, cur - 1)
            if cols_nav[2].button("Next ▶", disabled=(cur >= pages)):
                cur = min(pages, cur + 1)

            st.session_state['archive_page'] = cur

            start = (cur - 1) * page_size
            end = start + page_size
            slice_alerts = filtered[start:end]

            st.caption(
                f"Page {cur} / {pages} · Showing {len(slice_alerts)} of {total} filtered · {len(historical)} total"
            )

            if not slice_alerts:
                st.info("No posts match the current filters.")
            elif view_mode == "Cards":
                for _i, alert in enumerate(slice_alerts):
                    st.markdown(render_alert_card(alert, latest=False), unsafe_allow_html=True)
                    if _i < len(slice_alerts) - 1:
                        st.divider()
            else:
                def _trim(text, n=240):
                    text = text or ""
                    return text if len(text) <= n else text[: n - 1] + "…"

                df = pd.DataFrame([
                    {
                        "Date": to_local_str(pick_ts(a)),
                        "Impact": "High" if a.get('ai_analysis', {}).get('impact') else "Low",
                        "Sentiment": a.get('ai_analysis', {}).get('sentiment', '-'),
                        "Assets": ", ".join(map(str, a.get('ai_analysis', {}).get('affected_assets', []) or [])) or "-",
                        "Content": _trim(display_text(a)),
                        "Reasoning": _trim(a.get('ai_analysis', {}).get('reasoning', '-'), 180),
                    }
                    for a in slice_alerts
                ])
                st.dataframe(df, width='stretch')
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
