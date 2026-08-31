# 外卖点餐场景实现文档

> 模式标识：`waimai` ｜ 后端入口：`POST /api/waimai/chat` ｜ 核心模块：`server/waimai.py`
> 场景性质：**演示环境**，商家、菜单与下单均为模拟数据，无真实交易与配送，免凭据开箱即用
> 交互参考：**千问 App 外卖点餐**（图1 商品列表「选这个」→ 图2 规格弹窗改参数 →「选好了」发送大模型处理，后续费用预览与确认走对话）

## 1. 功能概述

外卖点餐场景演示了一个完整的「搜商家 → 弹窗选规格 → 发送大模型 → 表单收集 → 预览确认 → 下单 → 状态跟踪」交易类 agent 流程：

- 🍔 按品类（奶茶/汉堡/面食/烧烤等）或店名关键词搜索附近商家，以**横滑商品卡**展示（店名/标签/招牌菜图块/预估价/「进店选购」「选这个」按钮）
- 🍜 查询指定商家菜单（菜品/价格/月售/可选规格），部分招牌菜带**规格组**（份量/辣度/自选饮料等，选项可加价）
- 🪟 商品卡「选这个」弹出**规格弹窗**（对齐千问底部弹层：选项块单选/加价标注/数量步进，预估价随规格联动），点「选好了」把商品+规格+数量组装为用户消息**发送大模型处理**
- 📝 模型生成 A2UI **规格选择表单**（打字点餐时）与点餐表单（配送地址、联系电话、备注）；弹窗带来的消息已含规格参数，模型直接进入地址/电话确认环节
- 🔒 费用预览（菜品小计/餐盒费/配送费/满减/合计）+ 用户明确确认的**两步门控**后才提交订单
- 🧾 订单卡片对齐千问订单确认页明细行（送达时间/配送至/备注/其他费用/满减/总计），状态随时间流转：备餐中(30s) → 配送中(90s) → 已送达

## 2. 整体架构

与充话费/跑腿/天气等场景同款三层结构，复用 `main.py` 的通用 agent 循环 `agent_stream`：

```
用户输入（打字，或商品卡弹窗「选好了」/「进店选购」组装的消息）
  │
  ▼
client/src/App.jsx (mode='waimai')
  │  POST /api/waimai/chat  { model, messages }
  ▼
server/main.py  waimai_chat()
  │  system = build_a2ui_system_prompt() + waimai.system_prompt()
  ▼
agent_stream(body, waimai, system)            ← 通用 agent 循环（SSE）
  │  ① 携带 waimai.tools 请求网关 /v1/messages
  │  ② 解析 tool_use → waimai.execute_tool(name, args)
  │  ③ 回写 tool_result 继续循环，直至模型给出最终回答
  ▼
server/waimai.py  WaimaiService               ← 模拟商家/菜单数据 + 工具执行器
  │
  ├─ 给模型的结构化数据（tool_result）
  └─ 给前端的卡片（SSE 事件 waimai_card）
```

职责边界：**模型只决定「何时调用、传什么参数」**；服务端真正执行并回写结果；前端按事件渲染卡片与 A2UI 表单。

## 3. 后端实现（server/waimai.py）

### 3.1 模块结构

```
waimai.py
├── 常量        PHONE_PATTERN / PREP_SECONDS / DELIVERED_SECONDS / DEALS /
│              PACKING_PER_ITEM / PACKING_CAP / SHOPS（12 家模拟商家与菜单，
│              含 icon 占位图标 / signature 招牌推荐语 / 部分菜品 specs 规格组）
├── 模拟数据    _stable_num / mask_phone / _rating_word / _dish_out /
│              _shop_summary / _dish_sales / _search_shops / _validate_items /
│              _order_amounts / _items_summary / _new_order_no /
│              _order_stage / _order_card
├── 工具定义    WAIMAI_TOOLS + TOOL_LABELS（前端工具 chip 中文名）
├── 系统提示词  build_waimai_system_prompt()
└── 服务类      WaimaiService（execute_tool 分发 + summarize_result 摘要）
```

`WaimaiService` 实现 `agent_stream` 约定的接口：`tools` / `MAX_TOOL_ROUNDS`（=8）/ `configured`（恒 True）/ `system_prompt` / `tool_label` / `card_event_type`（返回 `'waimai_card'`）/ `execute_tool` / `summarize_result`。

