# 天气查询场景实现文档（Open-Meteo）

> 模式标识：`weather` ｜ 后端入口：`POST /api/weather/chat` ｜ 核心模块：`server/weather.py`
> 场景性质：Open-Meteo 公开 API **实时真实数据**（当前实况 + 7 天预报），**免凭据**——项目里唯一零配置即可运行的真实数据场景

## 1. 功能概述

接入 Open-Meteo（免费公开气象服务，无需注册），提供：

- 🌡️ **当前实况**：温度、体感、湿度、风速、天气现象
- 📅 **7 天逐日预报**：天气现象、最高/最低气温、降水量与降水概率、最大风速、紫外线指数

前端产出**天气卡片**：实况大数字视图 + 7 天逐日列表。

## 2. 整体架构

```
浏览器（weather 模式，agentLike，无 A2UI）
  │  POST /api/weather/chat
  ▼
server/main.py  weather_chat() → agent_stream(body, weather, weather.system_prompt())
  │  模型按 WEATHER_TOOLS 规划调用链（geocode → forecast）
  ▼
server/weather.py  WeatherService
  │  httpx 直调 Open-Meteo（15 秒超时）
  ▼
geocoding-api.open-meteo.com /v1/search        ← 地名 → 坐标
api.open-meteo.com /v1/forecast                ← 坐标 → 实况 + 预报
  │
  ├─ 给模型：{location, timezone, current, daily}  ← tool_result
  └─ 给前端：weather_card 事件 → WeatherCard
```

与出行助手同为「HTTP 直调 + 双工具编排」，差异仅在数据源免凭据、无卡片二分（只有一种天气卡片）。

## 3. 部署与凭据

**无需任何配置**。`configured` 恒为 `True`（Open-Meteo 免费公开），端点不会因缺 Key 拒绝——只要 LLM 网关配置正常即可直接使用。

## 4. 后端实现（server/weather.py）

### 4.1 工具定义（WEATHER_TOOLS，2 个）

| 工具 | 入参 | 行为 | 返回（给模型） |
|------|------|------|----------------|
| `weather_geocode` | name（必填） | Open-Meteo Geocoding 检索（`count=5`、`language=zh`），返回最多 5 个候选 | `{places: [{name, admin1, country, latitude, longitude}]}` |
| `weather_forecast` | latitude/longitude（必填）、name（卡片展示用） | Forecast API：`current` 6 字段 + `daily` 7 字段，`forecast_days=7`、`timezone=auto` | `{location, timezone, current, daily}` |

**多候选处理**是设计点：`weather_geocode` 返回所有候选而非取第一个，由模型在提示词约束下「选最合理的一个并在正文说明」——解决「朝阳」（北京/长春/沈阳）这类歧义地名。

### 4.2 WMO 天气代码映射

Open-Meteo 返回的 `weather_code` 是 WMO 整数编码，`WMO_CODES` 映射为 (中文描述, emoji) 共 30 项，如：

| 代码 | 映射 | 代码 | 映射 |
|------|------|------|------|
| 0 | 晴 ☀️ | 61/63/65 | 小雨/中雨/大雨 🌧️ |
| 1/2/3 | 大部晴朗/多云/阴 | 71/73/75 | 小雪/中雪/大雪 ❄️ |
| 45/48 | 雾/雾凇雾 | 80/81/82 | 小阵雨/阵雨/强阵雨 ⛈️ |
| 51–57 | 毛毛雨系 | 95–99 | 雷暴（±冰雹）⛈️ |

未知代码兜底为「未知 ❔」（`wmo_desc` 对非法输入也做了容错）。

### 4.3 日期友好化（weekday_cn）

`YYYY-MM-DD` → 「今天 / 明天 / 周X」——前两天不用算星期，符合日常口语；卡片与模型正文均使用。

### 4.4 查询参数（forecast）

```
current: temperature_2m, relative_humidity_2m, apparent_temperature,
         weather_code, wind_speed_10m, precipitation
daily:   weather_code, temperature_2m_max, temperature_2m_min,
         precipitation_sum, precipitation_probability_max,
         wind_speed_10m_max, uv_index_max
forecast_days=7, timezone=auto
```

逐日数据按 `daily.time` 数组下标对齐组装；实况时间截断到分钟并把 `T` 换成空格。

`MAX_TOOL_ROUNDS = 5`（典型调用链：geocode + forecast = 2 轮）。

## 5. 服务端接入（server/main.py）

```python
@app.post('/api/weather/chat')
async def weather_chat(request: Request):
    # messages 校验 + missing_llm_config 后：
    return agent_stream(body, weather, weather.system_prompt())
```

系统提示词要点：多候选「选最合理的一个并说明」；天气卡片自动附在回答上方，**正文只给简明中文解读**（当前实况、未来趋势、温差与降水提醒、穿衣/出行建议），严禁编造数据。

工具 chip 摘要（`summarize_result`）：定位显示「N 个候选」，预报显示「X°C 阴」或「N 天预报」。

## 6. 前端实现（client/src/App.jsx）

- **WeatherCard**（`weather_card` 事件 → `msg.weatherCard`）：
  - **实况区**：温度大数字 + emoji 图标 + 天气描述，辅助行显示体感/湿度/风速，标题附地点名
  - **7 天逐日区**：每行显示「今天/明天/周X · 图标 · 天气 · tmin~tmax · 降水概率 · 风速 · UV」
  - 卡片脚注标数据来源 `Open-Meteo`
- weather 模式属于 `agentLike`（工具 chip + 卡片），无表单交互
- 空状态推荐问题（`WEATHER_SUGGESTIONS`）：查当地天气 / 明天出行 / 周末天气等

## 7. 端到端交互流程

```
用户：深圳明天会下雨吗
  │
  ├─① weather_geocode("深圳") → 🔧「城市定位 · 1 个候选」
  │     （多候选时模型正文说明选了哪一个，如「已按广东深圳查询」）
  ├─② weather_forecast(lat, lon, name="深圳") → 🔧「天气预报 · 28°C 阵雨」
  │     → 🌦️ 天气卡片（实况 + 7 天逐日）
  └─③ 模型正文：明天降水概率 X%，建议带伞；未来三天转晴升温等
```

## 8. 已知限制与排障

- **数据精度**：Open-Meteo 为再分析+数值预报混合源，长时效日（5–7 天）误差偏大，属免费数据源的预期表现
- **7 天上限**：`forecast_days=7` 为当前设定，需要更长预报需扩展参数（Open-Meteo 支持最多 16 天）
- **地名歧义**：候选全不理想时，提示用户加上省份/国家重试（工具错误文案已内置该引导）
- **请求超时**：单次 15 秒，Open-Meteo 偶发限流（HTTP 429）时会透传为「天气服务请求失败」
- **与出行助手的组合**：天气场景无地图卡片；「去某地玩穿什么」类跨场景问题需在各自模式内分别问
