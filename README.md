# 🔵 Token Monitor

[![Python](https://img.shields.io/badge/Python-3.10%2B-blue)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.115%2B-009688)](https://fastapi.tiangolo.com/)
[![License](https://img.shields.io/badge/License-MIT-green)](LICENSE)

> **零侵入 · 零配置 · 纯只读**  
> 从 Claude Code 本地日志中解析 Token 用量，提供实时可视化仪表盘。  
> **不修改任何 Claude Code 配置文件，不对 API 请求做任何拦截。**

---

## ✨ 特性

| 特性 | 说明 |
|------|------|
| 🔒 **零侵入** | 只读 `~/.claude/projects/` 目录，绝不修改 Claude Code 配置 |
| 📊 **可视化面板** | Token 趋势、模型分布、项目用量、费用统计，四张图表一目了然 |
| 💰 **精确计价** | 支持多模型独立定价（DeepSeek / Anthropic / OpenAI），区分缓存命中/未命中 |
| 🔄 **实时扫描** | 每 5 秒增量扫描日志文件，新会话数据自动入库 |
| 📂 **多项目追踪** | 自动识别不同项目目录，分别统计 Token 消耗 |
| 🎨 **暗色主题** | 深色仪表盘，30 秒自动刷新，适配桌面/移动端 |
| 🪶 **极简依赖** | 仅需 `fastapi` + `uvicorn`，无数据库服务、无消息队列 |

---

## 📸 仪表盘预览

```
┌─────────────────────────────────────────────────────────┐
│  🔵 Token Monitor                         自动刷新 30s  │
├──────────┬──────────┬──────────┬──────────────────────────┤
│ 累计Token │ 累计费用  │ 今日费用  │   项目 / 会话             │
│  9.4M    │  ¥46.23  │  ¥8.32   │   13 个 / 18 个          │
├──────────┴──────────┴──────────┴──────────────────────────┤
│  📈 Token 趋势 (14天)          │  🥧 模型分布              │
│  ▓▓▓▓▓░░░  ▓▓▓▓░░░           │    ┌──────┐              │
│  ▓▓▓▓▓░░░  ▓▓▓▓▓░░  Prompt   │    │ V4   │ 87%          │
│  ▓▓▓▓▓▓▓░  ▓▓▓▓▓▓░  Compl.   │    │Flash │ 13%          │
│  ──────────────── 费用(¥)    │    └──────┘              │
├──────────────────────────────┼─────────────────────────────┤
│  📁 项目 Token 分布          │  ⏰ 每日用量 (7天)          │
│  project-a ▓▓▓▓▓▓▓▓▓▓       │  ▓▓  ▓▓▓  ▓▓▓▓            │
│  project-b ▓▓▓▓▓▓            │  ▓▓  ▓▓▓  ▓▓▓▓▓▓          │
│  project-c ▓▓▓               │  6/24 6/25 6/26 ...       │
├──────────────────────────────┴─────────────────────────────┤
│  📋 最近请求                                               │
│  时间  │ 项目 │ 模型 │ 输入 │ 输出 │ 总计 │ 缓存 │ 费用   │
│  刚刚   │ ...  │ v4-p │ 221  │ 729  │ 950  │ 129K │¥0.0076│
└─────────────────────────────────────────────────────────┘
```

---

## 🚀 快速开始

### 1. 安装

```bash
git clone https://github.com/your-username/token-monitor.git
cd token-monitor
pip install -r requirements.txt
```

### 2. 启动

```bash
python app.py
# 或
./start.sh
```

### 3. 打开浏览器

```
http://localhost:8765
```

**就这么简单。** 不需要设置环境变量，不需要改 Claude Code 配置。

---

## 🏗️ 工作原理

```
~/.claude/projects/
├── project-a/
│   ├── session-1.jsonl    ←─ Claude Code 自动写入
│   └── session-2.jsonl
├── project-b/
│   └── session-3.jsonl
        │
        │  后台扫描线程 (每 5s)
        ▼
┌──────────────┐
│  JSONL 解析器  │  提取 assistant 消息中的 usage 字段
└──────┬───────┘
       │
       ▼
┌──────────────┐
│   SQLite DB   │  本地存储，零运维
└──────┬───────┘
       │
       ▼
┌──────────────┐
│  FastAPI      │  REST API + 内嵌仪表盘 HTML
│  :8765        │
└──────────────┘
```

每条 Claude Code 会话日志中的 `assistant` 消息天然包含完整 Token 信息：

```json
{
  "type": "assistant",
  "message": {
    "model": "deepseek-v4-pro",
    "usage": {
      "input_tokens": 221,
      "output_tokens": 729,
      "cache_read_input_tokens": 129024,
      "cache_creation_input_tokens": 0
    }
  }
}
```

Token Monitor 只做一件事：**读取这些已有的数据，算费用，画图表。**

---

## 💰 定价配置

默认已内置以下模型定价（人民币 / 百万 Token）：

| 模型 | 输入 (未缓存) | 输入 (缓存命中) | 输出 |
|------|:----------:|:-----------:|:----:|
| **DeepSeek V4 Pro** | ¥3.00 | ¥0.025 | ¥6.00 |
| **DeepSeek V4 Flash** | ¥1.00 | ¥0.020 | ¥2.00 |
| Claude Sonnet 4.6 | $3.00 | $0.30 | $15.00 |
| Claude Opus 4.8 | $15.00 | $1.50 | $75.00 |
| GPT-4o | $2.50 | — | $10.00 |

> 在 `app.py` 的 `PRICING` 字典中修改或添加模型定价。

---

## 📡 API 端点

| 端点 | 说明 |
|------|------|
| `GET /` | 可视化仪表盘 (HTML) |
| `GET /api/summary` | 累计/今日汇总 |
| `GET /api/trend?days=14` | Token 趋势数据 |
| `GET /api/models` | 模型分布 |
| `GET /api/projects` | 项目用量排行 |
| `GET /api/hourly?days=7` | 每日用量 |
| `GET /api/requests?limit=50` | 最近请求明细 |
| `GET /health` | 健康检查 |

---

## 🛠️ 技术栈

| 层 | 技术 |
|----|------|
| Web 框架 | [FastAPI](https://fastapi.tiangolo.com/) |
| 数据库 | SQLite (WAL 模式) |
| 图表 | [ECharts 5](https://echarts.apache.org/) |
| 前端 | 原生 HTML/JS，零构建步骤 |
| 数据源 | Claude Code JSONL 会话日志 |

---

## ❓ FAQ

### 需要修改 Claude Code 设置吗？
**不需要。** Token Monitor 只读取 Claude Code 自动生成的会话日志。不影响任何功能。

### 支持哪些 AI 编程工具？
目前解析 `~/.claude/projects/` 下的 JSONL 日志。支持 Claude Code、Claude Desktop 等使用 Anthropic 协议的工具。

### 数据存在哪里？
`data/tokens.db` (SQLite)，完全本地，不会上传到任何地方。

### 如何修改扫描间隔？
修改 `app.py` 中的 `SCAN_INTERVAL` 变量（默认 5 秒）。

### 如何修改端口？
修改 `app.py` 中的 `PORT` 变量（默认 8765）。

### 如何添加新模型定价？
编辑 `app.py` 中的 `PRICING` 字典：

```python
PRICING = {
    "your-model-name": {
        "in": 5.0,           # 未缓存输入价格
        "out": 10.0,         # 输出价格
        "cache_read": 0.05,  # 缓存命中价格
        "cache_write": 5.0,  # 缓存写入价格
    },
}
```

---

## 📄 License

MIT © 2025
