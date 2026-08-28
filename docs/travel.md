# 美团酒旅场景实现文档

> 模式标识：`travel` ｜ 后端入口：`POST /api/travel/chat` ｜ 核心模块：`server/mttravel.py`
> 场景性质：真实供给查询（景点/酒店/机票/火车票/门票/度假/行程规划），**只读不交易**，个人开发者 Token 即可接入

## 1. 功能概述

接入美团开放平台 AI Hub 官方 `meituan-travel` Skill，基于美团真实供给数据完成旅行查询与规划：

- 🏨 酒店推荐（按目的地/预算/星级）
- ✈️🚄 机票、火车票、汽车票查询
- 🎡 景点门票、跟团度假商品推荐
- 🗺️ 定制化旅行攻略与行程规划

特点：**单一自然语言工具**驱动（无表单、无多工具编排），单次查询约 1–2 分钟，结果为含图片的 Markdown 富文本卡片。

## 2. 整体架构

与美团跑腿同为「CLI 子进程」集成，但 CLI 形态与凭据模型不同：

```
浏览器（travel 模式，agentLike，无 A2UI）
  │  POST /api/travel/chat  { model, messages }
  ▼
server/main.py  travel_chat()
  │  校验 cli_available / configured → agent_stream(body, travel, travel.system_prompt())
  ▼
server/mttravel.py  TravelService
  │  子进程：mttravel <城市> "<自然语言查询>"
  │  （CLI 自行读取 ~/.config/meituan-travel/config.json 的 key 请求美团开放平台）
  ▼
美团开放平台 API（真实供给）
  │
  ├─ 给模型：{city, query, content}  ← tool_result
  └─ 给前端：mt_travel_card 事件 → MeituanTravelCard（Markdown 富文本整卡渲染）
```

### 与美团跑腿的对比

| 维度 | 美团酒旅 | 美团跑腿 |
|------|---------|---------|
| CLI 形态 | 全局 npm 包 `mttravel` | 本地 Skill 包 `paotui.js` |
| 工具粒度 | 1 个自然语言查询 | 6 个结构化工具 |
| 凭据 | 应用级 Token（开发者中心创建） | 用户级扫码授权（pt-passport） |
| 返回形态 | Markdown 富文本卡片 | 结构化数据 + A2UI 表单 |
| 单次耗时 | 1–2 分钟 | 秒级 |
| 前端交互 | 查询即得，无表单 | A2UI 表单 + 两步确认 |

## 3. 部署与凭据

```bash
# 1. 安装官方 CLI（全局）
npm i -g @meituan-travel/travel-cli

# 2. 创建 Token：美团开发者中心 → 个人开发者控制台 → Token 管理
#    https://developer.meituan.com/zh/v2/dev/doc
#    填入 server/.env：
#    MEITUAN_TOKEN=你的Token
```

| 环境变量 | 默认值 | 说明 |
|----------|--------|------|
| `MEITUAN_TOKEN` | — | 酒旅 Skill Token；未配置时回退 CLI 自身 `~/.config/meituan-travel/config.json` |
| `MTTRAVEL_TIMEOUT` | `150` | 单次查询超时（秒），官方说明耗时约 1–2 分钟 |

## 4. 后端实现（server/mttravel.py）

### 4.1 模块结构

```
mttravel.py
├── 常量        CLI_COMMAND('mttravel') / CLI_TIMEOUT_SECONDS(150) / CLI_CONFIG(~/.config/meituan-travel/config.json)
├── 工具定义    TRAVEL_TOOLS（单一 mt_travel_query）+ TOOL_LABELS
├── 系统提示词  build_travel_system_prompt()
└── 服务类      TravelService
    ├── token / configured           Token 读取（env 优先 → CLI 配置回退）
    ├── ensure_cli_token             .env → CLI 配置自动同步
    ├── cli_available                shutil.which 检查 CLI 安装
    └── _exec_query                  唯一工具执行器（子进程 + 超时 kill）
```

### 4.2 工具定义（单一自然语言入口）

```jsonc
{
  "name": "mt_travel_query",
  "description": "美团酒旅查询：基于美团真实供给，查询并推荐酒店、机票、火车票、汽车票、
                  景点门票、跟团度假商品，或生成定制化旅行攻略/行程规划……单次查询耗时约 1-2 分钟",
  "input_schema": {
    "city":  "用户当前所在城市（出发地参考，获取不到默认北京）",
    "query": "自然语言查询，转述用户完整需求（目的地/时间/人数/预算/旅行风格）"
  },
  "required": ["city", "query"]
}
```

工具描述中明确「单次查询耗时约 1-2 分钟」，配合系统提示词防止模型因等待而重复调用。

### 4.3 Token 单点维护（ensure_cli_token）

