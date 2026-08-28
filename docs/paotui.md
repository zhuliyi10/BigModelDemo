# 美团跑腿场景实现文档

> 模式标识：`paotui` ｜ 后端入口：`POST /api/paotui/chat` ｜ 核心模块：`server/paotui.py`
> 场景性质：**真实下单**（真实消费），依赖官方 `meituan-paotui` Skill 包与美团账号扫码授权

## 1. 功能概述

接入美团开放平台 AI Hub 官方跑腿 Skill（`meituan-paotui`），支持三大场景的真实下单：

- 🛵 **帮取送**：A 地取件送 B 地（文件、蛋糕、鲜花等）
- 🛒 **帮买**：代购商品并送达（需购买明细）
- 🙋 **帮忙**：取号、帮搬装、扔杂物等（单地址，内容写备注）

核心能力：扫码授权（两阶段 + 自愈）、地址簿拉取、POI 地址搜索、费用预览、**两步确认下单**、订单状态查询。前端以 A2UI 表单收集订单信息与费用确认。

## 2. 整体架构

与官方 Skill 的集成方式为**子进程 CLI 执行**（区别于高德/天气的 HTTP 调用）：

```
用户输入
  │
  ▼
client/src/App.jsx (mode='paotui'，agentLike + a2uiLike 双标记)
  │  POST /api/paotui/chat  { model, messages }
  ▼
server/main.py  paotui_chat()
  │  system = build_a2ui_system_prompt() + paotui.system_prompt()
  ▼
agent_stream(body, paotui, system)              ← 通用 agent 循环（SSE）
  │  解析 tool_use → paotui.execute_tool(name, args)
  ▼
server/paotui.py  PaotuiService                 ← CLI 执行器 + 鉴权链路
  │  asyncio.create_subprocess_exec('node', 'paotui.js', <args>)
  │  env 注入 MEITUAN_PAOTUI_SOURCE_FROM=aihub + MCP_ACCESS_TOKEN
  ▼
skills/meituan-paotui/paotui.js                 ← 官方 Skill CLI（美团开放平台 AI Hub 下载）
  │  依赖 pt-passport CLI（Skill 包自带安装脚本）完成用户级授权
  ▼
美团开放平台 API
```

每次工具执行产出两份结果：给模型的结构化数据（`tool_result`）、给前端的卡片（授权卡片走 `paotui_card` 事件）。

## 3. 部署与凭据

### 3.1 Skill 包部署

```bash
# 1. 从美团开放平台 AI Hub 下载 meituan-paotui Skill 源文件包
#    解压到 server/skills/meituan-paotui（或用 MEITUAN_PAOTUI_DIR 指定路径）
#    确保 paotui.js 位于该目录下

# 2. 安装用户级授权依赖 CLI（本地 tgz 安装 pt-passport）
bash server/skills/meituan-paotui/references/meituan-passport-user-auth/scripts/install.sh
```

`cli_entry` 定位顺序：`paotui.js` → `dist/paotui.js` → `dist/run.sh`（`node` / `sh` 执行）；均不存在时 `configured=False`，端点返回 500 部署提示。

### 3.2 环境变量（server/.env）

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `MEITUAN_PAOTUI_DIR` | `server/skills/meituan-paotui` | Skill 包路径 |
| `MEITUAN_PAOTUI_SOURCE_FROM` | `aihub` | Skill 渠道标识（aihub 渠道无需 apiKey） |
| `MEITUAN_PASSPORT_TOKEN` | — | 用户授权 Token（配置后跳过扫码，直接注入 `MCP_ACCESS_TOKEN`） |
| `MEITUAN_PASSPORT_CLIENT_ID` | `ac76c2…` | passport 应用标识（Skill 包 skill-dependencies 声明，prod 环境） |
| `MEITUAN_PASSPORT_ENV` | `prod` | passport 环境 |
| `PAOTUI_TIMEOUT` | `120` | 单次 CLI 命令超时（秒） |
| `PAOTUI_CONFIRM_TIMEOUT` | `620` | 扫码授权长轮询超时（秒，约 10 分钟） |

## 4. 后端实现（server/paotui.py）

### 4.1 模块结构

