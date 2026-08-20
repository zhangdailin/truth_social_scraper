import os
import json
import re
import time
import hashlib
import base64
import io
import mimetypes
from datetime import datetime, timezone
from openai import OpenAI
from urllib.request import Request, urlopen
from urllib.parse import urlparse, quote
try:
    from huggingface_hub import InferenceClient
    HUGGINGFACE_HUB_AVAILABLE = True
except Exception as e:
    InferenceClient = None
    HUGGINGFACE_HUB_AVAILABLE = False
    print(f"[WARNING] Failed to import huggingface_hub: {e}")
    import traceback
    traceback.print_exc()
try:
    import cv2
except Exception:
    cv2 = None
try:
    from PIL import Image
except Exception:
    Image = None
import subprocess
import tempfile
import shutil
import socks
import socket
from utils import (
    ALERTS_FILE,
    PROJECT_ROOT,
    derive_content,
    env_flag,
    extract_media,
    fetch_truth_posts,
    normalize_iso,
    save_post_media_mapping,
    get_media_paths_by_post_id,
    _setup_proxy,
)

# ==========================================
# CONFIGURATION
# ==========================================
# Determine paths relative to repo root
PROCESSED_LOG_FILE = os.path.join(PROJECT_ROOT, "processed_posts.json")
# 保留的最大告警条数，None 表示不截断
MAX_ALERTS = None

# Media download directory
MEDIA_DIR = os.path.join(PROJECT_ROOT, "media")
IMAGES_DIR = os.path.join(MEDIA_DIR, "images")
VIDEOS_DIR = os.path.join(MEDIA_DIR, "videos")

# 确保目录存在
os.makedirs(IMAGES_DIR, exist_ok=True)
os.makedirs(VIDEOS_DIR, exist_ok=True)

# SiliconFlow API Configuration
SILICONFLOW_API_KEY = os.getenv("SILICONFLOW_API_KEY")
BASE_URL = "https://api.siliconflow.cn/v1"
SILICONFLOW_TEXT_MODEL = os.getenv("SILICONFLOW_TEXT_MODEL", "Qwen/Qwen3.6-35B-A3B")
# SiliconFlow OpenAI-compatible视觉模型；可通过环境变量切换到账号可用的更新模型。
SILICONFLOW_VISION_MODEL = os.getenv(
    "SILICONFLOW_VISION_MODEL",
    "Qwen/Qwen3.6-35B-A3B",
)
# 直接传视频会明显增加请求体和耗时；超过此大小时自动使用关键帧。
DIRECT_VIDEO_MAX_MB = float(os.getenv("SILICONFLOW_DIRECT_VIDEO_MAX_MB", "20"))

HUGGINGFACE_API_KEY = os.getenv("HUGGINGFACE_API_KEY")
# 默认使用 blip2-flan-t5-xxl，因为 blip-image-captioning-large 在推理 API 中不可用（404错误）
# 注意：主模型和后备模型已全部删除，因为它们总是返回StopIteration错误，没有任何输出
# 现在只使用 vision chat 模型（Qwen/Qwen2.5-VL-7B-Instruct），它有39次成功输出
HUGGINGFACE_IMAGE_MODEL = None  # 已禁用，因为总是返回StopIteration
HUGGINGFACE_IMAGE_MODEL_FALLBACKS = []  # 已清空，所有后备模型都无输出
HTML_FETCH_TIMEOUT = 10

# Truth Social configuration (cookie-based)
TRUTH_ACCOUNT_ID = os.getenv("TRUTH_ACCOUNT_ID", "107780257626128497")
TRUTH_COOKIE_RAW = os.getenv("TRUTH_COOKIE", "")
# 清理 Cookie：移除引号、换行符、制表符等
TRUTH_COOKIE = TRUTH_COOKIE_RAW.strip().strip('"').strip("'").replace('\n', '').replace('\r', '').replace('\t', '') if TRUTH_COOKIE_RAW else ""
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
# MEDIA DOWNLOAD FUNCTIONS
# ==========================================

def get_file_extension(url):
    """从URL中提取文件扩展名"""
    parsed = urlparse(url)
    path = parsed.path
    if '.' in path:
        ext = os.path.splitext(path)[1].lower()
        # 移除查询参数，只保留扩展名
        if '?' in ext:
            ext = ext.split('?')[0]
        return ext
    return '.jpg'  # 默认扩展名

def generate_filename(url, media_type='image'):
    """生成唯一的文件名（基于URL的hash）"""
    url_hash = hashlib.md5(url.encode('utf-8')).hexdigest()[:12]
    ext = get_file_extension(url)
    if media_type == 'video':
        # 确保视频扩展名正确
        if ext not in ['.mp4', '.webm', '.mov', '.avi', '.mkv', '.gif']:
            ext = '.mp4'
        return f"{url_hash}{ext}"
    else:
        # 确保图片扩展名正确
        if ext not in ['.jpg', '.jpeg', '.png', '.gif', '.webp']:
            ext = '.jpg'
        return f"{url_hash}{ext}"




def download_media(url, media_type='image', timeout=30, max_size_mb=50):
    """
    下载媒体文件到本地
    返回本地文件路径，如果下载失败返回None
    
    改进：
    - 验证文件完整性（检查Content-Length）
    - 验证文件类型（检查文件头/魔数）
    - 更好的HTTP头设置
    - 更详细的错误信息
    - 针对本地API增加超时时间
    """
    try:
        if not url:
            return None
        
        # 检查是否是本地API URL，如果是，直接读取本地文件
        is_local_api = "localhost" in url or "127.0.0.1" in url
        if is_local_api and "/api/media/" in url:
            # 从URL中提取文件路径：http://localhost:8000/api/media/images/xxx.jpg -> media/images/xxx.jpg
            try:
                url_parts = url.split("/api/media/")
                if len(url_parts) > 1:
                    relative_path = url_parts[1]
                    local_file_path = os.path.join(MEDIA_DIR, relative_path)
                    if os.path.exists(local_file_path):
                        # 直接返回本地文件路径，不需要下载
                        print(f"[Download] ✅ 直接使用本地文件: {local_file_path}")
                        return local_file_path
            except Exception as local_read_err:
                # 本地文件读取失败，回退到HTTP下载
                print(f"[Download] ⚠️ 本地文件读取失败，回退到HTTP下载: {local_read_err}")
        
        # 如果是本地API，增加超时时间（本地API可能响应较慢，特别是在处理多个并发请求时）
        if is_local_api:
            timeout = max(timeout, 120)  # 本地API至少120秒超时（2分钟）
        
        # 设置代理（如果配置了）
        proxy_enabled = _setup_proxy()
        if proxy_enabled:
            pass
        
        # 确定保存目录
        save_dir = VIDEOS_DIR if media_type == 'video' else IMAGES_DIR
        
        # 确保目录存在
        os.makedirs(save_dir, exist_ok=True)
        
        # 生成文件名
        filename = generate_filename(url, media_type)
        filepath = os.path.join(save_dir, filename)
        
        # 如果文件已存在，验证文件完整性
        if os.path.exists(filepath):
            file_size = os.path.getsize(filepath)
            if file_size > 0:
                if _validate_media_file(filepath, media_type):
                    return filepath
                else:
                    os.remove(filepath)
            else:
                os.remove(filepath)
        
        # 改进的HTTP头
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "video/mp4,video/*;q=0.9,image/*;q=0.8,*/*;q=0.7" if media_type == 'video' else "image/*,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Accept-Encoding": "identity",  # 不使用压缩，确保文件完整性
            "Referer": "https://truthsocial.com/",
            "Connection": "keep-alive",
        }
        
        # 下载文件
        max_size_bytes = max_size_mb * 1024 * 1024
        request = Request(url, headers=headers)
        
        with urlopen(request, timeout=timeout) as r:
            # 检查HTTP状态码
            if r.status != 200:
                print(f"[Download] ❌ HTTP {r.status} for {url}")
                return None
            
            # 获取Content-Type
            content_type = r.headers.get('Content-Type', '').lower()
            pass
            
            # 根据实际Content-Type调整媒体类型（服务器可能返回不同的类型）
            actual_media_type = media_type
            if 'video' in content_type:
                actual_media_type = 'video'
                # 如果原本期望是图片但实际是视频，需要调整保存目录
                if media_type == 'image':
                    print(f"[Download] ⚠️ Expected image but got video, adjusting...")
                    save_dir = VIDEOS_DIR
                    # 重新生成文件名（使用视频扩展名）
                    filename = generate_filename(url, 'video')
                    filepath = os.path.join(save_dir, filename)
            elif 'image' in content_type:
                actual_media_type = 'image'
                # 如果原本期望是视频但实际是图片，需要调整保存目录
                if media_type == 'video':
                    print(f"[Download] ⚠️ Expected video but got image, adjusting...")
                    save_dir = IMAGES_DIR
                    # 重新生成文件名（使用图片扩展名）
                    filename = generate_filename(url, 'image')
                    filepath = os.path.join(save_dir, filename)
            
            # 检查Content-Length
            content_length = r.headers.get('Content-Length')
            expected_size = None
            if content_length:
                expected_size = int(content_length)
                if expected_size > max_size_bytes:
                    print(f"[Download] ❌ File too large: {expected_size} bytes (max: {max_size_bytes})")
                    return None
                if expected_size == 0:
                    print(f"[Download] ❌ File size is 0")
                    return None
                pass
            
            # 分块下载，显示进度
            file_bytes = b''
            chunk_size = 8192  # 8KB chunks
            total_read = 0
            
            while True:
                chunk = r.read(chunk_size)
                if not chunk:
                    break
                file_bytes += chunk
                total_read += len(chunk)
                
                # 检查是否超过最大大小
                if total_read > max_size_bytes:
                    print(f"[Download] ❌ Downloaded file too large: {total_read} bytes")
                    return None
                
                pass
            
            
            # 验证下载完整性
            if expected_size and len(file_bytes) != expected_size:
                if abs(len(file_bytes) - expected_size) / expected_size > 0.01:
                    return None
            
            # 验证文件不为空
            if len(file_bytes) == 0:
                print(f"[Download] ❌ Downloaded file is empty")
                return None
            
            # 验证文件类型（检查文件头）- 使用实际检测到的媒体类型
            if not _validate_file_content(file_bytes, actual_media_type):
                print(f"[Download] ❌ File content validation failed (wrong file type or corrupted)")
                print(f"[Download] Expected: {media_type}, Actual: {actual_media_type}, Content-Type: {content_type}")
                return None
            
            # 保存到本地（使用临时文件，然后原子性重命名）
            temp_filepath = filepath + '.tmp'
            with open(temp_filepath, 'wb') as f:
                f.write(file_bytes)
            
            # 再次验证保存的文件 - 使用实际检测到的媒体类型
            if not _validate_media_file(temp_filepath, actual_media_type):
                print(f"[Download] ❌ Saved file validation failed")
                print(f"[Download] Expected: {media_type}, Actual: {actual_media_type}, Content-Type: {content_type}")
                os.remove(temp_filepath)
                return None
            
            # 原子性重命名（确保文件完整性）
            if os.path.exists(filepath):
                os.remove(filepath)
            os.rename(temp_filepath, filepath)
            
            final_size = os.path.getsize(filepath)
            return filepath
            
    except Exception as e:
        print(f"[Download] ❌ Error downloading {media_type} from {url}: {e}")
        import traceback
        traceback.print_exc()
        # 清理临时文件
        temp_filepath = filepath + '.tmp' if 'filepath' in locals() else None
        if temp_filepath and os.path.exists(temp_filepath):
            try:
                os.remove(temp_filepath)
            except:
                pass
        return None


