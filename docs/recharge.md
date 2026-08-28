# 充话费场景实现文档

> 模式标识：`recharge` ｜ 后端入口：`POST /api/recharge/chat` ｜ 核心模块：`server/recharge.py`
> 场景性质：**演示环境**，话费数据与充值下单均为模拟数据，无真实扣费，免凭据开箱即用

## 1. 功能概述

充话费场景演示了一个完整的「查询 → 表单收集 → 预览确认 → 下单 → 状态跟踪」交易类 agent 流程：

- 📱 查询手机号话费余额、归属地、运营商（附余额卡片）
- 💰 获取可充值面值档位与优惠售价（30 / 50 / 100 / 200 / 300 / 500 元）
- 📝 模型生成 A2UI 充值表单收集手机号、面值、支付方式
- 🔒 费用预览 + 用户明确确认的**两步门控**后才提交充值
- 🧾 充值结果卡片（订单号 / 应付金额 / 状态），订单 30 秒后由「充值中」流转为「充值成功」

## 2. 整体架构

与其他场景（出行助手 / 美团酒旅 / 跑腿 / 天气）同款三层结构，复用 `main.py` 的通用 agent 循环 `agent_stream`：

```
用户输入
  │
  ▼
client/src/App.jsx (mode='recharge')
  │  POST /api/recharge/chat  { model, messages }
  ▼
server/main.py  recharge_chat()
  │  system = build_a2ui_system_prompt() + recharge.system_prompt()
  ▼
agent_stream(body, recharge, system)          ← 通用 agent 循环（SSE）
  │  ① 携带 recharge.tools 请求网关 /v1/messages
  │  ② 解析 tool_use → recharge.execute_tool(name, args)
  │  ③ 回写 tool_result 继续循环，直至模型给出最终回答
  ▼
server/recharge.py  RechargeService           ← 模拟运营商数据 + 工具执行器
  │
  ├─ 给模型的结构化数据（tool_result）
  └─ 给前端的卡片（SSE 事件 recharge_card）
```

职责边界：**模型只决定「何时调用、传什么参数」**；服务端真正执行并回写结果；前端按事件渲染卡片与 A2UI 表单。

## 3. 后端实现（server/recharge.py）

### 3.1 模块结构

```
recharge.py
├── 常量        CARRIER_PREFIXES / CITIES / FACE_VALUES / SETTLE_SECONDS / PHONE_PATTERN
├── 模拟数据    _stable_num / mask_phone / _validate_phone / _phone_info / _plan / _new_order_no
├── 工具定义    RECHARGE_TOOLS + TOOL_LABELS（前端工具 chip 中文名）
├── 系统提示词  build_recharge_system_prompt()
└── 服务类      RechargeService（execute_tool 分发 + summarize_result 摘要）
```

`RechargeService` 实现 `agent_stream` 约定的接口：`tools` / `MAX_TOOL_ROUNDS`（=8）/ `configured`（恒 True）/ `system_prompt` / `tool_label` / `card_event_type`（返回 `'recharge_card'`）/ `execute_tool` / `summarize_result`。

### 3.2 工具定义（RECHARGE_TOOLS）

| 工具 | 入参 | 行为 | 返回卡片 |
|------|------|------|----------|
| `recharge_query` | `phone` | 校验手机号，返回归属地/运营商/余额/状态 | `kind=balance` 余额卡片 |
| `recharge_plans` | `phone` | 返回 6 个档位的面值、优惠、售价 | 无（供表单 ChoicePicker 使用） |
| `recharge_order` | `phone`, `face_value`, `confirm` | `confirm=false` 预览明细；`confirm=true` 提交生成订单 | 预览无卡片；提交返回 `kind=result` |
| `recharge_order_status` | `order_no` | 查询订单状态（内存记账） | `kind=result` 订单卡片 |

所有工具执行均返回二元组 `(给模型的数据, 给前端的卡片)`；异常在 `execute_tool` 兜底为 `{'error': ...}`，避免中断 agent 循环。

### 3.3 模拟数据设计

核心原则：**确定性**——同一手机号多次查询结果一致，保证多轮对话数据自洽。

