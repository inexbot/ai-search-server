#!/bin/bash
# 启动 AI 搜索前后端服务
# 前端 + Node.js 代理后端（转发请求到 Hermes Gateway）

cd "$(dirname "$0")"

export PORT=3001
export HERMES_API_URL=${HERMES_API_URL:-http://127.0.0.1:8643}
export HERMES_API_KEY=${HERMES_API_KEY:-hermes-website-search-2025}

echo "============================================"
echo "  iNexBot AI Search Service"
echo "============================================"
echo "  前端地址:    http://localhost:\$PORT"
echo "  Hermes API:  \$HERMES_API_URL"
echo "============================================"

node server/server.js
