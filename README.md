# Truth Social Monitor / Scraper

一个用于监控 Truth Social 帖子并进行 AI 分析的轻量系统，包含可视化仪表盘与 REST API。支持媒体下载与本地映射展示、图片字幕生成、外部上下文摘要，以及自动刷新与告警归档。

## 主要功能
- 仪表盘可视化：展示最新与近期帖子、AI 分析、媒体画廊
- 媒体处理：下载并通过 `/api/media/...` 提供图片/视频，支持点击弹窗放大查看
- AI 分析：
  - 文本侧：DeepSeek-V3（SiliconFlow）进行影响与情绪分析
  - 图片侧：Hugging Face 图像字幕（`InferenceClient.image_to_text`）
  - 外部上下文：简单网页检索关键词摘要
- REST API：告警数据、统计、媒体文件、健康检查与统一导出 JSON
- 自动刷新与定时拉取：可配置分钟级刷新周期，后台定时拉取数据并更新缓存

## 环境要求
- Python 3.10+（建议）
- pip 与虚拟环境（可选）
- 具备网络访问（如使用代理，需配置 `SOCKS_PROXY`）

## 安装
```bash
pip install -r requirements.txt
```

如果使用系统服务，请确保服务使用的 Python 环境已安装上述依赖。

## 环境变量
- `TRUTH_COOKIE`：Truth Social 的 Cookie（字符串）
- `TRUTH_ACCOUNT_ID`：账号 ID（默认：`107780257626128497`）
- `TRUTH_USERNAME`：用户名（默认：`realDonaldTrump`）
- `SILICONFLOW_API_KEY`：用于调用 DeepSeek-V3 的 API Key
- `HUGGINGFACE_API_KEY`：用于调用 Hugging Face Inference 的 Token
- `HUGGINGFACE_IMAGE_MODEL`：图片字幕模型（默认可用：`Salesforce/blip-image-captioning-large` 或 `nlpconnect/vit-gpt2-image-captioning`）
- `SOCKS_PROXY`：socks5 代理（例如：`127.0.0.1:7890` 或 `socks5://127.0.0.1:7890`）

建议将以上变量注入到 systemd 服务单位文件的 `[Service] Environment=...` 中。

## 运行仪表盘
```bash
streamlit run dashboard.py
```
运行后打开输出的 URL（默认 `http://0.0.0.0:8501`）。右上角可以设置 Auto-refresh（分钟），系统会按该周期自动刷新并后台拉取数据。

## 运行 API
开发模式：
```bash
uvicorn api:app --host 0.0.0.0 --port 8000
```

常用端点：
- `GET /`：基本信息
- `GET /api/alerts`：告警列表（分页与过滤）
- `GET /api/alerts/latest`：最新告警
- `GET /api/alerts/{id}`：指定告警
- `GET /api/stats`：统计信息
- `GET /api/media/{file_path}`：媒体文件（图片/视频）
- `GET /api/health`：健康检查（文件路径与工作目录）
- `GET /api/health/hf`：Hugging Face 健康检查（是否安装、Key 与示例字幕）
- `GET /api/export/dashboard`：统一仪表盘数据 JSON

## systemd 服务示例（Linux）
仪表盘（Streamlit）：
```ini
[Unit]
Description=Truth Social Monitor Dashboard
After=network-online.target

[Service]
Type=simple
WorkingDirectory=/root/truth_social_scraper
ExecStart=/usr/bin/python3 -m streamlit run /root/truth_social_scraper/dashboard.py --server.port 8501 --server.address 0.0.0.0
Restart=always
Environment=TRUTH_COOKIE=your_cookie
Environment=TRUTH_ACCOUNT_ID=107780257626128497
Environment=TRUTH_USERNAME=realDonaldTrump
Environment=SILICONFLOW_API_KEY=your_siliconflow_key
Environment=HUGGINGFACE_API_KEY=your_hf_token
Environment=HUGGINGFACE_IMAGE_MODEL=Salesforce/blip-image-captioning-large
Environment=SOCKS_PROXY=127.0.0.1:7890

[Install]
WantedBy=multi-user.target
```

API（FastAPI）：
```ini
[Unit]
Description=Truth Social Monitor API
After=network-online.target

[Service]
Type=simple
WorkingDirectory=/root/truth_social_scraper
ExecStart=/usr/bin/python3 -m uvicorn api:app --host 0.0.0.0 --port 8000
Restart=always
Environment=TRUTH_COOKIE=your_cookie
Environment=TRUTH_ACCOUNT_ID=107780257626128497
Environment=TRUTH_USERNAME=realDonaldTrump
Environment=SILICONFLOW_API_KEY=your_siliconflow_key
Environment=HUGGINGFACE_API_KEY=your_hf_token
Environment=HUGGINGFACE_IMAGE_MODEL=Salesforce/blip-image-captioning-large
Environment=SOCKS_PROXY=127.0.0.1:7890

[Install]
WantedBy=multi-user.target
```

## 目录结构
- `dashboard.py`：Streamlit 仪表盘。包含媒体网格、弹窗放大、自动刷新与后台定时拉取
- `api.py`：FastAPI 服务。媒体文件路由、告警与统计端点、健康检查与导出
- `monitor_trump.py`：抓取与分析逻辑。媒体下载映射、AI 图片字幕、文本分析与外部上下文
- `utils.py`：工具函数与常量（路径、代理设置、媒体映射等）
- `media/`：下载的图片与视频（`images/`、`videos/`）
- `processed_posts.json`、`market_alerts.json`：数据文件

## 媒体展示与弹窗
- 仪表盘卡片与媒体画廊点击图片/视频会打开弹窗放大查看
- 本地媒体通过 `GET /api/media/{relative_path}` 提供；UI 中会自动将 `/api/media/...` 补全为完整主机路径
- 如果是远程 URL（非本地映射），也允许直接展示；若跨域受限或链接失效，建议通过映射转换为本地路径

## 常见问题排查
- 端口占用：`[Errno 98] address already in use`
  - 确保仅运行一个 API 实例（`8000`）或修改端口
  - 使用 `systemctl stop` 旧服务，再 `restart`
- Hugging Face 字幕为 `sample_caption_present=false`
  - 确认已安装 `huggingface_hub`，且服务进程读取到了 `HUGGINGFACE_API_KEY`
  - 通过 `GET /api/health/hf` 查看 `hub_installed` 与错误信息
- 模块未找到：`ModuleNotFoundError: huggingface_hub`
  - 安装依赖到服务使用的解释器：`pip3 install huggingface_hub`
  - 重启 `systemctl` 服务
- 媒体不显示或下载失败
  - 检查 `/api/media/{file_path}` 路径是否存在、文件是否在 `media/` 下
  - 非本地的远程 URL 可能会被跳过或浏览器拦截，建议通过映射转换成本地路径
- 弹窗不出现
  - 目前已统一使用 JS 弹窗方法；若仍不出现，刷新页面或检查浏览器扩展干扰

## 安全注意
- 不要将密钥（`TRUTH_COOKIE`、`SILICONFLOW_API_KEY`、`HUGGINGFACE_API_KEY`）提交到仓库
- 生产环境建议限制 `CORS` 的 `allow_origins`

## 开发与贡献
- 欢迎提交 Issue 或 PR，建议先描述问题与场景
- 对 UI 交互（弹窗、媒体化）有建议可直接提出，帮助提升多媒体浏览体验

