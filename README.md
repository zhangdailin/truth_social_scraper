# Trump Truth Social Monitor

一个基于 Streamlit 的实时仪表盘，用于监控 Donald Trump 在 Truth Social 等平台的发帖，并用 AI 进行市场影响分析、生成交易建议与可视化展示。

## 功能特性
- 实时展示最新推文，统一样式的卡片视图（含影响标签与 AI 分析）
- 交易建议文本内自动识别股票代码并展示图表工具提示
- 最新推文年龄提示（分钟），支持降低抓取频率以节省资源
- 时间统一：抓取时统一保存为 UTC，前端全部按本地系统时区显示并标注 `UTC±offset`
- 去重：按标准化内容去重，避免 Recent Posts 与顶部卡片重复及列表内出现重复内容
- 可配置自动抓取间隔（5–120 分钟），无需重启页面
- 高影响提醒音效（仅首次未播放过的高影响警报）

## 快速开始（Windows）
1. 安装依赖
```
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```
2. 配置必要环境变量（至少需要其一）
```
# Cookie 抓取 Truth Social
$env:TRUTH_COOKIE="你的Truth Social Cookie"
$env:TRUTH_ACCOUNT_ID="107780257626128497"  # 默认 realDonaldTrump
$env:TRUTH_USERNAME="realDonaldTrump"

# SiliconFlow（用于 AI 分析 DeepSeek-V3）
$env:SILICONFLOW_API_KEY="你的SiliconFlow密钥"
```
3. 运行仪表盘
```
streamlit run dashboard.py
```

## 使用说明
- 页面右上角控制区：
  - `Auto-refresh (sec)`: 页面自动刷新间隔（秒）
  - `Fetch interval (min)`: 抓取最新数据的间隔（分钟），默认 30 分钟
- 顶部卡片：展示最新推文（统一风格）
- Recent Posts：展示最近的若干推文，已与顶部卡片去重
- 底部档案区：以表格展开更旧的推文记录

## 数据来源与抓取逻辑
- Truth Social Cookie API：
  - 使用 `monitor_trump.py` 中的 `fetch_truth_posts` 直接访问 Truth Social 公开接口
  - 需提供有效的 `TRUTH_COOKIE`，账号 ID/用户名可保持默认 `realDonaldTrump`
- 首次加载与定时抓取：
  - 页面初次无数据时尝试拉取最近数据 `run_fetch_recent`
  - 后续按 `Fetch interval (min)` 定时调用 `run_one_check`

## 时间与时区规则
- 保存：所有抓取到的时间统一转换并保存为 UTC ISO 字符串
- 显示：所有页面时间使用本地系统时区，并在旁标注 `UTC±offset`
- 关键代码：
  - 保存归一化：`monitor_trump.py:288-303`
  - 排序与展示：`dashboard.py:212-243`, `dashboard.py:392-397`, `dashboard.py:433-438`

## 去重规则
- 标准化内容（移除 URL、压缩空白、统一小写）后去重，仅保留最新一条
- 关键代码：`dashboard.py:212-243` 的 `load_alerts()` 返回去重后的列表

## 文件结构（关键）
- `dashboard.py`: Streamlit 仪表盘主界面与渲染逻辑
- `monitor_trump.py`: 数据抓取、AI 分析与告警写入
- `requirements.txt`: 依赖列表（Streamlit、Pandas、OpenAI）
- `market_alerts.json`: 告警数据文件
- `processed_posts.json`: 已处理的帖子 ID 集合

注意：`market_alerts.json` 与 `processed_posts.json` 现已写到项目当前目录（`truth_social_scraper`）。如需变更保存位置，可修改 `monitor_trump.py` 与 `dashboard.py` 中的 `PROJECT_ROOT`。

## 常见问题
- “Data age XXX min” 提示：表示最新推文本身距离当前的分钟数，并非抓取延迟。
- 没有新数据：
  - 检查 `TRUTH_COOKIE` 是否有效
  - 网络环境是否可达数据源接口
  - `Fetch interval (min)` 是否配置过大

## 安全与隐私
- 不会在日志中打印或存储你的密钥；请通过环境变量提供令牌与密钥
- 请勿将密钥直接硬编码在代码中或提交到版本库

## 许可
- 本项目仅用于学习与研究用途；请遵守数据源使用条款与相关法律法规
