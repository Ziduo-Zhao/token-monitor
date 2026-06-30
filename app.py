"""
Token Monitor — 从 Claude Code 本地日志解析 token 用量 + 可视化仪表盘
======================================================================
启动: python app.py
仪表盘: http://localhost:8765
数据源: ~/.claude/projects/**/*.jsonl (只读，不修改任何配置)
"""

import hashlib
import json
import os
import sqlite3
import threading
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import HTMLResponse

# ============================================================================
# 配置
# ============================================================================
DATA_DIR = Path(__file__).parent / "data"
DATA_DIR.mkdir(exist_ok=True)
DB_PATH = DATA_DIR / "tokens.db"
LOG_ROOT = Path.home() / ".claude" / "projects"
PORT = 8765
SCAN_INTERVAL = 5  # 扫描间隔 (秒)
USD_TO_CNY = 1.0  # 定价已为人民币, 无需转换

DB_LOCK = threading.Lock()

# ============================================================================
# 定价表 (人民币 / 1M tokens)
# ============================================================================
PRICING = {
    # Anthropic
    "claude-opus-4-8-20250514":    {"in": 15.0, "out": 75.0, "cache_read": 1.50, "cache_write": 18.75},
    "claude-opus-4-5-20251101":    {"in": 15.0, "out": 75.0, "cache_read": 1.50, "cache_write": 18.75},
    "claude-sonnet-4-6-20250514":  {"in": 3.0,  "out": 15.0, "cache_read": 0.30, "cache_write": 3.75},
    "claude-sonnet-4-5-20250902":  {"in": 3.0,  "out": 15.0, "cache_read": 0.30, "cache_write": 3.75},
    "claude-haiku-4-5-20251001":   {"in": 1.0,  "out": 5.0,  "cache_read": 0.10, "cache_write": 1.25},
    "claude-fable-5-20250902":     {"in": 3.0,  "out": 15.0, "cache_read": 0.30, "cache_write": 3.75},
    "claude-3-5-sonnet-20241022":  {"in": 3.0,  "out": 15.0, "cache_read": 0.30, "cache_write": 3.75},
    "claude-3-5-haiku-20241022":   {"in": 0.8,  "out": 4.0,  "cache_read": 0.08, "cache_write": 1.0},
    # DeepSeek (人民币 / 1M tokens)
    "deepseek-v4-pro":   {"in": 3.0,  "out": 6.0,  "cache_read": 0.025, "cache_write": 3.0},
    "deepseek-v4-flash": {"in": 1.0,  "out": 2.0,  "cache_read": 0.02,  "cache_write": 1.0},
    "deepseek-v3":       {"in": 0.27, "out": 1.10, "cache_read": 0.07,  "cache_write": 0.27},
    "deepseek-r1":       {"in": 0.55, "out": 2.19, "cache_read": 0.14,  "cache_write": 0.55},
}
DEFAULT_PRICE = {"in": 3.0, "out": 15.0, "cache_read": 0.30, "cache_write": 3.75}

# OpenAI (备用)
OPENAI_PRICE = {
    "gpt-5": {"in": 2.5, "out": 10.0}, "gpt-5-mini": {"in": 0.5, "out": 2.0},
    "gpt-4.1": {"in": 2.0, "out": 8.0}, "gpt-4.1-mini": {"in": 0.4, "out": 1.6},
    "gpt-4o": {"in": 2.5, "out": 10.0}, "gpt-4o-mini": {"in": 0.15, "out": 0.6},
    "o4-mini": {"in": 1.1, "out": 4.4}, "o3": {"in": 10.0, "out": 40.0},
    "o3-mini": {"in": 1.1, "out": 4.4},
}


def calc_cost(model: str, prompt: int, completion: int,
              cache_read: int = 0, cache_write: int = 0) -> float:
    """计算单次请求费用 (USD)

    定价模型:
    - input_tokens 包含全部输入 token（缓存命中 + 未命中）
    - cache_read_input_tokens 是其中命中缓存的部分
    - 未缓存输入 = input_tokens - cache_read_input_tokens → 按 "in" 价格
    - 缓存输入 = cache_read_input_tokens → 按 "cache_read" 价格
    - 输出 = output_tokens → 按 "out" 价格
    - 缓存写入 = cache_creation_input_tokens → 按 "cache_write" 价格
    """
    p = PRICING.get(model)
    if p is None:
        for k in PRICING:
            if model.startswith(k):
                p = PRICING[k]
                break
    if p is None:
        p = DEFAULT_PRICE

    # 未缓存输入 = 总输入 - 缓存命中 (避免负数)
    uncached_prompt = max(0, prompt - cache_read)

    cost = (uncached_prompt / 1_000_000) * p["in"] \
         + (completion / 1_000_000) * p["out"] \
         + (cache_read / 1_000_000) * p.get("cache_read", p["in"] * 0.1) \
         + (cache_write / 1_000_000) * p.get("cache_write", p["in"] * 1.25)
    return round(cost, 8)