### 3.2 工具定义（WAIMAI_TOOLS）

| 工具 | 入参 | 行为 | 返回卡片 |
|------|------|------|----------|
| `waimai_search_shops` | `keyword`（可空） | 按品类/店名/菜品名匹配商家，空关键词返回推荐商家；附招牌菜（名称/推荐语/预估价） | `kind=shops` 横滑商家商品卡 |
| `waimai_menu` | `shop_id`（商家 id **或店名**） | 返回该商家全部菜品（名称/价格/月售/图标/规格组 specs）；店名直查免搜索 | `kind=menu` 菜单卡片 |
| `waimai_order` | `shop_id`（商家 id **或店名**）, `items[]`(含 `specs?`), `address`, `phone`, `remark?`, `confirm` | `confirm=false` 预览金额明细；`confirm=true` 提交生成订单 | 预览无卡片；提交返回 `kind=result` |
| `waimai_order_status` | `order_no` | 查询订单状态（内存记账 + 时间流转） | `kind=result` 订单卡片 |

所有工具执行均返回二元组 `(给模型的数据, 给前端的卡片)`；异常在 `execute_tool` 兜底为 `{'error': ...}`，避免中断 agent 循环。

> **shop_id 双解析**：`waimai_menu`/`waimai_order` 的 shop_id 由 `_resolve_shop()` 解析，先按商家 id 后按店名（均精确匹配）——用户消息/弹窗已明确店名时模型可直接传店名，无需先 `waimai_search_shops`，省去一轮工具调用；找不到时返回错误引导搜索。

### 3.3 模拟数据设计

核心原则：**确定性**——同一商家多次查询结果一致，保证多轮对话数据自洽。

| 数据 | 生成规则 |
|------|----------|
| 商家与菜单 | 预置 12 家虚构商家（`SHOPS`：面食/汉堡炸鸡/麻辣烫/粥粉/奶茶咖啡/烧烤/饺子/轻食/快餐/云吞/川湘菜/黄焖鸡），每家 6 道菜；每家带 `icon` 占位图标（无真实图片，前端渲染为渐变图块）与 `signature` 招牌推荐语 |
| 规格组 | 部分招牌菜带 `specs`（如黄焖鸡米饭：份量[小份/大份+3] / 辣度[不辣/微辣/中辣/特辣] / 自选饮料[不需要/可乐/怡宝水/加多宝+2]；珍珠奶茶：甜度/温度/杯型；麻辣烫：份量/辣度；鸡腿堡：套餐/辣度），选项可带 `extra` 加价 |
| 评分 | `4.0 + md5(shop_id) % 10 / 10` → 4.0 ~ 4.9，附「超棒/很好/好」描述词 |
| 月售 | `100 + md5(shop_id) 位移取模 % 2900`；菜品月售 `30 + md5(shop_id:菜名) % 300` |
| 起送价/配送费/时长 | 起送 15/20/25 元、配送费 2~5 元、配送时长 25~44 分钟，均由 shop_id 位移取模生成 |
| 满减 | 从 `DEALS`（满20减3 / 满30减5 / 满35减6 / 满50减10）按 shop_id 选取，菜品小计达门槛才生效 |
| 餐盒费 | `每份 1 元 × 总份数`，封顶 6 元 |
| 金额 | 服务端以**菜单价格 + 规格加价**重算（`_order_amounts`），不信任模型传入的价格 |
| 订单号 | `WM + YYYYMMDDHHMMSS + 毫秒`（`_new_order_no`） |
| 送达时间 | 下单时计算 `立即配送，预计 {now+30min 的 HH:MM} 送达`（`arrival`，卡片与正文共用） |
| 状态流转 | 订单写入内存 `_ORDERS`；查询时按 `now - created_at`：< 30s 商家备餐中，< 90s 骑手配送中，≥ 90s 已送达 |

### 3.4 服务端校验（防模型编造）

- `_validate_items`：购买清单非空；每个菜品名必须在该商家菜单中；数量必须为正整数；规格选项必须取菜单 specs 的选项原文且每组至多一项（同组两项/未知选项/无规格菜传规格均报错并提示重新选择）；价格一律以**菜单基础价 + 规格加价**为准；
- 地址缺失、电话不合法（`^1[3-9]\d{9}$`）直接返回错误信息，模型据此生成点餐信息表单让用户重新填写；
- 商家 id 不存在时提示重新搜索。