| 数据 | 生成规则 |
|------|----------|
| 运营商 | 真实号段规律（`CARRIER_PREFIXES`：134-139 等→移动，130-156 等→联通，133-199 等→电信），未知号段兜底「移动」 |
| 归属城市 | `md5(手机号) % len(CITIES)`，从 10 城池（北京/上海/…/重庆）中选取，**模拟数据** |
| 余额 | `3.5 + md5(手机号) % 7000 / 100` → 3.50 ~ 73.49 元；< 10 元时状态提示「余额偏低，建议充值」 |
| 档位售价 | `md5(手机号:面值) % 6 × 0.5` → 立减 0 ~ 2.5 元，`price = 面值 - 优惠` |
| 订单号 | `RC + YYYYMMDDHHMMSS + 毫秒`（`_new_order_no`） |
| 到账 | 订单写入内存 `_ORDERS`；查询时 `now - created_at ≥ SETTLE_SECONDS(30s)` 即流转「充值成功」 |

### 3.4 两步确认门控（安全设计）

充值是消费行为，与美团跑腿下单同款门控，由**系统提示词 + 工具协议**双重保障：

1. 模型必须先 `recharge_order` 且 `confirm=false` 预览，拿到充值明细；
2. 生成 A2UI 费用确认表单（逐行展示号码/归属地/余额/面值/优惠/应付/支付方式 + 「确认充值」「取消」按钮）；
3. 用户点击「确认充值」（或明确文字回复）后才以**与预览完全一致**的参数 `confirm=true` 提交；
4. 单笔应付 > 200 元额外提醒；手机号缺失/格式非法（`^1[3-9]\d{9}$`）必须询问，不得编造；
5. 服务端二次校验：面值必须是 `FACE_VALUES` 档位，手机号必须合法。

### 3.5 系统提示词要点

`build_recharge_system_prompt()` 分五段：

- **输出规范**：禁展示英文字段名/JSON；手机号脱敏（`138****5678`）；金额到分；说明演示性质
- **安全门控**：上述两步确认流程
- **工作流程**：查余额 → 取档位 → A2UI 充值表单（`submit_recharge`）→ 预览 → 确认表单（`confirm_recharge` / `cancel_recharge`）→ 提交 → 查单
- **参数规范**：面值取整数元；ChoicePicker 的 label 与 value 均用中文（避免摘要出现 `alipay` 类英文值）
- **降级路径**：用户偏好纯文字交流时按文字流程（门控不变）

## 4. 服务端接入（server/main.py）

```python
from recharge import RechargeService
recharge = RechargeService()

@app.post('/api/recharge/chat')
async def recharge_chat(request: Request):
    # 常规校验（messages 非空 + 网关配置）后：
    system = build_a2ui_system_prompt() + '\n\n' + recharge.system_prompt()
    return agent_stream(body, recharge, system)
```

叠加 A2UI 协议提示词是关键——它约束模型按 A2UI v0.9 协议输出 `createSurface / updateComponents / updateDataModel / beginRendering` 四段消息，前端才能渲染出可交互表单。

## 5. 前端实现（client/src/App.jsx）

### 5.1 模式接入

```jsx
const agentLike = mode === 'agent' || ... || mode === 'recharge';
const a2uiLike  = mode === 'a2ui' || mode === 'recharge';
```

`recharge` 是项目中**唯一同时属于两个集合**的模式：

- `agentLike`：显示工具调用 chip（调用中/完成+摘要）与结构化卡片
- `a2uiLike`：回复正文用 `A2UISurface` 渲染（A2UI 表单可点击交互），而非普通 Markdown

> 注意：美团跑腿模式虽也注入 A2UI 提示词，但未加入 `a2uiLike`，其表单以代码块形式展示、不可交互——新增交互场景应参照 recharge 的双标记做法。

### 5.2 卡片渲染（RechargeCard）

SSE 事件 `recharge_card` → `msg.rechargeCard` → `<RechargeCard />`，按 `card.kind` 分两种视图：

| kind | 视图 | 字段 |
|------|------|------|
| `balance` | 余额大数字 + 状态角标 + 号码/运营商/归属地 | `phone` `carrier` `city` `balance` `status`（余额偏低时 `warn` 橙色） |
| `result` | 订单明细行（号码/面值/应付/订单号/状态）+ 到账提示 | `order_no` `phone` `face_value` `payable` `status`（`ok` 绿色 / `pending` 橙色）`eta` |

卡片底部固定脚注「演示数据 · 模拟运营商话费服务，无真实扣费」。样式类 `rc-*`（index.css），复用 `amap-card` 容器与主题变量（`--panel-strong` / `--border` / `--text-dim`），深浅主题自适应。

### 5.3 A2UI 表单交互

- 模型输出的 A2UI 消息由通用组件 [A2UISurface.jsx](../client/src/A2UISurface.jsx) 解析渲染（组件树 + 数据模型 + 原生控件）
- 用户点击按钮 → `handleA2UIEvent` → 以 `[A2UI_EVENT] <surfaceId>.<事件名> <dataModel JSON>` 回传后端继续流程
- 事件约定：充值表单 `submit_recharge`；确认表单 `confirm_recharge` / `cancel_recharge`