# ============================================================================
# 数据库
# ============================================================================
def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=3000")
    return conn


def init_db():
    with DB_LOCK:
        conn = get_db()
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS requests (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at  TEXT NOT NULL,
                project     TEXT NOT NULL DEFAULT '',
                session_id  TEXT NOT NULL DEFAULT '',
                model       TEXT NOT NULL,
                prompt_tokens       INTEGER DEFAULT 0,
                completion_tokens   INTEGER DEFAULT 0,
                total_tokens        INTEGER DEFAULT 0,
                cache_read_tokens   INTEGER DEFAULT 0,
                cache_write_tokens  INTEGER DEFAULT 0,
                cost_usd    REAL DEFAULT 0.0,
                message_id  TEXT DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS idx_created ON requests(created_at);
            CREATE INDEX IF NOT EXISTS idx_model   ON requests(model);
            CREATE INDEX IF NOT EXISTS idx_project ON requests(project);
            CREATE INDEX IF NOT EXISTS idx_session ON requests(session_id);

            CREATE TABLE IF NOT EXISTS processed_files (
                file_path   TEXT PRIMARY KEY,
                last_offset INTEGER DEFAULT 0,
                last_mtime  REAL DEFAULT 0,
                updated_at  TEXT NOT NULL
            );
        """)
        conn.commit()
        conn.close()


def save_request(data: dict):
    with DB_LOCK:
        conn = get_db()
        conn.execute("""
            INSERT OR IGNORE INTO requests
                (created_at, project, session_id, model,
                 prompt_tokens, completion_tokens, total_tokens,
                 cache_read_tokens, cache_write_tokens,
                 cost_usd, message_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            data["created_at"], data["project"], data["session_id"],
            data["model"], data["prompt_tokens"], data["completion_tokens"],
            data["total_tokens"], data.get("cache_read_tokens", 0),
            data.get("cache_write_tokens", 0), data["cost_usd"],
            data["message_id"],
        ))
        conn.commit()
        conn.close()


def query_db(sql: str, params=()):
    conn = get_db()
    cur = conn.execute(sql, params)
    rows = cur.fetchall()
    conn.close()
    return rows


# ============================================================================
# 日志扫描器
# ============================================================================
def list_log_files():
    """列出所有项目目录下的 JSONL 日志文件"""
    files = []
    if not LOG_ROOT.exists():
        return files
    for entry in LOG_ROOT.iterdir():
        if entry.is_dir():
            for f in entry.glob("*.jsonl"):
                files.append(f)
    return files


def process_log_file(filepath: Path):
    """处理单个日志文件，从上次位置继续"""
    fpath_str = str(filepath)
    mtime = filepath.stat().st_mtime

    with DB_LOCK:
        conn = get_db()
        row = conn.execute(
            "SELECT last_offset, last_mtime FROM processed_files WHERE file_path=?",
            (fpath_str,)
        ).fetchone()
        conn.close()

    last_offset = row[0] if row else 0
    last_mtime = row[1] if row else 0

    # 文件未修改 → 跳过
    if row and mtime == last_mtime:
        return 0

    new_lines = 0
    try:
        with open(filepath, "r", encoding="utf-8", errors="replace") as f:
            if last_offset > 0:
                f.seek(last_offset)
            elif mtime != last_mtime and row:
                # 文件被修改过，重新扫描
                f.seek(0)
                last_offset = 0

            for line in f:
                new_lines += process_line(line.strip(), filepath)
            current_offset = f.tell()
    except (OSError, IOError):
        return 0

    # 更新位置
    with DB_LOCK:
        conn = get_db()
        conn.execute("""
            INSERT OR REPLACE INTO processed_files (file_path, last_offset, last_mtime, updated_at)
            VALUES (?, ?, ?, ?)
        """, (fpath_str, current_offset, mtime, datetime.now(timezone.utc).isoformat()))
        conn.commit()
        conn.close()

    return new_lines


