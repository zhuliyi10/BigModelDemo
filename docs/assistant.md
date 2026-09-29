# 通用智能助手场景实现文档

> 模式标识：`assistant` ｜ 后端入口：`POST /api/assistant/chat` ｜ 核心模块：`server/assistant.py`
> 场景性质：**聚合入口**——单条对话自动识别用户意图，路由到天气 / 话费充值 / 外卖点餐 / 美团跑腿 / 美团酒旅 / 出行助手对应场景的工具执行，无需在多个场景间手动切换
> 实现方式：不复制任何业务逻辑，把六个场景服务组合为一个满足 `agent_stream` 接口协议的**组合服务**（Composite Service）

## 1. 功能概述

通用智能助手解决"每个场景一个模式、用户要先选场景再提问"的割裂体验：

- 🤖 单入口对话：用户直接说"帮我充 50 话费""点一份黄焖鸡米饭""查下杭州天气"，模型自动判断意图并调用对应场景的工具
- 🔀 多需求串联：一条消息包含多个需求（如"查下杭州天气，再帮我充 50 话费"）时按顺序分步完成
- 🧩 能力动态聚合：未配置凭据的场景自动从工具面剔除（如未填 `AMAP_KEY` 时不含高德工具），配置后无需改代码自动加入
- 🛡 安全门控继承：充值 / 外卖 / 跑腿的两步确认、A2UI 表单收集、参数校验在聚合场景下**原样生效**
- 🃏 卡片照常渲染：各场景的结构化卡片（天气 / 余额 / 商家 / 订单 / 路线）按原有事件类型下发，前端渲染逻辑零改动

## 2. 整体架构

在通用 agent 循环 `agent_stream` 之上增加一层**意图路由**，由组合服务把工具调用与卡片事件分发回各场景服务：

```
用户输入（自然语言，任意场景意图）
  │
  ▼
client/src/App.jsx (mode='assistant')
  │  POST /api/assistant/chat  { model, messages }
  ▼
server/main.py  assistant_chat()
  │  system = build_a2ui_system_prompt() + '\n\n' + assistant.system_prompt
  ▼
agent_stream(body, assistant, system)         ← 通用 agent 循环（SSE），与单场景完全同款
  │  ① 携带「全部可用场景工具的并集」请求网关 /v1/messages
  │  ② 解析 tool_use → assistant.execute_tool(name, args)
  │       └─ 按工具名路由 → 对应场景服务.execute_tool()   ← 意图路由的核心
  │  ③ 回写 tool_result 继续循环，直至模型给出最终回答
  ▼
AssistantService（组合层，server/assistant.py）
  │
  ├─ WeatherService   weather_geocode / weather_forecast        → weather_card
  ├─ RechargeService  recharge_query / plans / order / status   → recharge_card（A2UI 表单 + 两步确认）
  ├─ WaimaiService    waimai_search_shops / menu / order / …    → waimai_card（A2UI 表单 + 两步确认）
  ├─ AmapService      maps_geo / text_search / direction_*      → amap_card / amap_poi_list（按配置）
  ├─ TravelService    mt_travel_query                           → mt_travel_card（按配置）
  └─ PaotuiService    paotui_login / order / status / …         → paotui_card（按配置，真实下单）
```

职责边界：**组合层只做"路由"不做"业务"**——工具定义、参数校验、模拟数据、两步门控、卡片结构全部复用各场景服务现有实现；模型只决定"何时调用、调用哪个场景的工具"。

## 3. 后端实现（server/assistant.py）

### 3.1 接口协议（与单场景服务同款）

`AssistantService` 实现 `agent_stream` 约定的全部接口：`tools` / `MAX_TOOL_ROUNDS`（=12）/ `configured` / `system_prompt` / `tool_label` / `card_event_type` / `execute_tool` / `summarize_result`。构造时接收 `(场景名, 服务实例)` 列表：

```python
assistant = AssistantService([
    ('weather', weather), ('recharge', recharge), ('waimai', waimai),
    ('amap', amap), ('travel', travel), ('paotui', paotui),
])
```