### 3.5 两步确认门控（安全设计）

点餐是消费行为，与充值/跑腿下单同款门控，由**系统提示词 + 工具协议**双重保障：

1. 模型必须先 `waimai_order` 且 `confirm=false` 预览，拿到金额明细；
2. 生成 A2UI 费用确认表单（逐行展示商家/菜品清单/餐盒费/配送费/满减/合计/地址/电话/备注 + 「确认下单」「取消」按钮）；
3. 用户点击「确认下单」（或明确文字回复）后才以**与预览完全一致**的参数 `confirm=true` 提交；
4. 单笔合计 > 100 元额外提醒；地址/电话缺失时生成点餐信息表单（A2UI 地址/电话/备注输入框）收集，不得编造；
5. 服务端二次校验：菜品与价格以菜单为准，地址与电话必须合法。

### 3.6 系统提示词要点

`build_waimai_system_prompt()` 分六步工作流程（文案风格对齐千问）：

- **输出规范**：禁展示英文字段名/JSON；电话脱敏（`138****5678`）；金额到分；说明演示性质
- **安全门控**：两步确认流程；地址/电话缺失时必须生成点餐信息表单（A2UI）收集，不得文字追问或编造；对话中已有地址电话时优先复用并复述确认；弹窗消息已含完整规格时跳过规格表单，直接按 ③ 处理地址电话
- **工作流程**：① 搜商家（引导语 + 简评）→ ② 查菜单；带 specs 的菜品生成**规格选择表单**（每组一个 ChoicePicker，提交按钮 `submit_waimai_order` 文案"选好了"）→ ③ 若缺地址/电话生成**点餐信息表单**（地址/电话/备注 TextField，提交按钮 `submit_waimai_order` 文案"提交"）→ ④ 收到表单提交后组装 items 与收货信息预览 → ⑤ 费用确认表单（`confirm_waimai` / `cancel_waimai`）→ ⑥ 提交（正文参考"好的，已为你选好商品并提交订单，共优惠 X 元，预计 30 分钟送达。"）→ 查单
- **参数规范**：items 的 name/price 必须取菜单返回值；specs 必须取选项原文（不含加价后缀），每组至多一项；ChoicePicker 的 label 与 value 均用中文
- **降级路径**：用户偏好纯文字交流时按文字流程（门控不变）

## 4. 服务端接入（server/main.py）

### 4.1 LLM 流程端点

```python
from waimai import WaimaiService
waimai = WaimaiService()

@app.post('/api/waimai/chat')
async def waimai_chat(request: Request):
    # 常规校验（messages 非空 + 网关配置）后：
    system = build_a2ui_system_prompt() + '\n\n' + waimai.system_prompt()
    return agent_stream(body, waimai, system)
```

叠加 A2UI 协议提示词是关键——它约束模型按 A2UI v0.9 协议输出 `createSurface / updateComponents / updateDataModel / beginRendering` 四段消息，前端才能渲染出可交互表单。

## 5. 前端实现（client/src/App.jsx）

### 5.1 模式接入

```jsx
const agentLike = mode === 'agent' || ... || mode === 'recharge' || mode === 'waimai';
const a2uiLike  = mode === 'a2ui' || mode === 'recharge' || mode === 'paotui' || mode === 'waimai';
```

`waimai` 与 `recharge`/`paotui` 同时属于两个集合（agent 场景 + A2UI 表单交互）：

- `agentLike`：显示工具调用 chip（调用中/完成+摘要）与结构化卡片
- `a2uiLike`：回复正文用 `A2UISurface` 渲染（A2UI 表单可点击交互），而非普通 Markdown

### 5.2 卡片渲染（WaimaiCard）与规格弹窗

SSE 事件 `waimai_card` → `msg.waimaiCard` → `<WaimaiCard card={...} onBrowse={(q) => send(q)} />`，按 `card.kind` 分三种视图（对齐千问 App）：

