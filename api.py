"""
Truth Social Monitor API
提供REST API接口，允许外部系统获取监控数据
"""
import os
import json
import re
from datetime import datetime, timezone
from typing import Optional, List
from fastapi import FastAPI, Query, HTTPException, Header, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, FileResponse, HTMLResponse
from urllib.parse import quote, unquote
from urllib.request import Request, urlopen
import re
from pydantic import BaseModel
from threading import Thread, Lock
import hashlib

from utils import (
    ALERTS_FILE,
    pick_ts,
    PROJECT_ROOT,
    get_media_paths_by_post_id,
    DASHBOARD_JSON_FILE,
)

# ==========================================
# 媒体文件路径配置
# ==========================================
MEDIA_DIR = os.path.join(PROJECT_ROOT, "media")
IMAGES_DIR = os.path.join(MEDIA_DIR, "images")
VIDEOS_DIR = os.path.join(MEDIA_DIR, "videos")

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
            print(f"[API] ✅ 通过推文ID {post_id} 找到 {len(local_media_paths)} 个本地媒体文件，直接使用")
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
    
    # 如果没有通过推文ID找到，保留原始URL（向后兼容）
    return media_list

# ==========================================
# FastAPI 应用初始化
# ==========================================
app = FastAPI(
    title="Truth Social Monitor API",
    description="提供Truth Social监控数据的REST API接口",
    version="1.0.0"
)

# 配置CORS，允许跨域访问（包括视频文件）
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # 生产环境应限制为特定域名
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ==========================================
# 手动刷新（Trigger Refresh）
# ==========================================
REFRESH_TOKEN = os.getenv("API_REFRESH_TOKEN", "").strip()
REFRESH_LOCAL_ONLY = os.getenv("API_REFRESH_LOCAL_ONLY", "1").strip().lower() not in ("0","false","no")
_refresh_lock = Lock()
_refresh_state = {
    "running": False,
    "last_start": None,
    "last_end": None,
    "last_result": None,
    "last_error": None,
}
# 注意：不使用 app.mount，而是使用路由端点来提供媒体文件
# 这样可以更好地控制文件访问和错误处理
# 媒体文件将通过 /api/media/{file_path:path} 端点提供

# ==========================================
# 数据模型
# ==========================================
class AIAnalysis(BaseModel):
    """AI分析结果模型"""
    impact: Optional[bool] = None
    reasoning: Optional[str] = None
    recommendation: Optional[str] = None
    sentiment: Optional[str] = None
    affected_assets: Optional[List[str]] = None
    external_context_used: Optional[str] = None
    media_used: Optional[bool] = None
    media_caption_used: Optional[bool] = None
    error: Optional[str] = None

class MediaItem(BaseModel):
    """媒体项模型"""
    url: Optional[str] = None
    preview_url: Optional[str] = None
    description: Optional[str] = None
    type: Optional[str] = None

class Alert(BaseModel):
    """告警数据模型"""
    id: str
    created_at: str
    content: str
    url: Optional[str] = None
    media: Optional[List[MediaItem]] = None
    keywords: Optional[str] = None
    ai_analysis: Optional[AIAnalysis] = None
    detected_at: Optional[str] = None
    source: Optional[str] = None

class AlertListResponse(BaseModel):
    """告警列表响应模型"""
    total: int
    page: int
    page_size: int
    total_pages: int
    data: List[Alert]

class StatsResponse(BaseModel):
    """统计信息响应模型"""
    total_alerts: int
    high_impact_count: int
    low_impact_count: int
    sentiment_breakdown: dict
    latest_alert_time: Optional[str] = None
    oldest_alert_time: Optional[str] = None

# ==========================================
# 数据加载函数
# ==========================================
def load_alerts():
    """加载并排序告警数据，支持多种编码格式（与 dashboard.py 保持一致）"""
    if not os.path.exists(ALERTS_FILE):
        # 输出调试信息
        abs_path = os.path.abspath(ALERTS_FILE)
        current_dir = os.getcwd()
        print(f"[API] ALERTS_FILE not found!")
        print(f"[API] Expected path: {ALERTS_FILE}")
        print(f"[API] Absolute path: {abs_path}")
        print(f"[API] Current working directory: {current_dir}")
        print(f"[API] File exists check: {os.path.exists(ALERTS_FILE)}")
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
            data = data.get("alerts") or []
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
    
    # 按时间倒序排序
    data.sort(key=lambda x: _parse_ts(pick_ts(x)), reverse=True)
    
    # 改进去重逻辑：主要基于ID去重，保留最新的（与 dashboard.py 保持一致）
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
        
        # 转换媒体URL为本地路径（通过推文ID查找）
        if 'media' in a and a['media']:
            a['media'] = convert_media_urls_to_local(a['media'], post_id=alert_id)
        
        deduped.append(a)
    
    return deduped