```
paotui.py
├── 常量        SKILL_DIR / CLI_TIMEOUT_SECONDS / CONFIRM_AUTH_TIMEOUT_SECONDS / PASSPORT_*
├── 工具定义    PAOTUI_TOOLS（6 个）+ TOOL_LABELS
├── 系统提示词  build_paotui_system_prompt()（输出规范/安全门控/工作流程/参数规范/A2UI 交互）
└── 服务类      PaotuiService
    ├── cli_entry / configured        Skill 包定位
    ├── _env / _passport_token        环境注入 + Token 获取
    ├── _run_passport / _run_cli      子进程执行封装（超时 kill + 错误归一化）
    ├── _extract_auth_link            AUTH_LINK 行提取
    └── _exec_* × 6                   各工具执行器
```

### 4.2 工具定义（PAOTUI_TOOLS）

| 工具 | 入参 | 行为 | 返回卡片 |
|------|------|------|----------|
| `paotui_login` | 无 | 检查登录/授权状态；返回「已登录」或授权链接（AUTH_LINK） | `kind=auth` 授权卡片 |
| `paotui_confirm_auth` | 无 | 用户扫码确认后长轮询 Token 写入本地缓存（最长约 10 分钟），并回跑 login 校验 | 无 |
| `paotui_address_list` | `scene: send\|buy` | 拉取地址簿（坐标/电话/标签/最近使用），未给地址时展示最近 3 条 | 无 |
| `paotui_search_poi` | `keyword`, `city` | 把新地址描述转为下单坐标（lat/lng/cityId） | 无 |
| `paotui_order` | `sender/recipient/goods/business_type/…/confirm` | `confirm=false` 预览费用；`confirm=true` 提交订单 | 无（费用确认走 A2UI 表单） |
| `paotui_order_status` | `order_id` | 按 orderViewId 查询订单状态 | 无 |

CLI 命令映射：`login` / `get_address_list` / `search_poi` / `preview_and_submit` / `get_order_status`（官方 `references/commands.md` 命令面）。

### 4.3 鉴权链路（两阶段 + 自愈）

跑腿下单需要**用户级授权**（区别于应用级 Token），流程由依赖的 `pt-passport` CLI 完成：

```
① paotui_login
   ├─ paotui.js login 成功 → 「已登录」，直接进业务流程
   └─ 返回 AUTH_FAILED → 回退 pt-passport 链路：
      ├─ 自愈检查：/tmp/pt_passport_session_*.json 存在？
      │    └─ 是 → 先 poll-token --timeout 10（用户已扫码则秒级取回 Token）
      │           → 回跑 login 校验，通过则返回「已登录，授权有效」
      ├─ pt-passport auth get-code → 生成 AUTH_LINK（授权卡片展示，用户美团 App 扫码）
      └─ ⚠️ 严禁再次调用 paotui_login（不会取回 Token，且会重新生成授权码使当前扫码失效）
② 用户扫码后在美团 App 侧确认 → 回复「已授权/好了」等确认语
③ paotui_confirm_auth：pt-passport auth poll-token 长轮询（≤ CONFIRM_AUTH_TIMEOUT）
   → Token 写入 ~/.xiaomei-workspace/mt_passport_auth.json（缓存 30 天）→ login 校验打通
④ 后续每次执行 CLI 前：pt-passport get-token 取出 Token 注入 MCP_ACCESS_TOKEN
   （MEITUAN_PASSPORT_TOKEN 环境变量优先）
```

> 关键语义：**扫码 ≠ 授权生效**。扫码只完成美团侧确认，Token 必须等 `poll-token` 执行后才写入本地缓存——这是「已扫码却一直显示待授权」问题的根因，自愈逻辑即为此设计。

### 4.4 下单参数规范（系统提示词）

- **地址 JSON**：`{address, houseNumber:"", lat, lng, name:"", phone, cityId}`；`lat/lng` 为整数×1e6；`houseNumber/name` 固定留空（填写会触发风控）；地址簿返回的坐标可直接使用
- **三大场景**：帮取送 `sender+recipient+goods`；帮买 `recipient+purchase_detail`（`business_type=2`）；帮忙 `sender=recipient` 同一地址、`goods` 留空、内容写 `remark`
- **物品映射表**（`goodTypes` 与 `goodTypeNames` 整行取）：餐饮外卖 `[2]["餐饮"]`、文件合同 `[4]["文件"]`、生鲜水果 `[3]["生鲜"]`、蛋糕 `[9]["蛋糕"]`、鲜花 `[1]["鲜花"]`、数码 `[5]["数码"]`、服饰 `[7]["服饰"]`、快递/其他 `[8]["快递"/"其他"]`
- **场景标签**：`biz_type_scene_tag` 0 默认 / 1 餐厅取号 / 2 医院帮忙 / 3 其他取号 / 4 帮搬装 / 5 其他帮忙 / 6 帮扔杂物；帮买 `business_type_tag` 0 指定地址 / 1 就近购买
- **城市表**（cityId）：北京 110100、上海 310100、广州 440100、深圳 440300、成都 510100、杭州 330100、武汉 420100、南京 320100、西安 610100、重庆 500100；表外城市告知暂不支持，跨城配送不支持