def _validate_file_content(file_bytes, media_type):
    """
    验证文件内容（检查文件头/魔数）
    返回True如果文件类型正确
    """
    if not file_bytes or len(file_bytes) < 12:
        return False
    
    # 视频文件魔数检查
    if media_type == 'video':
        # MP4文件: ftyp box at offset 4
        if file_bytes[4:8] == b'ftyp':
            # 检查brand (mp41, isom, avc1等)
            brand = file_bytes[8:12]
            if brand in [b'mp41', b'isom', b'avc1', b'iso2', b'mp42']:
                return True
        
        # WebM文件: 以 1A 45 DF A3 开头
        if file_bytes[0:4] == b'\x1a\x45\xdf\xa3':
            return True
        
        # AVI文件: 以 RIFF 开头，然后是 AVI
        if file_bytes[0:4] == b'RIFF' and file_bytes[8:12] == b'AVI ':
            return True
        
        # MOV文件: ftyp box
        if file_bytes[4:8] == b'ftyp':
            return True
        
        print(f"[Validate] ⚠️ Video file magic number not recognized, but may still be valid")
        # 如果无法识别，检查是否包含常见的视频数据模式
        # 至少检查文件不是HTML错误页面
        if file_bytes[:100].startswith(b'<html') or file_bytes[:100].startswith(b'<!DOCTYPE'):
            print(f"[Validate] ❌ File appears to be HTML, not video")
            return False
        
        # 如果文件足够大且不是文本，可能是有效的视频文件
        # 但首先检查是否是图片（可能被误判为视频）
        if len(file_bytes) >= 2:
            # 检查是否是JPEG（可能被误判）
            if file_bytes[0:2] == b'\xff\xd8':
                print(f"[Validate] ⚠️ File appears to be JPEG, not video")
                return False
            # 检查是否是PNG
            if file_bytes[0:4] == b'\x89PNG':
                print(f"[Validate] ⚠️ File appears to be PNG, not video")
                return False
        
        # 如果文件足够大且不是文本/图片，可能是有效的视频文件
        if len(file_bytes) > 1000:
            # 检查是否包含HTML标签（错误页面）
            try:
                text_start = file_bytes[:500].decode('utf-8', errors='ignore')
                if '<html' in text_start.lower() or '<!doctype' in text_start.lower():
                    print(f"[Validate] ❌ File appears to be HTML error page")
                    return False
            except:
                pass
            return True
        
        return False
    
    # 图片文件魔数检查
    else:
        # JPEG: FF D8 FF
        if file_bytes[0:3] == b'\xff\xd8\xff':
            return True
        
        # PNG: 89 50 4E 47
        if file_bytes[0:4] == b'\x89PNG':
            return True
        
        # GIF: 47 49 46 38
        if file_bytes[0:4] == b'GIF8':
            return True
        
        # WebP: RIFF ... WEBP
        if file_bytes[0:4] == b'RIFF' and file_bytes[8:12] == b'WEBP':
            return True
        
        print(f"[Validate] ⚠️ Image file magic number not recognized")
        return False


