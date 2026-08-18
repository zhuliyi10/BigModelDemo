"""美团外卖开放平台服务封装：Web 服务 API 客户端 + 外卖点餐工具（function calling）执行器。

与 AmapService 同款三层结构：
- 模型只决定"何时调用、传什么参数"（MEITUAN_TOOLS 定义）；
- MeituanService 按官方签名规范（sig = MD5(uri + "?" + 按参数名排序的 k=v 串 + secret)）
  真正请求 https://waimaiopen.meituan.com/api/v1，并把原始响应解析为精简数据；
- 每次工具执行同时产出两份结果：给模型的结构化数据（含下单深链）、给前端的门店/菜单卡片。

跑通链路：搜索门店（poi/getids + poi/mget）→ 展示菜单（food/list）→ A2UI 渲染点餐界面 → 跳转下单深链。
main.py 只需持有一个 MeituanService 实例，无需了解美团 API 细节。
"""

import hashlib
import os
import time

import httpx

MEITUAN_BASE = os.getenv('MEITUAN_BASE', 'https://waimaiopen.meituan.com').rstrip('/')

# 下单深链：有美团内部门店 id 时直达店铺菜单页，否则落到外卖 H5 首页
def build_order_url(wm_poi_id=None) -> str:
    if wm_poi_id:
        return f'https://h5.waimai.meituan.com/waimai/mindex/menu?restaurant_id={wm_poi_id}'
    return 'https://h5.waimai.meituan.com/waimai/mindex/home'


# ---------- 本地演示数据（mock）：未配置美团凭据时兜底，保证全流程可演示 ----------

MOCK_SHOPS = [
    {'app_poi_code': 'demo-hg-001', 'name': '蜀都火锅（南山科技园店）', 'address': '深圳市南山区深南大道 9988 号',
     'min_price': 30, 'shipping_fee': 3, 'open': '周一至周日 10:30-22:00', 'keywords': ['火锅', '川渝', '冒菜']},
    {'app_poi_code': 'demo-mlt-002', 'name': '张记麻辣烫（软件园店）', 'address': '深圳市南山区科技中一路 12 号',
     'min_price': 20, 'shipping_fee': 2, 'open': '周一至周日 10:00-21:30', 'keywords': ['麻辣烫', '火锅', '冒菜']},
    {'app_poi_code': 'demo-tea-003', 'name': '云山茶事（万象天地店）', 'address': '深圳市南山区深南大道 9668 号',
     'min_price': 15, 'shipping_fee': 4, 'open': '周一至周日 09:00-22:30', 'keywords': ['奶茶', '茶饮', '甜品']},
    {'app_poi_code': 'demo-ff-004', 'name': '老乡鸡自选快餐（科兴店）', 'address': '深圳市南山区科苑路 15 号',
     'min_price': 18, 'shipping_fee': 2, 'open': '周一至周日 10:00-20:30', 'keywords': ['快餐', '盖饭', '中餐']},
    {'app_poi_code': 'demo-bbq-005', 'name': '湘野烧烤（后海店）', 'address': '深圳市南山区后海滨路 3001 号',
     'min_price': 25, 'shipping_fee': 5, 'open': '周一至周日 16:00-02:00', 'keywords': ['烧烤', '夜宵', '烤串']},
]

