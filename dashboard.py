import streamlit as st
import json
import time
import os
import pandas as pd
import re
import threading
import socket
import hashlib
from datetime import datetime, timezone
from urllib.parse import urlparse
from dotenv import load_dotenv

# 并发控制：限制同时进行的自动图片搜索数量
MAX_CONCURRENT_IMAGE_SEARCHES = 3
_image_search_semaphore = threading.Semaphore(MAX_CONCURRENT_IMAGE_SEARCHES)
from utils import (
    ALERTS_FILE, 
    describe_media, 
    local_tz_label, 
    pick_ts, 
    to_local_str, 
    PROJECT_ROOT,
    get_media_paths_by_post_id,
)

# 加载 .env 文件（在导入其他模块之前）
load_dotenv()

# ==========================================
# 0. API SERVER AUTO-START
# ==========================================
API_PORT = 8000
API_HOST = "0.0.0.0"

def _validate_api_url(url):
    """验证 API URL 格式是否有效"""
    if not url or not isinstance(url, str):
        return False
    url = url.strip()
    # 必须是 http:// 或 https:// 开头，且不包含无效协议
    return (url.startswith(('http://', 'https://')) and 
            'about:' not in url and 
            len(url) > 10)  # 最小长度检查

def get_api_base_url():
    """
    动态获取 API 基础 URL
    根据实际访问的地址（公网IP、域名或localhost）自动适配
    
    优先级：
    1. 环境变量 API_BASE_URL（如果手动配置了，最高优先级）
    2. 从 session_state 获取（已检测并保存的，性能最优）
    3. 从查询参数获取（由 JavaScript 自动检测并设置）
    4. 默认使用 localhost
    """
    # 1. 优先使用环境变量配置（如果用户手动配置了，最高优先级）
    api_base_url = os.getenv("API_BASE_URL", "").strip()
    if api_base_url:
        if _validate_api_url(api_base_url):
            # 如果环境变量存在且有效，清除 session_state 中的缓存（确保使用环境变量）
            if 'api_base_url' in st.session_state:
                cached_url = st.session_state['api_base_url']
                if cached_url != api_base_url.rstrip('/'):
                    pass
                    del st.session_state['api_base_url']
            pass
            return api_base_url.rstrip('/')
        else:
            pass
    
    # 2. 从 session_state 获取（已检测并保存的，避免重复读取查询参数）
    if 'api_base_url' in st.session_state:
        url = st.session_state['api_base_url']
        if _validate_api_url(url):
            return url.rstrip('/')
        else:
            # 如果 session_state 中的 URL 无效，清除它
            pass
            del st.session_state['api_base_url']
    
    # 3. 从查询参数获取（JavaScript 可能已经设置了）
    query_params = st.query_params
    if 'api_base_url' in query_params:
        url = query_params['api_base_url']
        if _validate_api_url(url):
            # 保存到 session_state，下次直接使用（提高性能）
            st.session_state['api_base_url'] = url.rstrip('/')
            return url.rstrip('/')
    
    # 4. 默认使用 localhost（首次访问且 JavaScript 还未执行时）
    default_url = f"http://localhost:{API_PORT}"
    return default_url

# 自动检测：通过 JavaScript 获取当前页面的 host
# 初始化时同步查询参数到 session_state（如果存在）
query_params = st.query_params
if 'api_base_url' in query_params:
    detected_url = query_params['api_base_url']
    if _validate_api_url(detected_url):
        # 如果 session_state 中没有或值不同，更新它
        if 'api_base_url' not in st.session_state or st.session_state['api_base_url'] != detected_url:
            st.session_state['api_base_url'] = detected_url.rstrip('/')

# 加载 Plyr 视频播放器库（全局加载一次）
if 'plyr_loaded' not in st.session_state:
    st.components.v1.html("""
    <link rel="stylesheet" href="https://cdn.plyr.io/3.7.8/plyr.css" />
    <script src="https://cdn.plyr.io/3.7.8/plyr.polyfilled.js"></script>
    <style>
    .plyr {
        border-radius: 8px;
        overflow: hidden;
    }
    .plyr__video-wrapper {
        position: relative;
    }
    .plyr__poster {
        background-size: cover;
    }
    /* 确保Plyr样式不影响后续内容 */
    .media-item .plyr {
        max-width: 100%;
        margin: 0;
    }
    .media-item {
        isolation: isolate; /* 创建新的层叠上下文，防止样式泄露 */
    }
    /* 确保AI Analyst Notes不受影响 */
    .hero-card > div:not(.media-item) {
        position: relative;
        z-index: 1;
    }
    </style>
    """, height=0)
    st.session_state['plyr_loaded'] = True

# 如果还没有检测过，且 session_state 中没有有效的 URL，注入 JavaScript 进行检测
if 'api_url_detected' not in st.session_state:
    # 首次访问，注入 JavaScript 来获取当前页面的 host 并设置到查询参数
    st.components.v1.html("""
    <script>
    (function() {
        // 如果已经有 api_base_url 参数，跳过
        if (window.location.search.includes('api_base_url=')) {
            return;
        }
        
        // 如果已经设置过，跳过（避免重复执行）
        if (sessionStorage.getItem('api_url_detected')) {
            return;
        }
        
        // 尝试从多个来源获取实际的 hostname
        let host = null;
        let protocol = 'http:';
        
        try {
            // 方法1: 尝试从父窗口获取（Streamlit iframe 环境）
            if (window.parent && window.parent !== window) {
                try {
                    const parentLoc = window.parent.location;
                    if (parentLoc && parentLoc.hostname && parentLoc.hostname !== 'srcdoc') {
                        host = parentLoc.hostname;
                        protocol = parentLoc.protocol;
                    }
                } catch (e) {
                    // 跨域限制，无法访问父窗口，继续尝试其他方法
                }
            }
            
            // 方法2: 从当前窗口获取（如果不是 iframe 或父窗口获取失败）
            if (!host && window.location.hostname && window.location.hostname !== 'srcdoc') {
                host = window.location.hostname;
                protocol = window.location.protocol;
            }
            
            // 方法3: 从 document.referrer 获取（作为后备方案）
            if (!host && document.referrer) {
                try {
                    const referrerUrl = new URL(document.referrer);
                    if (referrerUrl.hostname) {
                        host = referrerUrl.hostname;
                        protocol = referrerUrl.protocol;
                    }
                } catch (e) {
                    // referrer 解析失败，忽略
                }
            }
            
            // 如果仍然无法获取，使用默认值（localhost）
            if (!host) {
                host = 'localhost';
                protocol = 'http:';
            }
            
            const apiPort = '8000';
            const apiBaseUrl = protocol + '//' + host + ':' + apiPort;
            
            // 标记为已检测
            sessionStorage.setItem('api_url_detected', 'true');
            
            // 通过 URL 参数传递（最可靠的方法）
            try {
                // 获取当前页面的完整 URL
                const currentUrl = window.location.href;
                if (currentUrl && !currentUrl.includes('about:srcdoc')) {
                    const url = new URL(currentUrl);
                    url.searchParams.set('api_base_url', apiBaseUrl);
                    // 使用 replaceState 更新 URL（不触发页面重载）
                    window.history.replaceState({}, '', url);
                    
                    // 延迟刷新页面以读取新参数（仅首次）
                    setTimeout(function() {
                        window.location.reload();
                    }, 100);
                } else {
                    // 如果无法修改 URL，尝试通过 postMessage（可能不可靠）
                    if (window.parent && window.parent.postMessage) {
                        window.parent.postMessage({
                            type: 'streamlit:setComponentValue',
                            value: {api_base_url: apiBaseUrl}
                        }, '*');
                    }
                }
            } catch (e) {
                // 无法修改 URL，记录错误但不中断
                console.warn('无法设置 API URL 参数:', e);
            }
        } catch (error) {
            console.error('检测 API URL 时出错:', error);
        }
    })();
    </script>
    """, height=0)
    
    # 标记为已检测（即使 JavaScript 可能失败）
    st.session_state['api_url_detected'] = True

