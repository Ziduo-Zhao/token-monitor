#!/bin/bash
# Token Monitor — 日志扫描模式
# 只读 ~/.claude/projects/ 目录，不修改任何配置
cd "$(dirname "$0")"
echo "启动 Token Monitor → http://localhost:8765"
echo "数据源: ~/.claude/projects/**/*.jsonl (只读)"
python3 app.py