def build_dashboard_payload():
    alerts = load_alerts()
    latest = alerts[0] if alerts else None
    high_impact = 0
    low_impact = 0
    sentiment_counts = {"positive": 0, "negative": 0, "neutral": 0, "unknown": 0}
    for a in alerts:
        ai = a.get("ai_analysis", {}) or {}
        if ai.get("impact"):
            high_impact += 1
        else:
            low_impact += 1
        s = (ai.get("sentiment") or "").lower()
        if s in sentiment_counts:
            sentiment_counts[s] += 1
        else:
            sentiment_counts["unknown"] += 1
    metrics = {
        "total_alerts": len(alerts),
        "high_impact_count": high_impact,
        "low_impact_count": low_impact,
        "sentiment_breakdown": sentiment_counts,
        "latest_alert_time": pick_ts(latest) if latest else None,
        "oldest_alert_time": pick_ts(alerts[-1]) if alerts else None,
    }
    recent = alerts[1:6] if len(alerts) > 1 else []
    archive_count = max(0, len(alerts) - (1 + len(recent)))
    gallery_media = []
    for a in alerts[:20]:
        gallery_media.extend(a.get("media") or [])
    payload = {
        "metrics": metrics,
        "latest": latest,
        "recent": recent,
        "archive_count": archive_count,
        "media_gallery": gallery_media[:24],
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    try:
        with open(DASHBOARD_JSON_FILE, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, ensure_ascii=False)
    except Exception:
        pass
    return payload

def _analyze_media_background(max_alerts=None):
    try:
        print("[AI] ▶ Background media/web analysis started")
        alerts = load_alerts()
        if max_alerts:
            alerts = alerts[: int(max_alerts)]
        updated = []
        for a in alerts:
            try:
                from monitor_trump import analyze_local_media_for_alert, analyze_web_for_alert
                out = analyze_local_media_for_alert(a)
                ai = a.get("ai_analysis") or {}
                ai["media_multi_model"] = out.get("media_multi_model") or {}
                ai["media_ai_summary"] = out.get("media_ai_summary") or ""
                web_out = analyze_web_for_alert(a)
                ai["web_ai_summary"] = web_out.get("web_ai_summary") or ""
                a["ai_analysis"] = ai
                updated.append(a)
            except Exception:
                updated.append(a)
        try:
            base = {}
            if os.path.exists(ALERTS_FILE):
                with open(ALERTS_FILE, "r", encoding="utf-8") as f0:
                    cur = json.load(f0)
                if isinstance(cur, dict):
                    base["processed_ids"] = cur.get("processed_ids") or []
            base["alerts"] = updated
            with open(ALERTS_FILE, "w", encoding="utf-8") as f:
                json.dump(base, f, indent=2, ensure_ascii=False)
        except Exception:
            with open(ALERTS_FILE, "w", encoding="utf-8") as f:
                json.dump({"alerts": updated, "processed_ids": []}, f, indent=2, ensure_ascii=False)
        print("[AI] ✔ Background media/web analysis completed")
    except Exception:
        pass

@app.get("/api/health/hf", tags=["健康检查"])
async def hf_health():
    try:
        key_present = bool(os.getenv("HUGGINGFACE_API_KEY"))
        sample_path = None
        if os.path.isdir(IMAGES_DIR):
            for name in os.listdir(IMAGES_DIR):
                p = os.path.join(IMAGES_DIR, name)
                if os.path.isfile(p):
                    sample_path = p
                    break
        sample_caption = ""
        error_msg = ""
        hub_installed = True
        try:
            from monitor_trump import hf_caption_image, HUGGINGFACE_HUB_AVAILABLE
            hub_installed = bool(HUGGINGFACE_HUB_AVAILABLE)
            if sample_path:
                sample_caption = hf_caption_image(sample_path, timeout=12) or ""
        except Exception as e:
            error_msg = str(e)
            sample_caption = ""
        return {
            "hf_key_present": key_present,
            "hub_installed": hub_installed,
            "sample_image_path": sample_path or "",
            "sample_caption_present": bool(sample_caption),
            "sample_caption": (sample_caption or "")[:200],
            "error": error_msg[:200]
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

def _attach_local_media(alert):
    try:
        post_id = str(alert.get("id") or "")
        paths = get_media_paths_by_post_id(post_id)
        media_dir = MEDIA_DIR
        local_items = []
        for p in paths or []:
            if not os.path.exists(p):
                continue
            rel = os.path.relpath(p, media_dir).replace("\\", "/").lstrip("/")
            api_path = f"/api/media/{rel}"
            is_video = any(ext in p.lower() for ext in [".mp4", ".webm", ".mov", ".avi", ".mkv"])
            local_items.append({
                "url": api_path,
                "preview_url": api_path,
                "type": "video" if is_video else "image"
            })
        alert["local_media"] = local_items
    except Exception:
        alert["local_media"] = []
    return alert

# ==========================================
# API 端点
# ==========================================

@app.get("/api/media/{file_path:path}", tags=["媒体文件"])
async def serve_media_file(file_path: str):
    """
    提供媒体文件访问
    支持路径如: /api/media/images/abc123.jpg 或 /api/media/videos/def456.mp4
    """
    try:
        # 移除路径开头的斜杠（如果有）
        file_path = file_path.lstrip('/')
        
        # 构建完整文件路径
        # 注意：file_path 可能包含子目录，如 "images/abc123.jpg"
        full_path = os.path.join(MEDIA_DIR, file_path)
        
        # 标准化路径（处理 .. 和 . 等）
        full_path = os.path.normpath(full_path)
        media_dir_norm = os.path.normpath(MEDIA_DIR)
        
        # 安全检查：确保文件在MEDIA_DIR目录内（防止路径遍历攻击）
        # 使用 os.path.commonpath 来确保路径在 MEDIA_DIR 内
        try:
            # 获取绝对路径以确保比较准确
            full_path_abs = os.path.abspath(full_path)
            media_dir_abs = os.path.abspath(media_dir_norm)
            
            # 检查是否在 MEDIA_DIR 内
            if not full_path_abs.startswith(media_dir_abs + os.sep) and full_path_abs != media_dir_abs:
                print(f"[API Media] Security check failed:")
                print(f"  Requested: {full_path_abs}")
                print(f"  MEDIA_DIR: {media_dir_abs}")
                raise HTTPException(status_code=403, detail="Access denied")
        except ValueError:
            # 如果路径无法比较（跨驱动器等），拒绝访问
            print(f"[API Media] Security check failed: Cannot compare paths")
            raise HTTPException(status_code=403, detail="Access denied")
        
        # 检查文件是否存在
        if not os.path.exists(full_path):
            raise HTTPException(status_code=404, detail=f"File not found: {file_path}")
        
        # 检查是否为文件（不是目录）
        if not os.path.isfile(full_path):
            raise HTTPException(status_code=404, detail=f"Not a file: {file_path}")
        
        # 检测文件类型并设置正确的MIME类型
        file_ext = os.path.splitext(full_path)[1].lower()
        mime_type_map = {
            '.mp4': 'video/mp4',
            '.webm': 'video/webm',
            '.mov': 'video/quicktime',
            '.avi': 'video/x-msvideo',
            '.mkv': 'video/x-matroska',
            '.jpg': 'image/jpeg',
            '.jpeg': 'image/jpeg',
            '.png': 'image/png',
            '.gif': 'image/gif',
            '.webp': 'image/webp'
        }
        media_type = mime_type_map.get(file_ext, 'application/octet-stream')
        
        # 返回文件，设置正确的MIME类型和CORS头
        response = FileResponse(
            full_path,
            media_type=media_type,
            filename=os.path.basename(full_path),
            headers={
                "Accept-Ranges": "bytes",  # 支持范围请求（视频播放必需）
                "Cache-Control": "public, max-age=3600"  # 缓存1小时
            }
        )
        return response
    except HTTPException:
        raise
    except Exception as e:
        print(f"[API Media] Error serving file: {e}")
        print(f"[API Media] File path: {file_path}")
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"Error serving file: {str(e)}")

@app.get("/api/image-search/google", tags=["图片搜索"])
async def google_image_search(image_url: str = Query(..., description="图片URL")):
    """
    生成 Google 图片搜索页面
    该页面会自动下载图片并上传到 Google 进行搜索
    """
    try:
        # 解码 URL
        image_url = unquote(image_url)
        
        # 生成 HTML 页面，包含自动上传图片的表单
        html_content = f"""
<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <title>Google 图片搜索</title>
    <style>
        body {{
            font-family: Arial, sans-serif;
            display: flex;
            justify-content: center;
            align-items: center;
            height: 100vh;
            margin: 0;
            background: #f5f5f5;
        }}
        .container {{
            text-align: center;
            padding: 20px;
        }}
        .loading {{
            font-size: 18px;
            color: #666;
        }}
    </style>
</head>
<body>
    <div class="container">
        <div class="loading">正在加载图片并上传到 Google 搜索...</div>
    </div>
    <script>
        async function uploadImageToGoogle() {{
            try {{
                // 获取图片
                const response = await fetch('{image_url}', {{
                    mode: 'cors',
                    credentials: 'omit'
                }});
                
                if (!response.ok) {{
                    throw new Error('无法获取图片');
                }}
                
                const blob = await response.blob();
                
                // 创建 FormData
                const formData = new FormData();
                formData.append('encoded_image', blob);
                formData.append('image_url', '');
                formData.append('sbisrc', 'Chromium');
                formData.append('safe', 'off');
                
                // 创建表单并提交
                const form = document.createElement('form');
                form.method = 'POST';
                form.action = 'https://www.google.com/searchbyimage/upload';
                form.enctype = 'multipart/form-data';
                
                // 创建 File 对象
                const file = new File([blob], 'image.jpg', {{ type: blob.type || 'image/jpeg' }});
                
                // 使用 DataTransfer 来设置文件
                const dataTransfer = new DataTransfer();
                dataTransfer.items.add(file);
                
                // 添加文件输入
                const fileInput = document.createElement('input');
                fileInput.type = 'file';
                fileInput.name = 'encoded_image';
                fileInput.files = dataTransfer.files;
                form.appendChild(fileInput);
                
                // 添加其他字段（不添加额外关键词，让 Google 只基于图片搜索）
                const hiddenInputs = {{
                    'image_url': '',
                    'sbisrc': 'Chromium',
                    'safe': 'off',
                    'hl': 'en'  // 设置语言为英文，可能减少自动添加的关键词
                }};
                
                for (const [key, value] of Object.entries(hiddenInputs)) {{
                    const input = document.createElement('input');
                    input.type = 'hidden';
                    input.name = key;
                    input.value = value;
                    form.appendChild(input);
                }}
                
                document.body.appendChild(form);
                form.submit();
            }} catch (error) {{
                console.error('上传失败:', error);
                // 如果上传失败，回退到 URL 方式
                window.location.href = 'https://www.google.com/searchbyimage?image_url=' + encodeURIComponent('{image_url}');
            }}
        }}
        
        // 页面加载后自动执行
        window.onload = uploadImageToGoogle;
    </script>
</body>
</html>
        """
        return HTMLResponse(content=html_content)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"生成搜索页面失败: {str(e)}")

