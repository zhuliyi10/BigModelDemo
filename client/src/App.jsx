import { Fragment, useEffect, useRef, useState } from 'react';
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

// 出行助手推荐问题：触发模型调用高德工具
const AGENT_SUGGESTIONS = [
  '深圳北站准备去南山科技园，地铁怎么走换乘少',
  '从杭州东站到西湖景区打车要多久',
  '帮我找上海人民广场附近的 3 家热门餐厅',
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

/** 时间戳格式化为 HH:mm，用于消息头部展示 */
function formatTime(ts) {
  if (!ts) return '';
  const d = new Date(ts);
  return `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`;
}

/** 复制 AI 回复的原始 Markdown 文本，兼容不支持 clipboard API 的环境 */
async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    try {
      const ta = document.createElement('textarea');
      ta.value = text;
      ta.style.position = 'fixed';
      ta.style.opacity = '0';
      document.body.appendChild(ta);
      ta.select();
      const ok = document.execCommand('copy');
      ta.remove();
      return ok;
    } catch {
      return false;
    }
  }
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

const CopyIcon = () => (
  <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
    <rect x="9" y="9" width="13" height="13" rx="2" />
    <path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1" />
  </svg>
);

const CheckIcon = () => (
  <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
    <path d="M20 6 9 17l-5-5" />
  </svg>
);

/** 消息下方的复制按钮：复制原始文本（AI 消息为 Markdown 源码），成功后短暂显示「已复制」 */
function CopyMarkdownButton({ text }) {
  const [copied, setCopied] = useState(false);
  const timerRef = useRef(null);

  useEffect(() => () => clearTimeout(timerRef.current), []);

  const handleCopy = async () => {
    const ok = await copyText(text);
    if (!ok) return;
    setCopied(true);
    clearTimeout(timerRef.current);
    timerRef.current = setTimeout(() => setCopied(false), 1500);
  };

  return (
    <button className={`btn-copy${copied ? ' copied' : ''}`} onClick={handleCopy} title="复制消息内容">
      {copied ? <CheckIcon /> : <CopyIcon />}
      {copied ? '已复制' : '复制'}
    </button>
  );
}

/** 工具调用状态合并：calling 追加新条目，done 回填同名最近一条 */
function upsertTool(tools, tool) {
  const list = Array.isArray(tools) ? [...tools] : [];
  if (tool.status === 'calling') {
    list.push({ ...tool });
    return list;
  }
  for (let i = list.length - 1; i >= 0; i--) {
    if (list[i].name === tool.name && list[i].status === 'calling') {
      list[i] = { ...tool };
      return list;
    }
  }
  list.push({ ...tool });
  return list;
}

const SEG_ICON = { walk: '🚶', bus: '🚇', drive: '🚗' };

/** 评分描述词映射，对齐千问评分徽章风格 */
const ratingWord = (r) => (r >= 4.8 ? '超棒' : r >= 4.5 ? '很好' : r >= 4.0 ? '好' : r >= 3.5 ? '不错' : '一般');

/** 生成高德官方路线页链接：新窗口打开可交互查看/导航（仿千问卡片右上角展开） */
function buildAmapUri(card) {
  const enc = (v, fallback) => encodeURIComponent(v || fallback);
  const from = `${card.origin},${enc(card.origin_name, '起点')}`;
  const to = `${card.destination},${enc(card.destination_name, '终点')}`;
  const mode = card.kind === 'driving' ? 'car' : 'bus';
  return `https://uri.amap.com/navigation?from=${from}&to=${to}&mode=${mode}&callnative=0&src=bigmodeldemo`;
}

/** 交互式底图：配置了 JS API Key 后用高德 JS API 绘制起终点标记 + 路线折线 */
function AmapInteractive({ card, cfg }) {
  const ref = useRef(null);

  useEffect(() => {
    let map = null;
    let cancelled = false;
    window._AMapSecurityConfig = { securityJsCode: cfg.jsSecret || '' };
    import('@amap/amap-jsapi-loader')
      .then((m) => m.default.load({ key: cfg.jsKey, version: '2.0' }))
      .then((AMap) => {
        if (cancelled || !ref.current) return;
        map = new AMap.Map(ref.current, { zoom: 11, viewMode: '2D' });
        const path = (card.polyline || []).map((p) => p.split(',').map(Number));
        if (path.length > 1) {
          map.add(new AMap.Polyline({ path, strokeColor: '#00B0FF', strokeWeight: 6, strokeOpacity: 0.9 }));
        }
        const toPos = (s) => (s ? s.split(',').map(Number) : null);
        const o = toPos(card.origin);
        const d = toPos(card.destination);
        if (o) map.add(new AMap.Marker({ position: o, label: { content: '<span>起</span>' } }));
        if (d) map.add(new AMap.Marker({ position: d, label: { content: '<span>终</span>' } }));
        map.setFitView(null, false, [30, 30, 30, 30]);
      })
      .catch(() => {
        /* 加载失败时保持空白，静态图链接仍可通过右上角按钮跳转 */
      });
    return () => {
      cancelled = true;
      if (map) map.destroy();
    };
  }, [card, cfg]);

  return <div ref={ref} className="amap-js" />;
}

