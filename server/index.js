import path from 'node:path';
import { fileURLToPath } from 'node:url';
import express from 'express';
import cors from 'cors';
import dotenv from 'dotenv';

// 从 server/.env 加载环境变量（.env 不入库，见 .gitignore）
const __dirname = path.dirname(fileURLToPath(import.meta.url));
dotenv.config({ path: path.join(__dirname, '.env') });

const PORT = process.env.PORT || 3001;
// Anthropic 兼容网关地址，最终请求 {GATEWAY_BASE}/v1/messages
const GATEWAY_BASE =
  process.env.GATEWAY_BASE || 'https://lab.iwhalecloud.com/gpt-proxy/anthropic';
// API Key 仅从环境变量读取，禁止硬编码
const API_KEY = process.env.ANTHROPIC_API_KEY || '';

const app = express();
app.use(cors());
app.use(express.json({ limit: '2mb' }));

app.get('/api/health', (_req, res) => res.json({ ok: true }));

/**
 * 模型列表：透传网关 /v1/models
 */
app.get('/api/models', async (_req, res) => {
  if (!API_KEY) {
    return res.status(500).json({ error: '服务端未配置 ANTHROPIC_API_KEY，请在 server/.env 中设置' });
  }
  try {
    const upstream = await fetch(`${GATEWAY_BASE}/v1/models`, {
      headers: {
        'x-api-key': API_KEY,
        'anthropic-version': '2023-06-01',
      },
    });
    const text = await upstream.text();
    res.status(upstream.status).type('application/json').send(text);
  } catch (err) {
    console.error('[proxy] 获取模型列表失败:', err);
    res.status(502).json({ error: '获取模型列表失败: ' + err.message });
  }
});

/**
 * 问答接口：接收前端 messages，流式转发网关的 SSE 响应
 */
app.post('/api/chat', async (req, res) => {
  const { model, messages, max_tokens = 4096, temperature } = req.body || {};

  if (!Array.isArray(messages) || messages.length === 0) {
    return res.status(400).json({ error: 'messages 不能为空' });
  }

  if (!API_KEY) {
    return res.status(500).json({ error: '服务端未配置 ANTHROPIC_API_KEY，请在 server/.env 中设置' });
  }

  const payload = {
    model,
    messages,
    max_tokens,
    stream: true,
  };
  if (typeof temperature === 'number') payload.temperature = temperature;

  const controller = new AbortController();
  // 客户端断开连接且响应尚未结束时，终止上游请求
  res.on('close', () => {
    if (!res.writableEnded) controller.abort();
  });

  let upstream;
  try {
    upstream = await fetch(`${GATEWAY_BASE}/v1/messages`, {
      method: 'POST',
      headers: {
        'content-type': 'application/json',
        'x-api-key': API_KEY,
        'anthropic-version': '2023-06-01',
      },
      body: JSON.stringify(payload),
      signal: controller.signal,
    });
  } catch (err) {
    if (err.name === 'AbortError') return;
    console.error('[proxy] 请求网关失败:', err);
    return res.status(502).json({ error: '请求网关失败: ' + err.message });
  }

  // 非 SSE 响应（通常是错误 JSON）：原样透传
  const contentType = upstream.headers.get('content-type') || '';
  if (!upstream.ok || !contentType.includes('text/event-stream')) {
    const text = await upstream.text();
    console.error('[proxy] 网关返回异常:', upstream.status, text);
    res.status(upstream.status).type('application/json').send(text);
    return;
  }

  // SSE 流式透传
  res.setHeader('Content-Type', 'text/event-stream; charset=utf-8');
  res.setHeader('Cache-Control', 'no-cache, no-transform');
  res.setHeader('Connection', 'keep-alive');
  res.setHeader('X-Accel-Buffering', 'no');
  res.flushHeaders();

  try {
    const reader = upstream.body.getReader();
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      res.write(value);
    }
  } catch (err) {
    if (err.name !== 'AbortError') {
      console.error('[proxy] 流读取异常:', err);
    }
  } finally {
    res.end();
  }
});

app.listen(PORT, () => {
  console.log(`[server] 代理服务已启动: http://localhost:${PORT}`);
  console.log(`[server] 网关地址: ${GATEWAY_BASE}/v1/messages`);
  if (!API_KEY) {
    console.warn('[server] ⚠️ 未检测到 ANTHROPIC_API_KEY，请复制 .env.example 为 .env 并填入密钥');
  }
});