def extract_text_from_image(image_url):
    """
    从图片中提取文字（OCR功能）
    使用 HuggingFace API 进行 OCR
    如果 API 不可用或失败，返回空字符串（不影响其他功能）
    """
    try:
        from urllib.request import urlopen, Request
        from utils import _setup_proxy
        
        # 使用 HuggingFace API 进行 OCR
        try:
            from huggingface_hub import InferenceClient
            HUGGINGFACE_API_KEY = os.getenv("HUGGINGFACE_API_KEY")
            
            if not HUGGINGFACE_API_KEY:
                print("[OCR] ⚠️ HUGGINGFACE_API_KEY 未设置，跳过 OCR")
                return ""
            
            print("[OCR] 使用 HuggingFace API 进行 OCR...")
            # 增加超时时间到120秒，因为 OCR 模型可能需要较长时间处理
            try:
                client = InferenceClient(token=HUGGINGFACE_API_KEY, timeout=120.0)  # API 超时120秒
            except Exception as client_err:
                raise  # 重新抛出异常
            
            # 下载图片（如果是本地API，直接读取本地文件，避免通过API下载）
            image_data = None
            download_start = time.time()
            
            # 检查是否是本地API URL，如果是，尝试直接读取本地文件
            is_local_api = "localhost" in image_url or "127.0.0.1" in image_url
            if is_local_api and "/api/media/" in image_url:
                # 从URL中提取文件路径：http://localhost:8000/api/media/images/xxx.jpg -> media/images/xxx.jpg
                try:
                    url_parts = image_url.split("/api/media/")
                    if len(url_parts) > 1:
                        relative_path = url_parts[1]
                        local_file_path = os.path.join(MEDIA_DIR, relative_path)
                        if os.path.exists(local_file_path):
                            # 直接读取本地文件
                            with open(local_file_path, "rb") as f:
                                image_data = f.read()
                            print(f"[OCR] ✅ 直接读取本地文件: {local_file_path}")
                except Exception as local_read_err:
                    # 本地文件读取失败，回退到HTTP下载
                    print(f"[OCR] ⚠️ 本地文件读取失败，回退到HTTP下载: {local_read_err}")
                    image_data = None
            
            # 如果本地文件读取失败或不是本地API，使用HTTP下载
            if image_data is None:
                _setup_proxy()
                req = Request(image_url, headers={"User-Agent": "Mozilla/5.0"})
                # 如果是本地API，使用更长的超时时间（本地API可能响应较慢）
                timeout = 120 if is_local_api else 30
                try:
                    with urlopen(req, timeout=timeout) as response:
                        image_data = response.read()
                except Exception as download_err:
                    # 记录下载异常
                    error_type = type(download_err).__name__
                    error_msg = str(download_err)
                    is_timeout = "timeout" in error_msg.lower() or "timed out" in error_msg.lower()
                    print(f"[OCR] ❌ 图片下载失败: {download_err}")
                    raise  # 重新抛出异常，让外层处理
            # 图片数据已在上面的代码中获取（本地文件或HTTP下载）
            if image_data is None:
                print("[OCR] ❌ 图片数据为空，无法进行OCR")
                return ""
            
            # 使用 HuggingFace API 进行 OCR
            # 使用 microsoft/trocr-base-printed 模型
            result = None
            ocr_models = ["microsoft/trocr-base-printed", "microsoft/trocr-base-handwritten", "Salesforce/blip2-flan-t5-xxl"]
            last_error = None
            
            for model_name in ocr_models:
                try:
                    result = client.image_to_text(image_data, model=model_name)
                    # 如果成功，跳出循环
                    if result:
                        break
                except StopIteration as stop_err:
                    # StopIteration通常意味着provider不可用，尝试下一个模型
                    last_error = stop_err
                    continue
                except Exception as api_call_err:
                    # 其他异常，记录但继续尝试下一个模型
                    last_error = api_call_err
                    error_type = type(api_call_err).__name__
                    error_msg = str(api_call_err)
                    continue
            
            # 如果所有模型都失败，记录最后的错误
            if result is None:
                print(f"[OCR] ⚠️ 所有OCR模型都失败，最后错误: {last_error}")
                return ""  # 返回空字符串，不影响其他功能
            
            if isinstance(result, str):
                text = result.strip()
                if text:
                    print(f"[OCR] ✅ API OCR 成功，识别到 {len(text)} 个字符")
                return text
            elif isinstance(result, list) and result:
                text = result[0].get("generated_text", "") if isinstance(result[0], dict) else str(result[0])
                text = text.strip()
                if text:
                    print(f"[OCR] ✅ API OCR 成功，识别到 {len(text)} 个字符")
                return text
            else:
                print("[OCR] ⚠️ API OCR 返回空结果")
                return ""
                
        except ImportError:
            # 如果没有安装 huggingface_hub，跳过
            print("[OCR] ⚠️ huggingface_hub 未安装，跳过 OCR")
            return ""
        except Exception as e:
            # API 失败，返回空字符串
            import traceback
            error_type = type(e).__name__
            error_msg = str(e)
            error_repr = repr(e)
            error_traceback = traceback.format_exc()
            is_timeout = "timeout" in error_msg.lower() or "timed out" in error_msg.lower()
            print(f"[OCR] ❌ HuggingFace API OCR 失败: {e}")
            print(f"[OCR] 错误类型: {error_type}")
            print(f"[OCR] 错误详情: {error_repr}")
            if not error_msg:
                print(f"[OCR] ⚠️ 错误消息为空，完整堆栈跟踪:")
                print(error_traceback)
            return ""
        
    except Exception as e:
        # 静默失败，不影响其他功能
        import traceback
        error_type = type(e).__name__
        error_msg = str(e)
        error_repr = repr(e)
        error_traceback = traceback.format_exc()
        is_timeout = "timeout" in error_msg.lower() or "timed out" in error_msg.lower()
        print(f"[OCR] ❌ OCR 处理失败: {e}")
        print(f"[OCR] 错误类型: {error_type}")
        print(f"[OCR] 错误详情: {error_repr}")
        if not error_msg:
            print(f"[OCR] ⚠️ 错误消息为空，完整堆栈跟踪:")
            print(error_traceback)
        return ""