def process_line(line: str, filepath: Path) -> int:
    """解析一行 JSONL，如果是 assistant 消息则提取 token 数据"""
    if not line:
        return 0
    try:
        obj = json.loads(line)
    except json.JSONDecodeError:
        return 0

    if obj.get("type") != "assistant":
        return 0

    msg = obj.get("message", {})
    if not msg:
        return 0

    usage = msg.get("usage", {})
    if not usage:
        return 0

    model = msg.get("model", "unknown")
    input_tokens = usage.get("input_tokens", 0)
    output_tokens = usage.get("output_tokens", 0)
    cache_read = usage.get("cache_read_input_tokens", 0)
    cache_write = usage.get("cache_creation_input_tokens", 0)
    total = input_tokens + output_tokens

    if total == 0:
        return 0

    cost = calc_cost(model, input_tokens, output_tokens, cache_read, cache_write)

    # 从文件路径推导项目名
    try:
        project = filepath.parent.name
    except Exception:
        project = ""

    session_id = obj.get("sessionId", "")
    timestamp = obj.get("timestamp", datetime.now(timezone.utc).isoformat())
    message_id = obj.get("uuid", "")

    save_request({
        "created_at": timestamp,
        "project": project,
        "session_id": session_id,
        "model": model,
        "prompt_tokens": input_tokens,
        "completion_tokens": output_tokens,
        "total_tokens": total,
        "cache_read_tokens": cache_read,
        "cache_write_tokens": cache_write,
        "cost_usd": cost,
        "message_id": message_id,
    })
    return 1


def scanner_loop():
    """后台扫描线程"""
    while True:
        try:
            files = list_log_files()
            total = 0
            for fp in files:
                total += process_log_file(fp)
            if total > 0:
                print(f"[Scanner] 新增 {total} 条记录 (共 {len(files)} 个日志文件)")
        except Exception as e:
            print(f"[Scanner] 错误: {e}")
        time.sleep(SCAN_INTERVAL)


# ============================================================================
# FastAPI 应用
# ============================================================================
@asynccontextmanager
async def lifespan(application: FastAPI):
    init_db()

    def initial_scan():
        files = list_log_files()
        total = 0
        for fp in files:
            total += process_log_file(fp)
        print(f"[Init] 首次扫描完成: {total} 条新记录, {len(files)} 个日志文件")

    # 后台线程：先做首次全量扫描，之后定期增量
    def background():
        initial_scan()
        scanner_loop()

    t = threading.Thread(target=background, daemon=True)
    t.start()
    yield


app = FastAPI(title="Token Monitor", version="2.0.0", lifespan=lifespan)


