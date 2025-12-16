# Truth Social Scraper - 特朗普 Truth Social 监控系统

一个实时监控和分析特朗普 Truth Social 帖子的系统，使用 AI 技术评估帖子对金融市场的影响。

## 功能特性

- 🔍 **实时监控**: 自动抓取特朗普在 Truth Social 上的最新帖子
- 🤖 **AI 分析**: 使用 DeepSeek-V3 模型分析帖子对市场的影响
- 📊 **可视化仪表板**: Streamlit 构建的现代化 Web 界面
- 🖼️ **媒体处理**: 自动下载和管理帖子中的图片和视频
- 🔗 **REST API**: 提供完整的 REST API 接口供外部系统调用
- 📈 **市场影响评估**: 自动识别受影响的股票和资产
- 🌐 **外部上下文**: 自动获取相关新闻和市场信息

## 系统架构

```
truth_social_scraper/
├── api.py              # FastAPI REST API 服务器
├── dashboard.py        # Streamlit Web 仪表板
├── monitor_trump.py    # 核心监控和 AI 分析逻辑
├── utils.py            # 工具函数和配置
├── test_caption_cloud.py  # 测试脚本
├── market_alerts.json  # 告警数据存储
└── media/              # 媒体文件存储目录
    ├── images/         # 图片文件
    └── videos/         # 视频文件
```

## 安装步骤

### 1. 克隆仓库

```bash
git clone <repository-url>
cd truth_social_scraper
```

### 2. 安装依赖

```bash
pip install -r requirements.txt
```

### 3. 配置环境变量

创建 `.env` 文件并配置以下变量：

```env
# Truth Social API 配置
TRUTH_COOKIE=your_truth_social_cookie
TRUTH_ACCOUNT_ID=107780257626128497
TRUTH_USERNAME=realDonaldTrump

# AI API 配置
SILICONFLOW_API_KEY=your_siliconflow_api_key
HUGGINGFACE_API_KEY=your_huggingface_api_key

# 代理配置（可选）
SOCKS_PROXY=127.0.0.1:7890

# 功能开关（可选）
ENABLE_AI_ANALYSIS=true
ENABLE_REMOTE_FETCH=true
HF_LOCAL_FALLBACK=0

# API 服务器配置（可选）
API_BASE_URL=http://localhost:8000
```

### 4. 获取 Truth Social Cookie

1. 登录 Truth Social 网站
2. 打开浏览器开发者工具（F12）
3. 在 Network 标签中找到任意 API 请求
4. 复制请求头中的 `Cookie` 值

## 使用方法

### 启动 Web 仪表板

```bash
streamlit run dashboard.py
```

访问 `http://localhost:8501` 查看仪表板。

### 启动 API 服务器

```bash
python api.py
```

或使用 uvicorn：

```bash
uvicorn api:app --host 0.0.0.0 --port 8000
```

API 文档可在 `http://localhost:8000/docs` 查看。

### 手动运行监控脚本

```bash
python monitor_trump.py
```

## API 端点

### 告警数据

- `GET /api/alerts` - 获取告警列表（支持分页和过滤）
- `GET /api/alerts/latest` - 获取最新告警
- `GET /api/alerts/{id}` - 根据ID获取特定告警
- `GET /api/alerts/{id}/media-summary` - 获取告警的媒体分析结果

### 统计信息

- `GET /api/stats` - 获取统计信息

### 媒体文件

- `GET /api/media/{file_path}` - 获取媒体文件（图片/视频）

### 图片搜索

- `GET /api/image-search/google` - Google 图片搜索
- `GET /api/image-search/bing` - Bing 图片搜索
- `GET /api/image-search/result` - 获取图片搜索结果

### 其他

- `GET /health` - 健康检查
- `GET /api/debug` - 调试信息
- `GET /api/export/dashboard` - 导出仪表板数据

## 数据格式

### 告警数据结构

```json
{
  "id": "post_id",
  "created_at": "2025-01-01T00:00:00+00:00",
  "content": "帖子内容",
  "url": "https://truthsocial.com/@realDonaldTrump/posts/...",
  "media": [
    {
      "url": "/api/media/images/xxx.jpg",
      "preview_url": "/api/media/images/xxx.jpg",
      "type": "image"
    }
  ],
  "keywords": "关键词",
  "ai_analysis": {
    "impact": true,
    "reasoning": "分析原因",
    "recommendation": "Buy TSLA",
    "sentiment": "positive",
    "affected_assets": ["TSLA", "DJT"],
    "external_context_used": "外部上下文",
    "media_used": true,
    "media_caption_used": true
  },
  "detected_at": "2025-01-01T00:00:00+00:00",
  "source": "real"
}
```

## 配置说明

### 环境变量详解

| 变量名 | 必需 | 说明 |
|--------|------|------|
| `TRUTH_COOKIE` | 是 | Truth Social 登录 Cookie |
| `TRUTH_ACCOUNT_ID` | 是 | Truth Social 账户ID |
| `SILICONFLOW_API_KEY` | 是 | SiliconFlow API 密钥（用于 DeepSeek-V3） |
| `HUGGINGFACE_API_KEY` | 否 | HuggingFace API 密钥（用于图片描述） |
| `SOCKS_PROXY` | 否 | SOCKS5 代理地址（格式：`127.0.0.1:7890`） |
| `ENABLE_AI_ANALYSIS` | 否 | 是否启用 AI 分析（默认：true） |
| `ENABLE_REMOTE_FETCH` | 否 | 是否启用远程抓取（默认：true） |

## 功能特性详解

### AI 分析

系统使用 DeepSeek-V3 模型分析帖子内容，评估：
- 市场影响程度（高/低）
- 受影响资产（股票代码）
- 情感分析（正面/负面/中性）
- 交易建议

### 媒体处理

- 自动下载帖子中的图片和视频
- 使用 HuggingFace API 生成图片描述
- 支持视频关键帧提取和分析
- 本地文件缓存，避免重复下载

### 外部上下文

- 自动搜索相关新闻和市场信息
- 整合外部数据增强分析准确性

## 故障排除

### 常见问题

1. **Cookie 失效**
   - 重新登录 Truth Social 并更新 Cookie

2. **API 密钥错误**
   - 检查 `.env` 文件中的 API 密钥是否正确

3. **媒体下载失败**
   - 检查网络连接和代理配置
   - 查看日志了解具体错误信息

4. **端口被占用**
   - 修改 `dashboard.py` 中的 `API_PORT` 或 `api.py` 中的端口配置

## 开发说明

### 项目结构

- `api.py`: FastAPI REST API 实现
- `dashboard.py`: Streamlit Web 界面
- `monitor_trump.py`: 核心监控逻辑和 AI 分析
- `utils.py`: 工具函数、配置和辅助功能

### 扩展开发

- 添加新的 AI 模型：修改 `monitor_trump.py` 中的 `analyze_with_ai` 函数
- 自定义仪表板：编辑 `dashboard.py`
- 添加新的 API 端点：在 `api.py` 中添加路由

## 许可证

本项目仅供学习和研究使用。

## 免责声明

本工具仅用于技术研究和学习目的。使用本工具时请遵守 Truth Social 的服务条款和相关法律法规。

## 贡献

欢迎提交 Issue 和 Pull Request！

## 更新日志

### v1.0.0
- 初始版本发布
- 支持 Truth Social 帖子监控
- AI 市场影响分析
- Web 仪表板和 REST API

