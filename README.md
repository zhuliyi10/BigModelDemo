# 大模型问答 Demo

一个基于 **React + Python（FastAPI）** 的大模型问答应用。前端使用 React（Vite）实现聊天界面，后端作为代理服务，以流式（SSE）方式转发 Anthropic 兼容端点（如智谱 `https://open.bigmodel.cn/api/anthropic`）的 `/v1/messages` 接口。API Key 仅保存在服务端环境变量中，前端不接触密钥。

## 功能特性

- 💬 多轮对话，AI 回复**流式逐字渲染**
- ⚡ **A2UI 场景**：AI 按 A2UI 协议实时生成可交互界面（表单、点单、预订），填写后一键提交
- 🗺 **出行助手**：AI 以工具调用方式查询高德地图（地理编码 / POI / 路线规划），附交互式路线卡片
- 🍜 **外卖点餐**：AI 调用美团外卖开放平台（搜索门店 → 展示菜单 → A2UI 点餐界面 → 跳转美团下单）；未配置美团凭据时自动降级为**本地演示数据**，全流程照常可演示
- 🌤 **天气查询**：AI 工具调用 Open-Meteo（免凭据），当前实况 + 7 天逐日预报，附天气卡片与穿衣出行建议
- 🧠 回复内容支持 **Markdown** 渲染（代码块、表格、列表、引用等）
- 🌓 **深色 / 浅色主题**一键切换，跟随系统偏好并本地记忆，无闪烁
- 🤖 模型下拉框**动态加载**网关支持的模型列表（默认 `claude-4.6-sonnet`）
- ⏹️ 生成中可随时**停止**；支持一键清空对话
- ✨ 空状态提供推荐问题卡片，点击即可提问
- 📱 响应式布局，适配移动端
- 🔐 API Key 存放于 `server/.env`，不进入代码与 Git

## 技术栈

| 层 | 技术 |
|----|------|
| 前端 | React 18、Vite 5、react-markdown、remark-gfm |
| 后端 | Python 3、FastAPI、Uvicorn、httpx、python-dotenv |
| 通信 | 前端 → 后端 `/api/chat` → 网关 `/v1/messages`，SSE 流式透传 |

## 项目结构

```
BigModelDemo/
├── package.json            # 根配置，统一启动脚本（前端 npm workspace + 后端 uvicorn）
├── server/                 # 后端代理服务（FastAPI，默认端口 3001）
│   ├── main.py             # FastAPI 服务：/api/chat、/api/a2ui/chat、/api/agent/chat、/api/food/chat、/api/models
│   ├── amap.py             # 高德 Web 服务封装（出行助手工具执行器）
│   ├── meituan.py          # 美团外卖开放平台封装（外卖点餐工具执行器 + 签名）
│   ├── weather.py          # Open-Meteo 天气封装（天气查询工具执行器，免凭据）
│   ├── requirements.txt    # Python 依赖清单
│   ├── .venv/              # Python 虚拟环境（不入库）
│   ├── .env                # 真实密钥（不入库，需自行创建）
│   └── .env.example        # 环境变量模板
└── client/                 # React 前端（默认端口 5173）
    ├── vite.config.js      # 开发环境 /api 代理到 localhost:3001
    ├── index.html          # 含主题防闪烁脚本
    └── src/
        ├── App.jsx         # 聊天主组件（SSE 解析、主题切换）
        ├── index.css       # 双主题样式（CSS 变量）
        └── main.jsx
```

## 快速开始

### 1. 环境要求

- Node.js ≥ 18（前端构建与启动脚本）
- Python ≥ 3.10（后端服务）

### 2. 安装依赖

```bash
# 前端依赖
npm install

# 后端依赖（首次需要，创建虚拟环境并安装）
python3 -m venv server/.venv
server/.venv/bin/pip install -r server/requirements.txt
```

### 3. 配置密钥

```bash
cd server
cp .env.example .env
```

