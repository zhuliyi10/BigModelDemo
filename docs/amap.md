# 出行助手场景实现文档（高德地图）

> 模式标识：`agent` ｜ 后端入口：`POST /api/agent/chat` ｜ 核心模块：`server/amap.py`
> 场景性质：高德 Web 服务 API **实时真实数据**（地理编码 / POI / 路线规划），只读不交易

## 1. 功能概述

接入高德地图 Web 服务 API，是项目最早、也是「三层结构」范式的原型场景：

- 📍 **地理编码**：地址/地名 → 经纬度坐标（路线规划与周边搜索的前置步骤）
- 🔍 **POI 关键字搜索**：按名称搜餐厅、酒店、景点
- 🧭 **周边搜索**：以坐标为中心搜「附近/周边」的推荐点（含评分、人均、距离、照片）
- 🚇 **公交路线规划**：多个方案（耗时/步行距离/分段明细：线路、上下车站、站数）
- 🚗 **驾车路线规划**：耗时/距离/过路费/途经主要道路

前端产出两类卡片：**路线卡片**（地图 + 分段步骤）与 **POI 列表卡片**（评分/人均/距离/评语）。

## 2. 整体架构

```
浏览器（agent 模式，agentLike，无 A2UI）
  │  POST /api/agent/chat
  ▼
server/main.py  agent_chat() → agent_stream(body, amap, amap.system_prompt())
  │  模型按 AMAP_TOOLS 定义自主规划调用链（geo → direction / around_search）
  ▼
server/amap.py  AmapService
  │  httpx 直调 https://restapi.amap.com/*（统一注入 key，15 秒超时）
  ▼
高德 Web 服务 API
  │
  ├─ 给模型：精简结构化数据（plans / pois）← tool_result
  └─ 给前端：amap_card（路线）/ amap_poi_list（POI 列表）
```

与美团系场景的差异：**HTTP 直调**（非 CLI 子进程）；**多工具编排**（模型自主规划「先取坐标再规划路线」的调用链，而非单一自然语言入口）。

## 3. 部署与凭据（server/.env）

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `AMAP_KEY` | — | 高德 **Web 服务**类型 Key（控制台「Web 服务」应用），路线卡片静态图也用它 |
| `AMAP_JS_KEY` | — | 可选，高德 **Web 端 JS API** Key；配置后路线卡片升级为交互式底图 |
| `AMAP_JS_SECURITY` | — | 可选，JS API 2.0 的安全密钥（与 `AMAP_JS_KEY` 配对） |

> ⚠️ 高德「Web 服务」与「Web 端 JS API」是**两种不同类型的 Key**，不能混用；未申请 JS Key 时功能不降级（自动回退静态图）。

## 4. 后端实现（server/amap.py）

### 4.1 工具定义（AMAP_TOOLS，5 个）

| 工具 | 入参要点 | 返回（给模型） | 卡片 |
|------|----------|----------------|------|
| `maps_geo` | address（必填）、city | location、formatted_address | 无 |
| `maps_text_search` | keywords（必填）、city | pois（前 5 条精简字段） | `poi_list` |
| `maps_around_search` | center（必填，来自 geo）、keywords、radius（默认 1000）、center_name | center + pois | `poi_list` |
| `maps_direction_transit_integrated` | origin/destination（必填，来自 geo）、city、cityd、起终点地名 | taxi_cost + plans（≤3 个方案） | `transit` |
| `maps_direction_driving` | origin/destination（必填）、起终点地名 | duration/distance/tolls/main_roads | `driving` |

地名类展示字段（`origin_name` / `destination_name` / `center_name`）是**给界面卡片用的**，与 API 无关——这是本场景工具定义的一个约定：模型传地名，服务端塞进卡片，避免前端只显示生硬的坐标。

### 4.2 工具编排约定（系统提示词）

- 问「怎么走」：先 `maps_geo` 起终点坐标 → 再调路线规划工具
- 问「附近/周边」：先 `maps_geo` 中心坐标 → 再 `maps_around_search`
- 卡片由系统自动附在回答上方，**正文只给简明中文建议**（推荐理由/人均/特色，可引用评分），严禁编造数据
- 调用失败时按错误信息说明原因并给替代建议

### 4.3 数据解析（针对高德返回的适配）

