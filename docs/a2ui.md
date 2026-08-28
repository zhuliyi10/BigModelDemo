# A2UI 场景实现文档（可交互界面生成）

> 模式标识：`a2ui` ｜ 后端入口：`POST /api/a2ui/chat` ｜ 核心模块：`server/main.py`（提示词）+ `client/src/A2UISurface.jsx`（渲染器）
> 场景性质：**纯提示词驱动**——模型按 A2UI v0.9 协议输出界面描述，前端渲染为可交互界面并回传事件；无工具、无 agent 循环

## 1. 功能概述

A2UI（Agent-to-User Interface）是让大模型直接「画出」用户界面的协议：模型不追问「你要哪种、几杯、什么口味」，而是生成一张可点选的表单/卡片，用户在界面上操作，操作数据回传给模型继续处理。

典型用途：点单、预订、填表、配置、调研展示。本项目用它承载两类角色：

- **独立场景**（`a2ui` 模式）：演示任意交互界面生成（点咖啡、订会议室、投票等）
- **横切能力**：`recharge`（充话费）与 `paotui`（美团跑腿）场景**叠加同一份提示词**与同一渲染器，实现费用确认表单与订单表单——A2UI 是这两者的表单交互基座

## 2. 整体架构

```
浏览器（a2ui 模式，a2uiLike，无工具 chip）
  │  POST /api/a2ui/chat
  ▼
server/main.py  a2ui_chat() → stream_chat(request, build_a2ui_system_prompt())
  │  纯 SSE 透传（与 /api/chat 共用通用流式代理，仅注入系统提示词）
  ▼
模型输出 ```a2ui 代码块（四类消息的紧凑 JSON，每行一条）
  ▼
client/src/A2UISurface.jsx  parseOutput 解析 → 组件树渲染
  │  用户操作（输入/点选/按钮）
  ▼
[A2UI_EVENT] surfaceId.事件名 {数据模型快照}
  │  前端：文本化展示 + 携带事件发送
  ▼
后端/模型：基于事件数据文本回复，不再生成界面
```

**协议四类消息**（顺序固定）：

| 消息 | 作用 |
|------|------|
| `createSurface` | 声明界面（surfaceId，`sendDataModelWithEvents:true` 使事件携带数据） |
| `updateComponents` | 提交组件目录（id → 组件定义），必须含一个根容器 |
| `updateDataModel` | 提交数据模型初始值（JSON 树，控件经 `{"path":"/x"}` 绑定） |
| `beginRendering` | 指定 root 组件 id，开始渲染 |

## 3. 系统提示词（build_a2ui_system_prompt，动态生成）

每次请求**注入当前日期与星期**，避免模型在「今天/明天/下周」等相对日期上猜错（换算成确切 `YYYY-MM-DD`，严禁编造日期）。

### 3.1 输出格式约束

- 每条消息独立一行的紧凑 JSON，整体用 ```` ```a2ui ```` 代码块包裹；代码块外只允许一句简短中文说明
- 收到 `[A2UI_EVENT]` 开头的用户消息时：基于事件数据给简短文本回复，**不要再生成界面**

### 3.2 组件目录（13 个）

| 组件 | 用途 | 关键属性 |
|------|------|----------|
| Text | 文本 | text、variant（h1/h2/h3/body/caption） |
| Button | 按钮 | child（子组件 id）、action.event.name（事件名） |
| TextField | 文本输入 | textualType（shortText/longText/number）、placeholder |
| CheckBox | 是否开关 | 绑定布尔路径 |
| ChoicePicker | **选项块点选** | options（label/value 对）、variant（mutuallyExclusive / multipleSelection） |
| Slider | 连续数值 | minValue / maxValue |
| DateTimeInput | 日期时间 | enableDate / enableTime |
| Card / Column / Row / List / Image / Divider | 布局与媒体 | List 支持 `repeated`（模板组件 × 数组路径） |

### 3.3 关键规则（防渲染失败）

- **JSON 合法性**：只用 ASCII 标点，严禁全角逗号/冒号（中文模型最常见的协议破坏点，提示词重点强调）
- **根容器完整性**：`components` 必须含根容器 Column，`beginRendering.root` 指向它；所有组件 id 必须挂在根容器（或嵌套容器）的 items/child 下，`beginRendering` 前自检引用完整性
- **选择优先**：可枚举字段一律 ChoicePicker（可多选用 multipleSelection）、是否类用 CheckBox、连续数值用 Slider，只有真正自由的内容才用 TextField；ChoicePicker 初始值必须是 options 中已有的 value
- **数据绑定**：所有输入控件必须绑定数据模型路径，`updateDataModel` 给出对应初始值；事件名语义化（如 `submit_booking`）；界面文字用中文