编辑 `server/.env`，填入你的网关密钥：

```ini
ANTHROPIC_API_KEY=你的API-Key
ANTHROPIC_BASE_URL=https://open.bigmodel.cn/api/anthropic
# 可选：
# MODEL_ID=glm-4.5-air
# PORT=3001
```

> ⚠️ `.env` 已加入 `.gitignore`，请勿将密钥提交到 Git。

### 4. 启动

```bash
# 同时启动前后端（推荐）
npm run dev

# 或分别启动
npm run dev:server   # 后端 http://localhost:3001
npm run dev:client   # 前端 http://localhost:5173
```

打开浏览器访问 **http://localhost:5173** 即可使用。

## 后端接口

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/health` | 健康检查 |
| GET | `/api/models` | 透传网关模型列表（`/v1/models`） |
| POST | `/api/chat` | 问答接口，SSE 流式返回 |
| POST | `/api/a2ui/chat` | A2UI 场景问答（注入 A2UI 系统提示词） |
| POST | `/api/agent/chat` | 出行助手（高德工具调用循环），SSE 流式返回 |
| POST | `/api/food/chat` | 外卖点餐（美团工具调用 + A2UI 点餐界面），SSE 流式返回 |
| POST | `/api/weather/chat` | 天气查询（Open-Meteo 工具调用，免凭据），SSE 流式返回 |

`POST /api/chat` 请求体：

```json
{
  "model": "claude-4.6-sonnet",
  "messages": [{ "role": "user", "content": "你好" }],
  "max_tokens": 4096,
  "temperature": 0.7
}
```

响应为标准 Anthropic SSE 事件流（`message_start`、`content_block_delta`、`message_stop` 等），前端解析 `content_block_delta` 中的 `text_delta` 增量渲染。

## 环境变量

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `ANTHROPIC_API_KEY` | （必填） | 网关 API Key，从 `server/.env` 读取 |
| `ANTHROPIC_BASE_URL` | （必填） | Anthropic 兼容端点地址，如智谱 `https://open.bigmodel.cn/api/anthropic` |
| `MODEL_ID` | （可选） | 默认模型 ID，前端未指定模型时使用，并在模型列表中置顶 |
| `GATEWAY_BASE` | — | 旧变量名，仍兼容（`ANTHROPIC_BASE_URL` 优先） |
| `PORT` | `3001` | 后端端口 |
| `AMAP_KEY` | （可选） | 高德 Web 服务 Key，出行助手模式必需 |
| `AMAP_JS_KEY` / `AMAP_JS_SECURITY` | （可选） | 高德 JS API 凭据，配置后路线卡片升级为交互式底图 |
| `MEITUAN_APP_ID` / `MEITUAN_SECRET` | （可选） | 美团外卖开放平台凭据；未配置时外卖点餐自动使用本地演示数据 |
| `MEITUAN_MOCK` | 缺省自动 | `on` 强制演示数据 / `off` 强制真实 API；缺省时有凭据走真实、无凭据自动 mock |
| `MEITUAN_BASE` | `https://waimaiopen.meituan.com` | 美团开放平台地址，沙箱联调时按官方文档替换 |

## 生产构建

```bash
npm run build        # 构建前端到 client/dist
```

可将 `client/dist` 交由任意静态服务器托管，并将 `/api` 反向代理到后端服务；或由 FastAPI 挂载 `StaticFiles` 直接托管静态文件。

## 常见问题

- **模型名报错 `invalid_model_name`**：该网关使用自有模型命名（如 `claude-4.6-sonnet`、`gpt-5.5`、`glm-5`），并非 Anthropic 官方模型 ID。前端模型列表从 `/api/models` 动态获取，无需手动维护。
- **提示未配置 ANTHROPIC_API_KEY**：确认已创建 `server/.env` 并填写密钥后重启后端。
- **端口占用**：通过环境变量 `PORT`（后端）或 `vite.config.js` 中 `server.port`（前端）修改。
