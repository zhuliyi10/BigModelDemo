# 大模型问答 Demo

一个基于 **React + Node.js** 的大模型问答应用。前端使用 React（Vite）实现聊天界面，后端作为代理服务，以流式（SSE）方式转发 [Anthropic 兼容网关](https://lab.iwhalecloud.com/gpt-proxy/anthropic) 的 `/v1/messages` 接口。API Key 仅保存在服务端环境变量中，前端不接触密钥。

## 功能特性

- 💬 多轮对话，AI 回复**流式逐字渲染**
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
| 后端 | Node.js（原生 fetch）、Express、cors、dotenv |
| 通信 | 前端 → 后端 `/api/chat` → 网关 `/v1/messages`，SSE 流式透传 |

## 项目结构

```
BigModelDemo/
├── package.json            # npm workspaces 根配置，统一启动脚本
├── server/                 # 后端代理服务（默认端口 3001）
│   ├── index.js            # Express 服务：/api/chat、/api/models、/api/health
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

- Node.js ≥ 18（需原生 `fetch` 支持，推荐 20+）

### 2. 安装依赖

```bash
npm install
```

### 3. 配置密钥

```bash
cd server
cp .env.example .env
```

编辑 `server/.env`，填入你的网关密钥：

```ini
ANTHROPIC_API_KEY=你的API-Key
# 可选：
# GATEWAY_BASE=https://lab.iwhalecloud.com/gpt-proxy/anthropic
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
| `GATEWAY_BASE` | `https://lab.iwhalecloud.com/gpt-proxy/anthropic` | 网关地址 |
| `PORT` | `3001` | 后端端口 |

## 生产构建

```bash
npm run build        # 构建前端到 client/dist
```

可将 `client/dist` 交由任意静态服务器托管，并将 `/api` 反向代理到后端服务；或直接由 Express 托管静态文件。

## 常见问题

- **模型名报错 `invalid_model_name`**：该网关使用自有模型命名（如 `claude-4.6-sonnet`、`gpt-5.5`、`glm-5`），并非 Anthropic 官方模型 ID。前端模型列表从 `/api/models` 动态获取，无需手动维护。
- **提示未配置 ANTHROPIC_API_KEY**：确认已创建 `server/.env` 并填写密钥后重启后端。
- **端口占用**：通过环境变量 `PORT`（后端）或 `vite.config.js` 中 `server.port`（前端）修改。
