"""高德地图服务封装：Web 服务 API 客户端 + 出行助手工具（function calling）执行器。

与「千问 × 高德」同款三层结构：
- 模型只决定"何时调用、传什么参数"（AMAP_TOOLS 定义）；
- AmapService 真正请求高德 Web 服务 API，并把原始响应解析为精简数据；
- 每次工具执行同时产出两份结果：给模型的结构化数据、给前端的路线/POI 卡片。

main.py 只需持有一个 AmapService 实例，无需了解高德 API 细节。
"""

import os
from urllib.parse import quote

import httpx

AMAP_BASE = 'https://restapi.amap.com'

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

# 工具中文名：前端工具 chip 展示用
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


def _num(v):
    """高德数值字段常为字符串或空串，宽松转 float，失败返回 None"""
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def parse_poi(p: dict) -> dict:
    """提取 POI 卡片字段：评分/人均在 biz_ext 下；评语摘录覆盖稀疏，可能为空"""
    photos = p.get('photos') or []
    dist = p.get('distance')
    biz = p.get('biz_ext') or {}
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
        'rating': _num(biz.get('rating')),
        'cost': _num(biz.get('cost')),
        'type': p.get('keytag') or (type_segs[1] if len(type_segs) > 1 else (type_segs[0] if type_segs else None)),
        'tags': [t for t in (p.get('tag') or '').split(',') if t][:4],
        'review': review,
        'distance': int(dist) if str(dist).isdigit() else None,
        'photo': photos[0].get('url') if photos else None,
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


class AmapService:
    """高德 Web 服务客户端 + 出行助手工具执行器"""

    MAX_TOOL_ROUNDS = 5  # agent 循环中工具调用的最大轮数

    def __init__(self, key: str | None = None):
        self.key = key if key is not None else os.getenv('AMAP_KEY', '')

    @property
    def configured(self) -> bool:
        return bool(self.key)

    @property
    def tools(self) -> list:
        return AMAP_TOOLS

    @staticmethod
    def system_prompt() -> str:
        return build_agent_system_prompt()

    @staticmethod
    def tool_label(name: str) -> str:
        return TOOL_LABELS.get(name, name)

    @staticmethod
    def card_event_type(card: dict) -> str:
        """卡片下发的事件类型：poi_list 与路线卡片走独立事件，客户端分别渲染"""
        return 'amap_poi_list' if card.get('kind') == 'poi_list' else 'amap_card'

    @staticmethod
    def js_config() -> dict:
        """前端高德 JS API 配置：配置后底图升级为交互式地图，未配置时前端回退静态图"""
        return {
            'jsKey': os.getenv('AMAP_JS_KEY', ''),
            'jsSecret': os.getenv('AMAP_JS_SECURITY', ''),
        }

    # ---------- 底层请求 ----------

    async def _get(self, path: str, params: dict) -> dict:
        """请求高德 Web 服务 API（统一注入 key、过滤空参）"""
        params = {k: v for k, v in params.items() if v not in (None, '')}
        params['key'] = self.key
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(f'{AMAP_BASE}{path}', params=params)
            resp.raise_for_status()
            return resp.json()

    # ---------- 静态地图 ----------

    @staticmethod
    def _downsample(points: list, limit: int = 30) -> list:
        """折线点均匀抽样，控制静态地图 URL 长度"""
        if len(points) <= limit:
            return points
        step = (len(points) - 1) / (limit - 1)
        return [points[round(i * step)] for i in range(limit)]

    def _static_map_url(self, origin: str, destination: str, polyline: list) -> str:
        """生成带起终点标记 + 路线折线的静态地图 URL（无需前端 JS SDK）"""
        pts = self._downsample([p for p in polyline if p], 30)
        if not pts:
            return ''
        markers = f'mid,0x0080FF,起:{origin}|mid,0xFF0000,终:{destination}'
        paths = '6,0x00B0FF,1,,:' + ';'.join(pts)
        return (
            f'{AMAP_BASE}/v3/staticmap?size=750*400&scale=2'
            f'&markers={quote(markers, safe=":,;|")}'
            f'&paths={quote(paths, safe=":,;|")}'
            f'&key={self.key}'
        )

    # ---------- 各工具执行：均返回 (给模型的数据, 给前端的卡片) ----------

    async def _exec_geo(self, args: dict):
        data = await self._get('/v3/geocode/geo', {'address': args.get('address'), 'city': args.get('city')})
        if data.get('status') != '1' or not data.get('geocodes'):
            return {'error': data.get('info', '地理编码失败，未找到匹配地址')}, None
        geo = data['geocodes'][0]
        return {'location': geo.get('location'), 'formatted_address': geo.get('formatted_address')}, None

    async def _exec_text_search(self, args: dict):
        data = await self._get('/v3/place/text', {
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

    async def _exec_around_search(self, args: dict):
        data = await self._get('/v3/place/around', {
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

    async def _exec_transit(self, args: dict):
        data = await self._get('/v3/direction/transit/integrated', {
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
                    'map_url': self._static_map_url(args.get('origin', ''), args.get('destination', ''), polyline),
                    'origin': args.get('origin'), 'destination': args.get('destination'),
                    'origin_name': args.get('origin_name'), 'destination_name': args.get('destination_name'),
                    'city': args.get('city'),
                    'polyline': self._downsample(polyline, 100),
                    'duration_min': plan['duration_min'],
                    'distance_km': plan['distance_km'],
                    'stops': sum(s['stops'] for s in segments if s['mode'] == 'bus'),
                    'segments': segments,
                }
        return {'taxi_cost': route.get('taxi_cost'), 'plans': plans}, card

    async def _exec_driving(self, args: dict):
        data = await self._get('/v3/direction/driving', {
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
            'map_url': self._static_map_url(args.get('origin', ''), args.get('destination', ''), polyline),
            'origin': args.get('origin'), 'destination': args.get('destination'),
            'origin_name': args.get('origin_name'), 'destination_name': args.get('destination_name'),
            'polyline': self._downsample(polyline, 100),
            'duration_min': result['duration_min'],
            'distance_km': result['distance_km'],
            'segments': [{'mode': 'drive', 'road': r} for r in roads[:5]],
        }
        return result, card

    # ---------- 对外入口 ----------

    _HANDLERS = {
        'maps_geo': '_exec_geo',
        'maps_text_search': '_exec_text_search',
        'maps_around_search': '_exec_around_search',
        'maps_direction_transit_integrated': '_exec_transit',
        'maps_direction_driving': '_exec_driving',
    }

    async def execute_tool(self, name: str, args: dict):
        """执行一次高德工具调用，返回 (给模型的数据, 给前端的路线卡片)"""
        handler = self._HANDLERS.get(name)
        if not handler:
            return {'error': f'未知工具: {name}'}, None
        try:
            return await getattr(self, handler)(args)
        except httpx.HTTPError as err:
            return {'error': f'高德服务请求失败: {err}'}, None

    @staticmethod
    def summarize_result(name: str, result: dict) -> str:
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