### 4.5 安全门控（真实消费）

- 必须 `confirm=false` 预览 → A2UI 费用确认表单（逐行展示服务类型/地址/物品/距离/配送费/时效）→ 用户点击「确认下单」后才 `confirm=true`，参数与预览完全一致
- 费用 > 100 元需额外确认；取件/收件电话缺失必须询问，不得编造
- 手机号一律脱敏（`138****5678`）；严禁展示英文字段名/JSON/脚本路径等技术细节
- 下单成功提示：「15 分钟内打开美团 App → 我的订单完成支付」

## 5. 服务端接入（server/main.py）

```python
@app.post('/api/paotui/chat')
async def paotui_chat(request: Request):
    # 常规校验 + paotui.configured（Skill 包已部署）后：
    system = build_a2ui_system_prompt() + '\n\n' + paotui.system_prompt()
    return agent_stream(body, paotui, system)
```

`MAX_TOOL_ROUNDS = 8`（授权→地址→预览→确认→提交流程较长）。

## 6. 前端实现（client/src/App.jsx）

- **双标记**：`paotui` 同时属于 `agentLike`（工具 chip + 授权卡片）与 `a2uiLike`（A2UI 表单可交互）
- **授权卡片**：`paotui_card` 事件 → `PaotuiAuthCard`，展示授权说明与「点击前往授权 →」链接（新窗口打开美团 App 扫码）
- **A2UI 表单**：订单表单（场景 ChoicePicker / 地址与电话 TextField / 物品类别 ChoicePicker / 备注，按钮 `submit_paotui_order`）；费用确认表单（Text 逐行明细 + `confirm_order` / `cancel_order` 按钮）
- **事件文本化**：提交事件在聊天流中以普通文本显示（`A2UI_EVENT_LABELS`：`提交跑腿订单表单` / `确认下单` / `取消下单`），发往后端的仍为原始 `[A2UI_EVENT]` 事件串

## 7. 端到端交互流程

```
用户：帮我把一份合同从公司送到家
  │
  ├─① paotui_login → 已登录（或授权卡片 → 用户扫码 → 回复「已授权」→ paotui_confirm_auth）
  ├─② paotui_address_list / paotui_search_poi → 解析取件/收件地址
  ├─③ 模型输出 A2UI 订单表单（场景预选帮取送、物品类别预选文件合同）
  ▼
用户：填写并点击「提交」
  │  聊天流显示文本：提交跑腿订单表单：场景 帮取送，物品名称 合同，…
  ├─④ paotui_order(confirm=false) → 🔧 chip「跑腿下单 · 预览」
  ├─⑤ 模型输出 A2UI 费用确认表单（距离/配送费/时效逐行展示）
  ▼
用户：点击「确认下单」
  │  聊天流显示文本：确认下单
  ├─⑥ paotui_order(confirm=true) → 🔧 chip「跑腿下单 · 已提交」
  └─⑦ 模型正文：下单成功 + 订单号 +「15 分钟内打开美团 App → 我的订单完成支付」

用户：查一下订单状态
  └─⑧ paotui_order_status(order_id=orderViewId) → 骑手接单/配送状态
```

## 8. 已知限制与排障

- **真实消费**：下单后需 15 分钟内在美团 App 支付，未支付自动取消；测试时务必走「预览 → 取消」或在确认前中止
- **Skill 包依赖**：未部署时端点返回 500 并给出部署指引；`node` 不可用时返回环境错误
- **授权过期**：Token 缓存 30 天，过期后 `paotui_login` 重新进入扫码链路
- **「已扫码但一直待授权」**：先确认 `/tmp/pt_passport_session_*.json` 是否存在；存在则下次 `paotui_login` 会自愈取回；也可手动让模型调用 `paotui_confirm_auth`
- **城市覆盖**：仅支持城市表内 10 城，跨城配送不支持
- **命令超时**：默认 120 秒（授权等待 620 秒），可用 `PAOTUI_TIMEOUT` / `PAOTUI_CONFIRM_TIMEOUT` 调整