### 3.2 工具聚合与按名路由

各场景工具名前缀唯一（`weather_*` / `recharge_*` / `waimai_*` / `maps_*` / `mt_travel_*` / `paotui_*`），聚合即无冲突：

- `tools`：按子服务顺序拼接全部工具定义（全量聚合时约 20 个）；
- `_tool_map`：构造时建立 `工具名 → 子服务` 映射，`execute_tool` / `tool_label` / `summarize_result` 均按名路由，未知工具返回 `{'error': ...}` 兜底，不中断 agent 循环；
- `MAX_TOOL_ROUNDS = 12`：单场景为 5~8，通用场景一条消息可能串多个场景（如"查天气→充值"两段流程），轮次上限相应放宽。

### 3.3 卡片事件类型路由

组合服务不认识各场景的卡片结构，采用**打标记**方案精确路由：

1. `execute_tool` 调用子服务后，若返回卡片则打上内部标记 `card['_event'] = 子服务.card_event_type(card)`；
2. `agent_stream` 下发卡片时调用组合服务的 `card_event_type(card)`：读取并 `pop` 该标记（前端收到的卡片不含内部字段）；
3. 标记缺失时按卡片特征兜底（`_fallback_card_event`）：`kind=poi_list→amap_poi_list`、`kind=auth→paotui_card`、`kind=forecast→weather_card`、`kind=shops/menu→waimai_card`、`items_summary→waimai_card`、`face_value/balance→recharge_card` 等。

充值与外卖的订单卡片同为 `kind=result`，特征字段区分：外卖卡含 `items_summary` / `shop_name`，充值卡含 `face_value`。

### 3.4 系统提示词（意图路由总则 + 场景规范拼接）

`system_prompt` 为实例属性（property），按可用子服务动态生成，长度约 6.4K 字符：

```
你是通用生活服务智能助手……
当前具备以下生活服务能力（按需选用，禁止调用能力清单之外的工具）：
- 天气查询：weather_geocode / weather_forecast 工具（…）
- 话费充值（演示模拟数据）：recharge_* 工具，两步确认充值
- …（仅列当前可用场景，SCENARIOS 常量维护简介文案）

工作准则：
1. 先判断意图属于哪个场景，只调用该场景的工具；一个场景的任务未完成前不切换场景
2. 多需求按顺序分步完成：先完成第一个场景并给出结果，再开始下一个场景
3. 追问已完成场景的后续进展（查订单）时继续用对应场景工具
4. 意图不明确先一句话澄清；已给全参数直接执行
5. 各场景安全门控（两步确认/表单收集/禁止编造）是最强约束，以下方场景规范为准

【天气查询场景规范】 weather.system_prompt()
【话费充值场景规范】 recharge.system_prompt()
【外卖点餐场景规范】 waimai.system_prompt()
…（【场景规范】标题 + 各子服务完整提示词原样拼接）
```

各子服务提示词（六步工作流程、参数规范、降级路径等）**原样保留**，不因聚合而改写——单场景模式与通用模式下的行为完全一致。

### 3.5 可用性动态聚合

构造时过滤：`svc.configured and getattr(svc, 'cli_available', True)`。

| 场景 | 聚合条件 | 缺省状态 |
|------|----------|----------|
| weather / recharge / waimai | 恒 True（免凭据 / 演示数据） | 始终可用 |
| amap | 配置 `AMAP_KEY` | 未配置时自动剔除 |
| travel | `MEITUAN_TOKEN`（或 CLI config）+ `mttravel` CLI 已安装 | 未就绪时自动剔除 |
| paotui | Skill 包已部署（`paotui.js` 存在） | 未部署时自动剔除 |

新增场景时只需在 `main.py` 的列表里追加一项、在 `SCENARIOS` / `SCENARIO_TITLES` 补简介文案，组合层自动完成聚合与路由。

## 4. 服务端接入（server/main.py）