def _validate_media_file(filepath, media_type):
    """
    验证已保存的媒体文件
    返回True如果文件有效
    """
    try:
        if not os.path.exists(filepath):
            return False
        
        file_size = os.path.getsize(filepath)
        if file_size == 0:
            return False
        
        # 读取文件头验证
        with open(filepath, 'rb') as f:
            file_bytes = f.read(12)
        
        return _validate_file_content(file_bytes, media_type)
    except Exception as e:
        print(f"[Validate] Error validating file {filepath}: {e}")
        return False

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
    获取外部上下文信息（改进版：不仅获取标题，还获取文章摘要）
    返回格式化的字符串，包含标题和摘要
    """
    try:
        base = extract_keywords(query_text) or (query_text or "")[:50]
        _setup_proxy()
        results = []  # 存储 {title, summary, url} 字典
        seen_urls = set()
        max_results = 3  # 最多获取3个结果
        
        # 从DuckDuckGo获取结果（增加超时时间和重试机制）
        ddg_success = False
        for retry in range(2):  # 最多重试2次
            try:
                ddg = f"https://duckduckgo.com/html/?q={quote(base)}"
                req = Request(ddg, headers={"User-Agent": "Mozilla/5.0"})
                # 增加超时时间到30秒，并添加SSL上下文
                import ssl
                ssl_context = ssl.create_default_context()
                ssl_context.check_hostname = False
                ssl_context.verify_mode = ssl.CERT_NONE
                # 使用更长的超时时间处理SSL握手
                with urlopen(req, timeout=30, context=ssl_context) as r:
                    html = r.read().decode("utf-8", errors="ignore")
                    ddg_success = True
                    break
            except Exception as e:
                error_msg = str(e)
                is_ssl_timeout = "handshake" in error_msg.lower() or "ssl" in error_msg.lower() or "timeout" in error_msg.lower()
                if retry < 1:  # 还有重试机会
                    print(f"[External] DuckDuckGo error (retry {retry+1}/2): {e}")
                    time.sleep(2)  # 等待2秒后重试
                else:
                    print(f"[External] DuckDuckGo error: {e}")
                    pass
        
        if ddg_success:
            
            # 提取结果链接和标题
            for match in re.finditer(r'<a[^>]*class="result__a"[^>]*href=["\']([^"\']+)["\'][^>]*>(.*?)</a>', html, flags=re.IGNORECASE | re.S):
                url = match.group(1)
                title_html = match.group(2)
                title = re.sub(r"<[^>]+>", " ", title_html)
                title = re.sub(r"\s+", " ", title).strip()
                
                # 提取摘要（通常在 result__snippet 类中）
                snippet_match = re.search(r'<a[^>]*class="result__a"[^>]*>.*?</a>\s*<a[^>]*class="result__snippet"[^>]*>(.*?)</a>', html[html.find(match.group(0)):html.find(match.group(0))+500], flags=re.IGNORECASE | re.S)
                snippet = ""
                if snippet_match:
                    snippet = re.sub(r"<[^>]+>", " ", snippet_match.group(1))
                    snippet = re.sub(r"\s+", " ", snippet).strip()[:150]  # 限制长度
                
                # 处理相对URL
                if url and not url.startswith('http'):
                    if url.startswith('//'):
                        url = 'https:' + url
                    elif url.startswith('/'):
                        url = 'https://duckduckgo.com' + url
                
                if title and url and url not in seen_urls and url.startswith('http'):
                    seen_urls.add(url)
                    # 尝试获取URL的预览（获取更多信息）
                    try:
                        preview = _fetch_url_preview(url, timeout=8)
                        if preview and preview.get("summary"):
                            summary = preview.get("summary", "")[:200]  # 限制长度
                        else:
                            summary = snippet if snippet else title
                    except Exception:
                        summary = snippet if snippet else title
                    
                    results.append({
                        "title": title,
                        "summary": summary,
                        "url": url
                    })
                    if len(results) >= max_results:
                        break
        
        # 从Bing获取结果（如果DuckDuckGo结果不足，增加超时时间和重试机制）
        if len(results) < max_results:
            bing_success = False
            for retry in range(2):  # 最多重试2次
                try:
                    bing = f"https://www.bing.com/search?q={quote(base)}"
                    req = Request(bing, headers={"User-Agent": "Mozilla/5.0"})
                    # 增加超时时间到30秒，并添加SSL上下文
                    import ssl
                    ssl_context = ssl.create_default_context()
                    ssl_context.check_hostname = False
                    ssl_context.verify_mode = ssl.CERT_NONE
                    # 使用更长的超时时间处理SSL握手
                    with urlopen(req, timeout=30, context=ssl_context) as r:
                        html = r.read().decode("utf-8", errors="ignore")
                        bing_success = True
                        break
                except Exception as e:
                    error_msg = str(e)
                    is_ssl_timeout = "handshake" in error_msg.lower() or "ssl" in error_msg.lower() or "timeout" in error_msg.lower()
                    if retry < 1:  # 还有重试机会
                        print(f"[External] Bing error (retry {retry+1}/2): {e}")
                        time.sleep(2)  # 等待2秒后重试
                    else:
                        print(f"[External] Bing error: {e}")
                        pass
            
            if bing_success:
                # 提取Bing结果
                for match in re.finditer(r'<h2><a[^>]*href=["\']([^"\']+)["\'][^>]*>(.*?)</a></h2>', html, flags=re.IGNORECASE | re.S):
                    url = match.group(1)
                    title_html = match.group(2)
                    title = re.sub(r"<[^>]+>", " ", title_html)
                    title = re.sub(r"\s+", " ", title).strip()
                    
                    # 提取摘要
                    snippet_match = re.search(r'<p[^>]*>(.*?)</p>', html[html.find(match.group(0)):html.find(match.group(0))+500], flags=re.IGNORECASE | re.S)
                    snippet = ""
                    if snippet_match:
                        snippet = re.sub(r"<[^>]+>", " ", snippet_match.group(1))
                        snippet = re.sub(r"\s+", " ", snippet).strip()[:150]
                    
                    # 处理相对URL
                    if url and not url.startswith('http'):
                        if url.startswith('//'):
                            url = 'https:' + url
                        elif url.startswith('/'):
                            url = 'https://www.bing.com' + url
                    
                    if title and url and url not in seen_urls and url.startswith('http'):
                        seen_urls.add(url)
                        try:
                            preview = _fetch_url_preview(url, timeout=8)
                            if preview and preview.get("summary"):
                                summary = preview.get("summary", "")[:200]
                            else:
                                summary = snippet if snippet else title
                        except Exception:
                            summary = snippet if snippet else title
                        
                        results.append({
                            "title": title,
                            "summary": summary,
                            "url": url
                        })
                        if len(results) >= max_results:
                            break
        
        # 格式化输出
        if results:
            formatted = []
            for r in results:
                # 如果摘要和标题相似，只显示标题
                if r["summary"] and r["summary"] != r["title"] and len(r["summary"]) > len(r["title"]) + 20:
                    formatted.append(f"{r['title']} — {r['summary']}")
                else:
                    formatted.append(r["title"])
            return " | ".join(formatted)
        
        return f"Keywords: {base}"
    except Exception as e:
        print(f"[External] Error: {e}")
        return f"Keywords: {(query_text or '')[:50]}"

def hf_caption_image(image_path_or_url, timeout=10):
    """
    使用HuggingFace API为图片生成描述
    支持本地文件路径或URL（如果是URL，会先下载到本地）
    注意：只支持图片，不支持视频
    """
    try:
        if not image_path_or_url:
            return ""
        if not HUGGINGFACE_API_KEY:
            if not ENABLE_LOCAL_HF_FALLBACK:
                print("[AI][HF] ⚠ No API key, skip cloud caption")
                return ""
        
        # 检查 Image 是否可用
        if Image is None:
            print("[AI][HF] ⚠ PIL/Pillow not available, skipping image caption")
            return ""
        
        # 判断是本地路径还是URL
        is_url = image_path_or_url.startswith(('http://', 'https://'))
        
        # 如果是URL，先下载到本地（用于后续的HuggingFace API调用，不会触发本地模型下载）
        if is_url:
            # 检查URL是否是视频格式，如果是则跳过
            if any(ext in image_path_or_url.lower() for ext in ['.mp4', '.webm', '.mov', '.avi', '.mkv']):
                return ""  # 静默跳过视频
            
            # 下载图片到本地（用于后续的HuggingFace API调用）
            local_path = download_media(image_path_or_url, media_type='image', timeout=timeout, max_size_mb=10)
            if not local_path or not os.path.exists(local_path):
                return ""
            image_path = local_path
        else:
            # 使用本地路径
            if not os.path.exists(image_path_or_url):
                return ""
            image_path = image_path_or_url
        
        try:
            img = Image.open(image_path).convert("RGB")
            max_side = 1024
            w, h = img.size
            if max(w, h) > max_side:
                scale = max_side / float(max(w, h))
                img = img.resize((int(w * scale), int(h * scale)))
            bio = tempfile.NamedTemporaryFile(delete=False, suffix=".jpg")
            img.save(bio.name, format="JPEG", quality=85)
            with open(bio.name, "rb") as f:
                img_bytes = f.read()
            try:
                os.remove(bio.name)
            except Exception:
                pass
            if len(img_bytes) > 10 * 1024 * 1024:
                return ""
        except Exception:
            return ""
        
        if not HUGGINGFACE_HUB_AVAILABLE:
            print("[AI][HF] ⚠ huggingface_hub not installed, skip cloud caption")
            return ""
        _setup_proxy()
        
        # 为 huggingface_hub (使用 httpx) 配置代理
        # huggingface_hub 使用 httpx，需要通过 HTTP_PROXY/HTTPS_PROXY 环境变量配置
        # 如果使用 SOCKS 代理，需要转换为 httpx 支持的格式
        proxy_str = os.getenv("SOCKS_PROXY", "").strip()
        if proxy_str:
            # 解析 SOCKS 代理地址
            if proxy_str.startswith("socks5://") or proxy_str.startswith("socks4://"):
                proxy_url = proxy_str
            else:
                # 格式: 127.0.0.1:7890 -> socks5://127.0.0.1:7890
                if ":" in proxy_str:
                    host, port = proxy_str.rsplit(":", 1)
                    proxy_url = f"socks5://{host}:{port}"
                else:
                    proxy_url = f"socks5://{proxy_str}:1080"
            # 设置环境变量供 httpx 使用
            os.environ["HTTP_PROXY"] = proxy_url
            os.environ["HTTPS_PROXY"] = proxy_url
        print(f"[AI][HF] ▶ Caption request model=None (using vision chat model) bytes={len(img_bytes)}")
        try:
            # 尝试不同的provider，因为某些模型在hf-inference端点不可用
            # 优先使用默认provider（auto），它会自动选择可用的端点
            # 注意：根据 HuggingFace 文档，InferenceClient 应该自动处理 provider 选择
            client = None
            provider_used = None
            try:
                # 首先尝试不使用provider参数（默认使用auto，会自动选择可用的端点）
                # 根据文档，这应该是最推荐的方式
                client = InferenceClient(api_key=HUGGINGFACE_API_KEY)
                provider_used = "auto"
            except Exception as e1:
                try:
                    # 如果默认provider失败，尝试hf-inference
                    client = InferenceClient(provider="hf-inference", api_key=HUGGINGFACE_API_KEY)
                    provider_used = "hf-inference"
                except Exception as e2:
                    # 如果 InferenceClient 创建失败，设置 client 为 None，后续会尝试 HTTP API
                    client = None
                    provider_used = None
            # 如果 InferenceClient 创建失败，直接尝试 HTTP API
            txt = ""
            if client is None:
                txt = ""
            else:
                # 主模型和后备模型已全部删除，因为它们总是返回StopIteration错误，没有任何输出
                # 现在直接跳过主模型和后备模型的调用，直接使用 vision chat 模型
                txt = ""  # 设置为空，让代码继续执行到 vision chat 模型
            
            # 如果所有 InferenceClient 方法都失败，直接使用 router.huggingface.co 的 vision chat 模型
            if not txt:
                try:
                    import requests
                    # 配置 requests 的代理（如果设置了 SOCKS_PROXY）
                    proxies = None
                    proxy_str = os.getenv("SOCKS_PROXY", "").strip()
                    if proxy_str:
                        # 解析 SOCKS 代理地址
                        if proxy_str.startswith("socks5://") or proxy_str.startswith("socks4://"):
                            proxy_url = proxy_str
                        else:
                            if ":" in proxy_str:
                                host, port = proxy_str.rsplit(":", 1)
                                proxy_url = f"socks5://{host}:{port}"
                            else:
                                proxy_url = f"socks5://{proxy_str}:1080"
                        # requests 库需要 HTTP/HTTPS 代理格式，但 SOCKS 代理需要特殊处理
                        # 对于 requests，SOCKS 代理需要使用 socks 库
                        try:
                            import socks
                            import socket
                            if ":" in proxy_str and not proxy_str.startswith("socks"):
                                host, port = proxy_str.rsplit(":", 1)
                                port = int(port)
                            else:
                                # 从 proxy_url 解析
                                from urllib.parse import urlparse
                                parsed = urlparse(proxy_url)
                                host = parsed.hostname
                                port = parsed.port or 1080
                            # 注意：requests 的 SOCKS 代理支持需要安装 requests[socks] 或使用 urllib3[socks]
                            # 这里我们尝试使用环境变量，如果不行则跳过代理
                            proxies = {
                                "http": proxy_url,
                                "https": proxy_url,
                            }
                        except Exception:
                            proxies = None
                    # 直接使用支持 vision 的 chat 模型（Qwen2.5-VL）
                    api_url_chat = "https://router.huggingface.co/v1/chat/completions"
                    headers_chat = {
                        "Authorization": f"Bearer {HUGGINGFACE_API_KEY}",
                        "Content-Type": "application/json",
                    }
                    import base64
                    image_base64 = base64.b64encode(img_bytes).decode('utf-8')
                    payload_chat = {
                        "model": "Qwen/Qwen2.5-VL-7B-Instruct",
                        "messages": [
                            {
                                "role": "user",
                                "content": [
                                    {
                                        "type": "text",
                                        "text": "Describe the content of this image in detail."
                                    },
                                    {
                                        "type": "image_url",
                                        "image_url": {
                                            "url": f"data:image/jpeg;base64,{image_base64}"
                                        }
                                    }
                                ]
                            }
                        ]
                    }
                    print(f"[AI][HF] ▶ Trying router.huggingface.co/v1/chat/completions with vision model")
                    response_chat = requests.post(api_url_chat, headers=headers_chat, json=payload_chat, proxies=proxies, timeout=timeout)
                    if response_chat.status_code == 200:
                        result_chat = response_chat.json()
                        # 处理 OpenAI 兼容格式的响应
                        txt_chat = None
                        if isinstance(result_chat, dict):
                            if "choices" in result_chat and isinstance(result_chat["choices"], list) and len(result_chat["choices"]) > 0:
                                choice = result_chat["choices"][0]
                                if isinstance(choice, dict) and "message" in choice:
                                    message = choice["message"]
                                    if isinstance(message, dict) and "content" in message:
                                        txt_chat = message["content"].strip()
                        if txt_chat:
                            print("[AI][HF] ✔ Caption generated via router.huggingface.co/v1/chat/completions (vision model)")
                            return txt_chat
                    else:
                        print(f"[AI][HF] ⚠ vision chat model failed: {response_chat.status_code}")
                except ImportError:
                    # requests 库不可用，跳过
                    pass
                except Exception as e:
                    pass
            return txt
        except Exception as e:
            print(f"[AI][HF] ⚠ Caption request failed: {e}")
            import traceback
            traceback.print_exc()
            return ""
    except Exception as e:
        import traceback
        traceback.print_exc()
        return ""

def hf_run_model(image_bytes, model_id, timeout=10):
    try:
        if not HUGGINGFACE_API_KEY:
            print("[AI] ⚠ Missing HUGGINGFACE_API_KEY, skip model:", model_id)
            return ""
        if not HUGGINGFACE_HUB_AVAILABLE:
            print("[AI] ⚠ huggingface_hub not available, skip model:", model_id)
            return ""
        print(f"[AI][HF] ▶ Model request: {model_id} bytes={len(image_bytes)}")
        
        # InferenceClient调用已删除，因为这些模型总是返回StopIteration错误，没有任何输出
        # 现在直接跳过InferenceClient调用，直接使用 vision chat 模型
        
        # 使用 vision chat 模型（Qwen2.5-VL）替代已弃用的端点
        try:
            import requests
            import base64
            # 配置代理
            proxies = None
            proxy_str = os.getenv("SOCKS_PROXY", "").strip()
            if proxy_str:
                if proxy_str.startswith("socks5://") or proxy_str.startswith("socks4://"):
                    proxy_url = proxy_str
                else:
                    if ":" in proxy_str:
                        host, port = proxy_str.rsplit(":", 1)
                        proxy_url = f"socks5://{host}:{port}"
                    else:
                        proxy_url = f"socks5://{proxy_str}:1080"
                try:
                    proxies = {
                        "http": proxy_url,
                        "https": proxy_url,
                    }
                except Exception:
                    proxies = None
            # 使用 vision chat 模型
            api_url_chat = "https://router.huggingface.co/v1/chat/completions"
            headers_chat = {
                "Authorization": f"Bearer {HUGGINGFACE_API_KEY}",
                "Content-Type": "application/json",
            }
            image_base64 = base64.b64encode(image_bytes).decode('utf-8')
            payload_chat = {
                "model": "Qwen/Qwen2.5-VL-7B-Instruct",
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "text",
                                "text": "Describe the content of this image in detail."
                            },
                            {
                                "type": "image_url",
                                "image_url": {
                                    "url": f"data:image/jpeg;base64,{image_base64}"
                                }
                            }
                        ]
                    }
                ]
            }
            response_chat = requests.post(api_url_chat, headers=headers_chat, json=payload_chat, proxies=proxies, timeout=timeout)
            if response_chat.status_code == 200:
                result_chat = response_chat.json()
                # 处理 OpenAI 兼容格式的响应
                txt_chat = None
                if isinstance(result_chat, dict):
                    if "choices" in result_chat and isinstance(result_chat["choices"], list) and len(result_chat["choices"]) > 0:
                        choice = result_chat["choices"][0]
                        if isinstance(choice, dict) and "message" in choice:
                            message = choice["message"]
                            if isinstance(message, dict) and "content" in message:
                                txt_chat = message["content"].strip()
                if txt_chat:
                    return txt_chat
            else:
                print(f"[AI] ⚠ vision chat model failed: {response_chat.status_code}")
                return ""
        except ImportError:
            # requests 库不可用，跳过
            return ""
        except Exception as e:
            return ""
    except Exception as e:
        import traceback
        traceback.print_exc()
        return ""

LOCAL_IMG_CAPTIONER = None
ENABLE_LOCAL_HF_FALLBACK = os.getenv("HF_LOCAL_FALLBACK", "0").strip() == "1"
def local_caption_image(image_path, timeout=10):
    try:
        from PIL import Image
        from transformers import pipeline
        global LOCAL_IMG_CAPTIONER
        if LOCAL_IMG_CAPTIONER is None:
            LOCAL_IMG_CAPTIONER = pipeline("image-to-text", model="Salesforce/blip-image-captioning-base")
        img = Image.open(image_path).convert("RGB")
        out = LOCAL_IMG_CAPTIONER(img)
        if isinstance(out, list) and out:
            txt = str(out[0].get("generated_text") or "").strip()
            return txt
        return ""
    except Exception:
        return ""

def analyze_image_with_models(local_path, timeout=10):
    if Image is None:
        print("[AI] ⚠ PIL/Pillow not available, skipping image analysis")
        return {}
    try:
        if not os.path.exists(local_path):
            return {}
        try:
            img = Image.open(local_path).convert("RGB")
            max_side = 1024
            w, h = img.size
            if max(w, h) > max_side:
                scale = max_side / float(max(w, h))
                img = img.resize((int(w * scale), int(h * scale)))
            bio = tempfile.NamedTemporaryFile(delete=False, suffix=".jpg")
            img.save(bio.name, format="JPEG", quality=85)
            with open(bio.name, "rb") as f:
                img_bytes = f.read()
            try:
                os.remove(bio.name)
            except Exception:
                pass
            if len(img_bytes) == 0 or len(img_bytes) > 10 * 1024 * 1024:
                print("[AI] ⚠ Image bytes invalid or too large, skip:", local_path)
                return {}
        except Exception as e:
            import traceback
            traceback.print_exc()
            return {}
        _setup_proxy()
        
        # 为 huggingface_hub (使用 httpx) 配置代理
        proxy_str = os.getenv("SOCKS_PROXY", "").strip()
        if proxy_str:
            # 解析 SOCKS 代理地址
            if proxy_str.startswith("socks5://") or proxy_str.startswith("socks4://"):
                proxy_url = proxy_str
            else:
                # 格式: 127.0.0.1:7890 -> socks5://127.0.0.1:7890
                if ":" in proxy_str:
                    host, port = proxy_str.rsplit(":", 1)
                    proxy_url = f"socks5://{host}:{port}"
                else:
                    proxy_url = f"socks5://{proxy_str}:1080"
            # 设置环境变量供 httpx 使用
            os.environ["HTTP_PROXY"] = proxy_url
            os.environ["HTTPS_PROXY"] = proxy_url
        print(f"[AI][HF] ▶ Image analyze: {local_path} models=caption-only key_present={bool(HUGGINGFACE_API_KEY)}")
        out = {}
        # 优先使用BLIP/VIT caption端点（更稳定）
        cap_primary = hf_caption_image(local_path, timeout=max(12, timeout))
        if cap_primary:
            out["caption"] = cap_primary
        if not out and ENABLE_LOCAL_HF_FALLBACK:
            cap = hf_caption_image(local_path, timeout=max(10, timeout))
            if cap:
                out["fallback_caption"] = cap
            else:
                print("[AI] ⚠ No model outputs for:", local_path)
        if not out:
            print("[AI] ⚠ No model outputs for:", local_path)
        return out
    except Exception:
        return {}


def _load_media_caption_cache():
    """
    从已保存的 market_alerts 中加载媒体摘要，减少重复 HF 调用。
    key: 本地媒体路径
    value: 已生成的媒体摘要（media_ai_summary）
    """
    cache = {}
    try:
        if not os.path.exists(ALERTS_FILE):
            return cache
        with open(ALERTS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        alerts = []
        if isinstance(data, dict):
            alerts = data.get("alerts") or []
        elif isinstance(data, list):
            alerts = data
        for a in alerts:
            ai = a.get("ai_analysis") or {}
            summary = ai.get("media_ai_summary") or ""
            if not summary:
                continue
            local_paths = a.get("local_media_paths") or []
            for p in local_paths:
                if p and p not in cache:
                    cache[p] = summary
    except Exception:
        pass
    return cache

def analyze_local_media_for_alert(alert, max_images=3):
    try:
        paths = alert.get("local_media_paths") or []
        images = []
        videos = []
        for p in paths:
            if not p or not os.path.exists(p):
                continue
            is_video = any(ext in p.lower() for ext in [".mp4", ".webm", ".mov", ".avi", ".mkv"])
            if is_video:
                videos.append(p)
            else:
                images.append(p)
            if len(images) >= int(max_images) and len(videos) >= 2:
                break
        results = {}
        summary_parts = []
        print(f"[AI] ▶ Media paths: images={len(images)} videos={len(videos)}")
        # 图片识别
        for idx, p in enumerate(images[:max_images], start=1):
            print(f"[AI][HF] ▶ Begin image analyze: {p}")
            r = analyze_image_with_models(p, timeout=12)
            if r:
                results[p] = r
                cap = r.get("caption") or r.get("fallback_caption") or ""
                det = r.get("facebook/detr-resnet-50") or ""
                s = ""
                if cap:
                    s += cap
                if det:
                    if s:
                        s += " | "
                    s += det
                if s:
                    summary_parts.append(f"[img{idx}] {s}")
        # 视频抽帧识别
        ff_frames_used = False
        for vid_idx, vp in enumerate(videos[:2], start=1):
            frames = _extract_video_keyframes_ffmpeg(vp, num_frames=3)
            if frames:
                ff_frames_used = True
            if not frames and cv2 is not None:
                frames = _extract_video_keyframes(vp, num_frames=3)
            print(f"[AI] ▶ Video frames extracted: {len(frames)} using={'ffmpeg' if ff_frames_used else ('opencv' if cv2 is not None else 'none')}")
            for vid_idx, vp in enumerate(videos[:2], start=1):
                frame_caps = []
                for fb in frames:
                    # cap1和cap2的模型调用已删除，因为这些模型总是失败，没有任何输出
                    # 现在直接跳过这些调用
                    best = None
                if frame_caps:
                    v_summary = " | ".join(frame_caps)
                    summary_parts.append(f"[vid{vid_idx}] {v_summary}")
        summary = " ".join(summary_parts) if summary_parts else ""
        print(f"[AI] ✔ Media summary length: {len(summary)}")
        return {"media_multi_model": results, "media_ai_summary": summary}
    except Exception as e:
        import traceback
        traceback.print_exc()
        return {"media_multi_model": {}, "media_ai_summary": ""}

def _extract_urls_from_text(text):
    try:
        if not text:
            return []
        raw = str(text)
        urls = re.findall(r'https?://[^\s)]+', raw)
        uniq = []
        seen = set()
        for u in urls:
            u2 = u.strip().strip('.,;:')
            if u2 and u2 not in seen:
                seen.add(u2)
                uniq.append(u2)
        if not uniq:
            for m in re.finditer(r'https?://', raw):
                tail = raw[m.start():]
                end = re.search(r'[\s\r\n]', tail)
                seg = tail[: end.start()] if end else tail
                seg = re.sub(r'\s+', '', seg)
                seg = seg.strip().strip('.,;:')
                if seg and seg not in seen:
                    seen.add(seg)
                    uniq.append(seg)
        print(f"[AI] ▶ Extracted URLs: {len(uniq)}")
        return uniq
    except Exception:
        return []

def _fetch_url_preview(url, timeout=HTML_FETCH_TIMEOUT):
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": "https://truthsocial.com/",
        }
        _setup_proxy()
        req = Request(url, headers=headers)
        with urlopen(req, timeout=timeout) as resp:
            html = resp.read().decode("utf-8", errors="ignore")
        # 提取 <title> 与 meta description
        title_match = re.search(r'<title[^>]*>(.*?)</title>', html, re.IGNORECASE | re.S)
        title = ""
        if title_match:
            title = re.sub(r'\s+', ' ', title_match.group(1).strip())
        desc_match = re.search(r'<meta[^>]*name=["\']description["\'][^>]*content=["\'](.*?)["\']', html, re.IGNORECASE | re.S)
        desc = ""
        if desc_match:
            desc = re.sub(r'\s+', ' ', desc_match.group(1).strip())
        # 备选：提取首段文本
        body_text = ""
        try:
            # 去掉标签，仅保留文本前 200 字
            text_only = re.sub(r'<script[\s\S]*?</script>|<style[\s\S]*?</style>', ' ', html, flags=re.IGNORECASE)
            text_only = re.sub(r'<[^>]+>', ' ', text_only)
            text_only = re.sub(r'\s+', ' ', text_only).strip()
            body_text = text_only[:200]
        except Exception:
            body_text = ""
        summary_parts = []
        if title:
            summary_parts.append(title)
        if desc:
            summary_parts.append(desc)
        if not desc and body_text:
            summary_parts.append(body_text)
        summary = " — ".join(summary_parts) if summary_parts else ""
        return {"url": url, "title": title, "desc": desc, "summary": summary}
    except Exception:
        return {"url": url, "title": "", "desc": "", "summary": ""}

def analyze_web_for_alert(alert, max_urls=3):
    try:
        content = str(alert.get("content") or "").strip()
        urls = _extract_urls_from_text(content)
        urls = urls[: int(max_urls)]
        previews = []
        print(f"[AI] ▶ Web URLs found: {len(urls)}")
        for u in urls:
            prev = _fetch_url_preview(u, timeout=HTML_FETCH_TIMEOUT)
            if prev and (prev.get("summary") or "").strip():
                previews.append(prev)
        if previews:
            text = " | ".join([p.get("summary", "") for p in previews if p.get("summary")])
        else:
            text = ""
        ext_summary = ""
        if urls:
            full_html = _fetch_url_full(urls[0], timeout=HTML_FETCH_TIMEOUT)
            if full_html:
                ext_summary = ai_summarize_text(full_html, timeout=20)
        if ext_summary or text:
            print("[AI] ✔ Web summary generated")
        return {"web_previews": previews, "web_ai_summary": text, "external_ai_summary": ext_summary}
    except Exception:
        return {"web_previews": [], "web_ai_summary": "", "external_ai_summary": ""}

def _extract_video_keyframes(local_path, num_frames=3):
    try:
        if cv2 is None:
            return []
        cap = cv2.VideoCapture(local_path)
        if not cap.isOpened():
            return []
        fps = cap.get(cv2.CAP_PROP_FPS) or 25.0
        total = cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0
        duration = (total / fps) if fps else 0
        ms_positions = []
        if duration > 0:
            for i in range(num_frames):
                t = (i + 1) * (duration / (num_frames + 1))
                ms_positions.append(int(t * 1000))
        else:
            ms_positions = [i * 1000 for i in range(num_frames)]
        frames_bytes = []
        for ms in ms_positions:
            cap.set(cv2.CAP_PROP_POS_MSEC, ms)
            ok, frame = cap.read()
            if not ok or frame is None:
                continue
            try:
                # BGR->RGB 再编码为JPEG
                rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                ok2, buf = cv2.imencode(".jpg", rgb, [int(cv2.IMWRITE_JPEG_QUALITY), 85])
                if ok2:
                    frames_bytes.append(buf.tobytes())
            except Exception:
                continue
        cap.release()
        return frames_bytes
    except Exception:
        return []

def _extract_video_keyframes_ffmpeg(local_path, num_frames=3):
    try:
        ffmpeg_path = os.getenv("FFMPEG_PATH", "ffmpeg")
        if not shutil.which(ffmpeg_path):
            return []
        out_dir = tempfile.mkdtemp(prefix="frames_")
        try:
            cmd = [
                ffmpeg_path,
                "-hide_banner",
                "-loglevel", "error",
                "-i", local_path,
                "-vf", f"select='not(mod(n,{max(1, num_frames)}))',scale=640:-2",
                "-frames:v", str(num_frames),
                os.path.join(out_dir, "frame_%03d.jpg"),
            ]
            subprocess.run(cmd, check=False)
            frames = []
            for name in sorted(os.listdir(out_dir)):
                if name.lower().endswith(".jpg"):
                    p = os.path.join(out_dir, name)
                    try:
                        with open(p, "rb") as f:
                            frames.append(f.read())
                    except Exception:
                        continue
            return frames
        finally:
            try:
                shutil.rmtree(out_dir, ignore_errors=True)
            except Exception:
                pass
    except Exception:
        return []

def _fetch_url_full(url, timeout=HTML_FETCH_TIMEOUT):
    try:
        _setup_proxy()
        req = Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urlopen(req, timeout=timeout) as r:
            html = r.read().decode("utf-8", errors="ignore")
        html = re.sub(r"<script[\s\S]*?</script>|<style[\s\S]*?</style>", " ", html, flags=re.IGNORECASE)
        text = re.sub(r"<[^>]+>", " ", html)
        text = re.sub(r"\s+", " ", text).strip()
        return text[:8000]
    except Exception:
        return ""

def ai_summarize_text(text, timeout=20):
    try:
        if not text or not SILICONFLOW_API_KEY:
            return ""
        proxy_str = os.getenv("SOCKS_PROXY", "").strip()
        http_client = None
        if proxy_str:
            try:
                import httpx
                if proxy_str.startswith("socks5://") or proxy_str.startswith("socks4://"):
                    proxy_str2 = proxy_str
                else:
                    proxy_str2 = f"socks5://{proxy_str}"
                http_client = httpx.Client(proxies=proxy_str2, timeout=60.0)
            except Exception:
                http_client = None
        client = OpenAI(api_key=SILICONFLOW_API_KEY, base_url=BASE_URL, http_client=http_client)
        prompt = f"Summarize the following article in 3 bullet points and a 1-sentence thesis:\n\n{text[:6000]}"
        summary_options = {
            "model": SILICONFLOW_TEXT_MODEL,
            "messages": [{"role": "system", "content": "Output concise plain text."},
                         {"role": "user", "content": prompt}],
            "temperature": 0.2,
            "max_tokens": 256,
        }
        if "Qwen3.6" in SILICONFLOW_TEXT_MODEL:
            summary_options["extra_body"] = {"enable_thinking": False}
        resp = client.chat.completions.create(**summary_options)
        out = resp.choices[0].message.content or ""
        return out.strip()
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

def _image_bytes_to_data_url(image_bytes, mime_type="image/jpeg"):
    if not image_bytes:
        return ""
    return f"data:{mime_type};base64,{base64.b64encode(image_bytes).decode('ascii')}"

def _normalize_image_for_vision(path_or_bytes, max_side=1280):
    """Resize/compress an image into a vision-model friendly data URL."""
    try:
        if isinstance(path_or_bytes, (bytes, bytearray)):
            raw = bytes(path_or_bytes)
        else:
            with open(path_or_bytes, "rb") as f:
                raw = f.read()
        if not raw or Image is None:
            return ""
        img = Image.open(io.BytesIO(raw)).convert("RGB")
        w, h = img.size
        if max(w, h) > max_side:
            scale = max_side / float(max(w, h))
            img = img.resize((max(1, int(w * scale)), max(1, int(h * scale))))
        out = io.BytesIO()
        img.save(out, format="JPEG", quality=84, optimize=True)
        return _image_bytes_to_data_url(out.getvalue())
    except Exception:
        return ""

def _video_to_data_url(path, max_mb=DIRECT_VIDEO_MAX_MB):
    """Read a local video as a data URL for providers that support video_url."""
    try:
        if not path or not os.path.isfile(path):
            return ""
        if os.path.getsize(path) > float(max_mb) * 1024 * 1024:
            print(f"[AI] ⚠ Video exceeds direct-upload limit ({max_mb:g} MB): {path}")
            return ""
        with open(path, "rb") as f:
            raw = f.read()
        if not raw:
            return ""
        mime_type = mimetypes.guess_type(path)[0] or "video/mp4"
        return _image_bytes_to_data_url(raw, mime_type=mime_type)
    except Exception as e:
        print(f"[AI] ⚠ Direct video encoding failed: {e}")
        return ""

def _build_multimodal_parts(media, max_images=6, allow_direct_video=False):
    """
    Build OpenAI-compatible image/video parts from local media.
    When allow_direct_video is true, videos are sent as video_url first;
    callers can invoke this again with false to build a keyframe fallback.
    """
    parts = []
    used = 0
    seen_media = set()
    for index, item in enumerate(media or [], start=1):
        if used >= max_images:
            break
        if not isinstance(item, dict):
            continue
        raw_url = str(item.get("url") or item.get("preview_url") or "").strip()
        preview = str(item.get("preview_url") or "").strip()
        media_key = raw_url or preview
        if media_key in seen_media:
            continue
        seen_media.add(media_key)
        media_type = (item.get("type") or "").lower()
        is_video = media_type in ("video", "gifv") or any(
            ext in raw_url.lower() for ext in [".mp4", ".webm", ".mov", ".avi", ".mkv", "/videos/"]
        )
        local_path = raw_url if raw_url and os.path.exists(raw_url) else ""
        if is_video:
            if allow_direct_video and local_path:
                video_url = _video_to_data_url(local_path)
                if video_url:
                    parts.append({"type": "text", "text": f"视频 {index}（完整视频）："})
                    parts.append({"type": "video_url", "video_url": {"url": video_url}})
                    used += 1
                    continue
            frames = []
            if local_path and os.path.exists(local_path):
                frames = _extract_video_keyframes_ffmpeg(local_path, num_frames=3)
                if not frames and cv2 is not None:
                    frames = _extract_video_keyframes(local_path, num_frames=3)
            if frames:
                for frame_index, frame_bytes in enumerate(frames, start=1):
                    if used >= max_images:
                        break
                    data_url = _normalize_image_for_vision(frame_bytes)
                    if data_url:
                        parts.append({"type": "text", "text": f"视频 {index} 的关键帧 {frame_index}："})
                        parts.append({"type": "image_url", "image_url": {"url": data_url}})
                        used += 1
                continue
            fallback_path = preview if preview and os.path.exists(preview) else ""
            if fallback_path:
                data_url = _normalize_image_for_vision(fallback_path)
                if data_url:
                    parts.append({"type": "text", "text": f"视频 {index} 的预览帧："})
                    parts.append({"type": "image_url", "image_url": {"url": data_url}})
                    used += 1
            continue
        if not local_path:
            continue
        data_url = _normalize_image_for_vision(local_path)
        if data_url:
            parts.append({"type": "text", "text": f"图片 {index}："})
            parts.append({"type": "image_url", "image_url": {"url": data_url}})
            used += 1
    return parts

def _parse_json_model_output(content):
    text = str(content or "").strip()
    fence = chr(96) * 3
    if text.startswith(fence):
        text = text[len(fence):].lstrip()
        if text.lower().startswith("json"):
            text = text[4:].lstrip()
        if text.endswith(fence):
            text = text[:-len(fence)].rstrip()
    return json.loads(text)

def _normalize_ai_result(result):
    """Apply conservative guardrails after the model returns JSON."""
    if not isinstance(result, dict):
        result = {}
    out = dict(result)

    def _number(value, default=None):
        try:
            text = str(value).strip().replace('%', '')
            return float(text) if text else default
        except Exception:
            return default

    impact_type = str(out.get("impact_type") or "unknown").strip().lower()
    score = _number(out.get("market_impact_score"))
    confidence = _number(out.get("confidence"))
    if score is not None:
        score = max(0.0, min(100.0, score if score > 1 else score * 100))
    if confidence is not None:
        confidence = max(0.0, min(1.0, confidence if confidence <= 1 else confidence / 100))

    raw_impact = out.get("impact")
    if isinstance(raw_impact, str):
        impact = raw_impact.strip().lower() in {"true", "yes", "1"}
    else:
        impact = bool(raw_impact)
    # Political-only posts and low scores must not become trade alerts merely
    # because the model found a familiar ticker association.
    if impact_type in {"political_only", "political", "noise", "personal"}:
        impact = False
    if score is not None and score < 60:
        impact = False

    assets = out.get("affected_assets") or []
    if not isinstance(assets, list):
        assets = [assets]
    clean_assets = []
    for asset in assets[:8]:
        symbol = re.sub(r"[^A-Za-z0-9.\-]", "", str(asset).upper()).strip()
        if symbol and re.fullmatch(r"[A-Z][A-Z0-9.\-]{0,7}", symbol) and symbol not in clean_assets:
            clean_assets.append(symbol)

    out["impact"] = impact
    out["impact_type"] = impact_type
    out["market_impact_score"] = round(score) if score is not None else None
    out["confidence"] = round(confidence, 2) if confidence is not None else None
    out["fact_check_status"] = str(out.get("fact_check_status") or "unverified").strip().lower()
    out["affected_assets"] = clean_assets if impact else []
    if not impact:
        out["recommendation"] = "None"
    return out

def analyze_with_ai(post_content, media=None, retries=2, backoff=1.5):
    """
    Analyze text posts with the text model and attached media with the
    configured vision model via SiliconFlow. Videos are sent directly first;
    failed direct-video requests fall back to keyframes and then text-only AI.
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
    downloaded_media_paths = []  # 收集所有下载的媒体文件路径
    try:
        arr = media or []
        if arr:
            lines = []
            for i, m in enumerate(arr):
                if not isinstance(m, dict):
                    continue
                t = (m.get("type") or "").lower()
                raw_url = str(m.get("url") or "").strip()
                preview_url = str(m.get("preview_url") or "").strip()
                is_video = t in ("video", "gifv") or any(
                    ext in raw_url.lower()
                    for ext in (".mp4", ".webm", ".mov", ".avi", ".mkv", "/videos/")
                )
                media_url = raw_url or preview_url
                media_type = "video" if is_video else "image"
                local_path = None
                if media_url and os.path.exists(media_url):
                    local_path = media_url
                elif media_url:
                    local_path = download_media(
                        media_url,
                        media_type=media_type,
                        timeout=30,
                        max_size_mb=100 if is_video else 10,
                    )
                if local_path and local_path not in downloaded_media_paths:
                    downloaded_media_paths.append(local_path)

                description = (m.get("description") or "").strip()
                display_url = local_path or media_url or preview_url
                lines.append(
                    f"[{i + 1}] ({media_type}) "
                    f"{description or 'attached media will be inspected directly'} | {display_url}"
                )
            if lines:
                media_context = "\n".join(lines)
                caption_text = " ".join(
                    str(m.get("description") or "").strip()
                    for m in arr
                    if isinstance(m, dict) and str(m.get("description") or "").strip()
                )
    except Exception as e:
        # 记录错误但继续处理
        print(f"Error processing media: {e}")
        media_context = "No media attached."
        caption_text = ""

    # 配置 OpenAI 客户端
    # 如果配置了 SOCKS_PROXY，为 OpenAI 客户端也配置代理
    # 注意：SiliconFlow API 可能不需要代理，但为了统一性也配置
    proxy_str = os.getenv("SOCKS_PROXY", "").strip()
    http_client = None
    
    if proxy_str:
        try:
            # 解析代理地址
            if proxy_str.startswith("socks5://") or proxy_str.startswith("socks4://"):
                proxy_str = proxy_str.split("://")[1]
            
            if ":" in proxy_str:
                host, port = proxy_str.rsplit(":", 1)
                port = int(port)
            else:
                host = proxy_str
                port = 1080
            
            # 为 httpx 配置 SOCKS 代理
            import httpx
            proxy_url = f"socks5://{host}:{port}"
            http_client = httpx.Client(proxies=proxy_url, timeout=60.0)
        except Exception as e:
            print(f"[Proxy] OpenAI 客户端代理配置失败: {e}，将使用默认配置")
            http_client = None

    client = OpenAI(
        api_key=SILICONFLOW_API_KEY,
        base_url=BASE_URL,
        http_client=http_client
    )

    combined_text = (post_content or "").strip()
    if caption_text and combined_text:
        combined_text = combined_text + " [Media Interpretation] " + caption_text
    elif caption_text and not combined_text:
        combined_text = caption_text

    # Fetch external context only when no media present
    has_media_inputs = bool(arr) or bool(downloaded_media_paths)
    external_context = ""
    if not has_media_inputs:
        external_context = fetch_external_context(combined_text)
    
    # Fetch recent posts context (for trend analysis)
    recent_posts_context = get_recent_posts_context(limit=5)

    # 媒体不再先经过旧的 Hugging Face caption 流程；图片直接进入视觉模型，
    # 视频先抽取关键帧后进入同一个多模态请求，避免重复调用和错误 caption。
    media_multi = {}
    media_ai_summary = ""
    
    web_ai_summary = ""
    try:
        print("[AI] ▶ Web summary: start")
        web_out = analyze_web_for_alert({"content": post_content})
        web_ai_summary = web_out.get("web_ai_summary") or ""
        external_ai_summary = web_out.get("external_ai_summary") or ""
        if web_ai_summary and combined_text:
            combined_text = combined_text + " [Web Interpretation] " + web_ai_summary
        elif web_ai_summary and not combined_text:
            combined_text = web_ai_summary
    except Exception:
        web_ai_summary = ""
        external_ai_summary = ""

    # 图片直接使用视觉输入；视频优先完整发送，失败后重新构建关键帧输入。
    vision_sources = []
    for path in downloaded_media_paths:
        if path and os.path.exists(path):
            media_kind = "video" if any(
                ext in path.lower() for ext in [".mp4", ".webm", ".mov", ".avi", ".mkv"]
            ) else "image"
            vision_sources.append({"type": media_kind, "url": path, "preview_url": path})
    vision_sources.extend(arr)
    direct_parts = _build_multimodal_parts(
        vision_sources, max_images=6, allow_direct_video=True
    )
    keyframe_parts = _build_multimodal_parts(
        vision_sources, max_images=6, allow_direct_video=False
    )
    direct_video_enabled = any(
        part.get("type") == "video_url" for part in direct_parts
    )
    vision_parts = direct_parts or keyframe_parts
    vision_enabled = bool(vision_parts)
    has_video_sources = any(
        isinstance(item, dict) and (
            (item.get("type") or "").lower() in ("video", "gifv") or
            any(ext in str(item.get("url") or "").lower()
                for ext in (".mp4", ".webm", ".mov", ".avi", ".mkv", "/videos/"))
        )
        for item in vision_sources
    )
    analysis_model = SILICONFLOW_VISION_MODEL if vision_enabled else SILICONFLOW_TEXT_MODEL

    prompt = f"""
    You are a conservative financial-news analyst. Analyze the following social media post by Donald Trump.
    If attached images, a complete video, or video keyframes are present, inspect them directly. Do not rely only on captions.

    **STRICT MARKET-IMPACT RULE:**
    Set impact=true only when the post contains a plausible, direct and material causal path to a public company, industry, commodity, interest rate, tariff, regulation, sanction, government contract, or broad market. A political endorsement, local election graphic, insult, generic celebration, campaign slogan, or unverified image is impact=false by default. Political importance is not the same as tradable market impact.
    It is correct to return an empty affected_assets array. Never invent a ticker to satisfy the format. Do not infer DJT, GEO, SPY, COIN, or any other ticker merely from Trump's endorsement or political affiliation.

    **CONTEXT RULE:**
    Use Recent Trump Posts only to reinforce an already direct market mechanism; recent posts must never create market impact by themselves.
    
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
        "impact": boolean, // conservative tradable-market decision
        "impact_type": "policy" | "company" | "industry" | "macro" | "political_only" | "noise" | "unknown",
        "market_impact_score": 0-100,
        "confidence": 0.0-1.0,
        "fact_check_status": "verified" | "unverified" | "contradicted",
        "reasoning": "string", // Concise thesis using both text and visual evidence. Mention if this reinforces a recent trend from history. (max 80 words)
        "affected_assets": ["list", "of", "tickers"], // Empty unless a direct causal link is stated.
        "sentiment": "positive" | "negative" | "neutral",
        "recommendation": "string", // Optional research reference. "None" if no clear trade.
        "media_summary": "string" // What the attached image/video visibly shows and why it matters.
    }}
    """

    last_err = None
    media_input_mode = "text"
    for attempt in range(int(retries) + 1):
        try:
            active_model = SILICONFLOW_TEXT_MODEL
            active_parts = []
            if direct_video_enabled and attempt == 0:
                active_model = analysis_model
                active_parts = direct_parts
                media_input_mode = "direct_video"
            elif vision_enabled and attempt == 0:
                active_model = analysis_model
                active_parts = keyframe_parts
                media_input_mode = "keyframes" if has_video_sources else "image"
            elif direct_video_enabled and attempt == 1:
                active_model = analysis_model
                active_parts = keyframe_parts
                media_input_mode = "keyframes_fallback"
            elif vision_enabled and attempt == 1 and not direct_video_enabled:
                active_model = SILICONFLOW_TEXT_MODEL
                active_parts = []
                media_input_mode = "text_fallback"
            elif vision_enabled:
                active_model = SILICONFLOW_TEXT_MODEL
                active_parts = []
                media_input_mode = "text_fallback"
            user_content = prompt
            if active_parts:
                user_content = [{"type": "text", "text": prompt}] + active_parts
            messages = [
                {
                    "role": "system",
                    "content": "You are a helpful financial assistant. Output valid JSON only.",
                },
                {"role": "user", "content": user_content},
            ]
            print(
                f"[AI] ▶ {active_model} request attempt "
                f"{attempt+1}/{int(retries)+1} multimodal={bool(active_parts)}"
            )
            request_options = {
                "model": active_model,
                "messages": messages,
                "temperature": 0.0,
                "max_tokens": 900,
            }
            if "Qwen3.6" in active_model:
                request_options["extra_body"] = {"enable_thinking": False}
            try:
                response = client.chat.completions.create(
                    **request_options,
                    response_format={"type": "json_object"},
                )
            except Exception:
                # 部分视觉模型只支持普通 JSON 文本，不支持 response_format。
                response = client.chat.completions.create(**request_options)
            
            result_text = response.choices[0].message.content
            result_json = _normalize_ai_result(_parse_json_model_output(result_text))
            print(f"[AI] ✔ {active_model} response received")

            # 视觉模型能描述海报内容，但没有外部证据时不能证明海报中的
            # “获胜/背书/政策已生效”等事实。避免把图像识别误标为已核验。
            if vision_enabled and not (external_context or external_ai_summary):
                result_json["fact_check_status"] = "unverified"
            
            # Inject external context summary into the result for transparency
            context_preview = "No external news found."
            if "No related external news found" not in external_context:
                match = re.search(r'\[News\] (.*?):', external_context)
                if match:
                    context_preview = f"News Context: {match.group(1)}..."
                else:
                    context_preview = "External market data used."
            
            # 改进 external_context_used：直接使用 fetch_external_context 结果，若包含关键词则提炼显示
            try:
                ctx_preview = (external_ai_summary or external_context or "").strip()
                result_json['external_context_used'] = ctx_preview if ctx_preview else ""
            except Exception:
                result_json['external_context_used'] = ""
            result_json['media_used'] = bool(media_context and media_context != "No media attached.")
            result_json['media_caption_used'] = bool(caption_text)
            result_json['media_multi_model'] = media_multi
            result_json['media_input_mode'] = media_input_mode
            result_json['media_ai_summary'] = (
                result_json.get("media_summary") or media_ai_summary
            )
            result_json['web_ai_summary'] = web_ai_summary
            # #region agent log
            try:
                import json as json_lib
                debug_log_path = os.path.join(PROJECT_ROOT, ".cursor", "debug.log")
                os.makedirs(os.path.dirname(debug_log_path), exist_ok=True)
                with open(debug_log_path, "a", encoding="utf-8") as f:
                    f.write(json_lib.dumps({"sessionId":"debug-session","runId":"run1","hypothesisId":"M3","location":"monitor_trump.py:1550","message":"analyze_with_ai生成结果","data":{"caption_text_len":len(caption_text or ""),"media_context_len":len(media_context or ""),"media_ai_summary_len":len(media_ai_summary or ""),"media_used":result_json.get("media_used",False)},"timestamp":int(time.time()*1000)}) + "\n")
            except Exception:
                pass
            # #endregion
            
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
    try:
        if os.path.exists(ALERTS_FILE):
            with open(ALERTS_FILE, "r") as f:
                data = json.load(f)
            if isinstance(data, dict):
                ids = set(map(str, (data.get("processed_ids") or [])))
                for a in data.get("alerts") or []:
                    aid = str(a.get("id") or "")
                    if aid:
                        ids.add(aid)
                return ids
            if isinstance(data, list):
                return set(str(a.get("id") or "") for a in data if isinstance(a, dict))
    except:
        return set()
    return set()