## 4. 前端渲染器（client/src/A2UISurface.jsx）

通用 A2UI 渲染器，充话费/跑腿的表单也由它渲染：

- **parseOutput**：从流式增量文本中解析 ```` ```a2ui ```` 代码块，逐行 `JSON.parse` 为消息流；**流式渲染**——每个 SSE 分片后重放解析，界面随生成逐步成形
- **数据模型**：`updateDataModel` 的 path/value 写入 surface 状态树；组件属性里的 `{"path":"/x"}` 按指针绑定读取（支持 List.repeated 数组映射）
- **交互回写**：TextField 输入、ChoicePicker 点选、Slider 拖动等直接更新本地数据模型对应路径
- **事件回传**：Button 点击 → `onEvent({surfaceId, name, dataModel})` → App.jsx `handleA2UIEvent` 把事件序列化为 `[A2UI_EVENT] surfaceId.事件名 {dataModel}` 字符串，作为用户消息发回后端
- **正文兜底**：代码块外的普通文本（prose）走 ReactMarkdown 渲染——因此叠加了 A2UI 提示词的场景（充值/跑腿）在模型只回文字时也显示正常

### 4.1 事件文本化（App.jsx）

- 提交事件在聊天流中以**普通文本气泡**展示（不做特殊 UI）：`A2UI_EVENT_LABELS` 映射事件名 → 中文（充话费：`提交充值表单`/`确认充值`/`取消充值`；跑腿：`提交跑腿订单表单`/`确认下单`/`取消下单`；独立 a2ui 场景的任意事件名兜底显示「界面操作」）
- 有摘要时拼接表单字段（「提交充值表单：充值手机号 xxx，充值面值 30，支付方式 支付宝」），空数据模型（纯确认按钮）只显示事件标签
- 发往后端的仍是原始 `[A2UI_EVENT]` 事件串，保证模型能拿到完整数据模型快照

## 5. 服务端接入（server/main.py）

```python
@app.post('/api/a2ui/chat')
async def a2ui_chat(request: Request):
    """A2UI 场景接口：注入 A2UI 系统提示词，让模型输出可渲染的界面描述"""
    return await stream_chat(request, build_a2ui_system_prompt())
```

`stream_chat` 是与 `/api/chat`（文本对话）共用的通用流式代理：组装 payload（model/messages/max_tokens/temperature）→ 流式请求网关 `/v1/messages` → SSE 逐字节透传（读超时设为无限，客户端断开时在 `finally` 中关闭上游）→ 网关异常原样透传给前端。

叠加 A2UI 能力的场景（充值/跑腿）则在 `agent_stream` 前把本提示词与场景提示词拼接注入。

## 6. 端到端交互流程

```
用户：帮我点一杯咖啡，大杯少糖
  │
  ├─① 模型输出：一句说明 + ```a2ui 代码块
  │     → 前端渲染点单界面（杯型/糖度 ChoicePicker、备注 TextField、提交按钮）
  ▼
用户：改 selections、填备注，点击「提交」
  │  聊天流显示文本：界面操作（事件名兜底标签）
  ├─② [A2UI_EVENT] 咖啡点单.submit_order {杯型:大杯, 糖度:少糖, 备注:""}
  └─③ 模型基于数据文本确认：「已下单：大杯、少糖…」，不再生成界面
```

独立 a2ui 模式无工具与真实业务，事件由模型直接确认处理；充值/跑腿场景则由对应 Service 在工具层消费同一事件串（见各自文档）。

## 7. 已知限制与排障

- **协议遵循度依赖模型**：弱模型易出现全角标点、漏根容器、引用未定义 id 等错误 → 界面不渲染只显示代码块文本。提示词已做针对性约束（ASCII 标点、自检清单），换更强模型可显著改善
- **多轮界面**：渲染器按消息内容重放，一轮回复只保留最后一次 `beginRendering` 的界面；需要多步表单时用多轮对话承载
- **事件数据为空**：纯确认按钮无绑定控件时 dataModel 为空 → 前端只显示事件标签文本，属预期行为
- **XSS 面收敛**：组件属性按白名单目录解析，未识别的 component 忽略；Image 只接受 url 字段渲染 `<img>`
- **调试建议**：界面不显示时，先把模式切到普通对话或查看原始回复里的 ```` ```a2ui ```` 块，逐行校验 JSON 合法性与 root/items 引用完整性