| kind | 视图 | 字段 |
|------|------|------|
| `shops` | **横滑商品卡**（图1）+ 点击「选这个」弹出的**规格弹窗** | `shops[]`：`id` `name` `icon` `category` `rating` `rating_word` `min_order` `delivery_time` `signature_name` `signature_desc` `signature_price` `signature_specs`（招牌菜规格组，弹窗用）等 |
| `menu` | 商家信息行（含满减）+ 菜品列表（图标/名称/可选规格组提示/月售/价格） | `shop_name` `icon` `category` `rating` `deal` `dishes[]`：`name` `price` `monthly_sales` `icon` `specs[]`（`group` + `options[]`：`label`/`extra`） |
| `result` | 商家行 + 菜品摘要块 + 明细行（送达时间/配送至/联系电话/备注/其他费用/满减/总计高亮/状态/订单号） | `order_no` `shop_name` `icon` `items_summary` `arrival` `address` `phone` `remark` `packing_fee` `delivery_fee` `discount` `total` `status`（`ok` 绿色 / `pending` 橙色）`eta` |

**下单流程消息不再回显商家/菜品卡片**：同一条消息若含 `waimai_order` 工具调用（预览/提交），前端不渲染 `shops`/`menu` 卡（`result` 订单卡不受影响），防止模型误重复搜索时商家卡闪现；提示词侧同时约束进入下单流程后不得再调 `waimai_search_shops`/`waimai_menu`（shop_id 与菜单从历史工具结果取）。

**规格弹窗**（对应千问图2 底部弹层，`wm-modal` 遮罩 + `wm-sheet` 底部弹层，点遮罩/✕ 关闭）：

- 商品图块 + 店名·已选摘要 + 预估到手价（**随规格联动**：基础价 + 各组加价，有加价时划线显原价）
- 每个规格组一组选项块（`wm-opt`，单选，选中高亮，加价项标 `（+N 元）`），每组默认选第一项；无规格菜弹窗仅含数量
- 数量步进器（1~9）；底部「选好了」按钮

**「选好了」→ 发送大模型处理**（`confirmSheet`）：把商品+规格+数量组装为一条用户消息（如 `我要点「一品黄焖鸡米饭」的黄焖鸡米饭（大份/中辣/加多宝）×2`），关闭弹窗后经 `onBrowse` 调 `send()` 进入对话流程——模型收到完整规格参数后不再生成规格表单，缺地址/电话时生成点餐信息表单收集，随后预览，费用预览与两步确认门控照常生效。

**「进店选购」仍走对话**：通过 `onBrowse` 直接调用 `send()`，以用户消息（如 `看看「一品黄焖鸡米饭」的菜单`）触发 LLM 菜单流程——与千问"点击卡片继续对话"的交互一致。

卡片底部固定脚注「演示数据 · 模拟外卖点餐服务，无真实交易」。样式类 `wm-*`（index.css）：横滑容器 `wm-scroll`、商品卡 `wm-shop-card`、图块 `wm-media`（渐变背景 + icon）、弹窗 `wm-modal`/`wm-sheet`（底部滑入动画）、规格组 `wm-spec-*` + 选项块 `wm-opt` + 数量 `wm-qty`；订单明细行复用 `rc-rows` / `rc-row`，总计高亮 `wm-total`。深浅主题自适应。

### 5.3 A2UI 表单交互与事件文本化

- 模型输出的 A2UI 消息由通用组件 [A2UISurface.jsx](../client/src/A2UISurface.jsx) 解析渲染；规格选择表单的 ChoicePicker 渲染为可点选的选项块，对应千问底部弹层的规格选择（份量/辣度/自选饮料）
- 用户点击按钮 → `handleA2UIEvent` → 以 `[A2UI_EVENT] <surfaceId>.<事件名> <dataModel JSON>` 回传后端继续流程
- 事件约定：规格表单与点餐信息表单（地址/电话/备注）共用 `submit_waimai_order`（规格表单文案"选好了"，点餐信息表单文案"提交"）；确认表单 `confirm_waimai` / `cancel_waimai`
- 表单提交与按钮确认统一转为普通用户文本气泡（`A2UI_EVENT_LABELS` + `buildA2uiEventText`），无特殊卡片 UI

## 6. 端到端交互流程

### 6.1 弹窗选参数 → 发送大模型（推荐，对齐千问交互）

