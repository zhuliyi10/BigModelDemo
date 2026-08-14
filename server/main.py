"""后端代理服务：以 SSE 流式方式转发 Anthropic 兼容网关的 /v1/messages 接口。

接口与原 Express 版本保持一致：
- GET  /api/health     健康检查
- GET  /api/models     透传网关模型列表
- POST /api/chat       通用流式问答
- POST /api/a2ui/chat  A2UI 场景问答（注入 A2UI 系统提示词）
- POST /api/agent/chat 出行助手问答（模型以工具调用方式调用高德服务）
"""

import asyncio
import json
import os
from datetime import datetime
from pathlib import Path

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response, StreamingResponse

from amap import AmapService

# 从 server/.env 加载环境变量（.env 不入库，见 .gitignore）
BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / '.env')

PORT = int(os.getenv('PORT', '3001'))
# Anthropic 兼容网关地址，最终请求 {GATEWAY_BASE}/v1/messages
# 优先读标准的 ANTHROPIC_BASE_URL（如智谱 https://open.bigmodel.cn/api/anthropic），兼容旧变量名 GATEWAY_BASE
GATEWAY_BASE = (os.getenv('ANTHROPIC_BASE_URL') or os.getenv('GATEWAY_BASE', '')).rstrip('/')
# API Key 仅从环境变量读取，禁止硬编码
API_KEY = os.getenv('ANTHROPIC_API_KEY', '')
# 默认模型 ID：前端未指定模型或网关不提供模型列表时使用（如 glm-4.5-air）
DEFAULT_MODEL_ID = os.getenv('MODEL_ID', '')

GATEWAY_HEADERS = {
    'x-api-key': API_KEY,
    'anthropic-version': '2023-06-01',
}


def missing_llm_config():
    """校验 Anthropic 兼容网关配置：缺失时返回错误响应，完整时返回 None"""
    if not GATEWAY_BASE:
        return JSONResponse(
            {'error': '服务端未配置 ANTHROPIC_BASE_URL，请在 server/.env 中设置'},
            status_code=500,
        )
    if not API_KEY:
        return JSONResponse(
            {'error': '服务端未配置 ANTHROPIC_API_KEY，请在 server/.env 中设置'},
            status_code=500,
        )
    return None


app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=['*'],
    allow_methods=['*'],
    allow_headers=['*'],
)


@app.get('/api/health')
async def health():
    return {'ok': True}


@app.get('/api/amap/config')
async def amap_config():
    """前端高德 JS API 配置：配置后底图升级为交互式地图，未配置时前端回退静态图"""
    return amap.js_config()


@app.get('/api/models')
async def models():
    """模型列表：透传网关 /v1/models；网关不提供列表（如智谱端点）时用 MODEL_ID 兜底"""
    err = missing_llm_config()
    if err:
        return err
    try:
        async with httpx.AsyncClient(timeout=60) as client:
            upstream = await client.get(
                f'{GATEWAY_BASE}/v1/models', headers=GATEWAY_HEADERS
            )
    except httpx.HTTPError as err:
        print('[proxy] 获取模型列表失败:', err)
        upstream = None

    if upstream is not None and upstream.status_code < 400:
        try:
            data = upstream.json()
            if data.get('data'):
                # 配置了 MODEL_ID 时将其置顶，前端默认选中第一项
                if DEFAULT_MODEL_ID:
                    items = data['data']
                    hit = [m for m in items if m.get('id') == DEFAULT_MODEL_ID]
                    data['data'] = hit + [m for m in items if m.get('id') != DEFAULT_MODEL_ID]
                return JSONResponse(data)
        except json.JSONDecodeError:
            pass

    if DEFAULT_MODEL_ID:
        return {'data': [{'id': DEFAULT_MODEL_ID}]}
    if upstream is not None:
        return Response(
            content=upstream.content,
            status_code=upstream.status_code,
            media_type='application/json',
        )
    return JSONResponse({'error': '获取模型列表失败'}, status_code=502)