/** POI 深度链接：有 POI ID 时跳详情页，否则按坐标+名称标注（与千问列表点击同款） */
function buildPoiUri(p) {
  if (p.id) return `https://uri.amap.com/marker?poiid=${p.id}&callnative=0&src=bigmodeldemo`;
  return `https://uri.amap.com/marker?position=${p.location}&name=${encodeURIComponent(p.name || '')}&callnative=0&src=bigmodeldemo`;
}

/** POI 列表卡片：照片/评分/人均/距离，整条可点击跳转高德 */
function PoiListCard({ card }) {
  const pois = Array.isArray(card.pois) ? card.pois : [];
  return (
    <div className="amap-card">
      <div className="amap-route">
        <div className="amap-route-title">{card.title}</div>
        <div className="poi-list">
          {pois.map((p, i) => (
            <a
              key={p.id || i}
              className="poi-item"
              href={buildPoiUri(p)}
              target="_blank"
              rel="noreferrer"
              title="在高德地图中查看"
            >
              {p.photo ? (
                <div className="poi-photo-wrap">
                  <img className="poi-photo" src={p.photo} alt={p.name} loading="lazy" />
                  <span className="poi-watermark">高德地图</span>
                </div>
              ) : (
                <div className="poi-photo poi-photo-empty">📍</div>
              )}
              <div className="poi-info">
                <div className="poi-name">{p.name}</div>
                <div className="poi-meta">
                  {p.rating != null && (
                    <span className="poi-rating">
                      {p.rating}
                      <em>{ratingWord(p.rating)}</em>
                    </span>
                  )}
                  {p.cost != null && <span>¥{Math.round(p.cost)}/人</span>}
                  {p.type && <span className="poi-type">· {p.type}</span>}
                  {p.distance != null && <span className="poi-dist">{p.distance}米</span>}
                </div>
                {(p.review || (p.tags || []).length > 0) && (
                  <div className="poi-quote">{p.review ? `“${p.review}”` : `推荐: ${p.tags.join('、')}`}</div>
                )}
                {p.address && <div className="poi-addr">{p.address}</div>}
              </div>
            </a>
          ))}
        </div>
      </div>
    </div>
  );
}

/** 高德路线卡片：交互/静态底图 + 分段行程；静态图可点击跳转高德官方路线页 */
function AmapCard({ card }) {
  const [cfg, setCfg] = useState(null);

  useEffect(() => {
    fetch('/api/amap/config')
      .then((r) => (r.ok ? r.json() : {}))
      .then(setCfg)
      .catch(() => setCfg({}));
  }, []);

  const segs = Array.isArray(card.segments) ? card.segments : [];
  const uri = buildAmapUri(card);
  return (
    <div className="amap-card">
      <div className="amap-map">
        {cfg == null ? (
          <div className="amap-js" />
        ) : cfg.jsKey ? (
          <AmapInteractive card={card} cfg={cfg} />
        ) : (
          <a className="amap-static-link" href={uri} target="_blank" rel="noreferrer" title="在高德地图中查看路线">
            <img src={card.map_url} alt={card.title || '路线地图'} loading="lazy" />
          </a>
        )}
        <span className="amap-logo">高德地图</span>
        <a className="amap-expand" href={uri} target="_blank" rel="noreferrer" title="在高德地图中打开">
          ⤢
        </a>
      </div>
      <div className="amap-route">
        <div className="amap-route-title">{card.title}</div>
        {segs.length > 0 && (
          <div className="amap-segs">
            {segs.map((s, i) => (
              <Fragment key={i}>
                {i > 0 && <span className="amap-seg-arrow">▸</span>}
                <span className="amap-seg">
                  {SEG_ICON[s.mode] || '•'}{' '}
                  {s.mode === 'walk'
                    ? `${s.duration_min ?? 0}分钟`
                    : s.mode === 'bus'
                      ? s.line
                      : s.road || '驾车'}
                </span>
              </Fragment>
            ))}
          </div>
        )}
        <div className="amap-route-meta">
          {card.duration_min != null && <span>约{card.duration_min}分钟</span>}
          {card.stops != null && <span> · {card.stops}站</span>}
          {card.distance_km != null && <span> · {card.distance_km}公里</span>}
        </div>
      </div>
    </div>
  );
}

