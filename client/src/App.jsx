import { useEffect, useRef, useState } from 'react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import A2UISurface from './A2UISurface';

// 默认模型与兜底列表；实际列表从网关 /api/models 动态加载
const DEFAULT_MODEL = 'claude-4.6-sonnet';
const FALLBACK_MODELS = [
  DEFAULT_MODEL,
  'claude-4.5-haiku',
  'claude-4.6-opus',
  'gpt-5.5',
  'glm-5',
  'kimi-k2.5',
];

// 空状态推荐问题
const SUGGESTIONS = [
  '用一句话解释什么是递归',
  '帮我写一首关于秋天的短诗',
  '用表格对比 React 和 Vue 的异同',
  '给我讲一个有趣的冷知识',
];

// A2UI 场景推荐问题：引导模型生成可交互界面
const A2UI_SUGGESTIONS = [
  '帮我预订明天晚上 7 点、2 人的餐厅',
  '我想规划一次三天的杭州旅行',
  '帮我点一杯咖啡并选择配送方式',
  '生成一份团队活动报名表',
];

/** 解析 SSE 缓冲区，返回 [完整事件数组, 剩余缓冲] */
function parseSSE(buffer) {
  const events = [];
  const blocks = buffer.split('\n\n');
  const rest = blocks.pop(); // 最后一段可能不完整，留待下次
  for (const block of blocks) {
    const dataLines = block
      .split('\n')
      .filter((line) => line.startsWith('data:'))
      .map((line) => line.slice(5).trim());
    if (dataLines.length === 0) continue;
    try {
      events.push(JSON.parse(dataLines.join('\n')));
    } catch {
      /* 忽略无法解析的心跳/注释行 */
    }
  }
  return [events, rest];
}

/** 从网关错误响应中提取可读信息 */
function extractError(data) {
  return data?.error?.message || data?.error || JSON.stringify(data) || '请求失败';
}

/** 将界面提交的 dataModel 转成「标签: 值」摘要，供用户侧消息展示 */
function buildEventSummary(event) {
  const data = event.dataModel ?? {};
  const labelOf = {};
  for (const f of event.fields ?? []) {
    if (f?.path?.startsWith('/')) labelOf[f.path.slice(1)] = f.label;
  }
  const format = (v) => {
    if (v === true) return '是';
    if (v === false) return '否';
    if (v == null || v === '') return '未填写';
    if (Array.isArray(v)) return v.length > 0 ? v.join('、') : '未填写';
    if (typeof v === 'object') return JSON.stringify(v);
    return String(v);
  };
  return Object.entries(data).map(([key, value]) => ({
    label: labelOf[key] || key,
    value: format(value),
  }));
}

const SunIcon = () => (
  <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round">
    <circle cx="12" cy="12" r="4" />
    <path d="M12 2v2M12 20v2M4.93 4.93l1.41 1.41M17.66 17.66l1.41 1.41M2 12h2M20 12h2M4.93 19.07l1.41-1.41M17.66 6.34l1.41-1.41" />
  </svg>
);

const MoonIcon = () => (
  <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
    <path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z" />
  </svg>
);