def is_port_in_use(host, port):
    """检查端口是否已被占用"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind((host, port))
            return False
        except OSError:
            return True

def start_api_server():
    """在后台线程中启动API服务器"""
    try:
        from api import run_api_server
        # 使用守护线程，确保主程序退出时线程也会退出
        api_thread = threading.Thread(
            target=run_api_server,
            args=(API_HOST, API_PORT),
            daemon=True
        )
        api_thread.start()
        # 等待一小段时间确保服务器启动
        time.sleep(1)
        return True
    except Exception as e:
        print(f"Failed to start API server: {e}")
        return False

# 检查并启动API服务器（只启动一次）
if 'api_server_started' not in st.session_state:
    try:
        if not is_port_in_use(API_HOST, API_PORT):
            if start_api_server():
                st.session_state['api_server_started'] = True
                st.session_state['api_error'] = None
            else:
                st.session_state['api_server_started'] = False
                st.session_state['api_error'] = "启动失败"
        else:
            st.session_state['api_server_started'] = True
            st.session_state['api_error'] = None
    except Exception as e:
        st.session_state['api_server_started'] = False
        st.session_state['api_error'] = str(e)

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
        margin-bottom: 16px;
        display: grid;
        gap: 10px;
        grid-template-columns: repeat(auto-fit, minmax(var(--media-min, 180px), 1fr));
        clear: both;
    }
    .media-item {
        position: relative;
        overflow: hidden;
        border-radius: 10px;
        border: 1px solid #E2E8F0;
        background: #0F172A;
        min-height: 140px;
        max-width: 500px;
        isolation: isolate; /* 创建新的层叠上下文，防止样式泄露 */
    }
    .media-item img,
    .media-item video {
        width: 100%;
        height: auto;
        object-fit: cover;
        display: block;
        background: #0F172A;
    }
    .media-item video {
        max-height: 300px;
        max-width: 100%;
        aspect-ratio: 16 / 9;
    }
    .media-item img {
        aspect-ratio: 4 / 3;
        max-height: 400px;
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

def build_media_html(media, max_images=4, width=220, post_id=None):
    """
    Render media (images/videos) in a responsive grid with fallbacks.
    
    Args:
        media: 媒体列表
        max_images: 最大显示数量
        width: 宽度
        post_id: 推文ID（用于从映射中查找本地文件）
    """
    try:
        if not media:
            return ""

        items = []
        modals = []
        max_images = int(max_images)
        min_col = max(140, int(width))
        displayable_count = 0  # 实际应该显示的媒体数量（排除被跳过的预览图）

        # 使用更简单的错误处理，避免React错误
        video_onerror = ''
        img_onerror = 'onerror="this.style.display=\'none\';"'

        # 获取动态 API 基础 URL（在循环外获取一次，提高效率）
        api_base_url = get_api_base_url()
        
        # 优先通过推文ID从映射中获取本地文件路径
        local_media_paths = []
        if post_id:
            local_media_paths = get_local_media_paths_by_post_id(post_id)
            if local_media_paths:
                pass
        
        # 如果有本地文件，优先使用本地文件（完全使用映射，不再处理media列表，避免重复）
        if local_media_paths:
            # 先检查是否有视频文件
            has_video = any(
                any(ext in path.lower() for ext in ['.mp4', '.webm', '.mov', '.avi', '.mkv'])
                for path in local_media_paths
                if os.path.exists(path)
            )
            
            # 计算实际应该显示的媒体数量（排除被跳过的预览图）
            displayable_count = 0
            for local_path in local_media_paths:
                if not os.path.exists(local_path):
                    continue
                is_video_check = any(ext in local_path.lower() for ext in ['.mp4', '.webm', '.mov', '.avi', '.mkv'])
                # 如果有视频，跳过预览图（图片文件）
                if has_video and not is_video_check:
                    continue
                displayable_count += 1
            
            for i, local_path in enumerate(local_media_paths):
                if len(items) >= max_images:
                    break
                
                if not os.path.exists(local_path):
                    continue
                
                # 转换为相对路径和API路径
                try:
                    rel_path = os.path.relpath(local_path, MEDIA_DIR)
                    rel_path_normalized = rel_path.replace('\\', '/')
                    # 移除前导斜杠（API路径不需要）
                    rel_path_normalized = rel_path_normalized.lstrip('/')
                    # 构建API路径
                    api_path = f"{api_base_url}/api/media/{rel_path_normalized}"
                    
                    
                    
                    # 判断是视频还是图片
                    is_video = any(ext in local_path.lower() for ext in ['.mp4', '.webm', '.mov', '.avi', '.mkv'])
                    
                    # 如果有视频，跳过预览图（图片文件）
                    if has_video and not is_video:
                        continue
                    
                    if is_video:
                        # 使用简化的视频播放器（避免复杂的script导致渲染问题）
                        video_id = f"plyr-video-{hashlib.md5(api_path.encode()).hexdigest()[:8]}"
                        video_html = f'''<div class="media-item" style="position: relative; margin-bottom: 10px; max-width: 500px; isolation: isolate; clear: both; cursor: zoom-in;" onclick="openMediaModal_{post_id}('video','{api_path}')">
                            <video id="{video_id}" playsinline controls crossorigin="anonymous" style="width: 100%; max-height: 300px; display: block; margin: 0; padding: 0;">
                                <source src="{api_path}" type="video/mp4">
                            </video>
                            <div style="position: absolute; top: 5px; right: 5px; z-index: 10;">
                                <a href="{api_path}" download style="background: rgba(0,0,0,0.7); color: white; padding: 5px 10px; border-radius: 3px; text-decoration: none; font-size: 12px; display: inline-block;">⬇ 下载</a>
                            </div>
                        </div>
                        <script>
                        (function() {{
                            try {{
                                if (typeof Plyr !== 'undefined') {{
                                    const player = new Plyr('#{video_id}', {{
                                        controls: ['play-large', 'play', 'progress', 'current-time', 'mute', 'volume', 'settings', 'fullscreen'],
                                        settings: ['speed'],
                                        speed: {{selected: 1, options: [0.5, 0.75, 1, 1.25, 1.5, 1.75, 2]}},
                                        keyboard: {{focused: true, global: false}},
                                        clickToPlay: true
                                    }});
                                }}
                            }} catch(e) {{
                                console.warn('Plyr initialization failed:', e);
                            }}
                        }})();
                        </script>'''
                        items.append(video_html)
                    else:
                        items.append(
                            f"<div class=\"media-item\" style=\"cursor: zoom-in;\" onclick='openMediaModal_{post_id}(\"image\", \"{api_path}\")'>"
                            f"<img src=\"{api_path}\" alt=\"\" loading=\"lazy\" decoding=\"async\" {img_onerror} />"
                            "</div>"
                        )
                except Exception as e:
                    continue
            
            # 如果通过推文ID找到了本地文件，直接返回，不再处理media列表（避免重复）
            if items:
                # 使用实际应该显示的媒体数量来计算剩余数量（排除被跳过的预览图）
                remaining = max(0, displayable_count - len(items))
                if remaining > 0:
                    items.append(f'<div class="media-item media-more">+{remaining} <span>more</span></div>')
                modal = f'''
                <div id="media-modal-{post_id}" class="media-modal" style="display:none; position:fixed; z-index:1000; left:0; top:0; width:100%; height:100%; background:rgba(0,0,0,0.75);">
                  <div class="media-modal-content" style="position:relative; margin:40px auto; max-width:90%; max-height:85%; background:#000; border-radius:8px; padding:10px;">
                    <span onclick="closeMediaModal_{post_id}()" style="position:absolute; right:10px; top:6px; color:#fff; font-size:24px; cursor:pointer;">×</span>
                    <img id="media-modal-img-{post_id}" src="" style="display:none; max-width:100%; max-height:80vh;"/>
                    <video id="media-modal-video-{post_id}" controls style="display:none; width:100%; max-height:80vh; background:#000;"><source id="media-modal-video-src-{post_id}" src="" type="video/mp4"></video>
                  </div>
                </div>
                <script>
                function openMediaModal_{post_id}(type, src) {{
                  var m = document.getElementById('media-modal-{post_id}');
                  var img = document.getElementById('media-modal-img-{post_id}');
                  var vid = document.getElementById('media-modal-video-{post_id}');
                  var vsrc = document.getElementById('media-modal-video-src-{post_id}');
                  if (type === 'image') {{
                    img.style.display='block'; vid.style.display='none'; img.src=src;
                  }} else {{
                    img.style.display='none'; vid.style.display='block'; vsrc.src=src; vid.load();
                  }}
                  m.style.display='block';
                }}
                function closeMediaModal_{post_id}() {{
                  var m = document.getElementById('media-modal-{post_id}');
                  m.style.display='none';
                }}
                </script>
                '''
                return f'<div id="media-grid-{post_id}" class="media-grid" style="--media-min:{min_col}px;">' + ''.join(items) + '</div>' + modal
        
        # 如果没有通过推文ID找到本地文件，使用media列表中的URL（向后兼容）
        if not local_media_paths:
            # 先检查media列表中是否有视频
            has_video_in_media = False
            for m in media:
                vsrc_check = m.get("url") or m.get("remote_url") or ""
                t_check = (m.get("type") or "").lower()
                is_video_check = t_check in ("video", "gifv") or (vsrc_check and (".mp4" in vsrc_check.lower() or "video" in vsrc_check.lower() or "/videos/" in vsrc_check.lower()))
                if is_video_check and vsrc_check:
                    has_video_in_media = True
                    break
            
            # 计算实际应该显示的媒体数量（排除被跳过的预览图）
            displayable_count = 0
            for m in media:
                vsrc_check = m.get("url") or m.get("remote_url") or ""
                poster_check = m.get("preview_url") or ""
                t_check = (m.get("type") or "").lower()
                is_video_check = t_check in ("video", "gifv") or (vsrc_check and (".mp4" in vsrc_check.lower() or "video" in vsrc_check.lower() or "/videos/" in vsrc_check.lower()))
                # 如果有视频，跳过预览图（只有preview_url而没有实际视频URL的项）
                if has_video_in_media and not is_video_check and not vsrc_check and poster_check:
                    continue
                # 如果有有效的媒体源，计入可显示数量
                if (is_video_check and vsrc_check) or (poster_check or vsrc_check):
                    displayable_count += 1
            
            for m in media:
                if len(items) >= max_images:
                    break
                
                # 优先使用已转换的本地路径（API路径）
                vsrc = m.get("url") or m.get("remote_url") or ""
                poster = m.get("preview_url") or ""
                
                # 如果已经是 API 路径，添加完整的主机地址
                if vsrc.startswith('/api/media/'):
                    vsrc = f"{api_base_url}{vsrc}"
                elif poster.startswith('/api/media/'):
                    poster = f"{api_base_url}{poster}"
                
                t = (m.get("type") or "").lower()
                is_video = t in ("video", "gifv") or (vsrc and (".mp4" in vsrc.lower() or "video" in vsrc.lower() or "/videos/" in vsrc.lower()))

                # 如果有视频，跳过预览图（只有preview_url而没有实际视频URL的项）
                if has_video_in_media and not is_video and not vsrc and poster:
                    continue

                if is_video and vsrc:
                    # 使用简化的视频播放器（避免复杂的script导致渲染问题）
                    video_id = f"plyr-video-{hashlib.md5(vsrc.encode()).hexdigest()[:8]}"
                    modal_id = f"media-modal-{post_id}-{len(items)}"
                    # 视频不显示预览图（poster）
                    video_html = (
                        f'<a href="#{modal_id}" class="media-item" style="position: relative; margin-bottom: 10px; max-width: 500px; isolation: isolate; clear: both; cursor: zoom-in;">'
                        f'<video id="{video_id}" playsinline controls crossorigin="anonymous" style="width: 100%; max-height: 300px; display: block; margin: 0; padding: 0;">'
                        f'<source src="{vsrc}" type="video/mp4"></video>'
                        f'<div style="position: absolute; top: 5px; right: 5px; z-index: 10;"><a href="{vsrc}" download style="background: rgba(0,0,0,0.7); color: white; padding: 5px 10px; border-radius: 3px; text-decoration: none; font-size: 12px; display: inline-block;">⬇ 下载</a></div>'
                        f'</a>'
                    )
                    items.append(video_html)
                    modals.append(
                        f'<div id="{modal_id}" class="media-modal"><div class="media-modal-content">'
                        f'<a href="#" class="media-modal-close">×</a>'
                        f'<video controls style="width:100%; max-height:80vh; background:#000;"><source src="{vsrc}" type="video/mp4"></video>'
                        f'</div></div>'
                    )
                else:
                    src = poster or vsrc or ""
                    if src:
                        modal_id = f"media-modal-{post_id}-{len(items)}"
                        items.append(
                            f'<a href="#{modal_id}" class="media-item" style="cursor: zoom-in;">'
                            f'<img src="{src}" alt="" loading="lazy" decoding="async" {img_onerror} />'
                            f'</a>'
                        )
                        modals.append(
                            f'<div id="{modal_id}" class="media-modal"><div class="media-modal-content">'
                            f'<a href="#" class="media-modal-close">×</a>'
                            f'<img src="{src}" style="max-width:100%; max-height:80vh;"/>'
                            f'</div></div>'
                        )

        if not items:
            return ""

        # 计算剩余数量：使用实际应该显示的媒体数量（排除被跳过的预览图）
        if displayable_count > 0:
            remaining = max(0, displayable_count - len(items))
        else:
            remaining = max(0, len(media) - len(items))
        if remaining > 0:
            items.append(f'<div class="media-item media-more">+{remaining} <span>more</span></div>')

        style = '''
        <style>
          .media-modal { display:none; position:fixed; z-index:1000; left:0; top:0; width:100%; height:100%; background:rgba(0,0,0,0.75); }
          .media-modal:target { display:block; }
          .media-modal-content { position:relative; margin:40px auto; max-width:90%; max-height:85%; background:#000; border-radius:8px; padding:10px; }
          .media-modal-close { position:absolute; right:10px; top:6px; color:#fff; font-size:24px; text-decoration:none; }
        </style>
        '''
        return style + (f'<div id="media-grid-{post_id}" class="media-grid" style="--media-min:{min_col}px;">' + ''.join(items) + '</div>') + ''.join(modals)
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

def build_search_links(alert):
    """
    生成搜索链接（文本搜索）
    返回HTML字符串
    注意：图片搜索已改为自动执行，不再显示按钮
    """
    from urllib.parse import quote
    
    content = display_text(alert)
    
    search_links = []
    
    # 文本内容搜索链接
    if content and content.strip():
        # 截取前100个字符作为搜索关键词
        search_text = content[:100].strip()
        encoded_text = quote(search_text)
        
        # 谷歌搜索
        google_search_url = f"https://www.google.com/search?q={encoded_text}"
        search_links.append(f'<a href="{google_search_url}" target="_blank" style="margin-right:8px; color:#3B82F6; text-decoration:none; font-size:11px;">🔍 Google搜索</a>')
        
        # 必应搜索
        bing_search_url = f"https://www.bing.com/search?q={encoded_text}"
        search_links.append(f'<a href="{bing_search_url}" target="_blank" style="margin-right:8px; color:#3B82F6; text-decoration:none; font-size:11px;">🔍 Bing搜索</a>')
    
    # 不再显示图片搜索按钮，改为自动执行
    
    if search_links:
        return '<div style="margin-top:6px; padding-top:6px; border-top:1px solid #E2E8F0;">' + ''.join(search_links) + '</div>'
    return ''

def perform_auto_image_search(alert):
    """
    自动执行图片搜索（如果有图片）
    返回搜索结果字符串
    在后台线程中执行，避免阻塞
    """
    try:
        media_list = alert.get('media') or []
        if not media_list:
            return None
        
        api_base_url = get_api_base_url()
        post_id = str(alert.get('id', ''))
        
        # 获取第一个图片的URL
        image_url = None
        for m in media_list:
            img_url = m.get("url") or m.get("preview_url") or ""
            if img_url:
                if img_url.startswith('/api/media/'):
                    image_url = f"{api_base_url}{img_url}"
                elif img_url.startswith('http://') or img_url.startswith('https://'):
                    image_url = img_url
                else:
                    if post_id:
                        local_paths = get_local_media_paths_by_post_id(post_id)
                        for local_path in local_paths:
                            if os.path.exists(local_path):
                                is_video = any(ext in local_path.lower() for ext in ['.mp4', '.webm', '.mov', '.avi', '.mkv'])
                                if not is_video:
                                    rel_path = os.path.relpath(local_path, MEDIA_DIR)
                                    rel_path_normalized = rel_path.replace('\\', '/').lstrip('/')
                                    image_url = f"{api_base_url}/api/media/{rel_path_normalized}"
                                    break
                if image_url:
                    break
        
        if not image_url:
            return None
        
        # 调用图片搜索 API（使用 urllib 而不是 requests，避免额外依赖）
        try:
            from urllib.parse import quote
            from urllib.request import urlopen, Request
            from utils import _setup_proxy
            import time
            import json as json_lib
            import os
            
            encoded_url = quote(image_url)
            search_url = f"{api_base_url}/api/image-search/result?image_url={encoded_url}"
            
            _setup_proxy()
            req = Request(search_url, headers={"User-Agent": "Mozilla/5.0"})
            # 增加超时时间，因为 OCR 和搜索可能需要较长时间
            # OCR API 可能需要90秒，加上图片下载和搜索，总共可能需要120秒
            with urlopen(req, timeout=120) as response:
                import json
                data = json.loads(response.read().decode('utf-8'))
                
                if data.get('success') and data.get('results'):
                    return data.get('results')
        except Exception as e:
            error_type = type(e).__name__
            error_msg = str(e)
            is_timeout = "timeout" in error_msg.lower() or "timed out" in error_msg.lower()
            print(f"[Auto Image Search] 搜索失败: {e}")
        
        return None
    except Exception as e:
        print(f"[Auto Image Search] 错误: {e}")
        return None

def render_alert_card(alert, latest=False):
    ai = alert.get('ai_analysis', {})
    is_high = ai.get('impact', False)
    impact_class = "hero-alert-high" if is_high else "hero-alert-low"
    impact_label = '🚨 HIGH MARKET IMPACT' if is_high else '✅ LOW IMPACT'
    tz_lbl = local_tz_label()
    ts_disp = to_local_str(pick_ts(alert))
    media_html = build_media_html(alert.get('media'), 4 if latest else 3, 220 if latest else 180, post_id=str(alert.get('id', '')))
    rec_html = render_recommendation(ai)
    alert_id = str(alert.get('id', ''))
    
    # 改进reasoning显示：如果reasoning是默认或无意义的提示，显示更友好的内容
    reasoning = ai.get('reasoning', 'Analysis pending...')
    reasoning_lower = reasoning.lower()
    
    # 检查是否是默认或无意义的提示（扩展检测范围）
    generic_phrases = [
        'post lacks financial',
        'post contains no substantive',
        'no substantive content',
        'no actionable data',
        'no market-related',
        'no financial content',
        'no financial markets',
        'no specific companies',
        'analysis pending',
        'ai analysis failed',
        'lacks financial',
        'contains no substantive'
    ]
    
    is_generic = any(phrase in reasoning_lower for phrase in generic_phrases)
    
    # 如果有推荐或影响的资产，即使reasoning是通用的，也显示更有用的信息
    has_recommendation = ai.get('recommendation') and ai.get('recommendation') != 'None'
    has_assets = ai.get('affected_assets') and len(ai.get('affected_assets', [])) > 0
    sentiment = ai.get('sentiment', 'neutral')
    
    if is_generic and not has_recommendation and not has_assets:
        # 如果reasoning是通用的且没有其他有用信息，显示更简洁友好的提示
        if sentiment != 'neutral':
            reasoning = f"Sentiment: {sentiment.capitalize()}. No specific market impact detected."
        else:
            # 更简洁的提示，避免重复技术性描述
            reasoning = "This post does not contain specific financial or market-related content."
    elif is_generic and (has_recommendation or has_assets):
        # 如果有推荐或资产，但reasoning是通用的，优先显示资产信息
        if has_assets:
            assets_str = ", ".join(ai.get('affected_assets', []))
            reasoning = f"Affected assets: {assets_str}."
        elif has_recommendation:
            # 如果有推荐，显示推荐信息
            reasoning = f"Recommendation: {ai.get('recommendation')}."
    
    return f"""<div class="hero-card {impact_class}">
