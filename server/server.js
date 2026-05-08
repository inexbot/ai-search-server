/**
 * Hermes AI Search Backend
 * 
 * 调用 Hermes API Server（OpenAI 兼容），将用户搜索请求转发给 Hermes Agent，
 * 支持流式响应（SSE），并通过 /v1/chat/completions 接口与 Hermes 通信。
 * 
 * 环境变量：
 *   HERMES_API_URL  - Hermes API Server 地址，默认 http://127.0.0.1:8643
 *   HERMES_API_KEY  - Bearer 认证密钥
 */

const http = require('http');
const https = require('https');
const { spawn } = require('child_process');
const path = require('path');
const HERMES_API_URL = process.env.HERMES_API_URL || 'http://127.0.0.1:8643';
const HERMES_API_KEY = process.env.HERMES_API_KEY || 'hermes-website-search-2025';

/**
 * 从本地知识库检索相关内容（调用 inexbot-knowledge-base 检索脚本）
 * @param {string} query - 用户问题
 * @returns {Promise<{kbText: string, sources: Array<{title: string, url: string}>>}
 *   kbText: 供 LLM 参考的 Markdown 正文，sources: 原文链接列表
 */
function searchKnowledgeBase(query) {
  return new Promise((resolve, reject) => {
    const scriptPath = path.join(
      process.env.HERMES_SKILLS_DIR || '/home/inexbot/.hermes/skills',
      'productivity', 'inexbot-knowledge-base', 'scripts', 'retrieve.py'
    );
    const child = spawn('python3', [scriptPath, query, '3'], { timeout: 15000 });
    let stdout = '';
    let stderr = '';
    child.stdout.on('data', d => stdout += d.toString());
    child.stderr.on('data', d => stderr += d.toString());
    child.on('close', code => {
      if (code === 0 && stdout.trim()) {
        // 解析 retrieve.py 原始输出格式
        // 格式：===== 标题：xxx\n链接：xxx\n描述：xxx\n---...\n正文...\n=====
        const sections = stdout.trim().split('='.repeat(60) + '\n');
        const sources = [];
        let kbText = '';

        for (const section of sections) {
          if (!section.trim()) continue;
          const lines = section.split('\n');
          let title = '', url = '', desc = '', bodyLines = [];
          let readingBody = false;

          for (const line of lines) {
            if (line.startsWith('标题：')) {
              title = line.replace('标题：', '').trim();
            } else if (line.startsWith('链接：')) {
              url = line.replace('链接：', '').trim();
            } else if (line.startsWith('描述：')) {
              desc = line.replace('描述：', '').trim();
            } else if (line.startsWith('-'.repeat(60))) {
              readingBody = true;
            } else if (readingBody) {
              bodyLines.push(line);
            }
          }

          if (title && url) {
            sources.push({ title, url });
            const srcNum = sources.length; // 文档编号（1-based）
            // 在 KB 正文中注入编号标记和 Markdown 链接，方便 LLM 边回答边引用
            kbText += `【文档${srcNum}】 ${title}\n来源：[${title}](${url})\n\n`;
            if (desc) kbText += `> ${desc}\n\n`;
            kbText += bodyLines.join('\n').trim() + '\n\n';
          }
        }

        // KB 正文直接传给 LLM，不截断（完整内容才能保证回答全面）
        resolve({ kbText: kbText.trim(), sources });

      } else {
        if (stderr) console.warn('[KB Retrieval]', stderr.trim().split('\n').pop());
        resolve({ kbText: '', sources: [] });
      }
    });
    child.on('error', err => {
      console.warn('[KB Retrieval] 脚本执行失败:', err.message);
      resolve({ kbText: '', sources: [] });
    });
  });
}

/**
 * 将后端代理请求转发到 Hermes API Server
 * 使用 HTTP CONNECT 隧道代理（如果配置了 HTTPS_AGENT）
 */
function proxyToHermes(hermesPath, postData, response) {
  const url = new URL(hermesPath, HERMES_API_URL);
  const isHttps = url.protocol === 'https:';
  const client = isHttps ? https : http;

  const options = {
    hostname: url.hostname,
    port: url.port || (isHttps ? 443 : 80),
    path: url.pathname + url.search,
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      'Authorization': `Bearer ${HERMES_API_KEY}`,
      'Content-Length': Buffer.byteLength(postData),
    },
  };

  const req = client.request(options, (res) => {
    // 流式转发：直接 pipe 到前端
    response.writeHead(res.statusCode, {
      ...res.headers,
      // 确保前端能接收流式响应
      'Transfer-Encoding': 'chunked',
    });
    res.pipe(response);
  });

  req.on('error', (err) => {
    console.error('[Hermes Proxy] 请求错误:', err.message);
    if (!response.headersSent) {
      response.writeHead(502, { 'Content-Type': 'application/json' });
      response.end(JSON.stringify({ error: 'Hermes 服务不可用，请检查 Hermes Gateway 是否运行' }));
    } else {
      response.end();
    }
  });

    req.write(postData);
    req.end();
}

