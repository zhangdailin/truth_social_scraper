import streamlit as st
import json
import time
import os
import re
import threading
import socket
import hashlib
from datetime import datetime, timezone
from urllib.parse import urlparse
from html import escape
from dotenv import load_dotenv

from utils import (
    ALERTS_FILE, 
    describe_media, 
    local_tz_label, 
    pick_ts, 
    to_local_str, 
    PROJECT_ROOT,
    get_media_paths_by_post_id,
    serialize_local_media,
)

# 简易缓存与跨会话抓取锁（依赖 PROJECT_ROOT，因此放在 utils 导入之后）
_ALERTS_CACHE = {"mtime": None, "data": None}
LOCK_DIR = os.path.join(PROJECT_ROOT, ".cursor")
FETCH_LOCK_FILE = os.path.join(LOCK_DIR, "fetch.lock")
LOCK_STALE_SECONDS = 240  # 4 分钟后视为陈旧锁

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
    """在后台线程中启动API服务器

    Returns:
        (ok: bool, err: str|None)
    """
    try:
        from api import run_api_server
        # 使用守护线程，确保主程序退出时线程也会退出
        api_thread = threading.Thread(
            target=run_api_server,
            args=(API_HOST, API_PORT),
            daemon=True,
        )
        api_thread.start()
        # 等待一小段时间确保服务器启动
        time.sleep(1)
        return True, None
    except Exception as e:
        err = f"{type(e).__name__}: {e}"
        print(f"Failed to start API server: {err}")
        return False, err

# 检查并启动API服务器（只启动一次）
if 'api_server_started' not in st.session_state:
    try:
        if not is_port_in_use(API_HOST, API_PORT):
            ok, err = start_api_server()
            if ok:
                st.session_state['api_server_started'] = True
                st.session_state['api_error'] = None
            else:
                st.session_state['api_server_started'] = False
                st.session_state['api_error'] = err or "启动失败"
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

