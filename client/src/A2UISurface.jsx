import { useEffect, useState } from 'react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';

/**
 * 轻量级 A2UI v0.9 渲染器
 * 协议消息：createSurface / updateComponents / updateDataModel / beginRendering
 * 三层结构：界面结构（组件树） + 数据模型（状态绑定） + 客户端渲染（原生组件）
 */

// ---------- 协议解析 ----------

const MSG_KEYS = ['createSurface', 'updateComponents', 'updateDataModel', 'beginRendering'];
const FENCE_OPEN = /^```(a2ui|json)?\s*$/;
const FENCE_CLOSE = /^```\s*$/;

/** 逐行扫描模型输出：识别 A2UI JSON 消息，其余归入说明文本 */
function parseOutput(text) {
  const msgs = [];
  const proseLines = [];
  let inFence = false;
  for (const raw of text.split('\n')) {
    const line = raw.trim();
    if (FENCE_OPEN.test(line)) {
      if (line.startsWith('```a2ui') || line === '```json') inFence = true;
      continue;
    }
    if (inFence && FENCE_CLOSE.test(line)) {
      inFence = false;
      continue;
    }
    if (!line) continue;
    const parsed = tryParseMessage(line);
    if (parsed) {
      msgs.push(parsed);
    } else if (!inFence) {
      proseLines.push(raw);
    }
  }
  return { msgs, prose: proseLines.join('\n').trim() };
}

function tryParseMessage(line) {
  if (!line.startsWith('{')) return null;
  try {
    const obj = JSON.parse(line);
    return MSG_KEYS.some((k) => obj[k]) ? obj : null;
  } catch {
    return null;
  }
}

// ---------- 数据模型（JSON Pointer 风格路径 /a/b/0） ----------

function resolvePath(model, path) {
  if (!path || path === '/') return model;
  let cur = model;
  for (const seg of path.split('/').filter(Boolean)) {
    if (cur == null) return undefined;
    cur = cur[seg];
  }
  return cur;
}

function setPath(model, path, value) {
  const segs = path.split('/').filter(Boolean);
  const next = deepClone(model ?? {});
  let cur = next;
  for (let i = 0; i < segs.length - 1; i++) {
    const seg = segs[i];
    if (typeof cur[seg] !== 'object' || cur[seg] === null) cur[seg] = {};
    cur = cur[seg];
  }
  if (segs.length) cur[segs[segs.length - 1]] = value;
  return next;
}

function mergePath(model, path, value) {
  const segs = path.split('/').filter(Boolean);
  if (!segs.length) {
    // 根路径：对象则浅合并，否则替换
    if (value && typeof value === 'object' && !Array.isArray(value) && model && typeof model === 'object') {
      return { ...model, ...value };
    }
    return value;
  }
  const next = deepClone(model ?? {});
  let cur = next;
  for (let i = 0; i < segs.length - 1; i++) {
    const seg = segs[i];
    if (typeof cur[seg] !== 'object' || cur[seg] === null) cur[seg] = {};
    cur = cur[seg];
  }
  const last = segs[segs.length - 1];
  if (value && typeof value === 'object' && !Array.isArray(value) && typeof cur[last] === 'object' && cur[last]) {
    cur[last] = { ...cur[last], ...value };
  } else {
    cur[last] = value;
  }
  return next;
}

const deepClone = (v) => (v === undefined ? undefined : JSON.parse(JSON.stringify(v)));

// ---------- Surface 状态 ----------

const initialSurfaces = {};

/** 依次应用 A2UI 消息，得到各 surface 的组件表 / 数据模型 / 渲染入口 */
function applyMessage(state, msg) {
  const cs = msg.createSurface;
  const uc = msg.updateComponents;
  const dm = msg.updateDataModel;
  const br = msg.beginRendering;
  const id = cs?.surfaceId || uc?.surfaceId || dm?.surfaceId || br?.surfaceId;
  if (!id) return state;
  const surf = state[id] || { components: {}, dataModel: {}, root: null, rendering: false };
  const next = { ...surf };
  if (cs) next.dataModel = {};
  if (uc) {
    const components = { ...next.components };
    for (const c of uc.components || []) {
      if (c?.id) components[c.id] = c;
    }
    next.components = components;
  }
  if (dm) next.dataModel = mergePath(next.dataModel, dm.path || '/', dm.value);
  if (br) {
    next.root = br.root;
    next.rendering = true;
  }
  return { ...state, [id]: next };
}