/**
 * 转发到 Hermes，收到流式响应后，在最后附加上参考文档列表
 * @param {string} hermesPath - API 路径
 * @param {string} postData - 请求体
 * @param {object} response - 前端响应对象
 * @param {Array} sources - 知识库来源 [{title, url}]
 */
function proxyToHermes_withSources(hermesPath, postData, response, sources) {
  const url = new URL(hermesPath, HERMES_API_URL);
  const isHttps = url.protocol === 'https:';
  const client = isHttps ? https : http;

  const options = {
    hostname: url.hostname,
    port: url.port || (isHttps ? 443 : 80),
    path: url.pathname + url.search,
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      'Authorization': `Bearer ${HERMES_API_KEY}`,
      'Content-Length': Buffer.byteLength(postData),
    },
  };

  const req = client.request(options, (res) => {
    // 检查 Hermes 返回状态
    if (res.statusCode !== 200) {
      response.writeHead(res.statusCode, { 'Content-Type': 'application/json' });
      res.pipe(response);
      return;
    }

    response.writeHead(200, {
      'Content-Type': 'text/event-stream',
      'Cache-Control': 'no-cache',
      'Connection': 'keep-alive',
      'Access-Control-Allow-Origin': '*',
    });

    let buffer = '';
    let finished = false;

    // 生成参考文档的 SSE 数据块
    const refsText = '\n\n---\n\n**参考文档：**\n' + sources.map(s => `- [${s.title}](${s.url})`).join('\n');
    const refsSSE = sseFormat('message', {
      id: 'final-refs',
      object: 'chat.completion.chunk',
      created: Math.floor(Date.now() / 1000),
      model: 'mini-max',
      choices: [{ index: 0, delta: { content: refsText }, finish_reason: null }],
    });

    res.on('data', (chunk) => {
      const text = chunk.toString();
      buffer += text;

      // 找到完整的 data: ...\n\n 块并发送
      let idx;
      while ((idx = buffer.indexOf('\n\n')) !== -1) {
        const line = buffer.slice(0, idx);
        buffer = buffer.slice(idx + 2);
        // 先判断 [DONE]，再判断普通 data:
        if (line === 'data: [DONE]') {
          response.write(line + '\n\n');
          finished = true;
        } else if (line.startsWith('data: ')) {
          response.write(line + '\n\n');
        }
      }
    });

    res.on('end', () => {
      if (!finished) {
        // 发送未完成的 DONE
        response.write('data: [DONE]\n\n');
      }
      // LLM 回答结束后补充参考文档
      response.write(refsSSE);
      response.write('data: [DONE]\n\n');
      response.end();
    });

    res.on('error', () => {
      if (!response.headersSent) {
        response.writeHead(502, { 'Content-Type': 'application/json' });
        response.end(JSON.stringify({ error: 'Hermes stream error' }));
      }
    });
  });

  req.on('error', (err) => {
    console.error('[Hermes Proxy] 请求错误:', err.message);
    if (!response.headersSent) {
      response.writeHead(502, { 'Content-Type': 'application/json' });
      response.end(JSON.stringify({ error: 'Hermes 服务不可用' }));
    }
  });

  req.write(postData);
  req.end();
}

/**
 * SSE 格式化辅助
 */
function sseFormat(event, data) {
  return `event: ${event}\ndata: ${JSON.stringify(data)}\n\n`;
}

