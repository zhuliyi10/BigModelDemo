"""Open-Meteo 天气服务封装：地理编码 + 7 天预报工具（function calling）执行器。

与 AmapService / MeituanService 同款三层结构：
- 模型只决定"何时调用、传什么参数"（WEATHER_TOOLS 定义）；
- WeatherService 真正请求 Open-Meteo 公开 API（免费、无需 Key），解析为精简数据；
- 每次工具执行同时产出两份结果：给模型的结构化数据、给前端的天气卡片。

main.py 只需持有一个 WeatherService 实例，无需了解 Open-Meteo API 细节。
"""

from datetime import datetime, timedelta

import httpx

GEOCODE_URL = 'https://geocoding-api.open-meteo.com/v1/search'
FORECAST_URL = 'https://api.open-meteo.com/v1/forecast'

# WMO 天气解读代码 → (中文描述, emoji 图标)
WMO_CODES = {
    0: ('晴', '☀️'), 1: ('大部晴朗', '🌤️'), 2: ('多云', '⛅'), 3: ('阴', '☁️'),
    45: ('雾', '🌫️'), 48: ('雾凇雾', '🌫️'),
    51: ('小毛毛雨', '🌦️'), 53: ('毛毛雨', '🌦️'), 55: ('浓毛毛雨', '🌧️'),
    56: ('冻毛毛雨', '🌧️'), 57: ('浓冻毛毛雨', '🌧️'),
    61: ('小雨', '🌧️'), 63: ('中雨', '🌧️'), 65: ('大雨', '🌧️'),
    66: ('冻雨', '🌧️'), 67: ('大冻雨', '🌧️'),
    71: ('小雪', '🌨️'), 73: ('中雪', '🌨️'), 75: ('大雪', '❄️'), 77: ('米雪', '❄️'),
    80: ('小阵雨', '🌦️'), 81: ('阵雨', '🌧️'), 82: ('强阵雨', '⛈️'),
    85: ('小阵雪', '🌨️'), 86: ('大阵雪', '❄️'),
    95: ('雷暴', '⛈️'), 96: ('雷暴伴小冰雹', '⛈️'), 99: ('雷暴伴大冰雹', '⛈️'),
}

WEEKDAYS = ['一', '二', '三', '四', '五', '六', '日']


def wmo_desc(code) -> tuple:
    """WMO 天气代码转 (中文描述, emoji)，未知代码兜底为"未知" """
    try:
        return WMO_CODES.get(int(code), ('未知', '❔'))
    except (TypeError, ValueError):
        return ('未知', '❔')


def weekday_cn(date_str: str) -> str:
    """YYYY-MM-DD → 周X（今天/明天更直观）"""
    try:
        d = datetime.strptime(date_str, '%Y-%m-%d').date()
    except (TypeError, ValueError):
        return ''
    today = datetime.now().date()
    if d == today:
        return '今天'
    if d == today + timedelta(days=1):
        return '明天'
    return f'周{WEEKDAYS[d.weekday()]}'


# Anthropic tools 定义：模型据此规划"先地理编码取坐标，再查预报"的调用链
WEATHER_TOOLS = [
    {
        'name': 'weather_geocode',
        'description': '城市地理编码：把城市/地名（如"杭州"、"深圳南山"）转换为经纬度坐标与行政区划。天气查询工具需要坐标作为入参，应先用本工具取坐标。',
        'input_schema': {
            'type': 'object',
            'properties': {
                'name': {'type': 'string', 'description': '城市或地名，如"北京"'},
            },
            'required': ['name'],
        },
    },
    {
        'name': 'weather_forecast',
        'description': '天气预报查询：按经纬度查询当前实况与未来 7 天逐日预报（天气现象、最高/最低气温、降水、风力、紫外线）。用户询问天气时，先用 weather_geocode 取坐标再调用本工具。',
        'input_schema': {
            'type': 'object',
            'properties': {
                'latitude': {'type': 'number', 'description': '纬度（来自 weather_geocode）'},
                'longitude': {'type': 'number', 'description': '经度（来自 weather_geocode）'},
                'name': {'type': 'string', 'description': '地名，用于界面卡片展示'},
            },
            'required': ['latitude', 'longitude'],
        },
    },
]

# 工具中文名：前端工具 chip 展示用
TOOL_LABELS = {
    'weather_geocode': '城市定位',
    'weather_forecast': '天气预报',
}


def build_weather_system_prompt() -> str:
    return (
        '你是天气助手，已接入 Open-Meteo 实时天气服务（城市地理编码、当前实况与未来 7 天预报）。\n'
        '工作流程：用户询问某地天气时，先用 weather_geocode 把地名转成坐标'
        '（多个候选时选最合理的一个并在正文说明），再用 weather_forecast 查询预报；'
        '天气卡片由系统自动附在回答上方，正文只需基于真实数据给出简明中文解读'
        '（当前实况、未来趋势、温差与降水提醒、穿衣/出行建议），严禁编造任何数据。\n'
        '工具调用失败时，根据错误信息说明原因并给出替代建议。'
    )