def extract_person_names_from_text(text):
    """
    从文本中提取人名（从搜索结果中识别）
    查找常见的人名模式，特别是政治人物、名人等
    """
    import re
    if not text:
        return []
    
    names = []
    
    # 常见人名模式
    # 1. 全名模式：FirstName LastName
    full_name_pattern = r'\b([A-Z][a-z]+ [A-Z][a-z]+)\b'
    full_names = re.findall(full_name_pattern, text)
    names.extend(full_names)
    
    # 2. 从搜索结果中提取（通常包含人物名字）
    # 查找包含常见政治人物、名人的模式
    famous_people = [
        'Donald Trump', 'Trump', 'Joe Biden', 'Biden',
        'Barack Obama', 'Obama', 'Hillary Clinton', 'Clinton',
        'Elon Musk', 'Musk', 'Bill Gates', 'Gates'
    ]
    
    for person in famous_people:
        if person.lower() in text.lower():
            if person not in names:
                names.append(person)
    
    # 3. 从搜索结果标题中提取（通常格式为 "Person Name - ..." 或 "... Person Name ..."）
    # 查找可能的人名（大写字母开头的单词，在特定上下文中）
    capitalized_words = re.findall(r'\b([A-Z][a-z]{2,})\b', text)
    
    # 过滤常见非人名词汇
    common_words = {
        'The', 'This', 'That', 'There', 'They', 'These', 'Those',
        'When', 'Where', 'What', 'Which', 'Who', 'Why', 'How',
        'January', 'February', 'March', 'April', 'May', 'June',
        'July', 'August', 'September', 'October', 'November', 'December',
        'Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday',
        'America', 'American', 'United', 'States', 'President', 'White', 'House',
        'News', 'Breaking', 'Latest', 'Update', 'Report', 'Says', 'Said'
    }
    
    # 添加可能的人名（过滤常见词）
    for word in capitalized_words:
        if word not in common_words and word not in names and len(word) > 2:
            # 检查是否在搜索结果中频繁出现（可能是人名）
            count = text.lower().count(word.lower())
            if count >= 2:  # 出现2次以上，更可能是人名
                names.append(word)
    
    # 去重
    unique_names = []
    seen = set()
    for name in names:
        name_lower = name.lower()
        if name_lower not in seen:
            unique_names.append(name)
            seen.add(name_lower)
    
    return unique_names[:8]  # 最多返回8个