MOCK_FOODS = {
    'demo-hg-001': [
        {'name': '招牌鲜毛肚单人锅', 'price': 68.0, 'unit': '份', 'category_name': '热销单品', 'description': '鲜毛肚+牛油锅底+蘸料'},
        {'name': '双人牛油火锅套餐', 'price': 128.0, 'unit': '份', 'category_name': '套餐', 'description': '肥牛、毛肚、虾滑等 12 道菜'},
        {'name': '手切鲜牛肉', 'price': 42.0, 'unit': '份', 'category_name': '涮菜', 'description': '当日现切，涮 10 秒即食'},
        {'name': '水晶虾滑', 'price': 36.0, 'unit': '份', 'category_name': '涮菜', 'description': '手打虾滑，弹嫩爽口'},
        {'name': '红糖糍粑', 'price': 16.0, 'unit': '份', 'category_name': '小吃', 'description': '解辣必备，外酥里糯'},
        {'name': '冰镇酸梅汤', 'price': 12.0, 'unit': '杯', 'category_name': '饮品', 'description': '大杯 1L'},
    ],
    'demo-mlt-002': [
        {'name': '招牌麻辣烫（大份）', 'price': 32.0, 'unit': '份', 'category_name': '热销单品', 'description': '自选 12 种食材+骨汤底'},
        {'name': '番茄牛腩麻辣烫', 'price': 36.0, 'unit': '份', 'category_name': '招牌系列', 'description': '酸甜汤底，牛腩入味'},
        {'name': '加份方便面', 'price': 4.0, 'unit': '份', 'category_name': '加料', 'description': '吸汁神器'},
        {'name': '冰红茶', 'price': 6.0, 'unit': '瓶', 'category_name': '饮品', 'description': '500ml'},
    ],
    'demo-tea-003': [
        {'name': '云山桂花拿铁', 'price': 19.0, 'unit': '杯', 'category_name': '奶茶', 'description': '桂花乌龙+鲜奶'},
        {'name': '手打柠檬茶', 'price': 16.0, 'unit': '杯', 'category_name': '果茶', 'description': '香水柠檬现捶'},
        {'name': '杨枝甘露', 'price': 21.0, 'unit': '杯', 'category_name': '甜品', 'description': '芒果+西柚+椰奶'},
        {'name': '芋泥波波奶茶', 'price': 18.0, 'unit': '杯', 'category_name': '奶茶', 'description': '手煮芋泥，微糖更香'},
    ],
    'demo-ff-004': [
        {'name': '农家小炒肉套餐', 'price': 26.0, 'unit': '份', 'category_name': '套餐', 'description': '两荤一素+米饭'},
        {'name': '香菇滑鸡饭', 'price': 23.0, 'unit': '份', 'category_name': '盖饭', 'description': '现蒸滑鸡，酱香浓郁'},
        {'name': '西红柿鸡蛋汤', 'price': 6.0, 'unit': '碗', 'category_name': '汤品', 'description': '家常口味'},
        {'name': '可乐', 'price': 5.0, 'unit': '瓶', 'category_name': '饮品', 'description': '330ml 罐装'},
    ],
    'demo-bbq-005': [
        {'name': '招牌羊肉串（10 串）', 'price': 30.0, 'unit': '把', 'category_name': '烤串', 'description': '现切现穿，炭火直烤'},
        {'name': '烤生蚝（6 只）', 'price': 36.0, 'unit': '份', 'category_name': '海鲜', 'description': '蒜蓉粉丝'},
        {'name': '烤茄子', 'price': 14.0, 'unit': '份', 'category_name': '素菜', 'description': '蒜香浓郁'},
        {'name': '冰镇啤酒', 'price': 8.0, 'unit': '瓶', 'category_name': '饮品', 'description': '500ml', 'is_sold_out': True},
    ],
}


# Anthropic tools 定义：模型据此规划"搜门店 → 看菜单"的调用链
MEITUAN_TOOLS = [
    {
        'name': 'waimai_shop_search',
        'description': '美团外卖门店搜索：按关键词（品类或店名，如"火锅"、"炸鸡"）搜索当前授权的外卖门店，返回门店名称、地址、起送价、配送费、营业时段与下单链接。用户想点外卖时先调用本工具找店。',
        'input_schema': {
            'type': 'object',
            'properties': {
                'keyword': {'type': 'string', 'description': '品类或店名关键词，如"麻辣烫"；用户未指定时可传空串返回全部门店'},
            },
            'required': [],
        },
    },
    {
        'name': 'waimai_food_list',
        'description': '美团外卖菜单查询：按门店 id（app_poi_code，来自 waimai_shop_search）查询该门店的菜品列表，返回菜名、价格、分类、描述、图片与下单链接。用户想看某家店菜单或要点餐时调用。',
        'input_schema': {
            'type': 'object',
            'properties': {
                'app_poi_code': {'type': 'string', 'description': '门店 id（来自 waimai_shop_search 返回）'},
                'shop_name': {'type': 'string', 'description': '门店名称，用于界面卡片展示'},
            },
            'required': ['app_poi_code'],
        },
    },
]