st.markdown("""
<style>
    *, *::before, *::after { box-sizing: border-box; }
    html, body, #root, .stApp, [data-testid="stAppViewContainer"], section.main {
        width: 100% !important;
        max-width: 100% !important;
        min-width: 0 !important;
        margin: 0 !important;
        overflow-x: hidden !important;
    }
    html, body, [class*="css"] {
        background: #F8FAFC;
        color: #0F172A;
        font-family: "Inter", "Segoe UI", system-ui, -apple-system, sans-serif;
    }
    .block-container {
        width: 100% !important;
        max-width: 900px !important;
        margin: 0 auto !important;
        padding-top: 0.75rem !important;
        padding-bottom: 0 !important;
    }
    h1, h2, h3 { color: #0F172A; font-weight: 700; letter-spacing: -0.01em; }
    hr { margin: 8px 0 !important; border-top: 1px solid #E2E8F0 !important; }

    .app-header {
        background: linear-gradient(135deg, #FFFFFF 0%, #F8FBFF 100%);
        border: 1px solid #E2E8F0;
        border-top: 3px solid #2563EB;
        border-radius: 14px;
        padding: 15px 18px;
        margin-bottom: 10px;
        box-shadow: 0 6px 18px rgba(15, 23, 42, 0.05);
        max-width: 760px;
        margin-left: auto;
        margin-right: auto;
    }
    .app-header-row { display:flex; justify-content:space-between; align-items:center; gap:18px; flex-wrap:wrap; }
    .brand-block { display:flex; align-items:center; gap:12px; min-width:0; }
    .brand-mark {
        width:42px; height:42px; border-radius:12px; flex-shrink:0;
        display:flex; align-items:center; justify-content:center;
        color:#fff; background:linear-gradient(135deg,#2563EB,#1E40AF);
        font-size:14px; font-weight:900; letter-spacing:-0.04em;
        box-shadow:0 6px 12px rgba(37,99,235,0.22);
    }
    .app-eyebrow { color:#2563EB; font-size:11px; font-weight:800; letter-spacing:0.04em; }
    .app-title { color:#0F172A; font-size:23px; font-weight:800; letter-spacing:-0.025em; margin-top:1px; }
    .app-subtitle { color:#64748B; font-size:13px; margin-top:3px; }
    .header-status { text-align:right; }
    .status-pill { display:inline-flex; align-items:center; gap:6px; padding:6px 10px; border-radius:999px; font-size:11px; font-weight:800; white-space:nowrap; }
    .status-online { color:#047857; background:#ECFDF5; border:1px solid #A7F3D0; }
    .status-offline { color:#B91C1C; background:#FEF2F2; border:1px solid #FECACA; }
    .sync-meta { color:#64748B; font-size:11px; margin-top:6px; }
    .filter-shell {
        background: #ffffff;
        border: 1px solid #E2E8F0;
        border-radius: 14px;
        padding: 10px 12px 4px;
        margin-bottom: 12px;
        box-shadow: 0 5px 18px rgba(15, 23, 42, 0.04);
    }
    .asset-chip { display:inline-block; padding:3px 8px; margin:3px 4px 0 0; border-radius:999px; color:#1D4ED8; background:#DBEAFE; font-size:11px; font-weight:800; }

    .social-feed { max-width: 720px; margin: 0 auto; }
    .feed-pagination { max-width:720px; margin:18px auto 10px; }
    .feed-page-info {
        position: relative;
        z-index: 5;
        overflow: visible;
        text-align:center;
        color:#64748B;
        font-size:12px;
        padding-top:7px;
    }
    .hero-card, .feed-item {
        background: #fff;
        border: 1px solid #E5E7EB;
        border-radius: 12px;
        padding: 0 0 14px;
        margin-bottom: 12px;
        box-shadow: 0 1px 3px rgba(15, 23, 42, 0.04);
        /* 股票走势图是卡片内的绝对定位悬浮层，不能被卡片裁切。媒体自身仍由
           .media-item 负责 overflow:hidden，图片/视频圆角不会受影响。 */
        overflow: visible;
    }
    .hero-alert-high { border-left: 5px solid #ef4444; }
    .hero-alert-low  { border-left: 5px solid #10b981; }

    .feed-head { display:flex; align-items:flex-start; gap:10px; padding:14px 16px 0; }
    .avatar {
        width: 42px; height: 42px; border-radius: 50%; flex-shrink: 0;
        background: linear-gradient(135deg,#1e293b,#334155);
        display:flex; align-items:center; justify-content:center; color:#fff; font-weight:800;
        border: 2px solid #e2e8f0;
    }
    .author-line { display:flex; align-items:center; gap:8px; flex-wrap:wrap; }
    .author-block { flex:1; min-width:0; }
    .impact-pill { flex:0 0 auto; padding:5px 9px; border-radius:999px; font-size:10px; font-weight:850; white-space:nowrap; }
    .impact-pill-high { color:#B91C1C; background:#FEF2F2; border:1px solid #FECACA; }
    .impact-pill-low { color:#475569; background:#F1F5F9; border:1px solid #CBD5E1; }
    .author-name { font-weight: 800; color:#111827; font-size:16px; }
    .verified-badge { display:inline-flex; align-items:center; justify-content:center; width:16px; height:16px; border-radius:50%; color:#fff; background:#EC4899; font-size:10px; font-weight:900; }
    .post-meta { color:#6B7280; font-size:13px; margin-top:2px; }
    .post-time { color:#64748b; font-size:12px; }
    .post-content { font-size: 15px; line-height: 1.62; color: #1f2937; margin: 13px 16px 8px; white-space:pre-wrap; overflow-wrap:anywhere; }
    .feed-media { margin: 14px 16px 0; }
    .ai-note { position:relative; z-index:2; background:#F8FAFC; border:1px solid #E2E8F0; border-radius:10px; padding:11px 12px 10px; color:#475569; font-size:12px; line-height:1.5; margin:12px 16px 0; }
    .ai-panel-head { display:flex; align-items:center; gap:7px; flex-wrap:wrap; padding-bottom:8px; border-bottom:1px solid #E2E8F0; }
    .ai-label { color:#0F172A; font-size:12px; font-weight:850; }
    .ai-label-mark { display:inline-flex; align-items:center; justify-content:center; width:20px; height:20px; margin-right:5px; border-radius:6px; color:#fff; background:#2563EB; font-size:9px; font-weight:900; }
    .ai-type, .ai-confidence { color:#475569; background:#fff; border:1px solid #E2E8F0; border-radius:999px; padding:3px 7px; font-size:10px; font-weight:750; }
    .ai-summary { color:#1E293B; font-size:13px; font-weight:700; line-height:1.5; padding:9px 0 7px; }
    .ai-meta-row { position:relative; z-index:2; display:flex; align-items:center; gap:7px; flex-wrap:wrap; color:#64748B; font-size:11px; }
    .ai-meta-label { color:#64748B; font-weight:750; }
    .ai-reason { margin-top:9px; padding-top:9px; border-top:1px solid #E2E8F0; color:#475569; }
    .ai-reason-label { display:block; color:#334155; font-size:11px; font-weight:850; margin-bottom:3px; }
    .ai-media-summary { margin-top:9px; padding-top:9px; border-top:1px dashed #CBD5E1; color:#64748B; font-size:11px; line-height:1.55; }
    .ai-details { margin-top:9px; padding-top:8px; border-top:1px solid #E2E8F0; }
    .ai-details summary { cursor:pointer; color:#334155; font-size:11px; font-weight:850; list-style:none; }
    .ai-details summary::-webkit-details-marker { display:none; }
    .ai-details summary::before { content:'＋'; display:inline-block; margin-right:4px; color:#2563EB; font-weight:900; }
    .ai-details[open] summary::before { content:'−'; }
    .ai-details-body { padding-top:5px; color:#475569; line-height:1.6; }

    .media-grid {
        margin: 0 0 8px;
        display: grid;
        gap: 2px;
        grid-template-columns: repeat(2, minmax(0, 1fr));
        justify-content: stretch;
        align-items: stretch;
        overflow: hidden;
        border-radius: 14px;
        background: #E5E7EB;
    }
    .media-grid.media-count-1 { grid-template-columns: minmax(0, 1fr); }
    .media-grid.media-count-3,
    .media-grid.media-count-4 { grid-template-columns: repeat(2, minmax(0, 1fr)); }
    .media-item {
        position: relative;
        overflow: hidden;
        width: 100%;
        max-width: none;
        min-height: 0;
        border-radius: 0;
        border: 0;
        background: #E5E7EB;
        box-shadow: none;
    }
    .media-item img, .media-item video {
        width: 100%;
        height: 100%;
        min-height: 0;
        max-height: none;
        object-fit: cover;
        display: block;
        background: #E5E7EB;
    }
    .media-grid.media-count-1 .media-item { max-height: 560px; }
    .media-grid.media-count-1 .media-item img,
    .media-grid.media-count-1 .media-item video { height: auto; max-height: 560px; object-fit: contain; }
    .media-grid.media-count-2 .media-item,
    .media-grid.media-count-3 .media-item,
    .media-grid.media-count-4 .media-item { aspect-ratio: 1.08 / 1; }
    .media-grid.media-count-3 .media-item:first-child { grid-row: span 2; aspect-ratio: auto; }
    .media-grid.media-count-3 .media-item:first-child img,
    .media-grid.media-count-3 .media-item:first-child video { height: 100%; }
    .social-feed .media-item { max-width:100%; }
    .media-download {
        position: absolute;
        top: 8px;
        right: 8px;
        z-index: 3;
        padding: 5px 9px;
        border-radius: 6px;
        color: #fff;
        background: rgba(15, 23, 42, 0.78);
        text-decoration: none;
        font-size: 11px;
        font-weight: 700;
    }
    .media-open {
        position: absolute;
        right: 8px;
        bottom: 8px;
        z-index: 3;
        padding: 5px 8px;
        border-radius: 6px;
        color: #fff;
        background: rgba(15, 23, 42, 0.78);
        text-decoration: none;
        font-size: 14px;
        line-height: 1;
    }
    .media-more { display: flex; align-items: center; justify-content: center; color: #1F2937; background: #E5E7EB; font-weight: 800; font-size: 16px; gap: 6px; }
    .media-more span { font-size: 12px; color: #475569; font-weight: 600; letter-spacing: 0.5px; }
    .media-modal { display:none; position:fixed; z-index:1000; inset:0; background:rgba(0,0,0,0.75); }
    .media-modal:target { display:block; }
    .media-modal-content { position:relative; margin:40px auto; max-width:90%; max-height:85%; background:#000; border-radius:8px; padding:10px; }
    .media-modal-close { position:absolute; right:10px; top:6px; color:#fff; font-size:24px; text-decoration:none; }

    .stock-tooltip { position: relative; z-index: 10; display: inline-block; border-bottom: 2px dashed #F59E0B; cursor: help; font-weight: 700; color: #0F172A; }
    .stock-tooltip .tooltip-content { visibility: hidden; width: 420px; max-width: min(420px, calc(100vw - 24px)); background: #ffffff; text-align: center; border-radius: 10px; padding: 10px; position: absolute; z-index: 100000; top: calc(100% + 10px); left: 50%; margin-left: -210px; opacity: 0; pointer-events: none; transition: opacity 0.2s, visibility 0.2s; box-shadow: 0 12px 24px rgba(15, 23, 42, 0.16); border: 1px solid #E2E8F0; color: #1F2937;}
    .stock-tooltip:hover .tooltip-content { visibility: visible; opacity: 1; }
    .tooltip-image { width: 100%; height: auto; border-radius: 6px; }
    .stock-tooltip .tooltip-arrow { position: absolute; bottom: 100%; left: 50%; margin-left: -5px; border-width: 5px; border-style: solid; border-color: transparent transparent #E2E8F0 transparent; }

    #MainMenu, footer, header { visibility: hidden; }
    .stButton > button {
        background: #0F172A; color: #fff; border: 1px solid #0F172A; border-radius: 10px;
        min-height: 42px; padding: 0.55rem 0.85rem; font-weight: 700;
        box-shadow: 0 6px 14px rgba(15,23,42,0.12); touch-action: manipulation;
    }
    .stTextInput input, .stTextArea textarea, .stSelectbox div[data-baseweb="select"] > div {
        border-radius: 10px !important; border: 1px solid #E2E8F0 !important; background: #fff !important;
    }
    .stTabs [data-baseweb="tab-list"] { gap: 6px; border-bottom: 1px solid #E2E8F0; }
    .stTabs [data-baseweb="tab"] { padding: 10px 14px; color:#64748B; font-weight:700; }
    .stTabs [aria-selected="true"] { color:#1D4ED8 !important; }
    div[data-testid="stMetric"] { background:#fff; border:1px solid #E2E8F0; border-radius:12px; padding:12px; }
    @media (max-width: 768px) {
        html, body, #root, .stApp, [data-testid="stAppViewContainer"], section.main,
        section.main > div, section.main > div > div {
            width: 100% !important;
            max-width: 100% !important;
            min-width: 0 !important;
            overflow-x: hidden !important;
        }
        .block-container {
            width: 100% !important;
            max-width: none !important;
            padding: 0 12px 12px !important;
        }
        .app-header, .filter-shell, .hero-card,
        .feed-pagination, .social-feed {
            width: 100% !important;
            max-width: none !important;
            margin-left: 0 !important;
            margin-right: 0 !important;
        }
        .app-header, .filter-shell {
            border-radius: 12px !important;
        }
        .hero-card {
            border-left-width: 4px;
            border-right: 0 !important;
            border-radius: 0 !important;
            padding-left: 12px;
            padding-right: 12px;
        }
        .feed-head { padding:13px 12px 0; }
        .post-content { margin-left:12px; margin-right:12px; }
        .feed-media { margin-left:12px; margin-right:12px; }
        .ai-note { margin-left:12px; margin-right:12px; }
        .header-status { text-align:left; width:100%; }
        .app-header-row { gap:10px; }
        [data-testid="stHorizontalBlock"] { gap:6px !important; }
        [data-testid="stTextInput"] label, [data-testid="stSelectbox"] label { font-size:10px !important; }
        .app-title { font-size:20px; }
        .metric-value { font-size:18px; }
        .post-content { font-size:14px; }
        .media-grid { grid-template-columns: repeat(2, minmax(0, 1fr)) !important; gap: 2px; border-radius: 12px; }
        .media-grid.media-count-1 { grid-template-columns: minmax(0, 1fr) !important; }
        .media-item { width: 100%; max-width: 100%; }
        .media-item img, .media-item video { max-width: 100%; max-height: none; }
        .media-grid.media-count-1 .media-item img,
        .media-grid.media-count-1 .media-item video { max-height: 70vh; }
        .stock-tooltip .tooltip-content {
            position: fixed !important;
            left: 8px !important;
            right: 8px !important;
            bottom: 12px !important;
            top: auto !important;
            width: auto !important;
            max-width: none !important;
            margin-left: 0 !important;
        }
        .stock-tooltip .tooltip-arrow { display:none; }
        .feed-page-info {
            position: relative;
            z-index: 5;
            overflow: visible !important;
            white-space: normal;
            font-size: 11px;
            line-height: 1.35;
            padding-top: 4px;
        }
        .feed-page-info .page-current,
        .feed-page-info .page-total {
            display: block;
        }
        /* 移动端分页按钮需要足够大的触摸命中区，避免首次点击只获得焦点。 */
        [data-testid="stButton"] > button {
            width: 100% !important;
            min-height: 40px !important;
            padding: 0 2px !important;
            font-size: 12px !important;
            white-space: nowrap !important;
            touch-action: manipulation;
            -webkit-tap-highlight-color: rgba(37, 99, 235, 0.18);
        }
        [data-testid="stSelectbox"] [role="combobox"] {
            min-height: 40px !important;
        }
        /* Streamlit 在手机端默认会把 columns 纵向堆叠；只恢复分页这一行的三列布局。 */
        [data-testid="stHorizontalBlock"]:has(.feed-pagination-hook) {
            display: flex !important;
            flex-direction: row !important;
            flex-wrap: nowrap !important;
            align-items: center !important;
            gap: 6px !important;
        }
        [data-testid="stHorizontalBlock"]:has(.feed-pagination-hook) > [data-testid="stColumn"] {
            min-width: 0 !important;
        }
        [data-testid="stHorizontalBlock"]:has(.feed-pagination-hook) > [data-testid="stColumn"]:nth-child(1),
        [data-testid="stHorizontalBlock"]:has(.feed-pagination-hook) > [data-testid="stColumn"]:nth-child(3) {
            flex: 0 0 20% !important;
            width: 20% !important;
        }
        [data-testid="stHorizontalBlock"]:has(.feed-pagination-hook) > [data-testid="stColumn"]:nth-child(2) {
            flex: 0 0 60% !important;
            width: 60% !important;
        }
    }

    /* 跟随系统深色模式：保证 Streamlit 原生控件和自定义信息流都有足够对比度。 */
    @media (prefers-color-scheme: dark) {
        html, body, #root, .stApp, [data-testid="stAppViewContainer"],
        section.main, section.main > div, .block-container {
            background: #0B1120 !important;
            color: #E5E7EB !important;
        }
        [data-testid="stHeader"] { background: #0B1120 !important; }
        h1, h2, h3, p, label, [data-testid="stMarkdownContainer"] {
            color: #E5E7EB;
        }
        hr { border-top-color: #334155 !important; }

        .app-header, .filter-shell, .hero-card, .feed-item {
            background: #111827 !important;
            border-color: #334155 !important;
            color: #E5E7EB !important;
            box-shadow: 0 8px 24px rgba(0, 0, 0, 0.24);
        }
        .app-header { background: linear-gradient(135deg, #111827 0%, #172554 100%) !important; }
        .app-title, .author-name, .ai-label, .ai-summary, .stock-tooltip {
            color: #F8FAFC !important;
        }
        .app-subtitle, .app-eyebrow, .sync-meta, .post-meta, .post-time,
        .feed-page-info, .ai-meta-row, .ai-meta-label, .ai-media-summary {
            color: #CBD5E1 !important;
        }
        .post-content { color: #F1F5F9 !important; }
        .ai-note { background: #172033 !important; border-color: #334155 !important; color: #E2E8F0 !important; }
        .ai-panel-head, .ai-details { border-color: #334155 !important; }
        .ai-type, .ai-confidence { background: #1E293B !important; border-color: #475569 !important; color: #E2E8F0 !important; }
        .ai-details summary, .ai-details-body, .ai-reason, .ai-reason-label { color: #CBD5E1 !important; }
        .impact-pill-high { background: #451A1A !important; border-color: #7F1D1D !important; color: #FCA5A5 !important; }
        .impact-pill-low { background: #1E293B !important; border-color: #475569 !important; color: #CBD5E1 !important; }
        .asset-chip { background: #172554 !important; color: #BFDBFE !important; }

        .stTextInput label, .stTextArea label, .stSelectbox label { color: #CBD5E1 !important; }
        .stTextInput input, .stTextArea textarea,
        .stSelectbox div[data-baseweb="select"] > div,
        [data-testid="stSelectbox"] [role="combobox"] {
            background: #1E293B !important;
            color: #F8FAFC !important;
            border-color: #475569 !important;
            -webkit-text-fill-color: #F8FAFC !important;
        }
        .stTextInput input::placeholder, .stTextArea textarea::placeholder { color: #94A3B8 !important; opacity: 1; }
        [data-baseweb="popover"], [role="listbox"], [role="option"] {
            background: #1E293B !important;
            color: #F8FAFC !important;
        }
        [role="option"]:hover, [aria-selected="true"] { background: #334155 !important; color: #FFFFFF !important; }
        .stButton > button { background: #2563EB !important; border-color: #3B82F6 !important; color: #FFFFFF !important; }
        .stButton > button:disabled { background: #1E293B !important; border-color: #334155 !important; color: #64748B !important; }

        .media-grid { background: #334155 !important; }
        .media-item, .media-item img, .media-item video { background: #020617 !important; }
        .media-more { background: #1E293B !important; color: #F8FAFC !important; }
        .media-more span { color: #CBD5E1 !important; }
        .media-modal-content { background: #020617 !important; }
        .stock-tooltip .tooltip-content {
            background: #1E293B !important;
            border-color: #475569 !important;
            color: #F8FAFC !important;
        }
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

        # 视频的本地映射历史上可能同时包含视频缩略图；缩略图只用于
        # 下载/兼容回退，不应作为帖子媒体再次展示。混合图文帖子仍保留真实图片。
        media_is_video = lambda item: (
            (str(item.get("type") or "").lower() in ("video", "gifv")) or
            any(ext in str(item.get("url") or "").lower()
                for ext in (".mp4", ".webm", ".mov", ".avi", ".mkv", "/videos/"))
        ) if isinstance(item, dict) else False
        has_video_media = any(media_is_video(item) for item in media)
        has_image_media = any(not media_is_video(item) for item in media)
        if local_media_paths and has_video_media and not has_image_media:
            local_media_paths = [
                path for path in local_media_paths
                if any(ext in path.lower() for ext in (".mp4", ".webm", ".mov", ".avi", ".mkv"))
            ]

        # 如果有本地文件，优先使用本地文件（完全使用映射，不再处理media列表，避免重复）
        if local_media_paths:
            # 计算实际可显示媒体数量（保留视频+图片，不再因有视频而隐藏图片）
            displayable_count = sum(1 for local_path in local_media_paths if os.path.exists(local_path))
            
            for i, local_path in enumerate(local_media_paths):
                if len(items) >= max_images:
                    break
                
                if not os.path.exists(local_path):
                    continue
                
                try:
                    rel_path = os.path.relpath(local_path, MEDIA_DIR)
                    rel_path_normalized = rel_path.replace('\\', '/').lstrip('/')
                    api_path = f"{api_base_url}/api/media/{rel_path_normalized}"
                    is_video = any(ext in local_path.lower() for ext in ['.mp4', '.webm', '.mov', '.avi', '.mkv'])
                    if is_video:
                        video_mime = _video_mime_type(local_path)
                        items.append(
                            f'<div class="media-item media-video">'
                            f'<video playsinline controls preload="metadata" crossorigin="anonymous">'
                            f'<source src="{api_path}" type="{video_mime}"></video>'
                            f'<a class="media-download" href="{api_path}" download>⬇ 下载</a>'
                            f'</div>'
                        )
                    else:
                        items.append(
                            f'<div class="media-item media-image" style="cursor: zoom-in;">'
                            f'<img src="{api_path}" alt="" loading="lazy" decoding="async" {img_onerror} />'
                            f'</div>'
                        )
                except Exception:
                    continue
            
            if items:
                remaining = max(0, displayable_count - len(items))
                if remaining > 0:
                    items.append(f'<div class="media-item media-more">+{remaining} <span>more</span></div>')
                media_class = f"media-grid media-count-{min(4, max(1, len(items)))}"
                return f'<div id="media-grid-{post_id}" class="{media_class}" style="--media-min:{min_col}px;">' + ''.join(items) + '</div>'
        
        # 如果没有通过推文ID找到本地文件，使用media列表中的URL（向后兼容）
        if not local_media_paths:
            # 保留视频与图片，避免“有视频时图片被隐藏”
            displayable_count = 0
            filtered_media = []
            seen_media_keys = set()
            for m in media:
                vsrc_check = m.get("url") or m.get("remote_url") or ""
                poster_check = m.get("preview_url") or ""
                key = (str(vsrc_check).strip(), str(poster_check).strip(), str(m.get("type") or "").lower())
                if key in seen_media_keys:
                    continue
                seen_media_keys.add(key)
                if poster_check or vsrc_check:
                    filtered_media.append(m)
                    displayable_count += 1

            for m in filtered_media:
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


                if is_video and vsrc:
                    video_mime = _video_mime_type(vsrc)
                    video_id = f"plyr-video-{hashlib.md5(vsrc.encode()).hexdigest()[:8]}"
                    modal_id = f"media-modal-{post_id}-{len(items)}"
                    video_html = (
                        f'<div class="media-item media-video">'
                        f'<video id="{video_id}" playsinline controls preload="metadata" crossorigin="anonymous">'
                        f'<source src="{vsrc}" type="{video_mime}"></video>'
                        f'<a class="media-download" href="{vsrc}" download>⬇ 下载</a>'
                        f'<a class="media-open" href="#{modal_id}" aria-label="放大视频">⛶</a>'
                        f'</div>'
                    )
                    items.append(video_html)
                    modals.append(
                        f'<div id="{modal_id}" class="media-modal"><div class="media-modal-content">'
                        f'<a href="#" class="media-modal-close">×</a>'
                        f'<video controls preload="metadata" style="width:100%; max-height:80vh; background:#000;"><source src="{vsrc}" type="{video_mime}"></video>'
                        f'</div></div>'
                    )
                else:
                    src = poster or vsrc or ""
                    if src:
                        modal_id = f"media-modal-{post_id}-{len(items)}"
                        items.append(
                            f'<a href="#{modal_id}" class="media-item media-image" style="cursor: zoom-in;">'
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

        media_class = f"media-grid media-count-{min(4, max(1, len(items)))}"
        return f'<div id="media-grid-{post_id}" class="{media_class}" style="--media-min:{min_col}px;">' + ''.join(items) + '</div>' + ''.join(modals)
    except Exception:
        return ""

def display_text(item):
    try:
        c = (item.get('content') or '').strip()
        # 媒体帖子的占位内容不是正文，不在信息流重复显示。
        if re.fullmatch(r"\[(?:图片|视频)\]\s*\d+\s*(?:张|个)", c):
            return ""
        # 搜索关键词是后台上下文，不是特朗普的帖子正文。
        if c.lower().startswith("keywords:"):
            return ""
        if c:
            return c
        # 媒体专属帖子不再把“[图片] 1 张”伪装成正文，
        # 避免在媒体卡片上重复显示媒体描述。
        return ""
    except Exception:
        return item.get('content','')

def _video_mime_type(value):
    """根据本地路径或远程 URL 返回正确的视频 MIME 类型。"""
    raw = str(value or "").split("?", 1)[0].lower()
    ext = os.path.splitext(raw)[1]
    return {
        ".mp4": "video/mp4",
        ".m4v": "video/mp4",
        ".webm": "video/webm",
        ".mov": "video/quicktime",
        ".mkv": "video/x-matroska",
        ".avi": "video/x-msvideo",
        ".ogv": "video/ogg",
    }.get(ext, "video/mp4")

def render_asset_chips(assets):
    """Render affected assets with a hover chart, matching the original dashboard behavior."""
    chips = []
    seen = set()
    for asset in assets or []:
        symbol = re.sub(r"[^A-Za-z0-9.\-]", "", str(asset).strip().upper())
        if not symbol or symbol in seen or not re.fullmatch(r"[A-Z0-9][A-Z0-9.\-]{0,11}", symbol):
            continue
        seen.add(symbol)
        chart_html = get_chart_image_html(symbol)
        chips.append(
            f'<span class="stock-tooltip asset-chip">{escape(symbol)}'
            f'<div class="tooltip-content">{chart_html}</div>'
            f'<div class="tooltip-arrow"></div></span>'
        )
    return "".join(chips) or '<span style="color:#64748B;">暂无明确资产</span>'

def render_alert_card(alert, latest=False):
    ai = alert.get('ai_analysis', {}) or {}
    is_high = ai.get('impact', False)
    impact_class = "hero-alert-high" if is_high else "hero-alert-low"
    impact_label = '高影响' if is_high else '低影响'
    tz_lbl = local_tz_label()
    ts_disp = to_local_str(pick_ts(alert))
    post_id = str(alert.get('id', ''))
    media_candidates = alert.get('media') or []
    local_media_count = len(get_local_media_paths_by_post_id(post_id)) if post_id else 0
    media_limit = max(1, len(media_candidates), local_media_count)
    media_html = build_media_html(media_candidates, media_limit, 220 if latest else 180, post_id=post_id)

    content = (display_text(alert) or '').strip()
    try:
        content_augmented = inject_stock_tooltips(content, ai.get('affected_assets', []) or [])
    except Exception:
        content_augmented = content
    content_block = f'<div class="post-content">{content_augmented}</div>' if content_augmented else ''

    reasoning = (ai.get('reasoning') or 'AI 分析尚未完成。').strip()
    sentiment = (ai.get('sentiment') or '未判定').strip()
    assets = [str(asset).strip().upper() for asset in (ai.get('affected_assets', []) or []) if str(asset).strip()]
    asset_html = render_asset_chips(assets[:8])

    media_summary = (ai.get('media_ai_summary', '') or '').strip()
    impact_summary = 'AI 判断该内容可能造成短期市场波动。' if is_high else 'AI 暂未发现明显的市场影响信号。'
    impact_type_labels = {
        'policy': '政策', 'company': '公司', 'industry': '行业',
        'macro': '宏观', 'political_only': '仅政治', 'noise': '噪声',
        'personal': '个人言论', 'unknown': '未分类',
    }
    impact_type = impact_type_labels.get(str(ai.get('impact_type') or 'unknown').lower(), '未分类')
    confidence = ai.get('confidence')
    confidence_text = f"{float(confidence) * 100:.0f}%" if isinstance(confidence, (int, float)) else '—'
    media_block = f'<div class="feed-media">{media_html}</div>' if media_html else ''
    media_analysis_block = (
        f'<details class="ai-details"><summary>查看媒体观察</summary><div class="ai-details-body">{escape(media_summary)}</div></details>'
        if media_summary and media_candidates else ''
    )
    return f"""<div class="hero-card social-feed {impact_class}">
<div class="feed-head">
  <div class="avatar">DT</div>
  <div class="author-block">
    <div class="author-line">
      <span class="author-name">Donald J. Trump</span>
      <span class="verified-badge">✓</span>
    </div>
    <div class="post-meta">@realDonaldTrump · {ts_disp} ({tz_lbl})</div>
  </div>
  <span class="impact-pill {'impact-pill-high' if is_high else 'impact-pill-low'}">{impact_label}</span>
</div>
{content_block}
{media_block}
<div class="ai-note">
  <div class="ai-panel-head">
    <div class="ai-label"><span class="ai-label-mark">AI</span>判断</div>
    <span class="ai-type">{impact_type}</span>
    <span class="ai-confidence">置信度 {confidence_text}</span>
  </div>
  <div class="ai-summary">{impact_summary}</div>
  <div class="ai-meta-row"><span class="ai-meta-label">情绪</span>{escape(sentiment)} <span class="ai-meta-label">影响资产</span>{asset_html}</div>
  <details class="ai-details"><summary>查看判断依据</summary><div class="ai-details-body">{escape(reasoning)}</div></details>
  {media_analysis_block}
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
    
        local_media_paths = get_media_paths_by_post_id(post_id)
        if local_media_paths:
            converted = serialize_local_media(media_list, local_media_paths)
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

def _acquire_fetch_lock():
    """跨会话文件锁，避免多实例/多用户并发抓取造成重复请求。"""
    try:
        os.makedirs(LOCK_DIR, exist_ok=True)
        ts = str(time.time())
        # 尝试创建锁文件；存在则视为被占用
        fd = os.open(FETCH_LOCK_FILE, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        with os.fdopen(fd, "w") as f:
            f.write(ts)
        return True
    except FileExistsError:
        try:
            mtime = os.path.getmtime(FETCH_LOCK_FILE)
            if time.time() - mtime > LOCK_STALE_SECONDS:
                os.remove(FETCH_LOCK_FILE)
                return _acquire_fetch_lock()
        except Exception:
            pass
        return False
    except Exception:
        return False

def _release_fetch_lock():
    try:
        if os.path.exists(FETCH_LOCK_FILE):
            os.remove(FETCH_LOCK_FILE)
    except Exception:
        pass

def load_alerts():
    if not os.path.exists(ALERTS_FILE):
        return []
    
    # mtime 缓存，避免高频重复解析
    try:
        mtime = os.path.getmtime(ALERTS_FILE)
        if _ALERTS_CACHE["mtime"] == mtime and _ALERTS_CACHE["data"] is not None:
            return _ALERTS_CACHE["data"]
    except Exception:
        pass
    
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
    
    # 更新缓存
    try:
        _ALERTS_CACHE["mtime"] = os.path.getmtime(ALERTS_FILE)
        _ALERTS_CACHE["data"] = deduped
    except Exception:
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
# 自动刷新固定为 1 分钟，不在页面显示可调控件。
st.session_state['refresh_rate_minutes'] = 1
# 固定数据拉取间隔
refresh_rate_minutes_current = 1
check_interval_seconds_expected = int(refresh_rate_minutes_current) * 60
    # 如果 check_interval_seconds 与固定刷新间隔不同步，更新它
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
# 4. DASHBOARD HEADER & CONTROLS
# ==========================================

# 统一的应用头部：品牌与服务状态
api_started = st.session_state.get('api_server_started', False)
api_error = st.session_state.get('api_error')
api_status_text = "API 在线" if api_started else "API 离线"
status_class = "status-online" if api_started else "status-offline"
_now_local = datetime.now(timezone.utc).astimezone()
_tz_label = local_tz_label()

st.markdown(
    f"""<div class="app-header">
    <div class="app-header-row">
      <div class="brand-block">
        <div class="brand-mark">TS</div>
        <div>
          <div class="app-eyebrow">TRUTH SOCIAL · LIVE</div>
          <div class="app-title">特朗普动态监控</div>
          <div class="app-subtitle">@realDonaldTrump · 帖子与市场影响</div>
        </div>
      </div>
      <div class="header-status">
        <span class="status-pill {status_class}">● 实时连接</span>
        <div class="sync-meta">每分钟更新 · {_now_local.strftime('%H:%M:%S')} {_tz_label}</div>
      </div>
    </div>
    </div>""",
    unsafe_allow_html=True,
)
if api_error:
    st.warning(f"API 连接异常：{api_error}")

# 自动刷新固定为 1 分钟，页面不显示刷新控件。
refresh_rate_minutes = 1
refresh_rate_sec = 60
st.session_state['refresh_rate_minutes'] = refresh_rate_minutes
st.session_state['refresh_rate_sec'] = refresh_rate_sec
st.session_state['check_interval_seconds'] = refresh_rate_sec

# ==========================================
# 定时刷新检查（在刷新率滑块更新之后执行，确保使用最新的check_interval_seconds值）
# ==========================================
# 避免并发调用，确保同一时间只有一个fetch在进行
check_interval = float(st.session_state.get('check_interval_seconds', 300))  # 重新读取最新的值
current_time_check = time.time()
# 使用文件系统的时间戳，确保即使页面刷新也能正确计算时间差
time_elapsed = current_time_check - last_api_check_timestamp
if not st.session_state.get('is_fetching', False) and time_elapsed >= check_interval:
    lock_acquired = _acquire_fetch_lock()
    if not lock_acquired:
        # 另一实例正在抓取，直接跳过本轮，避免重复请求
        pass
    else:
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
            _release_fetch_lock()
            # 重新加载alerts并更新缓存
            st.session_state['cached_alerts'] = load_alerts()
            alerts = st.session_state['cached_alerts']
            # 注意：st.rerun() 已移除，因为它会阻止后续页面渲染代码的执行
            # 页面会在下一次用户交互或JavaScript自动刷新时更新
            # 不再调用 st.rerun()，让页面正常渲染，JavaScript会自动刷新

# ==========================================
# SOCIAL FEED
# ==========================================
# 主页面直接展示全部动态，图片和视频跟随帖子内联显示。
alerts = st.session_state.get('cached_alerts', [])

f_col1, f_col2, f_col3 = st.columns([0.52, 0.24, 0.24])
feed_search = f_col1.text_input(
    "搜索",
    value=st.session_state.get('feed_search', ''),
    key='feed_search',
    placeholder="搜索正文、资产或 AI 判断",
)
feed_impact = f_col2.selectbox(
    "影响",
    ["全部", "高影响", "低影响"],
    index=0,
    key='feed_impact_filter',
)
feed_media = f_col3.selectbox(
    "媒体",
    ["全部", "含媒体", "纯文本"],
    index=0,
    key='feed_media_filter',
)

def _social_match(alert):
    ai = alert.get('ai_analysis', {}) or {}
    impact_val = '高影响' if ai.get('impact') else '低影响'
    has_media = bool(alert.get('media'))
    blob = ' '.join([
        (display_text(alert) or ''),
        (ai.get('reasoning') or ''),
        ' '.join(map(str, ai.get('affected_assets', []) or [])),
    ]).lower()
    if feed_search and feed_search.lower().strip() not in blob:
        return False
    if feed_impact != '全部' and impact_val != feed_impact:
        return False
    if feed_media == '含媒体' and not has_media:
        return False
    if feed_media == '纯文本' and has_media:
        return False
    return True

filtered_alerts = [a for a in alerts if _social_match(a)]
page_size = int(st.session_state.get('social_page_size', 10))
total_filtered = len(filtered_alerts)
total_pages = max(1, (total_filtered + page_size - 1) // page_size)
filter_signature = (
    str(feed_search or '').strip(),
    feed_impact,
    feed_media,
)
if st.session_state.get('social_filter_signature') != filter_signature:
    st.session_state['social_filter_signature'] = filter_signature
    st.session_state['social_page'] = 1

current_page = int(st.session_state.get('social_page', 1))
current_page = min(max(current_page, 1), total_pages)
st.session_state['social_page'] = current_page

page_start = (current_page - 1) * page_size
page_alerts = filtered_alerts[page_start:page_start + page_size]
if not filtered_alerts:
    st.info("没有符合当前条件的动态。")
else:
    for _i, alert in enumerate(page_alerts):
        st.markdown(
            render_alert_card(alert, latest=(current_page == 1 and _i == 0)),
            unsafe_allow_html=True,
        )

# 分页固定放在动态列表底部，避免打断阅读。
st.markdown("<div class='feed-pagination'></div>", unsafe_allow_html=True)
pager_left, pager_info, pager_right = st.columns([0.20, 0.60, 0.20])
with pager_left:
    if st.button("上一条", disabled=(current_page <= 1), use_container_width=True, key="social_prev"):
        st.session_state['social_page'] = current_page - 1
        st.rerun()
with pager_info:
    st.markdown('<span class="feed-pagination-hook" aria-hidden="true"></span>', unsafe_allow_html=True)
    st.markdown(
        f'<div class="feed-page-info"><span class="page-current">第 {current_page} / {total_pages} 页</span><span class="page-total">共 {total_filtered} 条动态</span></div>',
        unsafe_allow_html=True,
    )
with pager_right:
    if st.button("下一条", disabled=(current_page >= total_pages), use_container_width=True, key="social_next"):
        st.session_state['social_page'] = current_page + 1
        st.rerun()

# 每页数量单独放在分页按钮下一行，避免在窄屏上挤压页码状态。
_, pager_size_col, _ = st.columns([0.36, 0.28, 0.36])
with pager_size_col:
    selected_page_size = st.selectbox(
        "每页显示",
        [10, 20, 50],
        index=[10, 20, 50].index(page_size) if page_size in [10, 20, 50] else 0,
        key="social_page_size",
        label_visibility="collapsed",
    )
    if int(selected_page_size) != page_size:
        st.session_state['social_page'] = 1
        st.rerun()