@app.get("/api/image-search/result", tags=["图片搜索"])
async def get_image_search_result(image_url: str = Query(..., description="图片URL")):
    """
    执行图片搜索并返回搜索结果摘要
    包括：图片描述、OCR文字识别、人物识别、搜索结果
    实现图片到文字的转换功能
    """
    try:
        from monitor_trump import hf_caption_image, fetch_external_context
        from utils import _setup_proxy
        
        # 解码 URL
        image_url = unquote(image_url)
        
        result_parts = []
        
        # 1. 获取图片描述（增加超时时间）
        caption = ""
        try:
            caption = hf_caption_image(image_url, timeout=20)
            if caption:
                caption = caption.strip()[:200]
                result_parts.append(f"📝 图片描述: {caption}")
        except Exception as e:
            # 图片描述失败不影响其他功能
            pass
        
        # 2. OCR 文字识别（图片到文字转换）
        # 注意：OCR 可能失败或超时，这是正常的，不影响其他功能
        ocr_text = ""
        try:
            ocr_text = extract_text_from_image(image_url)
            if ocr_text and ocr_text.strip():
                ocr_text = ocr_text.strip()[:300]  # 限制长度
                result_parts.append(f"🔤 图片中的文字: {ocr_text}")
        except Exception as e:
            # OCR 失败不影响其他功能，静默处理
            pass
        
        # 3. 从搜索结果中提取人物名字
        person_names = []
        search_query = caption if caption else (ocr_text if ocr_text else "image search")
        
        # 执行搜索（使用较短的超时，避免阻塞）
        try:
            search_results = fetch_external_context(search_query)
        except Exception as e:
            print(f"[Image Search] 搜索失败: {e}")
            search_results = ""
        
        # 从搜索结果中提取人物名字
        if search_results:
            # 合并所有文本进行人物识别
            all_text = f"{caption} {ocr_text} {search_results}"
            person_names = extract_person_names_from_text(all_text)
        
        if person_names:
            result_parts.append(f"👤 识别到的人物: {', '.join(person_names)}")
        
        # 4. 格式化搜索结果
        if search_results and search_results.strip():
            # 将结果格式化为列表
            parts = search_results.split(" | ")
            formatted_parts = []
            for i, part in enumerate(parts[:5], 1):  # 最多显示5个结果
                if part.strip():
                    # 清理 HTML 实体
                    clean_part = part.strip()
                    clean_part = clean_part.replace('&quot;', '"').replace('&#x27;', "'")
                    formatted_parts.append(f"{i}. {clean_part}")
            
            if formatted_parts:
                result_parts.append(f"🌐 搜索结果:\n" + "\n".join(formatted_parts))
            else:
                result_parts.append(f"🌐 搜索结果: {search_results}")
        
        # 组合所有结果
        if result_parts:
            result_text = "\n\n".join(result_parts)
            
            return JSONResponse(content={
                "success": True,
                "caption": caption,
                "ocr_text": ocr_text,
                "person_names": person_names,
                "results": result_text,
                "raw_results": search_results
            })
        else:
            return JSONResponse(content={
                "success": False,
                "caption": caption,
                "ocr_text": ocr_text,
                "person_names": [],
                "results": "未找到相关信息",
                "raw_results": ""
            })
    except Exception as e:
        import traceback
        traceback.print_exc()
        return JSONResponse(content={
            "success": False,
            "error": str(e),
            "results": f"搜索出错: {str(e)}"
        })