- `parse_poi`：评分/人均在 `biz_ext` 子对象下；类型取 `keytag`（缺失时取 `type` 第二段）；标签取前 4；评语摘录（`featured_reviews`，覆盖稀疏可能为空）；照片取首图；周边搜索的 `distance` 转整数米
- `parse_transit_segments`：把官方 `segments` 拆成 walk/bus 分段列表（步行仅在有距离时输出；乘车段每段取第一条线路），同时收集全程折线点
- `_num`：高德数值字段常为字符串或空串，宽松转 float 失败返回 None

### 4.4 静态地图与折线（无需前端 SDK）

- `_static_map_url`：调 `v3/staticmap` 生成带标记的静态图 URL——起点蓝色 `起:` 标记、终点红色 `终:` 标记、粗蓝色路线折线
- `_downsample`：折线点**均匀抽样**——静态图限 30 点（控制 URL 长度），下发前端的 polyline 限 100 点（供交互式底图绘制）
- 公交卡片取**第一个有折线的方案**作为展示方案；驾车卡片附带 `main_roads`（前 8 条道路）与过路费

### 4.5 卡片双事件

`card_event_type` 按卡片 kind 动态拆分：`poi_list` 走 `amap_poi_list` 事件，路线卡片走 `amap_card` 事件——前端据此分别渲染两种视觉完全不同的卡片。

`MAX_TOOL_ROUNDS = 5`（一次提问常见调用链：2 次 geo + 1 次规划，留有余量）。

## 5. 服务端接入（server/main.py）

```python
@app.post('/api/agent/chat')
async def agent_chat(request: Request):
    # messages 校验 + missing_llm_config 后：
    return agent_stream(body, amap, amap.system_prompt())
```

工具 chip 摘要（`summarize_result`）：地理编码显示坐标、POI 显示「N 条结果」、公交显示「N 个方案」、驾车显示「约 N 分钟」。

## 6. 前端实现（client/src/App.jsx）

- **AmapCard**（transit/driving）：配置了 `AMAP_JS_KEY` 时动态加载高德 JS API 2.0（含安全密钥 `AMAP_JS_SECURITY`），渲染**可拖拽缩放的交互式底图**并绘制路线折线；未配置时回退 `<img>` 静态图。卡片下方展示分段步骤（步行 → 乘车 → …，含线路名/上下车站/站数/分钟数）
- **PoiListCard**：每条 POI 含首图缩略、名称、评分徽章、人均、类型标签、区县、地址、距离、评语摘录；点击通过 `uri.amap.com/marker`（poiid 或坐标）跳转高德
- **路线跳转**：卡片底部「打开高德地图」链接走 `uri.amap.com/navigation`（起终点 + mode=bus/car，`callnative=0` 网页版兜底）
- 前端 JS 配置由服务端 `js_config()` 下发（Key 不下发 Web 服务 `AMAP_KEY`，只下发 JS Key）
- 空状态推荐问题（`AGENT_SUGGESTIONS`）：通勤路线 / 周边美食 / 驾车出行等

## 7. 端到端交互流程

```
用户：从深圳北站到世界之窗怎么坐地铁
  │
  ├─① maps_geo("深圳北站") → 🔧「地理编码 · 114.03,22.61」
  ├─② maps_geo("世界之窗") → 🔧「地理编码 · …」
  ├─③ maps_direction_transit_integrated(origin, destination, city="深圳")
  │     → 🔧「公交路线规划 · 3 个方案」
  │     → 🗺️ 路线卡片（交互式底图 + 推荐方案分段：步行/1号线/…）
  └─④ 模型正文：一两句建议（如「方案一最快 32 分钟」），不复述卡片

用户：南山地铁站附近有什么好吃的
  ├─ maps_geo → maps_around_search(keywords="美食", radius=1000)
  │     → 📋 POI 列表卡片（评分/人均/距离/评语，点击跳高德）
```

## 8. 已知限制与排障

- **Key 类型**：最常见的故障是拿 JS API Key 当 Web 服务 Key 用（`INVALID_USER_KEY`）——两者必须分别申请
- **配额**：个人开发者有日调用量限制，超限返回 `USER_DAILY_QUERY_OVER_LIMIT`，工具会原样透传错误信息
- **地名歧义**：地理编码无结果或匹配错误时，让用户补充城市名（`city` 参数可显著提升命中率）
- **静态图折线**：极长路线抽到 30 点可能失真，属预期内取舍（URL 长度限制）；交互式底图不受影响
- **评语稀疏**：`featured_reviews` 覆盖有限，POI 卡片评语区可能留空