# 工具中文名：前端工具 chip 展示用
TOOL_LABELS = {
    'waimai_shop_search': '门店搜索',
    'waimai_food_list': '菜单查询',
}


def build_food_system_prompt() -> str:
    return (
        '你是外卖点餐助手，已接入美团外卖开放平台实时服务（门店搜索、菜单查询）。\n'
        '工作流程：\n'
        '1. 用户想点外卖/找店时，先用 waimai_shop_search 搜索门店，正文简明介绍返回的门店'
        '（名称、起送价、配送费、营业时段），严禁编造任何数据；\n'
        '2. 用户想看菜单或点餐时，用 waimai_food_list 拉取该门店真实菜品，并立刻按 A2UI 协议生成点餐界面：'
        '界面须展示门店名、用 List/Text 呈现真实菜品（名称、价格、描述），用 ChoicePicker（multipleSelection）'
        '让用户勾选菜品，TextField 收集配送地址与备注，Button 提交（事件名 submit_order）；'
        '菜品选项必须来自工具返回的真实数据，最多列 12 道热销菜，严禁编造菜品或价格；\n'
        '3. 收到 "[A2UI_EVENT] ...submit_order" 的提交数据后，用普通 Markdown 回复订单小结'
        '（所选菜品、金额合计、配送地址），并给出下单方式：附上工具结果中的下单链接，'
        '写成 Markdown 链接形式 [前往美团外卖完成下单](<order_url>)，提示用户点击跳转完成支付。\n'
        '工具调用失败时，根据错误信息说明原因并给出替代建议。'
    )


def build_mock_note() -> str:
    """mock 模式附加提示：要求模型明确告知用户当前为本地演示数据"""
    return (
        '\n注意：当前未配置美团开放平台凭据，工具返回的是本地演示数据（mock）。'
        '在首次回复的末尾用一句话明确告知用户："当前为本地演示数据，配置美团凭据后可查询真实门店与菜品。"'
    )