@app.get("/api/image-search/bing", tags=["图片搜索"])
async def bing_image_search(image_url: str = Query(..., description="图片URL")):
    """
    生成 Bing 图片搜索页面
    该页面会自动下载图片并上传到 Bing 进行搜索
    """
    try:
        # 解码 URL
        image_url = unquote(image_url)
        
        # 生成 HTML 页面，包含自动上传图片的表单
        html_content = f"""
<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <title>Bing 图片搜索</title>
    <style>
        body {{
            font-family: Arial, sans-serif;
            display: flex;
            justify-content: center;
            align-items: center;
            height: 100vh;
            margin: 0;
            background: #f5f5f5;
        }}
        .container {{
            text-align: center;
            padding: 20px;
        }}
        .loading {{
            font-size: 18px;
            color: #666;
        }}
    </style>
</head>
<body>
    <div class="container">
        <div class="loading">正在加载图片并上传到 Bing 搜索...</div>
    </div>
    <script>
        async function uploadImageToBing() {{
            try {{
                // 获取图片
                const response = await fetch('{image_url}', {{
                    mode: 'cors',
                    credentials: 'omit'
                }});
                
                if (!response.ok) {{
                    throw new Error('无法获取图片');
                }}
                
                const blob = await response.blob();
                
                // 创建表单并提交
                const form = document.createElement('form');
                form.method = 'POST';
                form.action = 'https://www.bing.com/images/search';
                form.enctype = 'multipart/form-data';
                
                // 创建 File 对象
                const file = new File([blob], 'image.jpg', {{ type: blob.type || 'image/jpeg' }});
                
                // 使用 DataTransfer 来设置文件
                const dataTransfer = new DataTransfer();
                dataTransfer.items.add(file);
                
                // 添加文件输入
                const fileInput = document.createElement('input');
                fileInput.type = 'file';
                fileInput.name = 'imageBin';
                fileInput.files = dataTransfer.files;
                form.appendChild(fileInput);
                
                document.body.appendChild(form);
                form.submit();
            }} catch (error) {{
                console.error('上传失败:', error);
                // 如果上传失败，回退到 URL 方式
                window.location.href = 'https://www.bing.com/images/search?q=imgurl:' + encodeURIComponent('{image_url}');
            }}
        }}
        
        // 页面加载后自动执行
        window.onload = uploadImageToBing;
    </script>
</body>
</html>
        """
        return HTMLResponse(content=html_content)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"生成搜索页面失败: {str(e)}")

@app.get("/", tags=["根路径"])
async def root():
    """API根路径，返回基本信息"""
    return {
        "name": "Truth Social Monitor API",
        "version": "1.0.0",
        "description": "提供Truth Social监控数据的REST API接口",
        "endpoints": {
            "GET /api/alerts": "获取告警列表（支持分页和过滤）",
            "GET /api/alerts/latest": "获取最新告警",
            "GET /api/alerts/{id}": "根据ID获取特定告警",
            "GET /api/stats": "获取统计信息",
            "GET /api/media/{file_path}": "获取媒体文件（图片/视频）",
            "GET /api/image-search/google": "Google 图片搜索（上传图片）",
            "GET /api/image-search/bing": "Bing 图片搜索（上传图片）",
            "GET /docs": "查看API文档（Swagger UI）",
            "GET /redoc": "查看API文档（ReDoc）"
        }
    }

@app.get("/api/alerts", tags=["告警数据"])
async def get_alerts(
    page: int = Query(1, ge=1, description="页码，从1开始"),
    page_size: int = Query(20, ge=1, le=100, description="每页数量，最大100"),
    impact: Optional[str] = Query(None, description="过滤影响级别：high 或 low"),
    sentiment: Optional[str] = Query(None, description="过滤情感：positive, negative, neutral"),
    search: Optional[str] = Query(None, description="搜索关键词（在内容和推理中搜索）"),
    source: Optional[str] = Query(None, description="过滤来源：real 或 simulated")
):
    """
    获取告警列表
    
    - **page**: 页码（从1开始）
    - **page_size**: 每页数量（1-100）
    - **impact**: 过滤影响级别（high/low）
    - **sentiment**: 过滤情感（positive/negative/neutral）
    - **search**: 搜索关键词
    - **source**: 过滤来源（real/simulated）
    """
    alerts = load_alerts()
    
    # 应用过滤
    filtered = []
    for alert in alerts:
        ai = alert.get('ai_analysis', {}) or {}
        
        # 影响级别过滤
        if impact:
            alert_impact = "high" if ai.get('impact') else "low"
            if alert_impact != impact.lower():
                continue
        
        # 情感过滤
        if sentiment:
            alert_sentiment = (ai.get('sentiment') or '').lower()
            if alert_sentiment != sentiment.lower():
                continue
        
        # 来源过滤
        if source:
            alert_source = (alert.get('source') or '').lower()
            if alert_source != source.lower():
                continue
        
        # 搜索过滤
        if search:
            search_lower = search.lower()
            content = (alert.get('content') or '').lower()
            reasoning = (ai.get('reasoning') or '').lower()
            keywords = (alert.get('keywords') or '').lower()
            assets = ' '.join(map(str, ai.get('affected_assets', []) or [])).lower()
            
            if search_lower not in (content + ' ' + reasoning + ' ' + keywords + ' ' + assets):
                continue
        
        filtered.append(alert)
    
    total = len(filtered)
    total_pages = (total + page_size - 1) // page_size if total > 0 else 0
    
    # 分页
    start = (page - 1) * page_size
    end = start + page_size
    paginated = filtered[start:end]
    paginated = [_attach_local_media(a) for a in paginated]
    
    return {
        "total": total,
        "page": page,
        "page_size": page_size,
        "total_pages": total_pages,
        "data": paginated
    }