class WeatherService:
    """Open-Meteo 天气客户端 + 天气助手工具执行器"""

    MAX_TOOL_ROUNDS = 5  # agent 循环中工具调用的最大轮数

    @property
    def configured(self) -> bool:
        return True  # Open-Meteo 免费公开，无需凭据

    @property
    def tools(self) -> list:
        return WEATHER_TOOLS

    @staticmethod
    def system_prompt() -> str:
        return build_weather_system_prompt()

    @staticmethod
    def tool_label(name: str) -> str:
        return TOOL_LABELS.get(name, name)

    @staticmethod
    def card_event_type(card: dict) -> str:
        return 'weather_card'

    # ---------- 各工具执行：均返回 (给模型的数据, 给前端的卡片) ----------

    async def _exec_geocode(self, args: dict):
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(GEOCODE_URL, params={
                'name': args.get('name'), 'count': 5, 'language': 'zh', 'format': 'json',
            })
            resp.raise_for_status()
            data = resp.json()
        results = data.get('results') or []
        if not results:
            return {'error': f'未找到地点「{args.get("name")}」，请换个说法重试（如加上省份/国家）'}, None
        places = [{
            'name': r.get('name'),
            'admin1': r.get('admin1'),
            'country': r.get('country'),
            'latitude': r.get('latitude'),
            'longitude': r.get('longitude'),
        } for r in results[:5]]
        return {'places': places}, None

    async def _exec_forecast(self, args: dict):
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(FORECAST_URL, params={
                'latitude': args.get('latitude'),
                'longitude': args.get('longitude'),
                'current': 'temperature_2m,relative_humidity_2m,apparent_temperature,weather_code,wind_speed_10m,precipitation',
                'daily': 'weather_code,temperature_2m_max,temperature_2m_min,precipitation_sum,precipitation_probability_max,wind_speed_10m_max,uv_index_max',
                'forecast_days': 7,
                'timezone': 'auto',
            })
            resp.raise_for_status()
            data = resp.json()

        cur = data.get('current') or {}
        c_desc, c_icon = wmo_desc(cur.get('weather_code'))
        current = {
            'time': (cur.get('time') or '')[:16].replace('T', ' '),
            'temperature': cur.get('temperature_2m'),
            'feels_like': cur.get('apparent_temperature'),
            'humidity': cur.get('relative_humidity_2m'),
            'wind_speed': cur.get('wind_speed_10m'),
            'weather': c_desc,
            'icon': c_icon,
        }

        daily = data.get('daily') or {}
        times = daily.get('time') or []
        days = []
        for i, date in enumerate(times):
            desc, icon = wmo_desc((daily.get('weather_code') or [None] * len(times))[i])
            days.append({
                'date': date,
                'weekday': weekday_cn(date),
                'weather': desc,
                'icon': icon,
                'tmax': (daily.get('temperature_2m_max') or [None] * len(times))[i],
                'tmin': (daily.get('temperature_2m_min') or [None] * len(times))[i],
                'precip': (daily.get('precipitation_sum') or [None] * len(times))[i],
                'precip_prob': (daily.get('precipitation_probability_max') or [None] * len(times))[i],
                'wind_max': (daily.get('wind_speed_10m_max') or [None] * len(times))[i],
                'uv': (daily.get('uv_index_max') or [None] * len(times))[i],
            })
        if not days:
            return {'error': '天气预报返回为空'}, None

        location = {'name': args.get('name'), 'latitude': args.get('latitude'), 'longitude': args.get('longitude')}
        return {'location': location, 'timezone': data.get('timezone'), 'current': current, 'daily': days}, {
            'kind': 'forecast',
            'title': f'{args.get("name") or "当地"}天气',
            'location': location,
            'current': current,
            'days': days,
            'source': 'Open-Meteo',
        }

    # ---------- 对外入口 ----------

    _HANDLERS = {
        'weather_geocode': '_exec_geocode',
        'weather_forecast': '_exec_forecast',
    }

    async def execute_tool(self, name: str, args: dict):
        """执行一次天气工具调用，返回 (给模型的数据, 给前端的天气卡片)"""
        handler = self._HANDLERS.get(name)
        if not handler:
            return {'error': f'未知工具: {name}'}, None
        try:
            return await getattr(self, handler)(args)
        except httpx.HTTPError as err:
            return {'error': f'天气服务请求失败: {err}'}, None

    @staticmethod
    def summarize_result(name: str, result: dict) -> str:
        """工具结果的一句话摘要，供前端工具 chip 展示"""
        if result.get('error'):
            return '失败'
        if name == 'weather_geocode':
            return f'{len(result.get("places", []))} 个候选'
        if name == 'weather_forecast':
            cur = result.get('current') or {}
            if cur.get('temperature') is not None:
                return f'{cur["temperature"]}°C {cur.get("weather", "")}'
            return f'{len(result.get("daily", []))} 天预报'
        return ''