```
用户：我想吃黄焖鸡
  │
  ├─① LLM: waimai_search_shops("黄焖鸡") → 🔧 chip「商家搜索 · 1 家商家」 + 横滑商品卡（图1）
  │   正文：已为你找到附近 1 家黄焖鸡米饭店铺…你可以直接告诉我想选哪家店，或者需要我帮你推荐一下？
  ▼
用户：点击「选这个」→ 弹出规格弹窗（图2，底部弹层）
  │   选规格：大份（+3 元）/ 中辣 / 加多宝（+2 元），数量 x2，预估价 ¥20 → ¥25
  ▼
用户：点击「选好了」
  │   组装用户消息发送大模型：我要点「一品黄焖鸡米饭」的黄焖鸡米饭（大份/中辣/加多宝）×2
  │   （提示词约定：消息已含完整规格参数，模型不再生成规格表单）
  ├─② 对话中缺地址/电话 → 模型输出 A2UI 点餐信息表单（配送地址/联系电话/备注输入框 + "提交"按钮）
  ▼
用户：填写地址电话点击「提交」
  │   聊天流显示文本：提交点餐表单：配送地址 示例路88号3栋502室，联系电话 138****5678…
  ├─③ waimai_order(confirm=false) → 🔧 chip「外卖下单 · 预览」
  ├─④ 模型输出 A2UI 费用确认表单（图3：小计 ¥50 · 打包费 ¥2 · 配送费 ¥3 · 满减 -¥5 · 总计 ¥50 + 确认下单/取消）
  ▼
用户：点击「确认下单」
  ├─⑤ waimai_order(confirm=true) → 🔧 chip「外卖下单 · 已提交」 + 订单卡片（商家备餐中）
  └─⑥ 模型正文：好的，已为你选好商品并提交订单，共优惠 5 元，预计 30 分钟送达。
```

### 6.2 LLM 对话流程（打字点餐 / 进店选购）

```
用户：帮我点一份黄焖鸡米饭（或点击商品卡「进店选购」）
  │
  ├─① waimai_search_shops("黄焖鸡") → 🔧 chip「商家搜索 · 1 家商家」 + 横滑商家商品卡
  │   正文：已为你找到附近 1 家黄焖鸡米饭店铺…你可以直接告诉我想选哪家店，或者需要我帮你推荐一下？
  ▼
用户：点击卡片上的「进店选购」（或直接说"选一品黄焖鸡米饭"）
  │
  ├─② waimai_menu(shop_012) → 🔧 chip「菜单查询 · 5 道菜品」 + 菜单卡片（含可选规格提示）
  ├─③ 模型输出 A2UI 规格选择表单（份量/辣度/自选饮料，ChoicePicker 选项块，"选好了"按钮）
  ▼
用户：选好规格点击「选好了」（提交点餐表单）
  │  聊天流显示文本：提交点餐表单：份量 大份，辣度 不辣，自选饮料 可乐…
  ├─④ 模型补充询问配送地址与电话（首次）或复用已有 → waimai_order(confirm=false) → 🔧 chip「外卖下单 · 预览」
  ├─⑤ 模型输出 A2UI 费用确认表单（明细 + 确认下单/取消）
  ▼
用户：点击「确认下单」
  │  聊天流显示文本：确认下单
  ├─⑥ waimai_order(confirm=true) → 🔧 chip「外卖下单 · 已提交」 + 订单卡片（商家备餐中）
  └─⑦ 模型正文：好的，已为你选好商品并提交订单，共优惠 X 元，预计 30 分钟送达。

用户：我的外卖到哪了？
  └─⑧ waimai_order_status → 30~90 秒返回「骑手配送中」，90 秒后「已送达」，卡片状态变绿
```

## 7. 接口与数据结构

### 7.1 SSE 事件（`POST /api/waimai/chat` 响应流）

| 事件 | 说明 |
|------|------|
| `message_start` / `content_block_delta` / `message_stop` 等 | 网关事件原样透传（正文流式渲染） |
| `tool_call` | `{type, name, label, status: calling\|done, summary}` 工具 chip |
| `waimai_card` | `{type, card}` 结构化卡片 |
| `agent_done` | 终止帧（防止客户端自动重连） |

### 7.2 卡片结构