@app.get("/api/alerts/{alert_id}/media-summary", tags=["告警数据"])
async def get_alert_media_summary(alert_id: str):
    """
    获取指定告警的已缓存媒体分析结果（避免重复调用 HF）。
    """
    alerts = load_alerts()
    for alert in alerts:
        if str(alert.get("id")) == str(alert_id):
            ai = alert.get("ai_analysis") or {}
            return {
                "alert_id": alert_id,
                "media_ai_summary": ai.get("media_ai_summary") or "",
                "media_multi_model": ai.get("media_multi_model") or {},
                "media_used": bool(ai.get("media_used", False)),
                "media_caption_used": bool(ai.get("media_caption_used", False)),
                "local_media_paths": alert.get("local_media_paths") or [],
            }
    raise HTTPException(status_code=404, detail="Alert not found")

@app.get("/api/alerts/latest", tags=["告警数据"])
async def get_latest_alert():
    """获取最新的告警"""
    alerts = load_alerts()
    if not alerts:
        raise HTTPException(status_code=404, detail="没有找到任何告警数据")
    latest = alerts[0]
    return _attach_local_media(latest)

@app.get("/api/alerts/{alert_id}", tags=["告警数据"])
async def get_alert_by_id(alert_id: str):
    """根据ID获取特定告警"""
    alerts = load_alerts()
    for alert in alerts:
        if str(alert.get('id', '')) == str(alert_id):
            return _attach_local_media(alert)
    raise HTTPException(status_code=404, detail=f"未找到ID为 {alert_id} 的告警")

@app.get("/api/stats", response_model=StatsResponse, tags=["统计信息"])
async def get_stats():
    """获取统计信息"""
    alerts = load_alerts()
    
    if not alerts:
        return {
            "total_alerts": 0,
            "high_impact_count": 0,
            "low_impact_count": 0,
            "sentiment_breakdown": {},
            "latest_alert_time": None,
            "oldest_alert_time": None
        }
    
    high_impact = 0
    low_impact = 0
    sentiment_counts = {"positive": 0, "negative": 0, "neutral": 0, "unknown": 0}
    
    for alert in alerts:
        ai = alert.get('ai_analysis', {}) or {}
        if ai.get('impact'):
            high_impact += 1
        else:
            low_impact += 1
        
        sentiment = (ai.get('sentiment') or '').lower()
        if sentiment in sentiment_counts:
            sentiment_counts[sentiment] += 1
        else:
            sentiment_counts["unknown"] += 1
    
    # 获取最新和最旧的时间
    latest_ts = pick_ts(alerts[0]) if alerts else None
    oldest_ts = pick_ts(alerts[-1]) if alerts else None
    
    return {
        "total_alerts": len(alerts),
        "high_impact_count": high_impact,
        "low_impact_count": low_impact,
        "sentiment_breakdown": sentiment_counts,
        "latest_alert_time": latest_ts,
        "oldest_alert_time": oldest_ts
    }

@app.post("/api/analyze/media", tags=["分析"])
async def analyze_media(max_alerts: int | None = Query(None, ge=1, le=200)):
    t = Thread(target=_analyze_media_background, args=(max_alerts,), daemon=True)
    t.start()
    return {"status": "started", "max_alerts": max_alerts}

@app.get("/api/export/dashboard", tags=["导出"])
async def export_dashboard_json():
    payload = build_dashboard_payload()
    return payload

@app.get("/health", tags=["健康检查"])
async def health_check():
    """健康检查端点"""
    alerts = load_alerts()
    file_exists = os.path.exists(ALERTS_FILE)
    
    # 获取当前工作目录和文件绝对路径
    current_dir = os.getcwd()
    abs_file_path = os.path.abspath(ALERTS_FILE)
    
    return {
        "status": "healthy",
        "alerts_count": len(alerts),
        "data_file_exists": file_exists,
        "data_file_path": ALERTS_FILE,
        "data_file_absolute_path": abs_file_path,
        "current_working_directory": current_dir,
        "utils_file_location": os.path.dirname(os.path.abspath(__file__)),
        "timestamp": datetime.now(timezone.utc).isoformat()
    }