```python
@app.post('/api/assistant/chat')
async def assistant_chat(request: Request):
    # 常规校验（messages 非空 + 网关配置 + assistant.configured）后：
    system = build_a2ui_system_prompt() + '\n\n' + assistant.system_prompt
    return agent_stream(body, assistant, system)
```

叠加 A2UI 协议提示词：充值 / 外卖 / 跑腿场景的表单收集（地址电话、充值档位）与费用确认表单在通用场景**照常生效**；天气 / 酒旅等无需界面的意图不受影响（A2UI 提示词仅在"需要界面交互的需求"时触发生成界面）。

## 5. 前端实现（client/src/App.jsx）

改动极小，复用全部现有渲染链路：

1. **模式注册**：`mode='assistant'` 同时加入 `agentLike`（工具调用 chip + 结构化卡片）与 `a2uiLike`（正文用 `A2UISurface` 渲染可交互 A2UI 表单）——与 recharge/paotui/waimai 的双标记同款；
2. **端点映射**：`send()` 的 endpoint 三元链首分支 `mode === 'assistant' → '/api/assistant/chat'`；
3. **入口按钮**：模式栏新增「🤖 通用助手」（置于"文本对话"之后首位）；
4. **卡片渲染零改动**：SSE 事件类型按场景区分（`weather_card` / `recharge_card` / `waimai_card` / `amap_card` / `mt_travel_card` / `paotui_card`），现有分发逻辑直接命中对应卡片组件；A2UI 事件（`confirm_recharge` / `submit_waimai_order` / `confirm_waimai` 等）沿用 `handleA2UIEvent → send()` 回传，事件名无冲突。

## 6. 端到端交互流程

```
用户（mode=assistant）：帮我充 50 话费，充完帮我点一份一品黄焖鸡米饭的黄焖鸡米饭
  │
  ├─① LLM: recharge_query("…") → 🔧 chip「余额查询」 + 余额卡片
  ├─② recharge_plans → A2UI 充值表单（档位 ChoicePicker，50 元档）
  ▼
用户：提交充值表单（A2UI 事件 submit_recharge → 文本气泡回传）
  ├─③ recharge_order(confirm=false) → A2UI 费用确认表单（confirm_recharge / cancel_recharge）
  ▼
用户：点击「确认充值」
  ├─④ recharge_order(confirm=true) → 🔧「充值提交」 + 订单卡片（充值中→充值成功）
  ├─⑤ 场景切换：LLM 调 waimai_search_shops("一品黄焖鸡米饭") → 横滑商家卡
  ├─⑥ 缺地址/电话 → A2UI 点餐信息表单（submit_waimai_order）
  ├─⑦ waimai_order(confirm=false) → A2UI 费用确认表单（confirm_waimai / cancel_waimai）
  ▼
用户：点击「确认下单」
  └─⑧ waimai_order(confirm=true) → 🔧「外卖下单 · 已提交」 + 订单卡片（商家备餐中）

用户：外卖到哪了？
  └─⑨ waimai_order_status → 订单卡片状态流转（跨场景追问继续命中对应工具）
```

每一步的工具 chip、卡片、A2UI 表单与对应单场景模式完全一致。

## 7. 已知限制与设计取舍

- 工具面为全量并集（约 20 个工具）+ 系统提示词更长（约 6.4K 字符），首轮意图判断对模型要求高于单场景；跨场景多步流程轮次消耗更多（`MAX_TOOL_ROUNDS=12`，两条完整交易链可能接近上限）；
- 模型偶尔在聚合场景下误入相邻场景工具（如充值流程中误调 `waimai_search_shops`），子服务参数校验会返回错误引导模型纠正，不影响正确性但可能多耗轮次；
- paotui 为**真实下单**场景，聚合后通用对话中同样可触发；两步确认门控与授权流程照常生效，不希望暴露时可从 `main.py` 的聚合列表中移除；
- 意图路由完全依赖模型（无规则前置分类），极模糊输入（"帮我处理一下"）会先澄清再执行；
- 各场景已知限制（内存订单、演示数据、A2UI 表单质量依赖模型等）在聚合模式下原样继承，见各场景文档。