```jsonc
// kind=shops 横滑商家商品卡（对齐千问：招牌菜图块 + 预估价 + 选这个；signature_specs 供规格弹窗用）
{ "kind": "shops", "title": "「奶茶」附近商家",
  "shops": [{ "id": "shop_005", "name": "茶语鲜奶茶饮", "category": "奶茶咖啡",
               "icon": "🧋", "rating": 4.6, "rating_word": "很好", "monthly_sales": 1580,
               "min_order": 20, "delivery_fee": 3, "delivery_time": 32,
               "deal": "满 30 减 5", "deal_threshold": 30, "deal_discount": 5,
               "signature_name": "珍珠奶茶", "signature_desc": "珍珠 Q 弹奶茶香浓，人气招牌！",
               "signature_price": 12.0, "signature_specs": null }] }

// kind=menu 菜单卡片（含规格组）
{ "kind": "menu", "title": "一品黄焖鸡米饭 菜单", "shop_name": "一品黄焖鸡米饭",
  "icon": "🍛", "category": "黄焖鸡", "rating": 4.7, "min_order": 15, "delivery_fee": 3,
  "deal": "满 20 减 3",
  "dishes": [{ "name": "黄焖鸡米饭", "price": 20.0, "monthly_sales": 216, "icon": "🍛",
               "specs": [{ "group": "份量", "options": [{ "label": "小份" }, { "label": "大份", "extra": 3.0 }] },
                         { "group": "辣度", "options": [{ "label": "不辣" }, { "label": "微辣" }, { "label": "中辣" }, { "label": "特辣" }] },
                         { "group": "自选饮料", "options": [{ "label": "不需要" }, { "label": "可乐" }, { "label": "怡宝水" }, { "label": "加多宝", "extra": 2.0 }] }] }] }

// kind=result 订单卡片（对齐千问订单确认页明细行）
{ "kind": "result", "title": "外卖下单成功",
  "order_no": "WM20260828173809123", "shop_name": "一品黄焖鸡米饭", "icon": "🍛",
  "items_summary": "黄焖鸡米饭（大份/不辣/加多宝）×1、冬瓜汤×1",
  "subtotal": 30.0, "packing_fee": 2.0, "delivery_fee": 3.0, "discount": 5.0,
  "total": 30.0, "address": "示例路88号3栋502室", "phone": "138****8000",
  "remark": "放门口", "arrival": "立即配送，预计 17:38 送达",
  "status": "商家备餐中", "eta": "立即配送，预计 17:38 送达" }
```

## 8. 如何接入真实外卖 API

演示服务预留了清晰的替换点，接入真实外卖平台开放接口（需企业资质与密钥）时：

1. **替换执行器**：`_exec_search / _exec_menu / _exec_order / _exec_order_status` 内改为真实 HTTP 调用（参照 `weather.py` 的 httpx 用法），保持返回结构 `(data, card)` 不变；
2. **凭据管理**：`configured` 改为校验环境变量（如 `WAIMAI_API_KEY`，仅存 `server/.env`）；`main.py` 端点仿照 `agent_chat` 在未配置时返回 500 提示；
3. **真实图片**：商家/菜品可增加 `photo` 字段，前端 `wm-media` 图块改为 `<img>` 展示；
4. **模拟数据开关**：`SHOPS / _shop_summary / _ORDERS` 相关逻辑可保留为降级兑底，或删除；
5. **文案调整**：系统提示词删除「演示模拟数据」声明，卡片脚注 `rc-demo` 移除；真实下单的确认门控**必须保留**。

## 9. 已知限制

- 订单存储在进程内存（`_ORDERS`），服务重启后订单丢失，多进程/多实例部署不共享；
- 商家、菜单与坐标均为虚构演示数据，不接真实外卖平台；菜品图用 icon + 渐变图块占位（无真实图片）；
- 配送状态流转（30s 备餐 / 90s 送达）是演示加速，真实配送约 30 分钟；
- 商品卡「选这个」弹出规格弹窗改参数，点「选好了」组装商品+规格+数量消息发送大模型处理（后续地址/电话、预览与两步确认门控在对话流程中照常生效）；「进店选购」与弹窗一样通过 `onBrowse` 发送用户消息驱动（与全项目 A2UI 交互架构一致）；
- 弹窗仅支持单商品（商品卡上的招牌菜）点选，多商品组合请走 LLM 对话流程；
- A2UI 表单质量依赖模型遵循协议（组件 id 引用、JSON 合法性），系统提示词中已有自检约束；
- `MAX_TOOL_ROUNDS = 8`，正常流程（搜索→菜单→规格表单→预览→提交）约消耗 4~6 轮，首轮含地址追问时可能接近上限。
