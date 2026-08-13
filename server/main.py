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
from urllib.parse import quote

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response, StreamingResponse

# 从 server/.env 加载环境变量（.env 不入库，见 .gitignore）
BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / '.env')

PORT = int(os.getenv('PORT', '3001'))
# Anthropic 兼容网关地址，最终请求 {GATEWAY_BASE}/v1/messages
GATEWAY_BASE = os.getenv(
    'GATEWAY_BASE', 'https://lab.iwhalecloud.com/gpt-proxy/anthropic'
)
# API Key 仅从环境变量读取，禁止硬编码
API_KEY = os.getenv('ANTHROPIC_API_KEY', '')

GATEWAY_HEADERS = {
    'x-api-key': API_KEY,
    'anthropic-version': '2023-06-01',
}

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
    return {
        'jsKey': os.getenv('AMAP_JS_KEY', ''),
        'jsSecret': os.getenv('AMAP_JS_SECURITY', ''),
    }


@app.get('/api/models')
async def models():
    """模型列表：透传网关 /v1/models"""
    if not API_KEY:
        return JSONResponse(
            {'error': '服务端未配置 ANTHROPIC_API_KEY，请在 server/.env 中设置'},
            status_code=500,
        )
    try:
        async with httpx.AsyncClient(timeout=60) as client:
            upstream = await client.get(
                f'{GATEWAY_BASE}/v1/models', headers=GATEWAY_HEADERS
            )
    except httpx.HTTPError as err:
        print('[proxy] 获取模型列表失败:', err)
        return JSONResponse(
            {'error': f'获取模型列表失败: {err}'}, status_code=502
        )
    return Response(
        content=upstream.content,
        status_code=upstream.status_code,
        media_type='application/json',
    )


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
- components 中必须定义一个根容器组件（通常是 Column），beginRendering 的 root 必须指向这个已定义的根容器 id；所有其他组件的 id 都必须出现在根容器的 items（或嵌套容器的 items/child）中，不能只列叶子组件而漏掉根容器。
- 输出 beginRendering 前自检：root 引用的 id 是否已在 components 中定义；items/child 引用的每个 id 是否都已定义。任何引用缺失都会导致界面无法渲染。
- 组件 id 全局唯一；children/items/child/root 引用的 id 必须已在 components 中定义。
- 所有输入控件都必须绑定数据模型路径，且 updateDataModel 中要给出对应初始值。
- 优先使用选择而非输入：凡是选项可枚举的字段（种类、杯型、温度、甜度档位、配送方式、偏好、人数区间等）一律用 ChoicePicker（可多选时用 multipleSelection）；是否类用 CheckBox；连续数值用 Slider；只有真正自由的内容（姓名、电话、地址、备注）才用 TextField。ChoicePicker 的初始值必须是 options 中已有的 value（多选时为 value 数组）。
- 界面要完整收集任务所需信息，按钮 action 的 name 语义化（如 submit_booking）。
- 界面文字使用中文，控件标签简洁明确。