@app.get("/api/debug", tags=["调试"])
async def debug_info():
    """调试信息端点，显示原始数据加载情况"""
    file_exists = os.path.exists(ALERTS_FILE)
    alerts = load_alerts()
    
    # 尝试读取原始文件内容
    raw_preview = None
    raw_file_size = 0
    raw_data_count = 0
    
    if file_exists:
        try:
            # 获取文件大小
            raw_file_size = os.path.getsize(ALERTS_FILE)
            
            # 尝试读取原始JSON数据
            encodings = ['utf-8', 'utf-8-sig', 'gbk', 'gb2312', 'latin-1', 'cp1252']
            raw_data = None
            
            for encoding in encodings:
                try:
                    with open(ALERTS_FILE, "r", encoding=encoding) as f:
                        raw_data = json.load(f)
                        break
                except:
                    continue
            
            if raw_data is None:
                try:
                    with open(ALERTS_FILE, "r") as f:
                        raw_data = json.load(f)
                except Exception as e:
                    raw_data = None
                    raw_preview = f"无法解析JSON: {e}"
            
            if raw_data is not None:
                if isinstance(raw_data, list):
                    raw_data_count = len(raw_data)
                    raw_preview = f"文件包含 {raw_data_count} 条原始数据"
                else:
                    raw_preview = f"文件不是列表格式，类型: {type(raw_data)}"
                    
        except Exception as e:
            raw_preview = f"无法读取文件: {e}"
    
    # 检查媒体目录
    media_info = {
        "media_dir_exists": os.path.exists(MEDIA_DIR),
        "media_dir_path": MEDIA_DIR,
        "images_dir_exists": os.path.exists(IMAGES_DIR),
        "videos_dir_exists": os.path.exists(VIDEOS_DIR),
    }
    
    # 统计媒体文件数量
    image_count = 0
    video_count = 0
    if os.path.exists(IMAGES_DIR):
        try:
            image_count = len([f for f in os.listdir(IMAGES_DIR) if os.path.isfile(os.path.join(IMAGES_DIR, f))])
        except:
            pass
    if os.path.exists(VIDEOS_DIR):
        try:
            video_count = len([f for f in os.listdir(VIDEOS_DIR) if os.path.isfile(os.path.join(VIDEOS_DIR, f))])
        except:
            pass
    
    media_info["image_count"] = image_count
    media_info["video_count"] = video_count
    
    return {
        "file_exists": file_exists,
        "file_path": ALERTS_FILE,
        "file_size_bytes": raw_file_size,
        "raw_data_count": raw_data_count,
        "alerts_loaded_after_dedup": len(alerts),
        "first_alert_preview": alerts[0] if alerts else None,
        "raw_file_preview": raw_preview,
        "media_info": media_info,
        "timestamp": datetime.now(timezone.utc).isoformat()
    }


@app.post("/api/refresh", tags=["维护"])
async def trigger_refresh(
    request: Request,
    limit: int = Query(20, ge=1, le=200),
    fast_init: bool = Query(False),
    background: bool = Query(True),
    x_refresh_token: str | None = Header(default=None, alias="X-Refresh-Token"),
):
    """
    手动触发抓取/刷新数据（调用 monitor_trump.run_fetch_recent）。

    安全：
    - 默认不需要 key，但只允许 localhost 调用（API_REFRESH_LOCAL_ONLY=1）。
    - 若你要允许远程触发：设置 API_REFRESH_LOCAL_ONLY=0。
    - 可选：若设置 API_REFRESH_TOKEN，则需要 Header: X-Refresh-Token 匹配。

    参数：
    - limit: 拉取最近多少条（1-200）
    - fast_init: 传给 run_fetch_recent
    - background: True 则异步执行并立即返回
    """
    client_host = (request.client.host if request.client else "").strip()

    # Default: no key required, but restrict to localhost unless API_REFRESH_LOCAL_ONLY=0.
    if REFRESH_LOCAL_ONLY and client_host not in ("127.0.0.1", "::1", "localhost"):
        raise HTTPException(status_code=403, detail="forbidden")

    # Optional token gate (only if API_REFRESH_TOKEN is set)
    if REFRESH_TOKEN and (x_refresh_token or "") != REFRESH_TOKEN:
        raise HTTPException(status_code=401, detail="unauthorized")

    def _do_refresh():
        import traceback
        from datetime import datetime, timezone

        with _refresh_lock:
            if _refresh_state.get("running"):
                return
            _refresh_state["running"] = True
            _refresh_state["last_start"] = datetime.now(timezone.utc).isoformat()
            _refresh_state["last_error"] = None
            _refresh_state["last_result"] = None

        try:
            from monitor_trump import run_fetch_recent
            n = run_fetch_recent(limit=limit, fast_init=fast_init)
            with _refresh_lock:
                _refresh_state["last_result"] = {"new_posts": int(n)}
        except Exception as e:
            with _refresh_lock:
                _refresh_state["last_error"] = str(e)
            traceback.print_exc()
        finally:
            with _refresh_lock:
                _refresh_state["running"] = False
                _refresh_state["last_end"] = datetime.now(timezone.utc).isoformat()

    with _refresh_lock:
        running = bool(_refresh_state.get("running"))

    if running:
        return {"ok": False, "status": "running", "state": _refresh_state}

    if background:
        Thread(target=_do_refresh, daemon=True).start()
        return {"ok": True, "status": "started", "state": _refresh_state}

    _do_refresh()
    return {"ok": True, "status": "done", "state": _refresh_state}

@app.get("/api/refresh/status", tags=["维护"])
async def refresh_status():
    """查看最近一次 refresh 状态"""
    with _refresh_lock:
        return {"ok": True, **_refresh_state}

# ==========================================
# 错误处理
# ==========================================
@app.exception_handler(Exception)
async def global_exception_handler(request, exc):
    """全局异常处理"""
    return JSONResponse(
        status_code=500,
        content={
            "error": "内部服务器错误",
            "detail": str(exc),
            "path": str(request.url)
        }
    )

def run_api_server(host="0.0.0.0", port=8000):
    """运行API服务器（用于在后台线程中启动）"""
    import uvicorn
    uvicorn.run(app, host=host, port=port, log_level="warning")

if __name__ == "__main__":
    run_api_server()