# A2UI 场景的系统提示词：约束模型按 A2UI v0.9 协议输出可渲染的界面描述
# 动态生成：注入当前日期，避免模型在处理"今天/明天/下周"等相对日期时猜错
def build_a2ui_system_prompt() -> str:
    now = datetime.now()
    weekdays = ['一', '二', '三', '四', '五', '六', '日']
    today = now.strftime('%Y-%m-%d')
    weekday = weekdays[now.weekday()]

    return f"""你是一个支持 A2UI（Agent-to-User Interface）协议的智能体。当用户提出需要界面交互的需求（预订、填表、点单、配置、调研展示等）时，不要只用文字追问，而是直接生成一个可交互的界面。

当前日期是 {today}（星期{weekday}）。处理"今天、明天、后天、下周"等相对日期时，必须以此为准换算出确切的 YYYY-MM-DD，严禁编造日期。

回复格式要求（严格遵守）：
1. 每条 A2UI 消息是独立一行的紧凑 JSON 对象，用 ```a2ui 代码块包裹；代码块外只允许有一句简短的中文说明。
2. 消息按顺序依次为：createSurface、updateComponents、updateDataModel（可选）、beginRendering。

消息结构：
{{"version":"v0.9","createSurface":{{"surfaceId":"<id>","sendDataModelWithEvents":true}}}}
{{"version":"v0.9","updateComponents":{{"surfaceId":"<id>","components":[...]}}}}
{{"version":"v0.9","updateDataModel":{{"surfaceId":"<id>","path":"/","value":{{...}}}}}}
{{"version":"v0.9","beginRendering":{{"surfaceId":"<id>","root":"<根组件id>"}}}}

updateComponents 完整示例（注意：components 必须包含一个根容器 Column，且它的 id 与 beginRendering 的 root 一致；可枚举字段用 ChoicePicker 而非 TextField）：
{{"version":"v0.9","updateComponents":{{"surfaceId":"demo-001","components":[{{"id":"demo_title","component":"Text","text":"示例表单","variant":"h1"}},{{"id":"demo_type","component":"ChoicePicker","label":"类型","value":{{"path":"/type"}},"options":[{{"label":"堂食","value":"堂食"}},{{"label":"外带","value":"外带"}},{{"label":"外卖","value":"外卖"}}],"variant":"mutuallyExclusive"}},{{"id":"demo_name","component":"TextField","label":"姓名","value":{{"path":"/name"}},"textualType":"shortText","placeholder":"请输入姓名"}},{{"id":"demo_submit_label","component":"Text","text":"提交","variant":"body"}},{{"id":"demo_submit","component":"Button","child":"demo_submit_label","variant":"primary","action":{{"event":{{"name":"submit_demo"}}}}}},{{"id":"demo_root","component":"Column","items":["demo_title","demo_type","demo_name","demo_submit"],"gap":"medium","align":"stretch"}}]}}}}

组件目录（component 字段取组件名；props 为字面值，或 {{"path":"/a/b"}} 绑定数据模型）：
- Text: {{"component":"Text","text":"文本","variant":"h1|h2|h3|body|caption"}}
- Button: {{"component":"Button","child":"<子组件id，通常是Text>","variant":"primary|secondary","action":{{"event":{{"name":"<事件名>"}}}}}}
- TextField: {{"component":"TextField","label":"标签","value":{{"path":"/x"}},"textualType":"shortText|longText|number","placeholder":"提示"}}
- CheckBox: {{"component":"CheckBox","label":"标签","value":{{"path":"/x"}}}}
- ChoicePicker: {{"component":"ChoicePicker","label":"标签","value":{{"path":"/x"}},"options":[{{"label":"拿铁","value":"拿铁"}},{{"label":"美式","value":"美式"}}],"variant":"mutuallyExclusive|multipleSelection"}}（单选值为字符串，多选值为数组；渲染为可点选的选项块）
- Slider: {{"component":"Slider","label":"标签","value":{{"path":"/x"}},"minValue":0,"maxValue":100}}
- DateTimeInput: {{"component":"DateTimeInput","label":"标签","value":{{"path":"/x"}},"enableDate":true,"enableTime":true}}
- Card: {{"component":"Card","child":"<子组件id>","title":"可选标题"}}
- Column: {{"component":"Column","items":["id1","id2"],"gap":"small|medium","align":"start|center|stretch"}}
- Row: {{"component":"Row","items":["id1","id2"],"gap":"small|medium","align":"center|space-between"}}
- List: {{"component":"List","repeated":{{"componentId":"<模板组件id>","path":"/数组路径"}}}}
- Image: {{"component":"Image","url":"https://...","description":"描述"}}
- Divider: {{"component":"Divider"}}

规则：
- JSON 必须是合法格式：只能使用 ASCII 标点（逗号 ","、冒号 ":"），严禁在 JSON 结构中使用全角/中文标点（如 "，"、"："），组件数组元素之间必须用英文逗号分隔。
- 键与值之间必须用英文冒号分隔（如 "id":"trip_budget"），严禁漏写冒号写成 "id="trip_budget" 或 "id""trip_budget"。
- components 中必须定义一个根容器组件（通常是 Column），beginRendering 的 root 必须指向这个已定义的根容器 id；所有其他组件的 id 都必须出现在根容器的 items（或嵌套容器的 items/child）中，不能只列叶子组件而漏掉根容器。
- 输出 beginRendering 前自检：root 引用的 id 是否已在 components 中定义；items/child 引用的每个 id 是否都已定义。任何引用缺失都会导致界面无法渲染。
- 组件 id 全局唯一；children/items/child/root 引用的 id 必须已在 components 中定义。
- 所有输入控件都必须绑定数据模型路径，且 updateDataModel 中要给出对应初始值。
- 优先使用选择而非输入：凡是选项可枚举的字段（种类、杯型、温度、甜度档位、配送方式、偏好、人数区间等）一律用 ChoicePicker（可多选时用 multipleSelection）；是否类用 CheckBox；连续数值用 Slider；只有真正自由的内容（姓名、电话、地址、备注）才用 TextField。ChoicePicker 的初始值必须是 options 中已有的 value（多选时为 value 数组）。
- 界面要完整收集任务所需信息，按钮 action 的 name 语义化（如 submit_booking）。
- 界面文字使用中文，控件标签简洁明确。

当用户消息以 "[A2UI_EVENT]" 开头时，表示用户刚在界面上完成操作，消息中包含界面提交的 JSON 数据：请基于数据直接给出简短的中文确认或处理结果，用普通文本（可用 Markdown）回复，不要再生成界面。"""