// ---------- 渲染上下文 ----------

function resolveValue(v, ctx) {
  if (v == null) return undefined;
  if (typeof v === 'object') {
    if (typeof v.path === 'string') {
      const base = ctx.repeat ? resolvePath(ctx.surf.dataModel, ctx.repeat.path) : ctx.surf.dataModel;
      return resolvePath(base, v.path);
    }
    if ('literalString' in v) return v.literalString;
    if ('literalNumber' in v) return v.literalNumber;
    if ('literalBool' in v) return v.literalBool;
    return undefined;
  }
  return v;
}

function setValue(ctx, path, value) {
  const fullPath = ctx.repeat ? `${ctx.repeat.path.replace(/\/$/, '')}/${ctx.repeat.index}${path}` : path;
  ctx.onSetValue(fullPath, value);
}

const GAP_PX = { none: 0, small: 8, medium: 14, large: 20 };

function Render({ id, ctx }) {
  const comp = ctx.surf.components[id];
  if (!comp) return <span className="a2ui-missing">[缺少组件: {id}]</span>;
  const C = CATALOG[comp.component];
  if (!C) return <span className="a2ui-missing">[不支持的组件: {comp.component}]</span>;
  return <C comp={comp} ctx={ctx} />;
}

function Children({ comp, ctx }) {
  const items = Array.isArray(comp.items) ? comp.items : [];
  if (comp.repeated) {
    const arr = resolveValue({ path: comp.repeated.path }, ctx);
    if (!Array.isArray(arr)) return null;
    return arr.map((_, i) => (
      <Render
        key={i}
        id={comp.repeated.componentId}
        ctx={{ ...ctx, repeat: { path: comp.repeated.path, index: i, total: arr.length } }}
      />
    ));
  }
  return items.map((id) => <Render key={id} id={id} ctx={ctx} />);
}

// ---------- 组件目录（Basic Catalog 子集，渲染为原生 React 组件） ----------

const TextComp = ({ comp, ctx }) => {
  const text = resolveValue(comp.text, ctx);
  const variant = comp.variant || 'body';
  return <div className={`a2ui-text a2ui-text-${variant}`}>{text ?? ''}</div>;
};

const ButtonComp = ({ comp, ctx }) => (
  <button
    className={`a2ui-btn a2ui-btn-${comp.variant || 'secondary'}`}
    onClick={() => {
      const name = comp.action?.event?.name || comp.action?.name || 'unknown';
      ctx.dispatch({
        name,
        surfaceId: ctx.surfaceId,
        dataModel: ctx.surf.dataModel,
        fields: collectFields(ctx.surf),
      });
    }}
  >
    {comp.child ? <Render id={comp.child} ctx={ctx} /> : '按钮'}
  </button>
);

/** 收集界面中绑定数据模型的控件（path -> label），用于提交信息的可读展示 */
function collectFields(surf) {
  const fields = [];
  for (const c of Object.values(surf.components || {})) {
    const path = c?.value?.path;
    if (typeof path === 'string' && path.startsWith('/')) {
      fields.push({ path, label: typeof c.label === 'string' ? c.label : path.slice(1) });
    }
  }
  return fields;
}

const FieldShell = ({ label, children }) => (
  <label className="a2ui-field">
    {label && <span className="a2ui-field-label">{label}</span>}
    {children}
  </label>
);