class MeituanService:
    """美团外卖开放平台客户端 + 外卖点餐工具执行器"""

    MAX_TOOL_ROUNDS = 5  # agent 循环中工具调用的最大轮数
    BATCH_MGET_LIMIT = 20  # poi/mget 单次批量上限

    def __init__(self, app_id: str | None = None, secret: str | None = None):
        self.app_id = app_id if app_id is not None else os.getenv('MEITUAN_APP_ID', '')
        self.secret = secret if secret is not None else os.getenv('MEITUAN_SECRET', '')
        # app_poi_code → wm_poi_id 缓存：门店搜索时记录，菜单查询时用于生成下单深链
        self._wm_poi_ids: dict[str, str] = {}

    @property
    def configured(self) -> bool:
        return bool(self.app_id and self.secret)

    @property
    def mock_enabled(self) -> bool:
        """演示数据开关：MEITUAN_MOCK 显式指定时以它为准；未指定时，未配置凭据则自动降级为 mock"""
        env = os.getenv('MEITUAN_MOCK', '').strip().lower()
        if env in ('0', 'off', 'false', 'no'):
            return False
        if env in ('1', 'on', 'true', 'yes'):
            return True
        return not self.configured

    @property
    def tools(self) -> list:
        return MEITUAN_TOOLS

    @staticmethod
    def system_prompt() -> str:
        return build_food_system_prompt()

    @staticmethod
    def tool_label(name: str) -> str:
        return TOOL_LABELS.get(name, name)

    @staticmethod
    def card_event_type(card: dict) -> str:
        return 'meituan_card'

    # ---------- 底层请求 ----------

    def _sign(self, path: str, params: dict) -> str:
        """官方签名：MD5("{path}?k1=v1&k2=v2...(按参数名排序，不含 sig){secret}")，32 位小写"""
        items = sorted((k, v) for k, v in params.items() if k not in ('sig', 'img_data'))
        query = '&'.join(f'{k}={v}' for k, v in items)
        raw = f'{path}?{query}{self.secret}'
        return hashlib.md5(raw.encode('utf-8')).hexdigest()

    async def _request(self, path: str, params: dict) -> dict:
        """请求 waimaiopen.meituan.com/api/v1/{path}（注入系统级参数 + sig），返回业务 data"""
        full_path = f'/api/v1/{path}'
        params = {k: v for k, v in params.items() if v not in (None, '')}
        params['app_id'] = self.app_id
        params['timestamp'] = int(time.time())
        params['sig'] = self._sign(full_path, params)
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(f'{MEITUAN_BASE}{full_path}', params=params)
            resp.raise_for_status()
            body = resp.json()
        if isinstance(body, dict) and body.get('error'):
            err = body['error']
            raise RuntimeError(f'美团接口错误 {err.get("code")}: {err.get("msg")}')
        if isinstance(body, dict) and body.get('data') in (None, 'ng'):
            raise RuntimeError(body.get('msg') or '美团接口返回为空')
        return body.get('data') if isinstance(body, dict) else body

    # ---------- 各工具执行：均返回 (给模型的数据, 给前端的卡片) ----------

    async def _exec_shop_search(self, args: dict):
        """门店搜索：getids 取授权门店 → mget 批量取详情 → 按关键词过滤"""
        if self.mock_enabled:
            return self._mock_shop_search(args)
        ids = await self._request('poi/getids', {})
        ids = [str(i) for i in (ids or [])][:60]
        if not ids:
            return {'error': '当前开发者账号下没有授权门店，请先在美团开放平台绑定沙箱测试门店'}, None

        shops = []
        for start in range(0, len(ids), self.BATCH_MGET_LIMIT):
            batch = ids[start:start + self.BATCH_MGET_LIMIT]
            data = await self._request('poi/mget', {'app_poi_codes': ','.join(batch)})
            for p in data or []:
                code = str(p.get('app_poi_code') or '')
                wm_id = str(p.get('wm_poi_id') or '')
                if code and wm_id:
                    self._wm_poi_ids[code] = wm_id
                shops.append({
                    'app_poi_code': code,
                    'wm_poi_id': wm_id,
                    'name': p.get('name'),
                    'address': p.get('address'),
                    'shipping_fee': p.get('shipping_fee'),
                    'min_price': p.get('min_price'),
                    'open': p.get('open'),
                    'pic': p.get('pic'),
                    'order_url': build_order_url(p.get('wm_poi_id')),
                })

        keyword = (args.get('keyword') or '').strip()
        if keyword:
            matched = [s for s in shops if keyword.lower() in (s.get('name') or '').lower()
                       or keyword.lower() in (s.get('address') or '').lower()]
            shops = matched or shops  # 无命中时退回全部门店，由模型说明
        shops = shops[:5]
        if not shops:
            return {'error': '未找到可用门店'}, None
        return {'keyword': keyword, 'shops': shops}, {
            'kind': 'shop_list',
            'title': f'外卖门店: {keyword or "全部门店"}',
            'shops': shops,
        }

    @staticmethod
    def _mock_shop_search(args: dict):
        """mock 门店搜索：从内置演示数据中按关键词（品类/店名）过滤"""
        keyword = (args.get('keyword') or '').strip()
        shops = []
        for s in MOCK_SHOPS:
            hit = keyword and (
                keyword.lower() in s['name'].lower()
                or any(keyword.lower() in k for k in s['keywords'])
            )
            if not keyword or hit:
                shops.append({
                    'app_poi_code': s['app_poi_code'],
                    'wm_poi_id': '',
                    'name': s['name'],
                    'address': s['address'],
                    'shipping_fee': s['shipping_fee'],
                    'min_price': s['min_price'],
                    'open': s['open'],
                    'pic': None,
                    'order_url': build_order_url(),
                })
        shops = (shops or [{
            'app_poi_code': MOCK_SHOPS[0]['app_poi_code'],
            'wm_poi_id': '', 'name': MOCK_SHOPS[0]['name'], 'address': MOCK_SHOPS[0]['address'],
            'shipping_fee': MOCK_SHOPS[0]['shipping_fee'], 'min_price': MOCK_SHOPS[0]['min_price'],
            'open': MOCK_SHOPS[0]['open'], 'pic': None, 'order_url': build_order_url(),
        }])[:5]
        return {'keyword': keyword, 'demo_mode': True, 'shops': shops}, {
            'kind': 'shop_list',
            'demo': True,
            'title': f'外卖门店: {keyword or "全部门店"}',
            'shops': shops,
        }

    async def _exec_food_list(self, args: dict):
        """菜单查询：food/list 按门店拉菜品，给模型的数据与给前端的菜单卡片共用一份"""
        if self.mock_enabled:
            return self._mock_food_list(args)
        data = await self._request('food/list', {'app_poi_code': args.get('app_poi_code')})
        foods = []
        for f in data or []:
            foods.append({
                'app_food_code': str(f.get('app_food_code') or ''),
                'name': f.get('name'),
                'price': f.get('price'),
                'unit': f.get('unit'),
                'category_name': f.get('category_name'),
                'description': f.get('description'),
                'pic': f.get('picture') or f.get('pic'),
                'is_sold_out': bool(f.get('is_sold_out')),
            })
        foods = [f for f in foods if f.get('name')][:30]
        if not foods:
            return {'error': '该门店暂无菜品数据'}, None
        app_poi_code = str(args.get('app_poi_code') or '')
        order_url = build_order_url(self._wm_poi_ids.get(app_poi_code))
        shop = {
            'app_poi_code': app_poi_code,
            'name': args.get('shop_name') or app_poi_code,
            'order_url': order_url,
        }
        return {'shop': shop, 'foods': foods}, {
            'kind': 'menu',
            'title': f'{shop["name"]} · 菜单',
            'shop': shop,
            'foods': foods[:20],
            'order_url': order_url,
        }

    @staticmethod
    def _mock_food_list(args: dict):
        """mock 菜单查询：按门店 id 返回内置演示菜品；未知门店回退到第一家"""
        app_poi_code = str(args.get('app_poi_code') or '')
        if app_poi_code not in MOCK_FOODS:
            app_poi_code = MOCK_SHOPS[0]['app_poi_code']
        meta = next(s for s in MOCK_SHOPS if s['app_poi_code'] == app_poi_code)
        shop_name = args.get('shop_name') or meta['name']
        foods = [{
            'app_food_code': f'{app_poi_code}-{i}',
            'name': f['name'],
            'price': f['price'],
            'unit': f['unit'],
            'category_name': f['category_name'],
            'description': f['description'],
            'pic': None,
            'is_sold_out': bool(f.get('is_sold_out')),
        } for i, f in enumerate(MOCK_FOODS[app_poi_code])]
        shop = {'app_poi_code': app_poi_code, 'name': shop_name, 'order_url': build_order_url()}
        return {'shop': shop, 'demo_mode': True, 'foods': foods}, {
            'kind': 'menu',
            'demo': True,
            'title': f'{shop_name} · 菜单',
            'shop': shop,
            'foods': foods[:20],
            'order_url': shop['order_url'],
        }

    # ---------- 对外入口 ----------

    _HANDLERS = {
        'waimai_shop_search': '_exec_shop_search',
        'waimai_food_list': '_exec_food_list',
    }

    async def execute_tool(self, name: str, args: dict):
        """执行一次美团工具调用，返回 (给模型的数据, 给前端的卡片)"""
        handler = self._HANDLERS.get(name)
        if not handler:
            return {'error': f'未知工具: {name}'}, None
        try:
            return await getattr(self, handler)(args)
        except httpx.HTTPError as err:
            return {'error': f'美团服务请求失败: {err}'}, None
        except RuntimeError as err:
            return {'error': str(err)}, None

    @staticmethod
    def summarize_result(name: str, result: dict) -> str:
        """工具结果的一句话摘要，供前端工具 chip 展示"""
        if result.get('error'):
            return '失败'
        if name == 'waimai_shop_search':
            return f'{len(result.get("shops", []))} 家门店'
        if name == 'waimai_food_list':
            return f'{len(result.get("foods", []))} 道菜品'
        return ''