const server = http.createServer((req, res) => {
  // CORS 预检
  if (req.method === 'OPTIONS') {
    res.writeHead(204, {
      'Access-Control-Allow-Origin': '*',
      'Access-Control-Allow-Methods': 'POST, OPTIONS',
      'Access-Control-Allow-Headers': 'Content-Type, Authorization, X-Hermes-Session-Id',
      'Access-Control-Max-Age': '86400',
    });
    res.end();
    return;
  }

  // ========== /api/search ==========
  if (req.method === 'POST' && req.url === '/api/search') {
    let body = '';
    req.on('data', chunk => { body += chunk; });
    req.on('end', async () => {
      try {
        const { message, sessionId } = JSON.parse(body);

        if (!message || typeof message !== 'string' || message.trim().length === 0) {
          res.writeHead(400, { 'Content-Type': 'application/json' });
          res.end(JSON.stringify({ error: 'message 不能为空' }));
          return;
        }

        // 先检索知识库，获取相关背景信息
        const { kbText, sources } = await searchKnowledgeBase(message);

        // 构建 System Prompt
        // 强制要求：在回答的每个知识点后必须标注【文档编号】，末尾必须列参考文档，否则回答无效
        const systemPrompt = kbText
          ? `【核心任务】回答用户关于纳博特机器人的问题。

【知识库内容】
${kbText}

【强制要求】
1. 每个知识点的回答后面必须加来源标注，格式：【文档N】（N是数字）
2. 回答末尾必须有"参考文档："章节，列出所有参考过的文档，格式：- [标题](https://doc.inexbot.com/...)
3. 不要出现 ~/workspace 或任何本地路径，链接必须是 https://doc.inexbot.com/ 开头
4. 如果某个标定方法没有在知识库中找到，不要提及它
`
          : `你是一个专业的纳博特科技（iNexBot）工业机器人AI助手。
回答要求：
1. 如果你了解纳博特相关产品和技术，直接回答
2. 如果不确定，建议用户查阅 https://doc.inexbot.com 或联系技术支持
3. 使用简洁专业的技术语言，适当使用 Markdown 格式来组织回答`;

        // 构建 Hermes 请求（OpenAI Chat Completions 格式）
        // 注意：使用 mini-max 模型而非 hermes-agent，后者不遵守 KB system prompt 注入
        const hermesBody = JSON.stringify({
          model: 'mini-max',
          messages: [
            { role: 'system', content: systemPrompt },
            { role: 'user', content: message }
          ],
          stream: true,
        });

        // 转发到 Hermes（走 API Server 的 /v1/chat/completions 流式接口）
        const apiPath = '/v1/chat/completions';
        proxyToHermes_withSources(apiPath, hermesBody, res, sources);

      } catch (e) {
        res.writeHead(400, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({ error: '请求格式错误: ' + e.message }));
      }
    });
    return;
  }

  // ========== /api/health ==========
  if (req.method === 'GET' && req.url === '/api/health') {
    const url = new URL('/v1/models', HERMES_API_URL);
    const isHttps = url.protocol === 'https:';
    const client = isHttps ? https : http;

    const checkReq = client.get({
      hostname: url.hostname,
      port: url.port || (isHttps ? 443 : 80),
      path: url.pathname,
      headers: { 'Authorization': `Bearer ${HERMES_API_KEY}` },
    }, (hermesRes) => {
      let data = '';
      hermesRes.on('data', chunk => { data += chunk; });
      hermesRes.on('end', () => {
        if (hermesRes.statusCode === 200) {
          res.writeHead(200, { 'Content-Type': 'application/json' });
          res.end(JSON.stringify({ status: 'ok', hermes: 'connected' }));
        } else {
          res.writeHead(503, { 'Content-Type': 'application/json' });
          res.end(JSON.stringify({ status: 'error', hermes: 'unauthorized' }));
        }
      });
    });
    checkReq.on('error', () => {
      res.writeHead(503, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify({ status: 'error', hermes: 'offline' }));
    });
    return;
  }

  // 静态文件
  if (req.method === 'GET') {
    let filePath = req.url === '/' ? '/index.html' : req.url;
    const mimeTypes = {
      '.html': 'text/html',
      '.js': 'application/javascript',
      '.css': 'text/css',
      '.json': 'application/json',
      '.png': 'image/png',
      '.ico': 'image/x-icon',
    };
    const fs = require('fs');
    const path = require('path');
    const fullPath = path.join(__dirname, '..', 'public', filePath);
    const ext = path.extname(fullPath);

    fs.readFile(fullPath, (err, data) => {
      if (err) {
        res.writeHead(404, { 'Content-Type': 'text/plain' });
        res.end('Not Found');
        return;
      }
      res.writeHead(200, { 'Content-Type': mimeTypes[ext] || 'text/plain' });
      res.end(data);
    });
    return;
  }

  res.writeHead(404);
  res.end();
});

const PORT = process.env.PORT || 3002;
server.listen(PORT, () => {
  console.log(`[AI Search Backend] 启动成功 http://localhost:${PORT}`);
  console.log(`[AI Search Backend] 代理目标: ${HERMES_API_URL}`);
  console.log(`[AI Search Backend] 按 Ctrl+C 停止服务`);
});