const TextFieldComp = ({ comp, ctx }) => {
  const path = comp.value?.path;
  const raw = path ? resolveValue(comp.value, ctx) : '';
  const isNumber = comp.textualType === 'number';
  const value = raw == null ? '' : String(raw);
  const onChange = (v) => path && setValue(ctx, path, isNumber && v !== '' ? Number(v) : v);
  return (
    <FieldShell label={resolveValue(comp.label, ctx)}>
      {comp.textualType === 'longText' ? (
        <textarea className="a2ui-input" rows={3} placeholder={comp.placeholder} value={value} onChange={(e) => onChange(e.target.value)} />
      ) : (
        <input
          className="a2ui-input"
          type={isNumber ? 'number' : 'text'}
          placeholder={comp.placeholder}
          value={value}
          onChange={(e) => onChange(e.target.value)}
        />
      )}
    </FieldShell>
  );
};

const CheckBoxComp = ({ comp, ctx }) => {
  const path = comp.value?.path;
  const checked = path ? !!resolveValue(comp.value, ctx) : false;
  return (
    <label className="a2ui-check">
      <input type="checkbox" checked={checked} onChange={(e) => path && setValue(ctx, path, e.target.checked)} />
      <span>{resolveValue(comp.label, ctx)}</span>
    </label>
  );
};

/** 选择器：mutuallyExclusive 单选（值为字符串）/ multipleSelection 多选（值为数组），渲染为可点选的 chips */
const ChoicePickerComp = ({ comp, ctx }) => {
  const path = comp.value?.path;
  const multiple = comp.variant === 'multipleSelection';
  const raw = path ? resolveValue(comp.value, ctx) : undefined;
  const selected = multiple ? (Array.isArray(raw) ? raw : []) : raw;
  const toggle = (val) => {
    if (!path) return;
    if (multiple) {
      setValue(ctx, path, selected.includes(val) ? selected.filter((v) => v !== val) : [...selected, val]);
    } else {
      setValue(ctx, path, val);
    }
  };
  return (
    <FieldShell label={resolveValue(comp.label, ctx)}>
      <div className="a2ui-chips">
        {(comp.options || []).map((opt) => {
          const active = multiple ? selected.includes(opt.value) : selected === opt.value;
          return (
            <button
              key={String(opt.value)}
              type="button"
              className={`a2ui-chip${active ? ' active' : ''}`}
              onClick={() => toggle(opt.value)}
            >
              {opt.label}
            </button>
          );
        })}
      </div>
    </FieldShell>
  );
};

const SliderComp = ({ comp, ctx }) => {
  const path = comp.value?.path;
  const num = Number(resolveValue(comp.value, ctx));
  return (
    <FieldShell label={resolveValue(comp.label, ctx)}>
      <div className="a2ui-slider-row">
        <input
          className="a2ui-slider"
          type="range"
          min={comp.minValue ?? 0}
          max={comp.maxValue ?? 100}
          value={Number.isFinite(num) ? num : comp.minValue ?? 0}
          onChange={(e) => path && setValue(ctx, path, Number(e.target.value))}
        />
        <span className="a2ui-slider-value">{Number.isFinite(num) ? num : ''}</span>
      </div>
    </FieldShell>
  );
};