当用户消息以 "[A2UI_EVENT]" 开头时，表示用户刚在界面上完成操作，消息中包含界面提交的 JSON 数据：请基于数据直接给出简短的中文确认或处理结果，用普通文本（可用 Markdown）回复，不要再生成界面。"""


# ---------- 出行助手模式：模型以工具调用（function calling）方式调用高德服务 ----------
# 与「千问 × 高德」同款三层结构：
# 模型只决定"何时调用、传什么参数"；服务端真正请求高德 Web 服务 API 并回写 tool_result；
# 前端根据服务端下发的结构化数据（含静态地图 URL）渲染路线卡片。

AMAP_KEY = os.getenv('AMAP_KEY', '')
AMAP_BASE = 'https://restapi.amap.com'
MAX_TOOL_ROUNDS = 5

# Anthropic tools 定义：模型据此自主规划调用链（先地理编码取坐标，再路线规划）
AMAP_TOOLS = [
    {
        'name': 'maps_geo',
        'description': '高德地理编码：把地址/地名（如"深圳北站"）转换为经纬度坐标。路线规划工具需要坐标作为入参，应先用本工具取坐标。',
        'input_schema': {
            'type': 'object',
            'properties': {
                'address': {'type': 'string', 'description': '结构化地址或地名'},
                'city': {'type': 'string', 'description': '限定查询的城市名，如"深圳"，可省略'},
            },
            'required': ['address'],
        },
    },
    {
        'name': 'maps_text_search',
        'description': '高德 POI 关键字搜索：按名称搜索餐厅、酒店、景点等兴趣点，返回名称、地址与坐标。',
        'input_schema': {
            'type': 'object',
            'properties': {
                'keywords': {'type': 'string', 'description': '搜索关键词'},
                'city': {'type': 'string', 'description': '城市名，如"上海"'},
            },
            'required': ['keywords'],
        },
    },
    {
        'name': 'maps_around_search',
        'description': '高德周边 POI 搜索：以某坐标为中心搜索餐厅、酒店、景点等兴趣点，返回名称、地址、坐标、评分、人均消费、距中心距离与照片。用户要"附近/周边/旁边"的推荐时，先用 maps_geo 取中心坐标再调用本工具。',
        'input_schema': {
            'type': 'object',
            'properties': {
                'center': {'type': 'string', 'description': '中心坐标 lng,lat（来自 maps_geo）'},
                'keywords': {'type': 'string', 'description': '搜索关键词，如"餐厅"，可省略'},
                'radius': {'type': 'integer', 'description': '搜索半径（米），默认 1000'},
                'center_name': {'type': 'string', 'description': '中心地名，用于界面卡片展示'},
            },
            'required': ['center'],
        },
    },
    {
        'name': 'maps_direction_transit_integrated',
        'description': '高德公交路线规划：根据起终点坐标规划公交/地铁通勤方案，返回多个方案的耗时、距离、步行距离与分段明细（线路、上下车站、站数）。',
        'input_schema': {
            'type': 'object',
            'properties': {
                'origin': {'type': 'string', 'description': '起点坐标 lng,lat（来自 maps_geo）'},
                'destination': {'type': 'string', 'description': '终点坐标 lng,lat'},
                'city': {'type': 'string', 'description': '起点所在城市名，如"深圳"'},
                'cityd': {'type': 'string', 'description': '终点所在城市名，同城可省略'},
                'origin_name': {'type': 'string', 'description': '起点地名，用于界面卡片展示'},
                'destination_name': {'type': 'string', 'description': '终点地名，用于界面卡片展示'},
            },
            'required': ['origin', 'destination', 'city'],
        },
    },
    {
        'name': 'maps_direction_driving',
        'description': '高德驾车路线规划：根据起终点坐标规划驾车路线，返回耗时、距离、途经主要道路。',
        'input_schema': {
            'type': 'object',
            'properties': {
                'origin': {'type': 'string', 'description': '起点坐标 lng,lat'},
                'destination': {'type': 'string', 'description': '终点坐标 lng,lat'},
                'origin_name': {'type': 'string', 'description': '起点地名，用于界面卡片展示'},
                'destination_name': {'type': 'string', 'description': '终点地名，用于界面卡片展示'},
            },
            'required': ['origin', 'destination'],
        },
    },
]

TOOL_LABELS = {
    'maps_geo': '地理编码',
    'maps_text_search': 'POI 搜索',
    'maps_around_search': '周边搜索',
    'maps_direction_transit_integrated': '公交路线规划',
    'maps_direction_driving': '驾车路线规划',
}


def build_agent_system_prompt() -> str:
    return (
        '你是出行助手，已接入高德地图实时服务（地理编码、POI 搜索、周边搜索、公交/驾车路线规划）。\n'
        '工作流程：用户询问路线、怎么走时，先用 maps_geo 把地名转成坐标，再调用对应路线规划工具；'
        '用户要"附近/周边"的餐厅、酒店、景点推荐时，先 maps_geo 取中心坐标，再调用 maps_around_search；'
        '路线卡片与 POI 列表卡片由系统自动附在回答上方，正文只需给出简明的中文建议'
        '（推荐理由、人均、特色等，可结合评分与人均消费），严禁编造任何数据。\n'
        '工具调用失败时，根据错误信息说明原因并给出替代建议。'
    )


def sse_event(obj) -> str:
    """把转发/自定义事件序列化为一条 SSE data 帧"""
    return f'data: {json.dumps(obj, ensure_ascii=False)}\n\n'


async def amap_get(path: str, params: dict) -> dict:
    """请求高德 Web 服务 API（统一注入 key、过滤空参）"""
    params = {k: v for k, v in params.items() if v not in (None, '')}
    params['key'] = AMAP_KEY
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(f'{AMAP_BASE}{path}', params=params)
        resp.raise_for_status()
        return resp.json()


def downsample(points: list, limit: int = 30) -> list:
    """折线点均匀抽样，控制静态地图 URL 长度"""
    if len(points) <= limit:
        return points
    step = (len(points) - 1) / (limit - 1)
    return [points[round(i * step)] for i in range(limit)]


def build_static_map_url(origin: str, destination: str, polyline: list) -> str:
    """生成带起终点标记 + 路线折线的静态地图 URL（无需前端 JS SDK）"""
    pts = downsample([p for p in polyline if p], 30)
    if not pts:
        return ''
    markers = f'mid,0x0080FF,起:{origin}|mid,0xFF0000,终:{destination}'
    paths = '6,0x00B0FF,1,,:' + ';'.join(pts)
    return (
        f'{AMAP_BASE}/v3/staticmap?size=750*400&scale=2'
        f'&markers={quote(markers, safe=":,;|")}'
        f'&paths={quote(paths, safe=":,;|")}'
        f'&key={AMAP_KEY}'
    )


async def exec_geo(args: dict):
    data = await amap_get('/v3/geocode/geo', {'address': args.get('address'), 'city': args.get('city')})
    if data.get('status') != '1' or not data.get('geocodes'):
        return {'error': data.get('info', '地理编码失败，未找到匹配地址')}, None
    geo = data['geocodes'][0]
    return {'location': geo.get('location'), 'formatted_address': geo.get('formatted_address')}, None


def parse_poi(p: dict) -> dict:
    """提取 POI 卡片字段：评分/人均在 biz_ext 下；评语摘录覆盖稀疏，可能为空"""
    photos = p.get('photos') or []
    dist = p.get('distance')
    biz = p.get('biz_ext') or {}

    def num(v):
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    type_segs = [s for s in (p.get('type') or '').split(';') if s]
    reviews = p.get('featured_reviews') or []
    r0 = reviews[0] if reviews else None
    review = r0 if isinstance(r0, str) else (r0.get('review') or r0.get('content')) if isinstance(r0, dict) else None
    return {
        'id': p.get('id'),
        'name': p.get('name'),
        'address': p.get('address'),
        'location': p.get('location'),
        'district': p.get('adname'),
        'rating': num(biz.get('rating')),
        'cost': num(biz.get('cost')),
        'type': p.get('keytag') or (type_segs[1] if len(type_segs) > 1 else (type_segs[0] if type_segs else None)),
        'tags': [t for t in (p.get('tag') or '').split(',') if t][:4],
        'review': review,
        'distance': int(dist) if str(dist).isdigit() else None,
        'photo': photos[0].get('url') if photos else None,
    }


async def exec_text_search(args: dict):
    data = await amap_get('/v3/place/text', {
        'keywords': args.get('keywords'), 'city': args.get('city'), 'extensions': 'all',
    })
    if data.get('status') != '1':
        return {'error': data.get('info', '搜索失败')}, None
    pois = [parse_poi(p) for p in data.get('pois', [])[:5]]
    return {'pois': pois}, {
        'kind': 'poi_list',
        'title': f'为你找到: {args.get("keywords") or "相关地点"}',
        'pois': pois,
    }


async def exec_around_search(args: dict):
    data = await amap_get('/v3/place/around', {
        'location': args.get('center'), 'keywords': args.get('keywords'),
        'radius': args.get('radius') or 1000, 'extensions': 'all',
    })
    if data.get('status') != '1':
        return {'error': data.get('info', '周边搜索失败')}, None
    pois = [parse_poi(p) for p in data.get('pois', [])[:5]]
    return {'center': args.get('center'), 'pois': pois}, {
        'kind': 'poi_list',
        'title': f'{args.get("center_name") or "附近"}: {args.get("keywords") or "热门推荐"}',
        'pois': pois,
    }


def parse_transit_segments(transit: dict):
    """解析单个公交方案：步行/乘车分段列表 + 全程折线点"""
    segments, polyline = [], []
    for seg in transit.get('segments', []):
        walking = seg.get('walking') or {}
        if str(walking.get('distance') or '0') not in ('0', ''):
            segments.append({
                'mode': 'walk',
                'distance_m': int(walking.get('distance', 0)),
                'duration_min': round(int(walking.get('duration') or 0) / 60),
            })
        for line in ((seg.get('bus') or {}).get('buslines') or [])[:1]:
            polyline.extend(p for p in (line.get('polyline') or '').split(';') if p)
            segments.append({
                'mode': 'bus',
                'line': (line.get('name') or '').split('(')[0],
                'from': (line.get('departure_stop') or {}).get('name'),
                'to': (line.get('arrival_stop') or {}).get('name'),
                'stops': int(line.get('via_num') or 0),
                'duration_min': round(int(line.get('duration') or 0) / 60),
            })
    return segments, polyline


async def exec_transit(args: dict):
    data = await amap_get('/v3/direction/transit/integrated', {
        'origin': args.get('origin'), 'destination': args.get('destination'),
        'city': args.get('city'), 'cityd': args.get('cityd'),
    })
    if data.get('status') != '1':
        return {'error': data.get('info', '公交规划失败')}, None
    route = data.get('route') or {}
    plans, card = [], None
    for transit in route.get('transits', [])[:3]:
        segments, polyline = parse_transit_segments(transit)
        plan = {
            'duration_min': round(int(transit.get('duration') or 0) / 60),
            'distance_km': round(int(transit.get('distance') or 0) / 1000, 1),
            'walking_m': int(transit.get('walking_distance') or 0),
            'segments': segments,
        }
        plans.append(plan)
        if card is None and polyline:
            card = {
                'kind': 'transit',
                'title': f'公交前往: {args.get("destination_name") or "目的地"}',
                'map_url': build_static_map_url(args.get('origin', ''), args.get('destination', ''), polyline),
                'origin': args.get('origin'), 'destination': args.get('destination'),
                'origin_name': args.get('origin_name'), 'destination_name': args.get('destination_name'),
                'city': args.get('city'),
                'polyline': downsample(polyline, 100),
                'duration_min': plan['duration_min'],
                'distance_km': plan['distance_km'],
                'stops': sum(s['stops'] for s in segments if s['mode'] == 'bus'),
                'segments': segments,
            }
    return {'taxi_cost': route.get('taxi_cost'), 'plans': plans}, card


async def exec_driving(args: dict):
    data = await amap_get('/v3/direction/driving', {
        'origin': args.get('origin'), 'destination': args.get('destination'),
    })
    if data.get('status') != '1':
        return {'error': data.get('info', '驾车规划失败')}, None
    path = ((data.get('route') or {}).get('paths') or [{}])[0]
    polyline, roads = [], []
    for step in path.get('steps', []):
        polyline.extend(p for p in (step.get('polyline') or '').split(';') if p)
        if step.get('road') and step['road'] not in roads:
            roads.append(step['road'])
    result = {
        'duration_min': round(int(path.get('duration') or 0) / 60),
        'distance_km': round(int(path.get('distance') or 0) / 1000, 1),
        'tolls': path.get('tolls', '0'),
        'main_roads': roads[:8],
    }
    card = {
        'kind': 'driving',
        'title': f'驾车前往: {args.get("destination_name") or "目的地"}',
        'map_url': build_static_map_url(args.get('origin', ''), args.get('destination', ''), polyline),
        'origin': args.get('origin'), 'destination': args.get('destination'),
        'origin_name': args.get('origin_name'), 'destination_name': args.get('destination_name'),
        'polyline': downsample(polyline, 100),
        'duration_min': result['duration_min'],
        'distance_km': result['distance_km'],
        'segments': [{'mode': 'drive', 'road': r} for r in roads[:5]],
    }
    return result, card


async def execute_amap_tool(name: str, args: dict):
    """执行一次高德工具调用，返回 (给模型的数据, 给前端的路线卡片)"""
    try:
        if name == 'maps_geo':
            return await exec_geo(args)
        if name == 'maps_text_search':
            return await exec_text_search(args)
        if name == 'maps_around_search':
            return await exec_around_search(args)
        if name == 'maps_direction_transit_integrated':
            return await exec_transit(args)
        if name == 'maps_direction_driving':
            return await exec_driving(args)
        return {'error': f'未知工具: {name}'}, None
    except httpx.HTTPError as err:
        return {'error': f'高德服务请求失败: {err}'}, None


def summarize_tool_result(name: str, result: dict) -> str:
    """工具结果的一句话摘要，供前端工具 chip 展示"""
    if result.get('error'):
        return '失败'
    if name == 'maps_geo':
        return result.get('location', '')
    if name in ('maps_text_search', 'maps_around_search'):
        return f'{len(result.get("pois", []))} 条结果'
    if name == 'maps_direction_transit_integrated':
        return f'{len(result.get("plans", []))} 个方案'
    if name == 'maps_direction_driving':
        return f'约 {result.get("duration_min")} 分钟'
    return ''


async def stream_chat(request: Request, system: str | None = None):
    """通用流式问答：透传网关 SSE；system 可选，用于注入场景提示词"""
    body = await request.json()
    messages = body.get('messages')

    if not isinstance(messages, list) or not messages:
        return JSONResponse({'error': 'messages 不能为空'}, status_code=400)

    if not API_KEY:
        return JSONResponse(
            {'error': '服务端未配置 ANTHROPIC_API_KEY，请在 server/.env 中设置'},
            status_code=500,
        )

    payload = {
        'model': body.get('model'),
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
    if not API_KEY:
        return JSONResponse(
            {'error': '服务端未配置 ANTHROPIC_API_KEY，请在 server/.env 中设置'},
            status_code=500,
        )
    if not AMAP_KEY:
        return JSONResponse(
            {'error': '服务端未配置 AMAP_KEY，请在 server/.env 中填入高德 Web 服务 Key'},
            status_code=500,
        )

    history = [{'role': m.get('role'), 'content': m.get('content')} for m in messages]

    async def generate():
        client = httpx.AsyncClient(timeout=httpx.Timeout(60.0, read=None))
        try:
            for _ in range(MAX_TOOL_ROUNDS):
                payload = {
                    'model': body.get('model'),
                    'messages': history,
                    'max_tokens': body.get('max_tokens', 4096),
                    'stream': True,
                    'tools': AMAP_TOOLS,
                    'system': build_agent_system_prompt(),
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
                                    'label': TOOL_LABELS.get(current['name'], current['name']),
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
                    result, card = await execute_amap_tool(block['name'], tool_input)
                    if card:
                        # poi_list 走独立事件，客户端分别渲染 POI 列表卡片 / 路线卡片
                        yield sse_event({
                            'type': 'amap_poi_list' if card.get('kind') == 'poi_list' else 'amap_card',
                            'card': card,
                        })
                    yield sse_event({
                        'type': 'tool_call', 'name': block['name'],
                        'label': TOOL_LABELS.get(block['name'], block['name']),
                        'status': 'done', 'summary': summarize_tool_result(block['name'], result),
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
    print(f'[server] 网关地址: {GATEWAY_BASE}/v1/messages')
    if not API_KEY:
        print('[server] ⚠️ 未检测到 ANTHROPIC_API_KEY，请复制 .env.example 为 .env 并填入密钥')
    uvicorn.run(app, host='0.0.0.0', port=PORT)