### 5.4 提交事件的文本化展示

表单提交与按钮确认**不以特殊 UI 展示**，统一转为普通用户文本气泡：

```jsx
const A2UI_EVENT_LABELS = {
  submit_recharge: '提交充值表单',
  confirm_recharge: '确认充值',
  cancel_recharge: '取消充值',
};
function buildA2uiEventText(msg) {
  // 表单提交：'提交充值表单：充值手机号 138****8000，充值面值 30，支付方式 支付宝'
  // 按钮确认（无 dataModel）：'确认充值'
}
```

`handleA2UIEvent` 发送时在 extra 中记录 `a2uiEventName`，渲染时由 `buildA2uiEventText` 生成可读文本；发往后端的 `content` 仍为原始事件串，不影响模型流程。

## 6. 端到端交互流程

```
用户：帮我给 13800138000 充 100 元话费
  │
  ├─① recharge_query  → 🔧 chip「余额查询 · 余额 72.84 元」 + 余额卡片
  ├─② recharge_plans  → 🔧 chip「充值档位 · 6 个档位」
  ├─③ 模型输出 A2UI 充值表单（手机号预填、面值预选 100 元、支付方式）
  ▼
用户：填写并点击「提交充值」
  │  聊天流显示文本：提交充值表单：充值手机号 138****8000，充值面值 100，支付方式 支付宝
  ├─④ recharge_order(confirm=false) → 🔧 chip「充值下单 · 预览」
  ├─⑤ 模型输出 A2UI 费用确认表单（明细 + 确认充值/取消）
  ▼
用户：点击「确认充值」
  │  聊天流显示文本：确认充值
  ├─⑥ recharge_order(confirm=true) → 🔧 chip「充值下单 · 已提交」 + 订单卡片（充值中）
  └─⑦ 模型正文：充值已提交，订单号 RC2026…，预计 10 分钟内到账

用户：到账了吗？
  └─⑧ recharge_order_status → 超过 30 秒后返回「充值成功」，卡片状态变绿
```

## 7. 接口与数据结构

### 7.1 SSE 事件（`POST /api/recharge/chat` 响应流）

| 事件 | 说明 |
|------|------|
| `message_start` / `content_block_delta` / `message_stop` 等 | 网关事件原样透传（正文流式渲染） |
| `tool_call` | `{type, name, label, status: calling\|done, summary}` 工具 chip |
| `recharge_card` | `{type, card}` 结构化卡片 |
| `agent_done` | 终止帧（防止客户端自动重连） |

### 7.2 卡片结构

```jsonc
// kind=balance 余额卡片
{ "kind": "balance", "title": "话费余额查询",
  "phone": "138****8000", "carrier": "移动", "city": "杭州",
  "balance": 72.84, "status": "正常" }

// kind=result 订单卡片
{ "kind": "result", "title": "充值提交成功",
  "order_no": "RC20260828142209495", "phone": "138****8000",
  "face_value": 100, "payable": 99.0,
  "status": "充值中", "eta": "预计 10 分钟内到账" }
```

## 8. 如何接入真实充值 API

演示服务预留了清晰的替换点，接入聚合充值平台（需企业资质与密钥）时：

1. **替换执行器**：`_exec_query / _exec_plans / _exec_order / _exec_order_status` 内改为真实 HTTP 调用（参照 `weather.py` 的 httpx 用法），保持返回结构 `(data, card)` 不变；
2. **凭据管理**：`configured` 改为校验环境变量（如 `RECHARGE_API_KEY`，仅存 `server/.env`）；`main.py` 端点仿照 `agent_chat` 在未配置时返回 500 提示；
3. **模拟数据开关**：`_phone_info / _plan / _ORDERS` 相关逻辑可保留为降级兜底，或删除；
4. **文案调整**：系统提示词删除「演示模拟数据」声明，卡片脚注 `rc-demo` 移除；真实下单的确认门控**必须保留**。

## 9. 已知限制

- 订单存储在进程内存（`_ORDERS`），服务重启后订单丢失，多进程/多实例部署不共享；
- 归属城市为模拟数据（号段→运营商规律真实），不接真实号码归属地库；
- A2UI 表单质量依赖模型遵循协议（组件 id 引用、JSON 合法性），系统提示词中已有自检约束；
- `MAX_TOOL_ROUNDS = 8`，正常流程（查询→档位→表单→预览→提交）约消耗 4~6 轮。