# ---------- 出行助手模式：高德服务封装在 amap.py（AmapService） ----------
# 模型只决定"何时调用、传什么参数"；服务端真正请求高德 Web 服务 API 并回写 tool_result；
# 前端根据服务端下发的结构化数据（含静态地图 URL）渲染路线卡片。
amap = AmapService()


def sse_event(obj) -> str:
    """把转发/自定义事件序列化为一条 SSE data 帧"""
    return f'data: {json.dumps(obj, ensure_ascii=False)}\n\n'


async def stream_chat(request: Request, system: str | None = None):
    """通用流式问答：透传网关 SSE；system 可选，用于注入场景提示词"""
    body = await request.json()
    messages = body.get('messages')

    if not isinstance(messages, list) or not messages:
        return JSONResponse({'error': 'messages 不能为空'}, status_code=400)

    err = missing_llm_config()
    if err:
        return err

    payload = {
        'model': body.get('model') or DEFAULT_MODEL_ID,
        'messages': messages,
        'max_tokens': body.get('max_tokens', 4096),
        'stream': True,
    }
    if system:
        payload['system'] = system
    temperature = body.get('temperature')
    if isinstance(temperature, (int, float)) and not isinstance(temperature, bool):
        payload['temperature'] = temperature

    # 较长的读超时：SSE 流可能持续较久
    client = httpx.AsyncClient(timeout=httpx.Timeout(60.0, read=None))
    try:
        upstream = await client.send(
            client.build_request(
                'POST',
                f'{GATEWAY_BASE}/v1/messages',
                json=payload,
                headers={'content-type': 'application/json', **GATEWAY_HEADERS},
            ),
            stream=True,
        )
    except httpx.HTTPError as err:
        await client.aclose()
        print('[proxy] 请求网关失败:', err)
        return JSONResponse({'error': f'请求网关失败: {err}'}, status_code=502)

    # 非 SSE 响应（通常是错误 JSON）：原样透传
    content_type = upstream.headers.get('content-type', '')
    if upstream.status_code >= 400 or 'text/event-stream' not in content_type:
        text = (await upstream.aread()).decode('utf-8', errors='replace')
        print('[proxy] 网关返回异常:', upstream.status_code, text)
        await upstream.aclose()
        await client.aclose()
        return Response(
            content=text,
            status_code=upstream.status_code,
            media_type='application/json',
        )

    # SSE 流式透传；客户端断开时生成器被取消，finally 中关闭上游连接
    async def iterate():
        try:
            async for chunk in upstream.aiter_bytes():
                yield chunk
        except asyncio.CancelledError:
            raise
        except httpx.HTTPError as err:
            print('[proxy] 流读取异常:', err)
        finally:
            await upstream.aclose()
            await client.aclose()

    return StreamingResponse(
        iterate(),
        media_type='text/event-stream; charset=utf-8',
        headers={
            'Cache-Control': 'no-cache, no-transform',
            'Connection': 'keep-alive',
            'X-Accel-Buffering': 'no',
        },
    )


