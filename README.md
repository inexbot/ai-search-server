# iNexBot AI 搜索 — 部署指南

## 架构

```
用户浏览器
    ↓  http://localhost:3000
Node.js 后端（代理）
    ↓  http://127.0.0.1:8642/v1/chat/completions
Hermes Gateway API Server
    ↓  （调用 web search / 读文件等工具）
LLM + 工具链
```

## 第一步：配置 Hermes API Server

在 `~/.hermes/.env` 中添加（或修改现有配置）：

```bash
API_SERVER_ENABLED=true
API_SERVER_HOST=127.0.0.1
API_SERVER_PORT=8642
API_SERVER_KEY=hermes-website-search-2025
API_SERVER_CORS_ORIGINS=*
```

> 如果希望从其他机器访问，把 `API_SERVER_HOST` 改为 `0.0.0.0`，并通过防火墙限制 `API_SERVER_KEY` 的访问。

配置完成后**重启 Hermes Gateway**：
```bash
hermes gateway restart
```

## 第二步：启动 AI 搜索服务

```bash
cd /mnt/d/workspace/ai-search
./start.sh
```

访问 `http://localhost:3000` 即可。

## 环境变量

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `PORT` | `3000` | 前端服务端口 |
| `HERMES_API_URL` | `http://127.0.0.1:8642` | Hermes API Server 地址 |
| `HERMES_API_KEY` | `hermes-website-search-2025` | 与 `.env` 中的 `API_SERVER_KEY` 保持一致 |

## 生产环境部署

### 同机器（简单）
```bash
hermes gateway restart
cd /mnt/d/workspace/ai-search && ./start.sh
```

### 异机器

**方案 A：官网后端直连 Hermes（推荐）**

官网后端代码中设置 `HERMES_API_URL=http://你的VPS公网IP:8642`，
并在 VPS 防火墙开放 `8642` 端口，仅允许官网 IP 访问。

**方案 B：Nginx 反向代理**

```nginx
server {
    listen 443 ssl;
    server_name ai.inexbot.com;

    location / {
        proxy_pass http://127.0.0.1:3000;
    }

    # 可选：直接代理 /api/search 到 Hermes
    location /hermes/ {
        proxy_pass http://127.0.0.1:8642/v1/chat/completions;
        proxy_set_header Authorization "Bearer herems-website-search-2025";
        proxy_set_header Content-Type "application/json";
    }
}
```

## 暴露公网时的安全建议

1. **API_SERVER_KEY** 设置为强随机字符串
2. **API_SERVER_HOST=0.0.0.0** 时，通过 Nginx + IP 白名单限制来源
3. 建议 Nginx 配置 `proxy_buffering off` 支持真正的流式输出