export default function App() {
  const [messages, setMessages] = useState([]);
  const [input, setInput] = useState('');
  const [models, setModels] = useState(FALLBACK_MODELS);
  const [model, setModel] = useState(DEFAULT_MODEL);
  const [loading, setLoading] = useState(false);
  // 交互模式：chat = 纯文本问答；a2ui = 模型生成可交互界面
  const [mode, setMode] = useState('chat');
  const agentLike = mode === 'agent';
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

    const nextMessages = [...messages, { role: 'user', content: question, time: Date.now(), ...extra }];
    setMessages([...nextMessages, { role: 'assistant', content: '', time: Date.now() }]);
    setInput('');
    setLoading(true);

    const controller = new AbortController();
    abortRef.current = controller;

    try {
      const endpoint = mode === 'a2ui' ? '/api/a2ui/chat' : mode === 'agent' ? '/api/agent/chat' : '/api/chat';
      const res = await fetch(endpoint, {
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
          } else if (event.type === 'tool_call') {
            // 出行助手：工具调用状态（调用中/完成+摘要）
            const tool = { name: event.name, label: event.label, status: event.status, summary: event.summary };
            setMessages((prev) =>
              prev.map((m, i) => (i === prev.length - 1 ? { ...m, tools: upsertTool(m.tools, tool) } : m))
            );
          } else if (event.type === 'amap_card' || event.type === 'amap_poi_list') {
            // 出行助手：高德卡片（路线卡片 / POI 列表卡片，服务端按 kind 拆分事件）
            const card = event.card;
            setMessages((prev) => prev.map((m, i) => (i === prev.length - 1 ? { ...m, amapCard: card } : m)));
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
          <button className={`mode-tab${mode === 'agent' ? ' active' : ''}`} onClick={() => switchMode('agent')} disabled={loading}>
            🗺 出行助手
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
            <h2>
              {mode === 'a2ui'
                ? '让 AI 为你生成一个界面'
                : mode === 'agent'
                  ? '说出目的地，AI 调高德查路线'
                  : '有什么可以帮你的？'}
            </h2>
            <p>
              {mode === 'a2ui'
                ? 'AI 将根据需求实时生成可交互界面（A2UI 协议），填写后一键提交'
                : mode === 'agent'
                  ? 'AI 通过工具调用实时查询高德地图（地理编码 / 路线规划 / POI 搜索），并附路线卡片'
                  : '选择一个话题开始，或直接输入你的问题'}
            </p>
            <div className="suggestions">
              {(mode === 'a2ui'
                ? A2UI_SUGGESTIONS
                : mode === 'agent'
                  ? AGENT_SUGGESTIONS
                  : SUGGESTIONS
              ).map((s) => (
                <button key={s} className="chip" onClick={() => send(s)} disabled={loading}>
                  {s}
                </button>
              ))}
            </div>
          </div>
        )}
        {messages.map((msg, i) => {
          const isStreamingLast = loading && i === messages.length - 1;
          const canCopy = !!msg.content && !msg.error && !isStreamingLast;
          return (
            <div key={i} className={`msg msg-${msg.role}`}>
              <div className="msg-meta">
                {msg.role === 'user' && msg.time && <span className="msg-time">{formatTime(msg.time)}</span>}
                <div className="msg-avatar">{msg.role === 'user' ? '我' : 'AI'}</div>
                {msg.role === 'assistant' && msg.time && <span className="msg-time">{formatTime(msg.time)}</span>}
              </div>
              <div className="msg-body">
                <div className={`msg-bubble${msg.error ? ' msg-error' : ''}`}>
                  {msg.role === 'assistant' ? (
                    msg.content || (agentLike && ((msg.tools || []).length > 0 || msg.amapCard)) ? (
                      mode === 'a2ui' ? (
                        <A2UISurface
                          text={msg.content}
                          streaming={isStreamingLast}
                          onEvent={handleA2UIEvent}
                        />
                      ) : (
                        <>
                          {agentLike && (msg.tools || []).length > 0 && (
                            <div className="tool-chips">
                              {msg.tools.map((t, idx) => (
                                <span key={idx} className={`tool-chip ${t.status}`}>
                                  🔧 {t.label}
                                  {t.status === 'calling' ? '…' : t.summary ? ` · ${t.summary}` : ''}
                                </span>
                              ))}
                            </div>
                          )}
                          {agentLike && msg.amapCard &&
                            (msg.amapCard.kind === 'poi_list' ? (
                              <PoiListCard card={msg.amapCard} />
                            ) : (
                              <AmapCard card={msg.amapCard} />
                            ))}
                          {msg.content && (
                            <div className="markdown">
                              <ReactMarkdown remarkPlugins={[remarkGfm]}>{msg.content}</ReactMarkdown>
                            </div>
                          )}
                        </>
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
                {canCopy && <CopyMarkdownButton text={msg.content} />}
              </div>
            </div>
          );
        })}
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
          内容由 AI 生成，请注意甄别 ·{' '}
          {mode === 'a2ui'
            ? 'A2UI 模式：AI 生成可交互界面'
            : mode === 'agent'
              ? '出行助手模式：AI 工具调用高德服务'
              : '文本对话模式'} · 当前模型：{model}
        </p>
      </footer>
    </div>
  );
}