@app.post('/api/chat')
async def chat(request: Request):
    """问答接口：接收前端 messages，流式转发网关的 SSE 响应"""
    return await stream_chat(request)


@app.post('/api/a2ui/chat')
async def a2ui_chat(request: Request):
    """A2UI 场景接口：注入 A2UI 系统提示词，让模型输出可渲染的界面描述"""
    return await stream_chat(request, build_a2ui_system_prompt())


@app.post('/api/agent/chat')
async def agent_chat(request: Request):
    """出行助手接口：携带高德工具，解析 tool_use 并执行，循环至模型给出最终回答"""
    body = await request.json()
    messages = body.get('messages')
    if not isinstance(messages, list) or not messages:
        return JSONResponse({'error': 'messages 不能为空'}, status_code=400)
    err = missing_llm_config()
    if err:
        return err
    if not amap.configured:
        return JSONResponse(
            {'error': '服务端未配置 AMAP_KEY，请在 server/.env 中填入高德 Web 服务 Key'},
            status_code=500,
        )

    history = [{'role': m.get('role'), 'content': m.get('content')} for m in messages]

    async def generate():
        client = httpx.AsyncClient(timeout=httpx.Timeout(60.0, read=None))
        try:
            for _ in range(AmapService.MAX_TOOL_ROUNDS):
                payload = {
                    'model': body.get('model') or DEFAULT_MODEL_ID,
                    'messages': history,
                    'max_tokens': body.get('max_tokens', 4096),
                    'stream': True,
                    'tools': amap.tools,
                    'system': amap.system_prompt(),
                }
                try:
                    upstream = await client.send(
                        client.build_request(
                            'POST',
                            f'{GATEWAY_BASE}/v1/messages',
                            json=payload,
                            headers={'content-type': 'application/json', **GATEWAY_HEADERS},
                        ),
                        stream=True,
                    )
                except httpx.HTTPError as err:
                    yield sse_event({'type': 'error', 'error': {'message': f'请求网关失败: {err}'}})
                    break
                if upstream.status_code >= 400:
                    text = (await upstream.aread()).decode('utf-8', errors='replace')
                    await upstream.aclose()
                    yield sse_event({'type': 'error', 'error': {'message': text}})
                    break

                # 逐帧解析 SSE：原样转发给前端，同时累积 text / tool_use 块
                blocks, current, stop_reason, buf = [], None, None, ''
                async for chunk in upstream.aiter_text():
                    buf += chunk
                    while '\n' in buf:
                        line, buf = buf.split('\n', 1)
                        line = line.strip()
                        if not line.startswith('data:'):
                            continue
                        data = line[5:].strip()
                        if not data:
                            continue
                        try:
                            ev = json.loads(data)
                        except json.JSONDecodeError:
                            continue
                        yield sse_event(ev)
                        etype = ev.get('type')
                        if etype == 'content_block_start':
                            cb = ev.get('content_block') or {}
                            if cb.get('type') == 'tool_use':
                                current = {'type': 'tool_use', 'id': cb.get('id'), 'name': cb.get('name'), 'input': ''}
                                blocks.append(current)
                                yield sse_event({
                                    'type': 'tool_call', 'name': current['name'],
                                    'label': amap.tool_label(current['name']),
                                    'status': 'calling',
                                })
                            elif cb.get('type') == 'text':
                                current = {'type': 'text', 'text': ''}
                                blocks.append(current)
                            else:
                                current = None
                        elif etype == 'content_block_delta':
                            delta = ev.get('delta') or {}
                            if delta.get('type') == 'text_delta' and current and current['type'] == 'text':
                                current['text'] += delta.get('text', '')
                            elif delta.get('type') == 'input_json_delta' and current and current['type'] == 'tool_use':
                                current['input'] += delta.get('partial_json', '')
                        elif etype == 'content_block_stop':
                            current = None
                        elif etype == 'message_delta':
                            stop_reason = (ev.get('delta') or {}).get('stop_reason') or stop_reason
                await upstream.aclose()

                tool_uses = [b for b in blocks if b['type'] == 'tool_use']
                if stop_reason != 'tool_use' or not tool_uses:
                    break  # 模型已给出最终回答，结束循环

                # 组装 assistant 消息（text + tool_use），执行工具并回写 tool_result 继续循环
                assistant_content, results = [], []
                for block in blocks:
                    if block['type'] == 'text':
                        if block['text']:
                            assistant_content.append({'type': 'text', 'text': block['text']})
                        continue
                    try:
                        tool_input = json.loads(block['input'] or '{}')
                    except json.JSONDecodeError:
                        tool_input = {}
                    assistant_content.append(
                        {'type': 'tool_use', 'id': block['id'], 'name': block['name'], 'input': tool_input}
                    )
                    result, card = await amap.execute_tool(block['name'], tool_input)
                    if card:
                        # poi_list 走独立事件，客户端分别渲染 POI 列表卡片 / 路线卡片
                        yield sse_event({
                            'type': 'amap_poi_list' if card.get('kind') == 'poi_list' else 'amap_card',
                            'card': card,
                        })
                    yield sse_event({
                        'type': 'tool_call', 'name': block['name'],
                        'label': amap.tool_label(block['name']),
                        'status': 'done', 'summary': amap.summarize_result(block['name'], result),
                    })
                    results.append({
                        'type': 'tool_result', 'tool_use_id': block['id'],
                        'content': json.dumps(result, ensure_ascii=False),
                    })
                history.append({'role': 'assistant', 'content': assistant_content})
                history.append({'role': 'user', 'content': results})
            # 终止帧：RN 客户端（react-native-sse）在流结束后会自动重连，需明确信号结束会话
            yield sse_event({'type': 'agent_done'})
        finally:
            await client.aclose()

    return StreamingResponse(
        generate(),
        media_type='text/event-stream; charset=utf-8',
        headers={
            'Cache-Control': 'no-cache, no-transform',
            'Connection': 'keep-alive',
            'X-Accel-Buffering': 'no',
        },
    )


if __name__ == '__main__':
    import uvicorn

    print(f'[server] 代理服务已启动: http://localhost:{PORT}')
    print(f'[server] 网关地址: {GATEWAY_BASE}/v1/messages' if GATEWAY_BASE else '[server] ⚠️ 未配置网关地址')
    if not GATEWAY_BASE:
        print('[server] ⚠️ 未检测到 ANTHROPIC_BASE_URL，请在 server/.env 中设置 Anthropic 兼容端点地址')
    if not API_KEY:
        print('[server] ⚠️ 未检测到 ANTHROPIC_API_KEY，请复制 .env.example 为 .env 并填入密钥')
    uvicorn.run(app, host='0.0.0.0', port=PORT)