# ============================================================================
# 仪表盘 HTML
# ============================================================================
DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Token Monitor Dashboard</title>
<script src="https://cdn.jsdelivr.net/npm/echarts@5.6.0/dist/echarts.min.js"></script>
<style>
  :root {
    --bg: #0f172a; --card: #1e293b; --border: #334155;
    --text: #e2e8f0; --muted: #94a3b8; --accent: #38bdf8;
    --green: #4ade80; --amber: #fbbf24; --red: #f87171;
  }
  * { margin:0; padding:0; box-sizing:border-box; }
  body { background:var(--bg); color:var(--text); font-family:'Inter',system-ui,sans-serif; min-height:100vh; }
  .container { max-width:1400px; margin:0 auto; padding:24px; }
  h1 { font-size:1.5rem; font-weight:700; margin-bottom:24px; display:flex; align-items:center; gap:10px; }
  h1 .dot { width:10px; height:10px; background:var(--green); border-radius:50%; animation:pulse 2s infinite; }
  @keyframes pulse { 0%,100%{opacity:1} 50%{opacity:.3} }

  .stat-grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(220px,1fr)); gap:16px; margin-bottom:24px; }
  .stat-card { background:var(--card); border:1px solid var(--border); border-radius:12px; padding:20px; }
  .stat-card .label { font-size:0.8rem; color:var(--muted); margin-bottom:6px; text-transform:uppercase; letter-spacing:.05em; }
  .stat-card .value { font-size:2rem; font-weight:700; }
  .stat-card .sub { font-size:0.75rem; color:var(--muted); margin-top:4px; }

  .chart-row { display:grid; grid-template-columns:2fr 1fr; gap:16px; margin-bottom:24px; }
  @media(max-width:900px){ .chart-row{grid-template-columns:1fr;} }
  .chart-box { background:var(--card); border:1px solid var(--border); border-radius:12px; padding:16px; }
  .chart-box h3 { font-size:0.85rem; color:var(--muted); margin-bottom:12px; text-transform:uppercase; letter-spacing:.05em; }
  .chart-inner { width:100%; height:320px; }

  .table-box { background:var(--card); border:1px solid var(--border); border-radius:12px; overflow:hidden; margin-bottom:24px; }
  .table-box h3 { font-size:0.85rem; color:var(--muted); padding:16px 16px 8px; text-transform:uppercase; letter-spacing:.05em; }
  table { width:100%; border-collapse:collapse; font-size:0.82rem; }
  th { text-align:left; padding:10px 16px; color:var(--muted); font-weight:500; border-bottom:1px solid var(--border); }
  td { padding:10px 16px; border-bottom:1px solid rgba(51,65,85,0.5); }
  tr:last-child td { border-bottom:none; }
  .badge { display:inline-block; padding:2px 8px; border-radius:6px; font-size:0.72rem; font-weight:600; }
  .badge-ant { background:rgba(56,189,248,0.15); color:var(--accent); }
  .badge-oai { background:rgba(74,222,128,0.15); color:var(--green); }
  .badge-ds { background:rgba(167,139,250,0.15); color:#a78bfa; }
  .cost { font-variant-numeric:tabular-nums; color:var(--amber); }
  .empty-state { text-align:center; padding:48px 24px; color:var(--muted); }
  .empty-state .icon { font-size:3rem; margin-bottom:12px; }
  .refresh { font-size:0.7rem; color:var(--muted); margin-left:auto; }
  .data-source { font-size:0.7rem; color:var(--muted); margin-top:24px; text-align:center; }
</style>
</head>
<body>
<div class="container">
  <h1>🔵 Token Monitor <span class="dot"></span><span class="refresh" id="refreshLabel">自动刷新 30s</span></h1>

  <!-- 统计卡片 -->
  <div class="stat-grid">
    <div class="stat-card">
      <div class="label">累计 Token</div>
      <div class="value" id="totalTokens">—</div>
      <div class="sub" id="totalRequests">—</div>
    </div>
    <div class="stat-card">
      <div class="label">累计费用 (¥)</div>
      <div class="value cost" id="totalCost">—</div>
      <div class="sub" id="totalCostUSD">—</div>
    </div>
    <div class="stat-card">
      <div class="label">今日费用 (¥)</div>
      <div class="value cost" id="todayCost">—</div>
      <div class="sub" id="todayTokens">—</div>
    </div>
    <div class="stat-card">
      <div class="label">项目 / 会话</div>
      <div class="value" id="projectCount">—</div>
      <div class="sub" id="sessionCount">—</div>
    </div>
  </div>

  <!-- 图表行 -->
  <div class="chart-row">
    <div class="chart-box">
      <h3>📈 Token 趋势 (近 14 天)</h3>
      <div class="chart-inner" id="trendChart"></div>
    </div>
    <div class="chart-box">
      <h3>🥧 模型分布 (全部)</h3>
      <div class="chart-inner" id="modelChart"></div>
    </div>
  </div>

  <!-- 项目分布 -->
  <div class="chart-row">
    <div class="chart-box">
      <h3>📁 项目 Token 分布</h3>
      <div class="chart-inner" id="projectChart"></div>
    </div>
    <div class="chart-box">
      <h3>⏰ 每日用量 (近 7 天)</h3>
      <div class="chart-inner" id="hourlyChart"></div>
    </div>
  </div>

  <!-- 最近请求 -->
  <div class="table-box">
    <h3>📋 最近请求</h3>
    <div style="overflow-x:auto;">
      <table>
        <thead>
          <tr><th>时间</th><th>项目</th><th>模型</th><th>输入</th><th>输出</th><th>总计</th><th>缓存读取</th><th>费用 (¥)</th></tr>
        </thead>
        <tbody id="requestTable"></tbody>
      </table>
    </div>
    <div class="empty-state" id="emptyState">
      <div class="icon">📡</div>
      <div>等待数据…<br><small>数据源: ~/.claude/projects/**/*.jsonl (只读)</small></div>
    </div>
  </div>
  <div class="data-source">📂 数据来源: ~/.claude/projects/ · 自动扫描每 5s · 不修改任何配置文件</div>
</div>

<script>
const API = '/api';

function fmt(n) {
  if (n == null || n === 0) return '0';
  if (n >= 1_000_000) return (n/1_000_000).toFixed(2)+'M';
  if (n >= 1_000) return (n/1_000).toFixed(1)+'K';
  return String(Math.floor(n));
}
function fmtCNY(n) { return n == null ? '—' : '¥'+n.toFixed(2); }

let trendChart, modelChart, projectChart, hourlyChart;
function initCharts() {
  trendChart = echarts.init(document.getElementById('trendChart'));
  modelChart = echarts.init(document.getElementById('modelChart'));
  projectChart = echarts.init(document.getElementById('projectChart'));
  hourlyChart = echarts.init(document.getElementById('hourlyChart'));
  window.addEventListener('resize', () => {
    trendChart?.resize(); modelChart?.resize(); projectChart?.resize(); hourlyChart?.resize();
  });
}

const COLORS = ['#38bdf8','#818cf8','#4ade80','#fbbf24','#f87171','#fb923c','#a78bfa','#2dd4bf'];

async function refresh() {
  try {
    const [summary, trend, models, requests, projects, hourly] = await Promise.all([
      fetch(API+'/summary').then(r=>r.json()),
      fetch(API+'/trend?days=14').then(r=>r.json()),
      fetch(API+'/models').then(r=>r.json()),
      fetch(API+'/requests?limit=20').then(r=>r.json()),
      fetch(API+'/projects').then(r=>r.json()),
      fetch(API+'/hourly?days=7').then(r=>r.json()),
    ]);

    // 统计卡片
    document.getElementById('totalTokens').textContent = fmt(summary.total_tokens);
    document.getElementById('totalRequests').textContent = summary.total_requests + ' 次请求';
    document.getElementById('totalCost').textContent = fmtCNY(summary.total_cost_cny);
    document.getElementById('totalCostUSD').textContent = '$' + (summary.total_cost_cny / 7.25).toFixed(2);
    document.getElementById('todayCost').textContent = fmtCNY(summary.today_cost_cny);
    document.getElementById('todayTokens').textContent = fmt(summary.today_tokens) + ' tokens';
    document.getElementById('projectCount').textContent = summary.project_count;
    document.getElementById('sessionCount').textContent = summary.session_count + ' 个会话';

    // 趋势图
    trendChart.setOption({
      tooltip: {trigger:'axis', backgroundColor:'#1e293b', borderColor:'#334155', textStyle:{color:'#e2e8f0'}},
      grid: {left:55,right:55,top:10,bottom:35},
      xAxis: {type:'category', data:trend.dates, axisLine:{lineStyle:{color:'#334155'}}, axisLabel:{color:'#94a3b8',fontSize:11}},
      yAxis: [
        {type:'value', splitLine:{lineStyle:{color:'#1e3a5f'}}, axisLabel:{color:'#94a3b8',formatter:fmt}},
        {type:'value', splitLine:{show:false}, axisLabel:{color:'#94a3b8',formatter:v=>'¥'+v.toFixed(0)}}
      ],
      series: [
        {name:'Prompt', type:'bar', stack:'tokens', data:trend.prompt, itemStyle:{color:'#38bdf8'}},
        {name:'Completion', type:'bar', stack:'tokens', data:trend.completion, itemStyle:{color:'#818cf8'}},
        {name:'费用(¥)', type:'line', yAxisIndex:1, data:trend.costs, smooth:true, symbol:'circle', symbolSize:6,
         lineStyle:{color:'#fbbf24',width:2}, itemStyle:{color:'#fbbf24'}},
      ],
      legend: {bottom:2, textStyle:{color:'#94a3b8',fontSize:11}, data:['Prompt','Completion','费用(¥)']},
    }, true);

    // 模型分布
    modelChart.setOption({
      tooltip: {trigger:'item', backgroundColor:'#1e293b', borderColor:'#334155', textStyle:{color:'#e2e8f0'},
                formatter: p => `${p.name}<br/>${fmt(p.value)} tokens (${p.percent}%)`},
      series: [{
        type:'pie', radius:['40%','65%'], center:['50%','43%'],
        label: {color:'#94a3b8',fontSize:10},
        data: models.map(m=>({name:m.model,value:m.tokens})),
        itemStyle: {borderColor:'#0f172a',borderWidth:2},
      }],
      color: COLORS,
      legend: {bottom:5, textStyle:{color:'#94a3b8',fontSize:10}},
    }, true);

    // 项目分布
    projectChart.setOption({
      tooltip: {trigger:'axis', backgroundColor:'#1e293b', borderColor:'#334155', textStyle:{color:'#e2e8f0'}},
      grid: {left:120,right:60,top:10,bottom:30},
      xAxis: {type:'value', splitLine:{lineStyle:{color:'#1e3a5f'}}, axisLabel:{color:'#94a3b8',formatter:fmt}},
      yAxis: {type:'category', data:projects.names, axisLabel:{color:'#94a3b8',fontSize:11}, inverse:true},
      series: [{
        type:'bar', data:projects.tokens,
        itemStyle:{color:new echarts.graphic.LinearGradient(0,0,1,0,[
          {offset:0,color:'#1d4ed8'},{offset:1,color:'#38bdf8'}
        ])},
      }],
    }, true);

    // 每日用量柱状图 (7天)
    hourlyChart.setOption({
      tooltip: {trigger:'axis', backgroundColor:'#1e293b', borderColor:'#334155', textStyle:{color:'#e2e8f0'},
                formatter: p => `${p[0].name}<br/>${fmt(p[0].value)} tokens`},
      grid: {left:50,right:20,top:10,bottom:30},
      xAxis: {type:'category', data:hourly.labels, axisLabel:{color:'#94a3b8',fontSize:11}},
      yAxis: {type:'value', splitLine:{lineStyle:{color:'#1e3a5f'}}, axisLabel:{color:'#94a3b8',formatter:fmt}},
      series: [{
        type:'bar', data:hourly.tokens,
        itemStyle:{color:'#38bdf8',borderRadius:[4,4,0,0]},
      }],
    }, true);

    // 请求表格
    const tbody = document.getElementById('requestTable');
    const empty = document.getElementById('emptyState');
    if (requests.length === 0) {
      tbody.innerHTML = '';
      empty.style.display = 'block';
    } else {
      empty.style.display = 'none';
      tbody.innerHTML = requests.map(r => `
        <tr>
          <td title="${r.created_at}">${r.time_ago}</td>
          <td title="${r.project}">${(r.project||'—').substring(0,20)}</td>
          <td style="max-width:160px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;" title="${r.model}">${r.model.split('-').slice(0,3).join('-')}</td>
          <td>${fmt(r.prompt_tokens)}</td>
          <td>${fmt(r.completion_tokens)}</td>
          <td><b>${fmt(r.total_tokens)}</b></td>
          <td>${fmt(r.cache_read_tokens)}</td>
          <td class="cost">${fmtCNY(r.cost_cny)}</td>
        </tr>
      `).join('');
    }
  } catch(e) {
    console.error('Refresh error:', e);
  }
}

let timer;
function autoRefresh() {
  refresh().then(() => { timer = setTimeout(autoRefresh, 30_000); });
}
initCharts();
autoRefresh();
</script>
</body>
</html>"""


@app.get("/", response_class=HTMLResponse)
async def dashboard():
    return DASHBOARD_HTML


# ============================================================================
# Dashboard API
# ============================================================================
@app.get("/api/summary")
async def api_summary():
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    total = query_db("""
        SELECT COUNT(*), COALESCE(SUM(total_tokens),0), COALESCE(SUM(cost_usd),0)
        FROM requests
    """)[0]

    today_data = query_db("""
        SELECT COUNT(*), COALESCE(SUM(total_tokens),0), COALESCE(SUM(cost_usd),0)
        FROM requests WHERE date(created_at) = ?
    """, (today,))[0]

    projects = query_db("SELECT COUNT(DISTINCT project) FROM requests")[0][0]
    sessions = query_db("SELECT COUNT(DISTINCT session_id) FROM requests")[0][0]

    return {
        "total_tokens": total[1],
        "total_requests": total[0],
        "total_cost_cny": round(total[2] * USD_TO_CNY, 2),
        "today_tokens": today_data[1],
        "today_requests": today_data[0],
        "today_cost_cny": round(today_data[2] * USD_TO_CNY, 2),
        "project_count": projects,
        "session_count": sessions,
    }


@app.get("/api/trend")
async def api_trend(days: int = 14):
    dates, prompt, completion, costs = [], [], [], []
    for i in range(days - 1, -1, -1):
        d = (datetime.now(timezone.utc) - timedelta(days=i)).strftime("%Y-%m-%d")
        dates.append(d[5:])
        row = query_db("""
            SELECT COALESCE(SUM(prompt_tokens),0), COALESCE(SUM(completion_tokens),0),
                   COALESCE(SUM(cost_usd),0)
            FROM requests WHERE date(created_at) = ?
        """, (d,))[0]
        prompt.append(row[0])
        completion.append(row[1])
        costs.append(round(row[2] * USD_TO_CNY, 2))
    return {"dates": dates, "prompt": prompt, "completion": completion, "costs": costs}


@app.get("/api/models")
async def api_models():
    rows = query_db("""
        SELECT model, SUM(total_tokens) as t, SUM(cost_usd) as c
        FROM requests GROUP BY model HAVING t > 0 ORDER BY t DESC
    """)
    return [{"model": r[0], "tokens": r[1], "cost_cny": round(r[2] * USD_TO_CNY, 4)} for r in rows]


@app.get("/api/projects")
async def api_projects():
    rows = query_db("""
        SELECT project, SUM(total_tokens) as t, SUM(cost_usd) as c
        FROM requests WHERE project != ''
        GROUP BY project HAVING t > 0 ORDER BY t DESC LIMIT 15
    """)
    return {
        "names": [r[0] or "unknown" for r in rows],
        "tokens": [r[1] for r in rows],
        "costs_cny": [round(r[2] * USD_TO_CNY, 2) for r in rows],
    }


@app.get("/api/hourly")
async def api_hourly(days: int = 7):
    """返回按天聚合的 token 用量 (7天 = 7个数据点)"""
    labels, tokens = [], []
    for i in range(days - 1, -1, -1):
        d = (datetime.now(timezone.utc) - timedelta(days=i)).strftime("%Y-%m-%d")
        labels.append(d[5:])
        row = query_db("""
            SELECT COALESCE(SUM(total_tokens),0)
            FROM requests WHERE date(created_at) = ?
        """, (d,))[0]
        tokens.append(row[0])
    return {"labels": labels, "tokens": tokens}


@app.get("/api/requests")
async def api_requests(limit: int = 50):
    rows = query_db("""
        SELECT created_at, project, model, prompt_tokens, completion_tokens,
               total_tokens, cache_read_tokens, cost_usd
        FROM requests ORDER BY id DESC LIMIT ?
    """, (limit,))

    now = datetime.now(timezone.utc)
    result = []
    for r in rows:
        created_at, project, model, pt, ct, tt, cr, cost = r
        try:
            dt = datetime.fromisoformat(created_at)
            diff = now - (dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt)
            if diff < timedelta(minutes=1): ta = "刚刚"
            elif diff < timedelta(hours=1): ta = f"{int(diff.total_seconds()/60)}分钟前"
            elif diff < timedelta(days=1): ta = f"{int(diff.total_seconds()/3600)}小时前"
            else: ta = f"{diff.days}天前"
        except Exception:
            ta = created_at

        result.append({
            "created_at": created_at, "time_ago": ta, "project": project,
            "model": model, "prompt_tokens": pt, "completion_tokens": ct,
            "total_tokens": tt, "cache_read_tokens": cr,
            "cost_cny": round(cost * USD_TO_CNY, 4),
        })
    return result


@app.get("/health")
async def health():
    total = query_db("SELECT COUNT(*) FROM requests")[0][0]
    files = list_log_files()
    return {"status": "ok", "requests_tracked": total, "log_files": len(files)}


# ============================================================================
# 入口
# ============================================================================
if __name__ == "__main__":
    import uvicorn
    print(f"""
╔══════════════════════════════════════════════════════╗
║          🔵  Token Monitor v2 (日志模式)             ║
╠══════════════════════════════════════════════════════╣
║  仪表盘:    http://localhost:{PORT}                  ║
║  数据源:    ~/.claude/projects/**/*.jsonl (只读)     ║
║  扫描间隔:  每 {SCAN_INTERVAL}s                                    ║
╠══════════════════════════════════════════════════════╣
║  不修改任何配置文件，纯只读扫描 ✅                    ║
╚══════════════════════════════════════════════════════╝
""")
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="info")