export default function App() {
  const [messages, setMessages] = useState([]);
  const [input, setInput] = useState('');
  const [models, setModels] = useState(FALLBACK_MODELS);
  const [model, setModel] = useState(DEFAULT_MODEL);
  const [loading, setLoading] = useState(false);
  // 交互模式：chat = 纯文本问答；a2ui = 模型生成可交互界面
  const [mode, setMode] = useState('chat');
  const abortRef = useRef(null);
  const listRef = useRef(null);
  const inputRef = useRef(null);

  // 加载网关支持的模型列表
  useEffect(() => {
    fetch('/api/models')
      .then((res) => (res.ok ? res.json() : Promise.reject()))
      .then((data) => {
        const ids = (data.data || []).map((m) => m.id);
        if (ids.length > 0) {
          setModels(ids);
          if (!ids.includes(DEFAULT_MODEL)) setModel(ids[0]);
        }
      })
      .catch(() => {
        /* 获取失败时使用兜底列表 */
      });
  }, []);

  // 主题：初始跟随 localStorage > 系统偏好，写入 data-theme 属性
  const [theme, setTheme] = useState(() => {
    if (typeof window === 'undefined') return 'dark';
    return (
      localStorage.getItem('theme') ||
      (window.matchMedia('(prefers-color-scheme: light)').matches ? 'light' : 'dark')
    );
  });

  useEffect(() => {
    document.documentElement.dataset.theme = theme;
    localStorage.setItem('theme', theme);
  }, [theme]);

  const toggleTheme = () => setTheme((t) => (t === 'dark' ? 'light' : 'dark'));

  // 新消息/流式更新时自动滚动到底部
  useEffect(() => {
    listRef.current?.scrollTo({ top: listRef.current.scrollHeight });
  }, [messages, loading]);

  const stop = () => abortRef.current?.abort();

  const switchMode = (next) => {
    if (next === mode || loading) return;
    setMode(next);
    setMessages([]);
  };

  /** A2UI 界面事件：用户点击按钮后，把界面数据作为新消息回传给模型 */
  const handleA2UIEvent = (event) => {
    if (loading) return;
    const payload = `[A2UI_EVENT] ${event.surfaceId}.${event.name} ${JSON.stringify(event.dataModel ?? {})}`;
    send(payload, { a2uiSummary: buildEventSummary(event) });
  };

  const send = async (preset, extra) => {
    const question = (preset ?? input).trim();
    if (!question || loading) return;

    const nextMessages = [...messages, { role: 'user', content: question, ...extra }];
    setMessages([...nextMessages, { role: 'assistant', content: '' }]);
    setInput('');
    setLoading(true);

    const controller = new AbortController();
    abortRef.current = controller;

    try {
      const res = await fetch(mode === 'a2ui' ? '/api/a2ui/chat' : '/api/chat', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          model,
          messages: nextMessages.map(({ role, content }) => ({ role, content })),
        }),
        signal: controller.signal,
      });

      const contentType = res.headers.get('content-type') || '';
      // 非流式响应 = 出错，直接展示错误信息
      if (!res.ok || !contentType.includes('text/event-stream')) {
        let data = null;
        try {
          data = await res.json();
        } catch {
          data = { error: await res.text() };
        }
        const errMsg = extractError(data);
        setMessages((prev) =>
          prev.map((m, i) =>
            i === prev.length - 1 ? { ...m, content: `⚠️ ${errMsg}`, error: true } : m
          )
        );
        setLoading(false);
        return;
      }

      // 流式读取并增量更新最后一条助手消息
      const reader = res.body.getReader();
      const decoder = new TextDecoder();
      let buffer = '';
      let answer = '';

      for (;;) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        const [events, rest] = parseSSE(buffer);
        buffer = rest;

        for (const event of events) {
          if (event.type === 'content_block_delta' && event.delta?.type === 'text_delta') {
            answer += event.delta.text;
            const snapshot = answer;
            setMessages((prev) =>
              prev.map((m, i) => (i === prev.length - 1 ? { ...m, content: snapshot } : m))
            );
          } else if (event.type === 'error') {
            throw new Error(extractError(event));
          }
        }
      }

      if (!answer) {
        setMessages((prev) =>
          prev.map((m, i) =>
            i === prev.length - 1 ? { ...m, content: '⚠️ 未收到回复内容，请重试', error: true } : m
          )
        );
      }
    } catch (err) {
      if (err.name !== 'AbortError') {
        setMessages((prev) =>
          prev.map((m, i) =>
            i === prev.length - 1 ? { ...m, content: `⚠️ ${err.message}`, error: true } : m
          )
        );
      }
    } finally {
      setLoading(false);
      abortRef.current = null;
      inputRef.current?.focus();
    }
  };

  const onKeyDown = (e) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      send();
    }
  };

  const clearChat = () => {
    if (!loading) setMessages([]);
  };

  return (
    <div className="app">
      <header className="header">
        <div className="header-title">
          <div className="logo-orb">✦</div>
          <h1>大模型问答</h1>
        </div>
        <div className="mode-tabs">
          <button className={`mode-tab${mode === 'chat' ? ' active' : ''}`} onClick={() => switchMode('chat')} disabled={loading}>
            文本对话
          </button>
          <button className={`mode-tab${mode === 'a2ui' ? ' active' : ''}`} onClick={() => switchMode('a2ui')} disabled={loading}>
            ⚡ A2UI 场景
          </button>
        </div>
        <div className="header-actions">
          <button className="btn-theme" onClick={toggleTheme} title={theme === 'dark' ? '切换明亮模式' : '切换深色模式'}>
            {theme === 'dark' ? <SunIcon /> : <MoonIcon />}
          </button>
          <select value={model} onChange={(e) => setModel(e.target.value)} disabled={loading}>
            {models.map((m) => (
              <option key={m} value={m}>
                {m}
              </option>
            ))}
          </select>
          <button className="btn-ghost" onClick={clearChat} disabled={loading || messages.length === 0}>
            清空对话
          </button>
        </div>
      </header>

      <main className="chat-list" ref={listRef}>
        {messages.length === 0 && (
          <div className="empty-state">
            <div className="empty-orb">✦</div>
            <h2>{mode === 'a2ui' ? '让 AI 为你生成一个界面' : '有什么可以帮你的？'}</h2>
            <p>
              {mode === 'a2ui'
                ? 'AI 将根据需求实时生成可交互界面（A2UI 协议），填写后一键提交'
                : '选择一个话题开始，或直接输入你的问题'}
            </p>
            <div className="suggestions">
              {(mode === 'a2ui' ? A2UI_SUGGESTIONS : SUGGESTIONS).map((s) => (
                <button key={s} className="chip" onClick={() => send(s)} disabled={loading}>
                  {s}
                </button>
              ))}
            </div>
          </div>
        )}
        {messages.map((msg, i) => (
          <div key={i} className={`msg msg-${msg.role}`}>
            <div className="msg-avatar">{msg.role === 'user' ? '我' : 'AI'}</div>
            <div className={`msg-bubble${msg.error ? ' msg-error' : ''}`}>
              {msg.role === 'assistant' ? (
                msg.content ? (
                  mode === 'a2ui' ? (
                    <A2UISurface
                      text={msg.content}
                      streaming={loading && i === messages.length - 1}
                      onEvent={handleA2UIEvent}
                    />
                  ) : (
                    <div className="markdown">
                      <ReactMarkdown remarkPlugins={[remarkGfm]}>{msg.content}</ReactMarkdown>
                    </div>
                  )
                ) : (
                  <div className="typing">
                    <span />
                    <span />
                    <span />
                  </div>
                )
              ) : msg.content.startsWith('[A2UI_EVENT]') ? (
                <div className="a2ui-event-msg">
                  <div className="a2ui-event-tag">⚡ 界面提交</div>
                  {Array.isArray(msg.a2uiSummary) && msg.a2uiSummary.length > 0 ? (
                    <ul className="a2ui-event-summary">
                      {msg.a2uiSummary.map((row, idx) => (
                        <li key={idx}>
                          <span className="a2ui-event-label">{row.label}</span>
                          <span className="a2ui-event-value">{row.value}</span>
                        </li>
                      ))}
                    </ul>
                  ) : (
                    <pre>{msg.content.slice(msg.content.indexOf('{'))}</pre>
                  )}
                </div>
              ) : (
                msg.content
              )}
            </div>
          </div>
        ))}
      </main>

      <footer className="composer">
        <div className="composer-box">
          <textarea
            ref={inputRef}
            rows={1}
            placeholder="输入问题，Enter 发送 · Shift+Enter 换行"
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={onKeyDown}
          />
          {loading ? (
            <button className="btn-stop" onClick={stop} title="停止生成">
              ■
            </button>
          ) : (
            <button className="btn-send" onClick={send} disabled={!input.trim()} title="发送">
              ↑
            </button>
          )}
        </div>
        <p className="composer-tip">
          内容由 AI 生成，请注意甄别 · {mode === 'a2ui' ? 'A2UI 模式：AI 生成可交互界面' : '文本对话模式'} · 当前模型：{model}
        </p>
      </footer>
    </div>
  );
}