def save_processed_posts(processed_ids):
    """
    保存已处理的帖子ID列表
    注意：这个函数会读取并更新整个文件，需要确保不会丢失 alerts 数据
    """
    try:
        data = {}
        alerts_before = []
        if os.path.exists(ALERTS_FILE):
            try:
                with open(ALERTS_FILE, "r", encoding='utf-8') as f:
                    cur = json.load(f)
                if isinstance(cur, dict):
                    data["alerts"] = cur.get("alerts") or []
                    data["processed_ids"] = set(map(str, (cur.get("processed_ids") or [])))
                elif isinstance(cur, list):
                    # 兼容旧格式（list）
                    data["alerts"] = cur
                    data["processed_ids"] = set()
                else:
                    data["alerts"] = []
                    data["processed_ids"] = set()
                
                alerts_before = data["alerts"].copy()
            except Exception as e:
                import traceback
                traceback.print_exc()
                data["alerts"] = []
                data["processed_ids"] = set()
        else:
            data["alerts"] = []
            data["processed_ids"] = set()
        
        # 合并 processed_ids（保留现有的，添加新的）
        if processed_ids:
            data["processed_ids"].update(map(str, processed_ids))
        
        # 确保 alerts 数据没有被丢失
        if len(data["alerts"]) < len(alerts_before):
            # 如果 alerts 减少了，使用之前的 alerts（更安全）
            print(f"[WARNING] save_processed_posts: alerts count decreased from {len(alerts_before)} to {len(data['alerts'])}, restoring previous alerts")
            data["alerts"] = alerts_before
        
        output = {
            "alerts": data["alerts"],
            "processed_ids": sorted(list(data["processed_ids"]))
        }
        
        with open(ALERTS_FILE, "w", encoding='utf-8') as f:
            json.dump(output, f, indent=2, ensure_ascii=False)
    except Exception as e:
        import traceback
        traceback.print_exc()
        # 不要静默失败，至少打印错误
        print(f"[ERROR] save_processed_posts failed: {e}")