官方 CLI 只认自身配置文件 `~/.config/meituan-travel/config.json`，而项目密钥规范是统一放 `server/.env`。处理方式：

1. `token` 属性：优先读 `MEITUAN_TOKEN`，为空时回退读 CLI 配置的 `key` 字段；
2. `ensure_cli_token()`：执行查询前，若 `.env` 配置了 Token 而 CLI 配置**缺失或不一致**，自动把 Token 写入 CLI 配置（目录不存在则创建）——用户只需维护 `.env` 一处，Token 始终不下发浏览器。

### 4.4 健壮性设计

| 场景 | 处理 |
|------|------|
| CLI 未安装 | `cli_available=False`（`shutil.which`），端点与工具分别返回安装指引 |
| Token 缺失 | 端点返回 500 配置指引；工具返回错误说明（含创建 Token 的入口） |
| 查询超时 | 默认 150 秒，`asyncio.wait_for` + `proc.kill()`；提示「查询人数可能较多，换个问法或稍后再试」 |
| 非零退出/空输出 | 归一化为 `{'error': '美团酒旅查询失败：<detail>'}` |
| 任意异常 | `execute_tool` 兜底捕获，避免中断 agent 循环 |
| 重复调用 | `MAX_TOOL_ROUNDS = 3`（查询耗时长，硬性限制轮数） |

### 4.5 系统提示词要点

- **工作流程**：提取「所在城市」（默认北京，用户明确指定以用户为准，无法判断时主动询问）→ `query` 转述完整需求 → 调用工具 → 基于结果简短建议
- **防内容重复**：工具返回的 Markdown 已由系统完整渲染为卡片，**正文严禁复述结果内容**，只附一两句建议或提醒
- **防幻觉约束**：价格原样输出；占位符（X/XX/XXX）**不得自行还原成具体数字**；评分以「X.X 分（美团真实评分）」、星级以「美团 X 星级」呈现
- **错误引导**：超时建议换问法，鉴权失败提示重新配置 Token
- **边界声明**：不适用于出国签证、护照办理等非旅行类问题，遇到时直接说明

## 5. 服务端接入（server/main.py）

```python
@app.post('/api/travel/chat')
async def travel_chat(request: Request):
    # messages 校验 + missing_llm_config 后：
    if not travel.cli_available:
        return JSONResponse({'error': '服务端未安装美团酒旅 CLI，请执行：npm i -g @meituan-travel/travel-cli'}, ...)
    if not travel.configured:
        return JSONResponse({'error': '未配置美团酒旅 Token：…填入 server/.env 的 MEITUAN_TOKEN'}, ...)
    return agent_stream(body, travel, travel.system_prompt())
```

纯查询场景，**不叠加 A2UI 提示词**（无表单交互需求）。

## 6. 前端实现（client/src/App.jsx）

- `travel` 模式属于 `agentLike`（工具 chip + 结果卡片），**不属于** `a2uiLike`（无表单）
- 工具 chip：「🔧 美团酒旅查询 · N 字结果」（`summarize_result` 摘要）
- `mt_travel_card` 事件 → `MeituanTravelCard`：`ReactMarkdown + remark-gfm` 渲染官方返回的富文本（标题/列表/图片/链接原样呈现），标题栏附城市徽标
- 空状态推荐问题（`TRAVEL_SUGGESTIONS`）：景点推荐 / 酒店查询 / 行程规划 / 火车票

## 7. 端到端交互流程

```
用户：上海外滩附近有哪些五星酒店
  │
  ├─① 模型提取所在城市（未提及 → 默认北京作为出发地参考）与查询需求
  ├─② mt_travel_query(city="北京", query="上海外滩五星酒店")
  │     → 🔧 chip「美团酒旅查询 · 2310 字结果」（期间 1–2 分钟）
  │     → 🏨 结果卡片（酒店列表：图片/价格/评分/链接，整卡 Markdown 渲染）
  └─③ 模型正文：一两句简短建议（如「预算紧张可关注第二家」），不复述卡片内容
```

## 8. 已知限制与排障

- **查询耗时**：单次 1–2 分钟属正常（官方侧检索耗时），前端有工具 chip 的 calling 态提示；高峰期可能超时，换个问法重试即可
- **命令找不到**：确认全局 npm bin 在 PATH 中（`which mttravel`）；nvm 多版本 Node 环境下注意后端启动 shell 的 PATH
- **鉴权失败**：Token 失效或未创建，重新到开发者中心生成并更新 `.env` 的 `MEITUAN_TOKEN`（重启后端后 `ensure_cli_token` 会自动同步到 CLI 配置）
- **只读场景**：不涉及下单/支付；酒店机票预订需跳转美团 App 自行完成
- **内容边界**：出国签证、护照办理等非旅行问题会被提示词引导拒绝