<div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:12px;">
<span class="tag {'tag-red' if is_high else 'tag-green'}">{impact_label}</span>
<span class="tag tag-gray">{'REAL' if alert.get('source','real')=='real' else 'SIMULATED'}</span>
<span style="color:#64748B; font-size:12px;">{ts_disp} ({tz_lbl})</span>
</div>
<div class="post-content">“{display_text(alert)}”</div>
<div style="clear: both; margin-bottom: 8px;">{media_html}</div>
{rec_html}
<div style="margin-top:16px; padding-top:16px; border-top:1px solid #E2E8F0; clear: both; position: relative; z-index: 1;">
<div style="font-weight:600; font-size:14px; color:#475569; margin-bottom:4px;">🤖 AI Analyst Notes:</div>
<div style="color:#334155; font-size:14px; margin-bottom:8px;">{reasoning}</div>
</div>
<details style="margin-top:8px;">
<summary style="cursor:pointer; color:#334155; font-size:12px; font-weight:600;">AI Context Details</summary>
<div style="padding:8px; border:1px dashed #CBD5E1; border-radius:6px; margin-top:6px;">
<div style="font-size:12px; color:#64748B; line-height:1.6; padding:8px; background-color:#F8FAFC; border-radius:4px; margin-bottom:8px;"><strong>🖼️ Media:</strong><br/>{ai.get('media_ai_summary','').strip() or '<em style="color:#94A3B8;">No media analysis available.</em>'}</div>
</div>
</details>
</div>
</div>"""

# ==========================================
# 媒体文件路径转换工具函数
# ==========================================
MEDIA_DIR = os.path.join(PROJECT_ROOT, "media")
IMAGES_DIR = os.path.join(MEDIA_DIR, "images")
VIDEOS_DIR = os.path.join(MEDIA_DIR, "videos")

def get_file_extension(url):
    """从URL中提取文件扩展名"""
    parsed = urlparse(url)
    path = parsed.path
    if '.' in path:
        ext = os.path.splitext(path)[1].lower()
        if '?' in ext:
            ext = ext.split('?')[0]
        return ext
    return '.jpg'

def generate_filename(url, media_type='image'):
    """生成唯一的文件名（基于URL的hash，与 monitor_trump.py 保持一致）"""
    url_hash = hashlib.md5(url.encode('utf-8')).hexdigest()[:12]
    ext = get_file_extension(url)
    if media_type == 'video':
        if ext not in ['.mp4', '.webm', '.mov', '.avi', '.mkv', '.gif']:
            ext = '.mp4'
        return f"{url_hash}{ext}"
    else:
        if ext not in ['.jpg', '.jpeg', '.png', '.gif', '.webp']:
            ext = '.jpg'
        return f"{url_hash}{ext}"

def get_local_media_paths_by_post_id(post_id):
    """
    根据推文ID获取本地媒体文件路径列表
    返回文件路径列表（只返回存在的文件）
    """
    if not post_id:
        return []
    
    paths = get_media_paths_by_post_id(post_id)
    return paths

def convert_media_urls_to_local(media_list, post_id=None):
    """
    将媒体列表中的远程URL转换为本地路径（通过API访问）
    返回转换后的媒体列表
    
    Args:
        media_list: 媒体列表
        post_id: 推文ID（用于从映射中查找本地文件）
    
    优先通过推文ID从映射中查找本地文件，如果找到则直接使用
    """
    if not media_list:
        return media_list
    
    # 优先通过推文ID从映射中获取本地文件路径
    if post_id:
        local_media_paths = get_media_paths_by_post_id(post_id)
        if local_media_paths:
            # 如果有本地文件，直接转换为API路径
            converted = []
            for local_path in local_media_paths:
                if not os.path.exists(local_path):
                    continue
                rel_path = os.path.relpath(local_path, MEDIA_DIR)
                rel_path_normalized = rel_path.replace('\\', '/')
                api_path = f'/api/media/{rel_path_normalized}'
                
                # 判断是视频还是图片
                is_video = any(ext in local_path.lower() for ext in ['.mp4', '.webm', '.mov', '.avi', '.mkv'])
                
                converted.append({
                    'url': api_path,
                    'preview_url': api_path,
                    'type': 'video' if is_video else 'image'
                })
            
            if converted:
                return converted
    
    # 如果没有通过推文ID找到，保留原始URL（不再通过URL查找，只使用推文ID映射）
    # 如果media中已经是API路径，保留它
    converted = []
    for media in media_list:
        new_media = media.copy() if isinstance(media, dict) else dict(media)
        original_url = media.get('url') or media.get('preview_url') or ''
        
        # 如果已经是API路径，保留它
        if original_url.startswith('/api/media/'):
            converted.append(new_media)
        # 如果是远程URL，保留原始URL（不转换，因为应该通过推文ID映射来查找）
        else:
            converted.append(new_media)
    
    return converted

def _save_alerts_back(alerts_to_save):
    """
    将转换后的告警数据保存回文件（仅更新媒体URL）
    这是一个辅助函数，用于在加载时如果发现需要转换，转换后立即保存
    这样可以避免每次加载都重新转换，降低访问 Truth Social 的频率
    """
    if not alerts_to_save:
        return
    
    try:
        # 读取现有数据
        if not os.path.exists(ALERTS_FILE):
            return
        
        # 读取所有告警
        encodings = ['utf-8', 'utf-8-sig', 'gbk', 'gb2312', 'latin-1', 'cp1252']
        all_data = None
        
        for encoding in encodings:
            try:
                with open(ALERTS_FILE, "r", encoding=encoding) as f:
                    all_data = json.load(f)
                    break
            except Exception as e:
                continue
        
        if all_data is None:
            try:
                with open(ALERTS_FILE, "r") as f:
                    all_data = json.load(f)
            except Exception as e:
                return
        
        # 处理不同的文件格式：dict 或 list
        if isinstance(all_data, dict):
            # 如果是 dict 格式（{"alerts": [...], "processed_ids": [...]}），提取 alerts 和 processed_ids
            all_alerts = all_data.get("alerts") or []
            processed_ids = set(map(str, (all_data.get("processed_ids") or [])))
        elif isinstance(all_data, list):
            # 如果是 list 格式（旧格式或 _save_alerts_back 之前保存的），只有 alerts
            all_alerts = all_data
            processed_ids = set()
        else:
            return
        
        # 更新告警中的媒体URL（使用ID作为键）
        alerts_dict = {str(a.get('id', '')): a for a in all_alerts}
        updated_count = 0
        
        for alert in alerts_to_save:
            alert_id = str(alert.get('id', ''))
            if alert_id and alert_id in alerts_dict:
                # 更新媒体URL
                old_media = alerts_dict[alert_id].get('media', [])
                alerts_dict[alert_id]['media'] = alert.get('media', [])
                updated_count += 1
            elif alert_id:
                # 如果告警不存在，添加它
                alerts_dict[alert_id] = alert
                updated_count += 1
                # 添加到 processed_ids
                processed_ids.add(alert_id)
        
        # 保存回文件（保持与 save_alert 相同的格式）
        output = {
            "alerts": list(alerts_dict.values()),
            "processed_ids": sorted(list(processed_ids))
        }
        
        with open(ALERTS_FILE, "w", encoding='utf-8') as f:
            json.dump(output, f, indent=2, ensure_ascii=False)
        
    except Exception as e:
        import traceback
        traceback.print_exc()

def load_alerts():
    if not os.path.exists(ALERTS_FILE):
        return []
    
    # 尝试多种编码格式
    encodings = ['utf-8', 'utf-8-sig', 'gbk', 'gb2312', 'latin-1', 'cp1252']
    data = None
    
    for encoding in encodings:
        try:
            with open(ALERTS_FILE, "r", encoding=encoding) as f:
                data = json.load(f)
                break
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
        except Exception:
            continue
    
    # 如果所有编码都失败，尝试使用系统默认编码
    if data is None:
        try:
            with open(ALERTS_FILE, "r") as f:
                data = json.load(f)
        except Exception as e:
            print(f"Error loading alerts with all encodings: {e}")
            return []
    
    if not isinstance(data, list):
        if isinstance(data, dict):
            alerts_data = data.get("alerts")
            if alerts_data is None:
                print(f"Warning: ALERTS_FILE dict has no 'alerts' key")
                return []
            data = alerts_data if isinstance(alerts_data, list) else []
        else:
            print(f"Warning: ALERTS_FILE is not a list, got {type(data)}")
            return []
    
    def _parse_ts(s):
        try:
            s2 = (s or '').replace('Z', '+00:00')
            dt = datetime.fromisoformat(s2)
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except Exception:
            return datetime.min.replace(tzinfo=timezone.utc)
    
    try:
        data.sort(key=lambda x: _parse_ts(pick_ts(x)), reverse=True)
    except Exception as sort_err:
        print(f"Warning: Failed to sort alerts: {sort_err}")
    
    # 改进去重逻辑：主要基于ID去重，保留最新的
    seen_ids = set()
    deduped = []
    
    for a in data:
        alert_id = str(a.get('id', ''))
        
        # 只使用ID去重（最可靠），相同ID只保留第一个（最新的）
        if alert_id:
            if alert_id in seen_ids:
                continue  # 跳过重复ID
            seen_ids.add(alert_id)
        # 如果没有ID，保留所有（可能是测试数据）
        
        # 转换媒体URL为本地路径（仅当URL还是远程URL时转换）
        # 如果已经是 /api/media/ 格式，说明已经转换过，直接使用
        if 'media' in a and a['media']:
            # 检查是否还需要转换（如果已经是本地路径，跳过）
            needs_conversion = False
            for m in a['media']:
                url = m.get('url') or m.get('preview_url') or ''
                # 如果URL是远程URL，需要转换
                if url and url.startswith(('http://', 'https://')):
                    needs_conversion = True
                    break
            
            # 只有在需要时才转换（避免重复转换）
            if needs_conversion:
                original_media = a['media'].copy()
                alert_id = str(a.get('id', ''))
                a['media'] = convert_media_urls_to_local(a['media'], post_id=alert_id)
                # 检查是否有转换（如果转换了，保存回文件）
                if a['media'] != original_media:
                    # 标记需要保存
                    if 'needs_save' not in st.session_state:
                        st.session_state['needs_save'] = []
                    st.session_state['needs_save'].append(a)
        
        deduped.append(a)
    
    # 如果有需要保存的告警，批量保存（避免频繁写入文件）
    if 'needs_save' in st.session_state and st.session_state['needs_save']:
        try:
            _save_alerts_back(st.session_state['needs_save'])
            st.session_state['needs_save'] = []  # 清空列表
        except Exception as e:
            pass
    
    return deduped

# 只在session_state中缓存alerts，避免重复加载
# 首先确保 cached_alerts 在 session_state 中初始化（即使为空列表）
if 'cached_alerts' not in st.session_state:
    st.session_state['cached_alerts'] = []

# 检查是否需要重新加载（只在特定条件下重新加载）
# 添加：如果距离上次加载超过1分钟，也重新加载（确保获取最新数据）
should_reload = (
    ('force_reload_alerts' in st.session_state and st.session_state['force_reload_alerts']) or
    (len(st.session_state.get('cached_alerts', [])) == 0) or
    (time.time() - st.session_state.get('last_alerts_load_time', 0) > 60)  # 超过1分钟重新加载
)

if should_reload:
    st.session_state['cached_alerts'] = load_alerts()
    st.session_state['last_alerts_load_time'] = time.time()  # 记录加载时间
    if 'force_reload_alerts' in st.session_state:
        del st.session_state['force_reload_alerts']

alerts = st.session_state.get('cached_alerts', [])

if 'initial_fetch_done' not in st.session_state:
    st.session_state['initial_fetch_done'] = False
# Auto-refresh 设置（分钟级，默认5分钟）
if 'refresh_rate_minutes' not in st.session_state:
    st.session_state['refresh_rate_minutes'] = 5  # 默认5分钟
# 固定数据拉取间隔（根据refresh_rate_minutes动态调整）
# 确保check_interval_seconds始终与refresh_rate_minutes同步
refresh_rate_minutes_current = st.session_state.get('refresh_rate_minutes', 5)
check_interval_seconds_expected = int(refresh_rate_minutes_current) * 60
# 如果check_interval_seconds与refresh_rate_minutes不同步，更新它
if st.session_state.get('check_interval_seconds', 0) != check_interval_seconds_expected:
    st.session_state['check_interval_seconds'] = check_interval_seconds_expected
# 使用JavaScript实现自动刷新（不依赖外部包）
# 注意：_auto_refresh_ms 变量在下面的JavaScript代码中使用
_auto_refresh_ms = int(st.session_state.get('refresh_rate_minutes', 5)) * 60 * 1000
# 注入JavaScript实现自动刷新（使用更可靠的方法）
# 使用sessionStorage来跟踪刷新状态，避免重复设置
# 同时添加一个定期检查机制，确保即使setTimeout失败也能刷新
st.components.v1.html(f"""
<script>
(function() {{
    // 使用sessionStorage来跟踪是否已设置定时器
    const timerKey = 'autoRefreshTimer_{st.session_state.get("refresh_rate_minutes", 5)}';
    const refreshInterval = {_auto_refresh_ms}; // {st.session_state.get('refresh_rate_minutes', 5)}分钟
    const checkInterval = 60000; // 每分钟检查一次（60秒）
    let startTime = Date.now();
    
    // 检查是否已经设置过定时器
    const storedTime = sessionStorage.getItem(timerKey);
    if (!storedTime) {{
        sessionStorage.setItem(timerKey, startTime.toString());
        console.log('[Auto Refresh] 设置自动刷新间隔:', refreshInterval, 'ms ({st.session_state.get("refresh_rate_minutes", 5)}分钟)');
        
        // 方法1: 使用setTimeout（主要方法）
        setTimeout(function() {{
            console.log('[Auto Refresh] 自动刷新触发 (setTimeout) - 重新加载页面');
            sessionStorage.removeItem(timerKey);
            window.location.reload();
        }}, refreshInterval);
        
        // 方法2: 使用setInterval定期检查（备用方法，确保即使setTimeout失败也能刷新）
        const checkTimer = setInterval(function() {{
            const elapsed = Date.now() - startTime;
            if (elapsed % 60000 < 1000) {{ // 每分钟只打印一次
                console.log('[Auto Refresh] 定时检查: 已过', Math.round(elapsed / 1000), '秒，目标', Math.round(refreshInterval / 1000), '秒');
            }}
            
            if (elapsed >= refreshInterval) {{
                console.log('[Auto Refresh] 自动刷新触发 (setInterval检查) - 重新加载页面');
                clearInterval(checkTimer);
                sessionStorage.removeItem(timerKey);
                window.location.reload();
            }}
        }}, checkInterval);
    }} else {{
        const storedTimeNum = parseInt(storedTime);
        const elapsed = Date.now() - storedTimeNum;
        console.log('[Auto Refresh] 定时器已设置，已过', Math.round(elapsed / 1000), '秒，目标', Math.round(refreshInterval / 1000), '秒');
        
        // 如果已经超过刷新间隔，立即刷新
        if (elapsed >= refreshInterval) {{
            console.log('[Auto Refresh] 检测到超时，立即刷新页面');
            sessionStorage.removeItem(timerKey);
            window.location.reload();
        }} else {{
            // 如果还没到时间，继续等待，但设置一个检查定时器
            const remainingTime = refreshInterval - elapsed;
            setTimeout(function() {{
                console.log('[Auto Refresh] 延迟刷新触发 - 重新加载页面');
                sessionStorage.removeItem(timerKey);
                window.location.reload();
            }}, remainingTime);
        }}
    }}
}})();
</script>
""", height=0)
if not alerts and not st.session_state['initial_fetch_done']:
    try:
        from monitor_trump import run_fetch_recent
        import os
        # 检查环境变量
        env_check = {
            'TRUTH_COOKIE': bool(os.getenv('TRUTH_COOKIE')),
            'SILICONFLOW_API_KEY': bool(os.getenv('SILICONFLOW_API_KEY')),
            'TRUTH_ACCOUNT_ID': bool(os.getenv('TRUTH_ACCOUNT_ID')),
        }
        
        cnt = run_fetch_recent(limit=10, fast_init=True)
        cnt = run_fetch_recent(limit=50, fast_init=True)
        if not int(cnt or 0):
            cnt = 0
    except Exception as e:
        cnt = 0
        import traceback
        traceback.print_exc()
    st.session_state['initial_fetch_done'] = True
    # 重新加载alerts并更新缓存
    st.session_state['cached_alerts'] = load_alerts()
    alerts = st.session_state['cached_alerts']
    pass

# 使用文件系统持久化last_api_check时间戳（避免session_state在页面刷新时被重置）
# 文件路径
_last_check_file = os.path.join(PROJECT_ROOT, ".cursor", "last_api_check.txt")

def get_last_api_check():
    """从文件系统读取last_api_check时间戳"""
    try:
        if os.path.exists(_last_check_file):
            with open(_last_check_file, "r") as f:
                content = f.read().strip()
                if content:
                    timestamp = float(content)
                    return timestamp
    except Exception as e:
        pass
    # 如果文件不存在或读取失败，返回5分钟前的时间（这样首次运行时会立即触发一次刷新）
    current_time = time.time()
    initial_timestamp = current_time - 300  # 5分钟前，确保首次运行能立即触发
    try:
        os.makedirs(os.path.dirname(_last_check_file), exist_ok=True)
        with open(_last_check_file, "w") as f:
            f.write(str(initial_timestamp))
    except Exception:
        pass
    return initial_timestamp

def set_last_api_check(timestamp):
    """将last_api_check时间戳保存到文件系统"""
    try:
        os.makedirs(os.path.dirname(_last_check_file), exist_ok=True)
        with open(_last_check_file, "w") as f:
            f.write(str(timestamp))
    except Exception as e:
        pass

# 初始化last_api_check（从文件系统读取，如果不存在则创建）
last_api_check_timestamp = get_last_api_check()
# 同时更新session_state（用于兼容性）
if 'last_api_check' not in st.session_state:
    st.session_state['last_api_check'] = last_api_check_timestamp
else:
    # 如果session_state存在，但文件系统的时间戳更新，使用文件系统的值
    if last_api_check_timestamp > st.session_state['last_api_check']:
        st.session_state['last_api_check'] = last_api_check_timestamp
    else:
        # 如果session_state的值更新，更新文件系统
        set_last_api_check(st.session_state['last_api_check'])
        last_api_check_timestamp = st.session_state['last_api_check']
if 'is_fetching' not in st.session_state:
    st.session_state['is_fetching'] = False

# 注意：定时检查逻辑已移动到刷新率滑块更新之后（第1929行之后），
# 以确保使用最新的check_interval_seconds值

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
# 处理图片搜索结果（从 sessionStorage 读取）
# ==========================================
# 使用 JavaScript 检查 sessionStorage 并设置到 session_state
st.components.v1.html("""
<script>
(function() {
    // 检查 sessionStorage 中是否有图片搜索结果
    const alertId = sessionStorage.getItem('image_search_alert_id');
    const result = sessionStorage.getItem('image_search_result_' + alertId);
    
    if (alertId && result) {
        // 通过 URL 参数传递（因为 Streamlit 无法直接访问 sessionStorage）
        const params = new URLSearchParams(window.location.search);
        params.set('image_search_alert_id', alertId);
        // 如果结果太长，截断（URL 参数有长度限制）
        const truncatedResult = result.length > 2000 ? result.substring(0, 2000) + '...' : result;
        params.set('image_search_result', truncatedResult);
        
        // 只在第一次加载时设置
        if (!params.has('image_search_processed')) {
            params.set('image_search_processed', '1');
            window.location.search = params.toString();
        }
    }
})();
</script>
""", height=0)

# 检查 URL 参数中是否有图片搜索结果
if 'image_search_alert_id' in st.query_params and 'image_search_result' in st.query_params:
    alert_id = st.query_params.get('image_search_alert_id')
    result = st.query_params.get('image_search_result')
    if alert_id and result:
        cache_key = f'image_search_{alert_id}'
        st.session_state[cache_key] = result
        # 清除 URL 参数（避免重复处理）
        if 'image_search_processed' in st.query_params:
            # 保留其他参数，只移除图片搜索相关的参数
            new_params = dict(st.query_params)
            new_params.pop('image_search_alert_id', None)
            new_params.pop('image_search_result', None)
            new_params.pop('image_search_processed', None)
            st.query_params.clear()
            for k, v in new_params.items():
                st.query_params[k] = v

# ==========================================
# 4. DASHBOARD HEADER & CONTROLS
# ==========================================

# Top Layout: Title (Left) + Controls (Right)
c_header, c_control = st.columns([0.75, 0.25])

with c_header:
    st.markdown("# 🦅 Trump Truth Social Monitor")
    st.markdown("Real-time surveillance of Truth Social posts with **AI-driven market impact analysis**.")
    st.caption("Powered by **DeepSeek-V3** via SiliconFlow")
    
    # API Server 状态显示（紧凑布局）
    api_started = st.session_state.get('api_server_started', False)
    api_error = st.session_state.get('api_error')
    api_status = "🟢 Running" if api_started else "🔴 Stopped"
    
    if api_started:
        api_url = get_api_base_url()
        st.caption(f"🔌 API Server: {api_status} | 📍 [{api_url}]({api_url}) | 📚 [Docs]({api_url}/docs)")
    elif api_error:
        st.caption(f"🔌 API Server: {api_status} | ⚠️ {api_error}")
    else:
        st.caption(f"🔌 API Server: {api_status}")

with c_control:
    st.markdown("**⚙️ System Control**")
    # Auto-refresh 设置为分钟级（1-60分钟，默认5分钟）
    old_rate = st.session_state.get("refresh_rate_minutes", 5)
    refresh_rate_minutes = st.slider(
        "Auto-refresh (minutes)", 
        min_value=1, 
        max_value=60, 
        value=int(st.session_state.get("refresh_rate_minutes", 5)),
        step=1,
        help="页面自动刷新间隔（1-60分钟）",
        key="refresh_slider"  # 添加key避免重复渲染
    )
    # 保存到 session_state，确保刷新后保持
    new_rate = int(refresh_rate_minutes)
    old_rate_value = st.session_state.get('refresh_rate_minutes', 5)
    st.session_state['refresh_rate_minutes'] = int(refresh_rate_minutes)
    # 转换为秒数用于实际刷新
    refresh_rate_sec = refresh_rate_minutes * 60
    st.session_state['refresh_rate_sec'] = refresh_rate_sec
    st.session_state['check_interval_seconds'] = refresh_rate_sec
    _now_local = datetime.now(timezone.utc).astimezone()
    _tz_label = local_tz_label()
    st.success(f"● Online | {_now_local.strftime('%H:%M:%S')} ({_tz_label})")

# ==========================================
# 定时刷新检查（在刷新率滑块更新之后执行，确保使用最新的check_interval_seconds值）
# ==========================================
# 避免并发调用，确保同一时间只有一个fetch在进行
check_interval = float(st.session_state.get('check_interval_seconds', 300))  # 重新读取最新的值
current_time_check = time.time()
# 使用文件系统的时间戳，确保即使页面刷新也能正确计算时间差
time_elapsed = current_time_check - last_api_check_timestamp
if not st.session_state.get('is_fetching', False) and time_elapsed >= check_interval:
    try:
        st.session_state['is_fetching'] = True
        from monitor_trump import run_fetch_recent
        result = run_fetch_recent(limit=5, fast_init=True)
    except Exception as e:
        print(f"Error in scheduled fetch: {e}")
    finally:
        # 更新文件系统和session_state中的时间戳
        new_timestamp = time.time()
        set_last_api_check(new_timestamp)
        st.session_state['last_api_check'] = new_timestamp
        st.session_state['is_fetching'] = False
        # 重新加载alerts并更新缓存
        st.session_state['cached_alerts'] = load_alerts()
        alerts = st.session_state['cached_alerts']
        # 注意：st.rerun() 已移除，因为它会阻止后续页面渲染代码的执行
        # 页面会在下一次用户交互或JavaScript自动刷新时更新
        # 不再调用 st.rerun()，让页面正常渲染，JavaScript会自动刷新

st.markdown("---")

# Metrics Grid
# 重新从session_state读取alerts，确保使用最新的数据（可能在定时刷新逻辑中已更新）
alerts = st.session_state.get('cached_alerts', [])
# 使用真正最新的告警（alerts[0]），即使它没有内容
latest = alerts[0] if alerts else None
alerts_vis = [a for a in alerts if str(display_text(a) or '').strip()]

if latest:
    high_impact_count = sum(1 for a in alerts if a.get('ai_analysis', {}).get('impact'))
    
    c1, c2, c3, c4 = st.columns(4)
    latest_ai = latest.get('ai_analysis', {}) or {}
    impact = "HIGH" if latest_ai.get('impact') else "LOW"
    impact_color = "#EF4444" if latest_ai.get('impact') else "#10B981"

    # 计算时间差并格式化
    def format_time_ago(minutes):
        """格式化时间差：60分钟内显示分钟，60分钟以上显示小时+分钟"""
        if minutes < 0:
            return "刚刚"
        if minutes < 60:
            return f"{minutes}分钟"
        hours = minutes // 60
        mins = minutes % 60
        if mins == 0:
            return f"{hours}小时"
        return f"{hours}小时{mins}分钟"

    try:
        _lts = pick_ts(latest)
        _ts = datetime.fromisoformat(_lts.replace('Z','+00:00'))
        if _ts.tzinfo is None:
            _ts = _ts.replace(tzinfo=timezone.utc)
        _age_min2 = int((datetime.now(timezone.utc) - _ts.astimezone(timezone.utc)).total_seconds() / 60)
        time_ago_str = format_time_ago(_age_min2)
    except Exception:
        _age_min2 = 0
        time_ago_str = "未知"

    metric_payloads = [
        ("Monitored Posts", len(alerts), None),
        ("High Impact Alerts", high_impact_count, "#EF4444" if high_impact_count > 0 else "#0F172A"),
        ("Latest Impact", impact, impact_color),
        ("Time Ago", time_ago_str, None),
    ]

    for col, payload in zip((c1, c2, c3, c4), metric_payloads):
        label, value, color = payload
        with col:
            st.markdown(metric_card(label, value, color), unsafe_allow_html=True)

    st.markdown("<br>", unsafe_allow_html=True)

    # HERO SECTION (Latest Post) - 使用真正最新的告警
    # 注意：如果 slider 刚刚改变，上面的 st.stop() 会阻止执行到这里
    st.markdown(render_alert_card(latest, latest=True), unsafe_allow_html=True)
    
    # FEED SECTION
    c_feed_title, c_feed_sort = st.columns([0.8, 0.2])
    with c_feed_title:
        st.subheader("📜 Recent Posts")
    
    # List Layout - 显示5个最新的推文（跳过顶部已显示的最新一个）
    # 使用 alerts（未过滤）确保有足够的推文，即使某些没有文字内容
    _feed = alerts[1:6]  # 从索引1开始，取5个（索引1,2,3,4,5）
    for _i, alert in enumerate(_feed):
        st.markdown(render_alert_card(alert, latest=False), unsafe_allow_html=True)

        if _i < len(_feed) - 1:
            st.divider()

    st.markdown("---")
    tab_archive, tab_gallery, tab_settings = st.tabs(["📚 Archive", "🖼️ Gallery", "⚙️ Settings"])
    with tab_archive:
        historical = alerts[6:] if len(alerts) > 6 else []
        f1, f2, f3 = st.columns([0.5, 0.25, 0.25])
        search_text = f1.text_input("Search text", key="archive_search", placeholder="Filter content, assets, reasoning…")
        sentiment_filter = f2.selectbox("Sentiment", ["All", "Positive", "Neutral", "Negative"], index=0)
        impact_filter = f3.selectbox("Impact", ["All", "High", "Low"], index=0)
        v1, v2 = st.columns([0.5, 0.5])
        view_mode = v1.radio("View mode", ["Cards", "Table"], horizontal=True, key="archive_view")
        page_size = v2.selectbox("Rows per page", [10, 20, 50], index=1, key="archive_page_size")
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
                text_blob = " ".join([(display_text(alert) or ""), ai.get('reasoning', '') or "", " ".join(map(str, ai.get('affected_assets', []) or []))]).lower()
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
        st.caption(f"Page {cur} / {pages} · Showing {len(slice_alerts)} of {total} filtered · {len(historical)} total")
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
    with tab_gallery:
        st.subheader("🖼️ Media Gallery")
        gallery_media = []
        for a in alerts[:20]:
            gallery_media.extend(a.get('media') or [])
        gallery_html = build_media_html(gallery_media, max_images=12, width=180)
        if gallery_html:
            st.markdown(gallery_html, unsafe_allow_html=True)
        else:
            st.info("No media available.")
    with tab_settings:
        st.subheader("⚙️ System Settings")
        st.caption("Adjust refresh rate and view API status")
        st.write(f"API Base URL: {get_api_base_url()}")
        st.write(f"Alerts file: {ALERTS_FILE}")
        st.write(f"Unified JSON: {get_api_base_url()}/api/export/dashboard")
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