def save_alert(post, keywords, ai_analysis=None, source=None, downloaded_media_paths=None):
    """
    Saves an alert to a JSON file for the dashboard to read.
    
    Args:
        post: 推文数据
        keywords: 关键词列表
        ai_analysis: AI分析结果
        source: 数据来源
        downloaded_media_paths: 已下载的媒体文件路径列表（用于建立推文ID映射）
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
    
    # 转换媒体URL为本地API路径（在保存前转换，避免每次加载都重新转换）
    # 这样可以降低访问 Truth Social 的频率，因为URL已经保存在文件中
    if media:
        try:
            # 检查每个媒体项，如果本地文件存在，转换为本地路径
            converted_media = []
            for m in media:
                new_m = m.copy() if isinstance(m, dict) else dict(m)
                original_url = m.get('url') or m.get('preview_url') or ''
                media_type = (m.get('type') or '').lower()
                
                # 判断是否为视频（多种方式检测）
                is_video = (
                    media_type in ('video', 'gifv') or
                    (original_url and any(ext in original_url.lower() for ext in ['.mp4', '.webm', '.mov', '.avi', '.mkv', '/videos/']))
                )
                
                # 如果URL是远程URL，尝试查找本地文件
                if original_url and original_url.startswith(('http://', 'https://')):
                    # 确定保存目录
                    save_dir = VIDEOS_DIR if is_video else IMAGES_DIR
                    # 生成文件名（与 dashboard.py 和 get_local_media_path 中的逻辑一致）
                    url_hash = hashlib.md5(original_url.encode('utf-8')).hexdigest()[:12]
                    
                    # 从URL提取扩展名
                    from urllib.parse import urlparse
                    parsed = urlparse(original_url)
                    path = parsed.path
                    if '.' in path:
                        ext = os.path.splitext(path)[1].lower()
                        if '?' in ext:
                            ext = ext.split('?')[0]
                    else:
                        ext = '.mp4' if is_video else '.jpg'
                    
                    # 确保扩展名正确
                    if is_video:
                        if ext not in ['.mp4', '.webm', '.mov', '.avi', '.mkv', '.gif']:
                            ext = '.mp4'
                    else:
                        if ext not in ['.jpg', '.jpeg', '.png', '.gif', '.webp']:
                            ext = '.jpg'
                    
                    filename = f"{url_hash}{ext}"
                    filepath = os.path.join(save_dir, filename)
                    
                    # 如果本地文件存在，转换为API路径
                    if os.path.exists(filepath):
                        rel_path = os.path.relpath(filepath, MEDIA_DIR)
                        api_path = '/api/media/' + rel_path.replace('\\', '/')
                        new_m['url'] = api_path
                        if 'preview_url' in new_m:
                            new_m['preview_url'] = api_path
                        new_m['original_url'] = original_url  # 保存原始URL用于调试
                    else:
                        # 文件不存在，保留原始URL（可能还未下载）
                        if is_video:
                            pass
                
                converted_media.append(new_m)
            
            media = converted_media
            converted_count = sum(1 for m in converted_media if m.get('url', '').startswith('/api/media/'))
        except Exception as e:
            import traceback
            traceback.print_exc()

    alert_data = {
        "id": post.get("id"),
        "created_at": created_iso,
        "content": _content,
        "url": post.get("url", "https://truthsocial.com/@realDonaldTrump"),
        "media": media,  # 使用转换后的媒体URL
        "keywords": keywords,
        "ai_analysis": ai_analysis,
        "detected_at": datetime.now(timezone.utc).isoformat(),
        "source": source or ("simulated" if str(post.get("id", "")).startswith("simulated") else "real"),
        "local_media_paths": downloaded_media_paths or []
    }

    container_alerts = []
    container_processed = set()
    if os.path.exists(ALERTS_FILE):
        try:
            with open(ALERTS_FILE, "r", encoding='utf-8') as f:
                content = f.read()
                # 尝试解析完整的JSON
                try:
                    existing = json.loads(content)
                except json.JSONDecodeError as json_err:
                    # 如果JSON解析失败，尝试修复文件
                    # 方法1: 尝试找到第一个完整的JSON对象
                    print(f"[save_alert] JSON解析错误: {json_err}, 尝试修复文件...")
                    # 尝试找到第一个有效的JSON对象结束位置
                    brace_count = 0
                    bracket_count = 0
                    in_string = False
                    escape_next = False
                    valid_end = -1
                    
                    for i, char in enumerate(content):
                        if escape_next:
                            escape_next = False
                            continue
                        if char == '\\':
                            escape_next = True
                            continue
                        if char == '"' and not escape_next:
                            in_string = not in_string
                            continue
                        if in_string:
                            continue
                        if char == '{':
                            brace_count += 1
                        elif char == '}':
                            brace_count -= 1
                            if brace_count == 0 and bracket_count == 0:
                                valid_end = i + 1
                                break
                        elif char == '[':
                            bracket_count += 1
                        elif char == ']':
                            bracket_count -= 1
                            if brace_count == 0 and bracket_count == 0:
                                valid_end = i + 1
                                break
                    
                    if valid_end > 0:
                        # 尝试解析截断后的内容
                        try:
                            existing = json.loads(content[:valid_end])
                            print(f"[save_alert] 成功修复JSON文件，使用前{valid_end}个字符")
                        except json.JSONDecodeError:
                            # 如果截断后仍然失败，创建备份并重置
                            backup_file = ALERTS_FILE + ".backup." + str(int(time.time()))
                            try:
                                import shutil
                                shutil.copy2(ALERTS_FILE, backup_file)
                                print(f"[save_alert] 创建备份文件: {backup_file}")
                            except Exception:
                                pass
                            existing = None
                    else:
                        # 无法修复，创建备份并重置
                        backup_file = ALERTS_FILE + ".backup." + str(int(time.time()))
                        try:
                            import shutil
                            shutil.copy2(ALERTS_FILE, backup_file)
                            print(f"[save_alert] 创建备份文件: {backup_file}")
                        except Exception:
                            pass
                        existing = None
                
                if existing is not None:
                    if isinstance(existing, dict):
                        container_alerts = existing.get("alerts") or []
                        container_processed = set(map(str, (existing.get("processed_ids") or [])))
                    elif isinstance(existing, list):
                        # 兼容旧格式（list）
                        container_alerts = existing
                        container_processed = set()
        except Exception as e:
            import traceback
            print(f"[save_alert] 读取文件时发生错误: {e}")
            traceback.print_exc()
            # 创建备份文件
            try:
                import shutil
                backup_file = ALERTS_FILE + ".backup." + str(int(time.time()))
                if os.path.exists(ALERTS_FILE):
                    shutil.copy2(ALERTS_FILE, backup_file)
                    print(f"[save_alert] 创建备份文件: {backup_file}")
            except Exception:
                pass
            container_alerts = []
    
    # 检查是否已存在相同ID的告警，避免重复
    alert_id = alert_data.get("id")
    if alert_id:
        alerts = container_alerts
        # 移除已存在的相同ID的告警，避免重复
        container_alerts = [a for a in container_alerts if str(a.get("id", "")) != str(alert_id)]
        alerts = container_alerts
    
    # Add new alert to the beginning
    container_alerts.insert(0, alert_data)
    
    # Keep only last N alerts if MAX_ALERTS is set
    if MAX_ALERTS:
        container_alerts = container_alerts[:int(MAX_ALERTS)]

    container_processed.add(str(alert_id or ""))
    out = {"alerts": container_alerts, "processed_ids": sorted(list(container_processed))}
    
    try:
        # 使用原子写入：先写入临时文件，然后重命名，避免写入过程中文件损坏
        temp_file = ALERTS_FILE + ".tmp." + str(int(time.time() * 1000000))
        try:
            with open(temp_file, "w", encoding='utf-8') as f:
                json.dump(out, f, indent=2, ensure_ascii=False)
                # 确保文件已完全写入磁盘
                if hasattr(f, 'fileno'):
                    try:
                        os.fsync(f.fileno())
                    except Exception:
                        pass  # 某些系统可能不支持fsync
            # 原子重命名（在Windows上可能需要特殊处理）
            try:
                # 在Windows上，如果目标文件存在，需要先删除
                if os.path.exists(ALERTS_FILE):
                    os.replace(temp_file, ALERTS_FILE)
                else:
                    os.rename(temp_file, ALERTS_FILE)
            except Exception as rename_err:
                # 如果重命名失败，尝试直接复制
                try:
                    import shutil
                    shutil.move(temp_file, ALERTS_FILE)
                except Exception:
                    # 如果移动也失败，尝试直接写入（非原子）
                    with open(ALERTS_FILE, "w", encoding='utf-8') as f:
                        json.dump(out, f, indent=2, ensure_ascii=False)
                    # 清理临时文件
                    try:
                        os.remove(temp_file)
                    except Exception:
                        pass
            print(f"Alert saved to {ALERTS_FILE} (ID: {alert_id})")
        except Exception as write_err:
            # 清理临时文件
            try:
                if os.path.exists(temp_file):
                    os.remove(temp_file)
            except Exception:
                pass
            raise write_err
    except Exception as e:
        import traceback
        print(f"[save_alert] 写入文件时发生错误: {e}")
        traceback.print_exc()
        raise  # 重新抛出异常，避免静默失败

def run_fetch_recent(limit=20, fast_init=False, force=False):
    run_t0 = time.time()
    print(f"[refresh] start limit={limit} fast_init={fast_init} force={force}")
    if not ENABLE_REMOTE_FETCH:
        print("[run_fetch_recent] Remote fetch disabled via ENABLE_REMOTE_FETCH flag.")
        return 0
    if not (TRUTH_COOKIE and TRUTH_ACCOUNT_ID and str(TRUTH_ACCOUNT_ID).isdigit()):
        print(f"[run_fetch_recent] Missing required environment variables:")
        print(f"  TRUTH_COOKIE: {'SET' if TRUTH_COOKIE else 'NOT SET'}")
        print(f"  TRUTH_ACCOUNT_ID: {TRUTH_ACCOUNT_ID if TRUTH_ACCOUNT_ID else 'NOT SET'}")
        print(f"  TRUTH_ACCOUNT_ID is digit: {str(TRUTH_ACCOUNT_ID).isdigit() if TRUTH_ACCOUNT_ID else False}")
        return 0

    t_fetch0 = time.time()
    items = fetch_truth_posts(
        TRUTH_ACCOUNT_ID,
        TRUTH_USERNAME,
        TRUTH_COOKIE,
        limit=limit,
        fast_init=fast_init,
    )
    print(f"CookieAPI fetched items: {len(items) if isinstance(items, list) else 0}; fetch_sec={time.time()-t_fetch0:.2f}")
    
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
        try:
            post_t0 = time.time()
            post_id = str(post.get("id") or "").strip()
            
            # 默认跳过已处理帖子；force=True 时允许重刷覆盖
            if (not force) and post_id and post_id in processed_ids:
                continue  # 跳过已处理的帖子
            
            media_atts = post.get("media_attachments", [])
            media = extract_media(media_atts)
            content = derive_content(post, media_atts)
            keywords = extract_keywords(content)
            
            print(f"Processing post {post_id}...")
            downloaded_paths = []
            media_for_ai = []
            t_media0 = time.time()
            if media_atts:
                for m in media_atts:
                    t = (m.get("type") or "").lower()
                    is_video = t in ("video", "gifv") or any(ext in (m.get("url") or "").lower() for ext in ['.mp4', '.webm', '.mov', '.avi', '.mkv', '/videos/'])
                    if is_video:
                        u = m.get("url") or ""
                        pv = m.get("preview_url") or ""
                        lp = download_media(u, media_type='video', timeout=30, max_size_mb=100)
                        if lp:
                            downloaded_paths.append(lp)
                            # 将原视频交给 AI；不要只把缩略图当成视频分析输入。
                            media_for_ai.append({"type": "video", "url": lp, "preview_url": pv})
                        continue
                    else:
                        u = m.get("preview_url") or m.get("url") or ""
                        lp = download_media(u, media_type='image', timeout=30, max_size_mb=10)
                        if lp:
                            downloaded_paths.append(lp)
                            media_for_ai.append({"type": "image", "url": lp, "preview_url": lp})
            media_sec = time.time()-t_media0
            t_ai0 = time.time()
            ai_result = analyze_with_ai(content, media=media_for_ai)
            ai_sec = time.time()-t_ai0
            print(f"AI analysis completed for post {post_id}; media_sec={media_sec:.2f} ai_sec={ai_sec:.2f}")
            
            created_iso = normalize_iso(post.get("created_at"))
            url = post.get("url") or "https://truthsocial.com/@realDonaldTrump"

            if force or alerts_empty or (post_id and post_id not in processed_ids):
                t_save0 = time.time()
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
                    source="real",
                    downloaded_media_paths=downloaded_paths  # 传递下载的媒体文件路径
                )
                if post_id:
                    processed_ids.add(post_id)
                new_posts_count += 1
                print(f"Saved alert for post {post_id}; save_sec={time.time()-t_save0:.2f}; post_total_sec={time.time()-post_t0:.2f}")
        except Exception as e:
            print(f"Error processing post {post.get('id', 'unknown')}: {e}; post_total_sec={time.time()-post_t0:.2f}")
            import traceback
            traceback.print_exc()
            # 继续处理下一个帖子，不要因为一个失败就停止
            continue

    save_processed_posts(processed_ids)
    print(f"CookieAPI wrote alerts: {new_posts_count}; total_sec={time.time()-run_t0:.2f}")
    return new_posts_count

 