/** 将 ISO 时间串裁剪为 <input> 可用的本地格式 */
function toInputDateTime(v, enableDate, enableTime) {
  const s = String(v || '');
  if (!s) return '';
  const d = new Date(s);
  if (Number.isNaN(d.getTime())) return s;
  const date = `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
  const time = `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`;
  if (enableDate && enableTime) return `${date}T${time}`;
  if (enableDate) return date;
  return time;
}

function fromInputDateTime(v, enableDate, enableTime) {
  if (!v) return '';
  if (enableDate && enableTime) {
    const d = new Date(v);
    return Number.isNaN(d.getTime()) ? v : d.toISOString();
  }
  return v;
}

const DateTimeInputComp = ({ comp, ctx }) => {
  const path = comp.value?.path;
  const enableDate = comp.enableDate !== false;
  const enableTime = !!comp.enableTime;
  const type = enableDate && enableTime ? 'datetime-local' : enableDate ? 'date' : 'time';
  const raw = path ? resolveValue(comp.value, ctx) : '';
  return (
    <FieldShell label={resolveValue(comp.label, ctx)}>
      <input
        className="a2ui-input"
        type={type}
        value={toInputDateTime(raw, enableDate, enableTime)}
        onChange={(e) => path && setValue(ctx, path, fromInputDateTime(e.target.value, enableDate, enableTime))}
      />
    </FieldShell>
  );
};

const CardComp = ({ comp, ctx }) => (
  <div className="a2ui-card">
    {comp.title && <div className="a2ui-card-title">{resolveValue(comp.title, ctx)}</div>}
    {comp.description && <div className="a2ui-card-desc">{resolveValue(comp.description, ctx)}</div>}
    {comp.child && <Render id={comp.child} ctx={ctx} />}
  </div>
);

const ColumnComp = ({ comp, ctx }) => (
  <div
    className="a2ui-column"
    style={{ gap: GAP_PX[comp.gap] ?? GAP_PX.medium, alignItems: comp.align === 'center' ? 'center' : 'stretch' }}
  >
    <Children comp={comp} ctx={ctx} />
  </div>
);

const RowComp = ({ comp, ctx }) => (
  <div
    className="a2ui-row"
    style={{
      gap: GAP_PX[comp.gap] ?? GAP_PX.small,
      justifyContent: comp.align === 'space-between' ? 'space-between' : 'flex-start',
    }}
  >
    <Children comp={comp} ctx={ctx} />
  </div>
);

const ImageComp = ({ comp, ctx }) => {
  const url = resolveValue(comp.url, ctx);
  if (!url) return null;
  return <img className="a2ui-image" src={url} alt={resolveValue(comp.description, ctx) || ''} loading="lazy" />;
};

const DividerComp = () => <hr className="a2ui-divider" />;

const CATALOG = {
  Text: TextComp,
  Button: ButtonComp,
  TextField: TextFieldComp,
  CheckBox: CheckBoxComp,
  ChoicePicker: ChoicePickerComp,
  Slider: SliderComp,
  DateTimeInput: DateTimeInputComp,
  Card: CardComp,
  Column: ColumnComp,
  Row: RowComp,
  List: Children,
  Image: ImageComp,
  Divider: DividerComp,
};

// ---------- 对外组件 ----------

/**
 * A2UISurface：消费流式累积的模型输出文本
 * @param text    助手消息的完整文本（持续更新）
 * @param streaming 是否仍在流式生成中
 * @param onEvent 界面事件回调（点击按钮时触发）
 */
export default function A2UISurface({ text, streaming, onEvent }) {
  const { msgs, prose } = parseOutput(text);
  const [surfaces, setSurfaces] = useState(initialSurfaces);

  // 模型输出变化（流式增量）时，重放全部消息重建 surface 状态
  useEffect(() => {
    setSurfaces(msgs.reduce(applyMessage, initialSurfaces));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [text]);

  const rendered = Object.entries(surfaces).filter(([, s]) => s.rendering && s.root && s.components[s.root]);
  const [surfaceId, surf] = rendered.length > 0 ? rendered[rendered.length - 1] : [];

  return (
    <div className="a2ui-wrap">
      {prose && (
        <div className="markdown">
          <ReactMarkdown remarkPlugins={[remarkGfm]}>{prose}</ReactMarkdown>
        </div>
      )}
      {surfaceId ? (
        <div className="a2ui-surface">
          <div className="a2ui-badge">⚡ A2UI 动态界面</div>
          <Render
            id={surf.root}
            ctx={{
              surf,
              surfaceId,
              onSetValue: (path, value) =>
                setSurfaces((s) => ({
                  ...s,
                  [surfaceId]: { ...s[surfaceId], dataModel: setPath(s[surfaceId].dataModel, path, value) },
                })),
              dispatch: onEvent,
            }}
          />
        </div>
      ) : (
        msgs.length > 0 && streaming && <div className="a2ui-loading">正在生成界面…</div>
      )}
    </div>
  );
}
