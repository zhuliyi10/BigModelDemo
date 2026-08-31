"""外卖点餐（演示场景）服务封装：模拟外卖平台商家/菜单数据 + 点餐下单工具执行器。

与 RechargeService 同款三层结构，交互参考千问 App 外卖点餐：
- 模型只决定"何时调用、传什么参数"（WAIMAI_TOOLS 定义）；
- WaimaiService 在内存中模拟外卖平台侧数据（商家/菜单/订单）并回传结果；
- 每次工具执行同时产出两份结果：给模型的结构化数据、给前端的卡片。

模拟数据约定（无真实商家与配送，界面与卡片明确标注"演示数据"）：
- 商家与菜单为预置数据；评分/月售/起送价/配送费/满减由商家 id 确定性生成，多次查询一致；
- 招牌菜带推荐语与占位图标（无真实图片），供商家卡片以"图块 + 预估价 + 选这个"形式展示；
- 部分菜品带规格组（specs：份量/辣度/自选饮料等，选项可加价），模型传选项原文，
  服务端按选项加价重算单价并校验合法性，防止编造；
- 菜品价格以服务端菜单为准，下单时重新校验，防止模型编造价格或菜品；
- 下单仅在服务内存记账，订单状态随时间流转：备餐中(30s) → 配送中(90s) → 已送达。

⚠️ 点餐是消费行为：系统提示词强制"预览 → 用户明确确认 → 提交"两步门控。
"""

import hashlib
import re
import time
from datetime import datetime, timedelta

PHONE_PATTERN = re.compile(r'^1[3-9]\d{9}$')

# 状态流转阈值（秒）：下单 30s 后进入配送，90s 后送达（演示加速，真实配送约 30 分钟）
PREP_SECONDS = 30
DELIVERED_SECONDS = 90

# 满减档位池（满 X 减 Y），按商家 id 确定性选取
DEALS = [(20, 3), (30, 5), (35, 6), (50, 10)]

# 餐盒费：每份 1 元，封顶 6 元
PACKING_PER_ITEM = 1
PACKING_CAP = 6

# 已提交订单（内存记账，单进程演示足够）
_ORDERS = {}

# 模拟商家池（演示数据，店名与菜品均为虚构；keywords 供品类搜索匹配；
# icon 为菜品占位图标（无真实图片），signature 为招牌菜推荐语，对齐千问商品卡文案）
SHOPS = [
    {
        'id': 'shop_001', 'name': '老王家牛肉面', 'category': '面食', 'keywords': '面食 米线 面条 牛肉面',
        'icon': '🍜', 'signature': '红烧牛肉面汤浓肉烂，面条劲道，招牌必点！',
        'menu': [
            {'name': '红烧牛肉面', 'price': 18.0},
            {'name': '酸汤肥牛面', 'price': 20.0},
            {'name': '老北京炸酱面', 'price': 15.0},
            {'name': '凉拌黄瓜', 'price': 8.0},
            {'name': '卤蛋', 'price': 3.0},
            {'name': '冰镇酸梅汤', 'price': 6.0},
        ],
    },
    {
        'id': 'shop_002', 'name': '麦香堡炸鸡汉堡', 'category': '汉堡炸鸡', 'keywords': '汉堡 炸鸡 快餐 鸡腿堡',
        'icon': '🍔', 'signature': '香辣鸡腿堡外酥里嫩，趁热吃超满足！',
        'menu': [
            {'name': '香辣鸡腿堡', 'price': 16.0, 'specs': [
                {'group': '套餐', 'options': [{'label': '单堡'}, {'label': '套餐（薯条+可乐）', 'extra': 8.0}]},
                {'group': '辣度', 'options': [{'label': '微辣'}, {'label': '中辣'}]},
            ]},
            {'name': '双层牛肉堡', 'price': 22.0},
            {'name': '脆皮炸鸡桶', 'price': 26.0},
            {'name': '黄金薯条', 'price': 9.0},
            {'name': '劲爆鸡米花', 'price': 12.0},
            {'name': '可口可乐', 'price': 5.0},
        ],
    },
    {
        'id': 'shop_003', 'name': '蜀香麻辣烫', 'category': '麻辣烫', 'keywords': '麻辣烫 冒菜 串串',
        'icon': '🍲', 'signature': '秘制骨汤锅底，现烫现吃更过瘾！',
        'menu': [
            {'name': '经典麻辣烫', 'price': 19.0, 'specs': [
                {'group': '份量', 'options': [{'label': '小份'}, {'label': '大份', 'extra': 4.0}]},
                {'group': '辣度', 'options': [{'label': '微辣'}, {'label': '中辣'}, {'label': '特辣'}]},
            ]},
            {'name': '番茄麻辣烫', 'price': 21.0},
            {'name': '加金针菇', 'price': 6.0},
            {'name': '加午餐肉', 'price': 8.0},
            {'name': '加宽粉', 'price': 4.0},
            {'name': '冰镇酸梅汤', 'price': 6.0},
        ],
    },
    {
        'id': 'shop_004', 'name': '广式靓粥铺', 'category': '粥粉', 'keywords': '粥 早茶 肠粉 虾饺',
        'icon': '🥣', 'signature': '皮蛋瘦肉粥绵滑暖胃，正宗广式风味！',
        'menu': [
            {'name': '皮蛋瘦肉粥', 'price': 12.0},
            {'name': '香菇滑鸡粥', 'price': 14.0},
            {'name': '水晶虾饺', 'price': 16.0},
            {'name': '鲜虾肠粉', 'price': 11.0},
            {'name': '豉汁蒸排骨', 'price': 15.0},
            {'name': '现炸油条', 'price': 4.0},
        ],
    },
    {
        'id': 'shop_005', 'name': '茶语鲜奶茶饮', 'category': '奶茶咖啡', 'keywords': '奶茶 咖啡 饮品 果茶 柠檬茶',
        'icon': '🧋', 'signature': '珍珠 Q 弹奶茶香浓，人气招牌！',
        'menu': [
            {'name': '珍珠奶茶', 'price': 12.0, 'specs': [
                {'group': '甜度', 'options': [{'label': '标准糖'}, {'label': '半糖'}, {'label': '无糖'}]},
                {'group': '温度', 'options': [{'label': '冰'}, {'label': '去冰'}, {'label': '热'}]},
                {'group': '杯型', 'options': [{'label': '中杯'}, {'label': '大杯', 'extra': 2.0}]},
            ]},
            {'name': '芋泥波波奶茶', 'price': 16.0},
            {'name': '手打柠檬绿茶', 'price': 9.0},
            {'name': '冰美式咖啡', 'price': 10.0},
            {'name': '杨枝甘露', 'price': 18.0},
            {'name': '奶盖乌龙', 'price': 14.0},
        ],
    },
    {
        'id': 'shop_006', 'name': '深夜烧烤研究所', 'category': '烧烤', 'keywords': '烧烤 烤串 撸串 夜宵',
        'icon': '🍢', 'signature': '炭火现烤，孜然香辣过瘾，夜宵首选！',
        'menu': [
            {'name': '烤羊肉串（5 串）', 'price': 25.0},
            {'name': '烤鸡翅（2 个）', 'price': 14.0},
            {'name': '烤茄子', 'price': 12.0},
            {'name': '烤韭菜', 'price': 8.0},
            {'name': '烤冷面', 'price': 10.0},
            {'name': '冰镇酸梅汤', 'price': 6.0},
        ],
    },
    {
        'id': 'shop_007', 'name': '东北饺子馆', 'category': '饺子', 'keywords': '饺子 水饺 锅贴',
        'icon': '🥟', 'signature': '皮薄馅大手工水饺，家的味道！',
        'menu': [
            {'name': '猪肉白菜水饺（12 只）', 'price': 16.0},
            {'name': '韭菜鸡蛋水饺（12 只）', 'price': 15.0},
            {'name': '酸菜猪肉水饺（12 只）', 'price': 17.0},
            {'name': '凉拌木耳', 'price': 9.0},
            {'name': '蒜泥白肉', 'price': 18.0},
            {'name': '紫菜蛋花汤', 'price': 6.0},
        ],
    },
    {
        'id': 'shop_008', 'name': '田园轻食沙拉', 'category': '轻食', 'keywords': '轻食 沙拉 减脂 三明治 健身餐',
        'icon': '🥗', 'signature': '低卡轻食，健身减脂人群首选！',
        'menu': [
            {'name': '经典凯撒沙拉', 'price': 22.0},
            {'name': '牛油果鸡胸沙拉', 'price': 26.0},
            {'name': '全麦鸡胸三明治', 'price': 18.0},
            {'name': '鲜榨橙汁', 'price': 15.0},
            {'name': '低脂酸奶', 'price': 8.0},
            {'name': '水煮蛋', 'price': 4.0},
        ],
    },
    {
        'id': 'shop_009', 'name': '沙县小吃·盖浇饭', 'category': '快餐简餐', 'keywords': '快餐 盖浇饭 卤肉饭 拌面',
        'icon': '🍚', 'signature': '卤肉饭酱香浓郁，实惠管饱！',
        'menu': [
            {'name': '台式卤肉饭', 'price': 19.0},
            {'name': '番茄鸡蛋盖饭', 'price': 15.0},
            {'name': '鱼香肉丝盖饭', 'price': 17.0},
            {'name': '蒸饺（6 只）', 'price': 8.0},
            {'name': '花生拌面', 'price': 9.0},
            {'name': '海带排骨汤', 'price': 5.0},
        ],
    },
    {
        'id': 'shop_010', 'name': '云吞世家', 'category': '云吞粉面', 'keywords': '云吞 馄饨 竹升面 粉面',
        'icon': '🍜', 'signature': '鲜虾云吞皮薄馅鲜，汤底清甜！',
        'menu': [
            {'name': '鲜虾云吞面', 'price': 17.0},
            {'name': '竹升云吞面', 'price': 19.0},
            {'name': '净云吞（8 只）', 'price': 13.0},
            {'name': '豉油皇炒面', 'price': 12.0},
            {'name': '白灼菜心', 'price': 8.0},
            {'name': '例汤', 'price': 4.0},
        ],
    },
    {
        'id': 'shop_011', 'name': '川湘小炒·米饭快餐', 'category': '川湘菜', 'keywords': '川菜 湘菜 小炒 盖饭 辣',
        'icon': '🌶', 'signature': '小锅现炒镬气足，下饭神器！',
        'menu': [
            {'name': '辣子鸡盖饭', 'price': 21.0},
            {'name': '回锅肉盖饭', 'price': 20.0},
            {'name': '麻婆豆腐盖饭', 'price': 16.0},
            {'name': '剁椒鱼头套餐', 'price': 38.0},
            {'name': '酸辣土豆丝', 'price': 9.0},
            {'name': '紫菜蛋花汤', 'price': 5.0},
        ],
    },
    {
        'id': 'shop_012', 'name': '一品黄焖鸡米饭', 'category': '黄焖鸡', 'keywords': '黄焖鸡 米饭 鸡肉',
        'icon': '🍛', 'signature': '黄焖鸡嫩滑入味，搭配香喷喷米饭，超满足！',
        'menu': [
            {'name': '黄焖鸡米饭', 'price': 20.0, 'specs': [
                {'group': '份量', 'options': [{'label': '小份'}, {'label': '大份', 'extra': 3.0}]},
                {'group': '辣度', 'options': [{'label': '不辣'}, {'label': '微辣'}, {'label': '中辣'}, {'label': '特辣'}]},
                {'group': '自选饮料', 'options': [{'label': '不需要'}, {'label': '可乐'}, {'label': '怡宝水'}, {'label': '加多宝', 'extra': 2.0}]},
            ]},
            {'name': '加鸡腿', 'price': 6.0},
            {'name': '加金针菇', 'price': 4.0},
            {'name': '加宽粉', 'price': 4.0},
            {'name': '冬瓜汤', 'price': 5.0},
        ],
    },
]

_SHOP_MENU_INDEX = {s['id']: s for s in SHOPS}
_SHOP_NAME_INDEX = {s['name']: s for s in SHOPS}


def _resolve_shop(value):
    """按商家 id 或店名解析商家（均精确匹配，先 id 后店名）；找不到返回 None"""
    value = str(value or '').strip()
    if not value:
        return None
    return _SHOP_MENU_INDEX.get(value) or _SHOP_NAME_INDEX.get(value)


def _stable_num(text: str) -> int:
    """文本 → 稳定伪随机整数（同一商家/菜品多次查询结果一致）"""
    return int(hashlib.md5(text.encode('utf-8')).hexdigest(), 16)


def mask_phone(phone: str) -> str:
    """手机号脱敏：138****5678（提示词与卡片统一使用脱敏形式）"""
    return f'{phone[:3]}****{phone[7:]}' if PHONE_PATTERN.match(phone) else phone


def _rating_word(rating: float) -> str:
    """评分描述词，与前端徽章文案一致"""
    if rating >= 4.8:
        return '超棒'
    if rating >= 4.5:
        return '很好'
    return '好'


def _dish_out(shop: dict, dish: dict) -> dict:
    """菜单菜品的对外结构：名称/价格/月售/占位图标/规格组（选项附加价）"""
    out = {
        'name': dish['name'],
        'price': dish['price'],
        'monthly_sales': _dish_sales(shop['id'], dish['name']),
        'icon': shop['icon'],
    }
    if dish.get('specs'):
        out['specs'] = [
            {
                'group': g['group'],
                'options': [{'label': o['label'], **({'extra': o['extra']} if o.get('extra') else {})}
                            for o in g['options']],
            }
            for g in dish['specs']
        ]
    return out


def _shop_summary(shop: dict) -> dict:
    """商家概要：评分/月售/起送价/配送费/满减由 id 确定性生成；附招牌菜（名称/推荐语/预估价）"""
    n = _stable_num(shop['id'])
    threshold, discount = DEALS[n % len(DEALS)]
    rating = round(4.0 + n % 10 / 10, 1)  # 4.0 ~ 4.9
    signature = shop['menu'][0]
    return {
        'id': shop['id'],
        'name': shop['name'],
        'category': shop['category'],
        'icon': shop['icon'],
        'rating': rating,
        'rating_word': _rating_word(rating),
        'monthly_sales': 100 + (n >> 4) % 2900,  # 100 ~ 2999
        'min_order': 15 + (n >> 10) % 3 * 5,  # 15 / 20 / 25 元
        'delivery_fee': 2 + (n >> 8) % 4,  # 2 ~ 5 元
        'delivery_time': 25 + (n >> 12) % 20,  # 25 ~ 44 分钟
        'deal': f'满 {threshold} 减 {discount}',
        'deal_threshold': threshold,
        'deal_discount': discount,
        'signature_name': signature['name'],
        'signature_desc': shop['signature'],
        'signature_price': signature['price'],
        # 招牌菜规格组：前端卡片内本地规格选择（千问图2 弹层）直接使用，不经 LLM
        'signature_specs': signature.get('specs'),
    }


def _dish_sales(shop_id: str, dish_name: str) -> int:
    """菜品月售：由商家+菜名确定性生成（30 ~ 329）"""
    return 30 + _stable_num(f'{shop_id}:{dish_name}') % 300


def _search_shops(keyword: str) -> list:
    """按品类/店名/菜品关键词搜索商家；关键词为空时返回推荐商家"""
    kw = keyword.strip()
    if not kw:
        return [_shop_summary(s) for s in SHOPS[:6]]
    hits = []
    for shop in SHOPS:
        hay = f"{shop['name']} {shop['category']} {shop['keywords']} " + ' '.join(d['name'] for d in shop['menu'])
        if kw in hay:
            hits.append(shop)
    return [_shop_summary(s) for s in hits[:6]]


def _validate_items(raw, shop: dict) -> tuple:
    """校验购买清单：非空、菜品在菜单中、数量为正整数、规格选项合法；
    价格一律以服务端菜单为准（基础价 + 规格加价）。
    返回 (规范化清单, 错误信息)，合法时错误信息为空串。"""
    if not isinstance(raw, list) or not raw:
        return [], '购买清单为空：请先与用户确认要购买的菜品后再下单'
    menu = {d['name']: d for d in shop['menu']}
    items = []
    for it in raw:
        if not isinstance(it, dict):
            return [], '购买清单格式不正确，请按菜单重新选择'
        name = str(it.get('name', '')).strip()
        dish = menu.get(name)
        if not dish:
            return [], f'菜品「{name}」不在 {shop["name"]} 的菜单中，请从菜单返回的菜品里选择'
        try:
            quantity = int(it.get('quantity', 1))
        except (TypeError, ValueError):
            quantity = 0
        if quantity < 1:
            return [], f'菜品「{name}」的数量必须为正整数'

        # 规格校验：每个选项必须取菜单规格组的选项原文，且每组至多选一个
        raw_specs = it.get('specs') or []
        chosen, extra = [], 0
        if raw_specs:
            if not dish.get('specs'):
                return [], f'菜品「{name}」没有可选规格，specs 请留空'
            group_of = {}
            for g in dish['specs']:
                for o in g['options']:
                    group_of[o['label']] = (g['group'], o)
            for s in raw_specs:
                label = str(s).strip()
                if label not in group_of:
                    return [], f'菜品「{name}」不支持规格「{label}」，请从菜单规格选项中选择'
                group, opt = group_of[label]
                if any(g == group for g, _ in chosen):
                    return [], f'菜品「{name}」的规格组「{group}」只能选择一项'
                chosen.append((group, opt))
                extra += opt.get('extra', 0)

        items.append({
            'name': name,
            'price': round(dish['price'] + extra, 2),
            'quantity': quantity,
            'specs': [label for _, opt in chosen for label in [opt['label']]],
        })
    return items, ''


def _order_amounts(shop: dict, items: list) -> dict:
    """按服务端菜单价格计算订单金额明细（菜品小计/餐盒费/配送费/满减/合计）"""
    summary = _shop_summary(shop)
    subtotal = round(sum(i['price'] * i['quantity'] for i in items), 2)
    total_qty = sum(i['quantity'] for i in items)
    packing_fee = round(min(PACKING_PER_ITEM * total_qty, PACKING_CAP), 2)
    discount = summary['deal_discount'] if subtotal >= summary['deal_threshold'] else 0
    total = round(subtotal + packing_fee + summary['delivery_fee'] - discount, 2)
    return {
        'subtotal': subtotal,
        'packing_fee': packing_fee,
        'delivery_fee': summary['delivery_fee'],
        'discount': discount,
        'total': total,
    }


def _items_summary(items: list) -> str:
    """购买清单摘要：黄焖鸡米饭（大份/加多宝）×1、冬瓜汤×1"""
    parts = []
    for i in items:
        name = f"{i['name']}（{'/'.join(i['specs'])}）" if i['specs'] else i['name']
        parts.append(f'{name}×{i["quantity"]}')
    return '、'.join(parts)


def _new_order_no() -> str:
    """生成演示订单号：WM + 时间戳"""
    return f'WM{datetime.now().strftime("%Y%m%d%H%M%S")}{int(time.time() * 1000) % 1000:03d}'


def _order_stage(order: dict) -> str:
    """按下单时间推进订单状态：备餐中(30s) → 配送中(90s) → 已送达"""
    elapsed = time.time() - order['created_at']
    if elapsed >= DELIVERED_SECONDS:
        return '已送达'
    if elapsed >= PREP_SECONDS:
        return '骑手配送中'
    return '商家备餐中'


def _order_card(order: dict, title: str) -> dict:
    """订单卡片（对齐千问订单确认页明细行）：下单成功与状态查询共用同一结构"""
    status = _order_stage(order)
    return {
        'kind': 'result',
        'title': title,
        'order_no': order['order_no'],
        'shop_name': order['shop_name'],
        'icon': order['icon'],
        'items_summary': order['items_summary'],
        'subtotal': order['subtotal'],
        'packing_fee': order['packing_fee'],
        'delivery_fee': order['delivery_fee'],
        'discount': order['discount'],
        'total': order['total'],
        'address': order['address'],
        'phone': order['phone'],
        'remark': order['remark'],
        'arrival': order['arrival'],
        'status': status,
        'eta': '' if status == '已送达' else order['arrival'],
    }


# Anthropic tools 定义：模型据此规划"搜商家 → 查菜单 → 规格表单 → 预览 → 确认提交"的调用链
WAIMAI_TOOLS = [
    {
        'name': 'waimai_search_shops',
        'description': '搜索附近外卖商家（演示数据）。可按品类（奶茶/汉堡/面食/烧烤/轻食等）或店名关键词搜索，'
                       '不传关键词时返回推荐商家。返回商家列表（含 shop_id/评分/起送价/配送费/招牌菜与预估价）。',
        'input_schema': {
            'type': 'object',
            'properties': {
                'keyword': {'type': 'string', 'description': '品类或店名关键词，如"奶茶""汉堡""黄焖鸡"，可空'},
            },
            'required': [],
        },
    },
    {
        'name': 'waimai_menu',
        'description': '查询指定商家的菜单（菜品名/价格/月售/规格组 specs）。用户选定商家后调用；'
                       '带 specs 的菜品可让用户选规格（选项可加价），下单时菜品的 name/price/specs '
                       '必须取本工具返回值（服务端会按菜单校验）。',
        'input_schema': {
            'type': 'object',
            'properties': {
                'shop_id': {'type': 'string', 'description': '商家 id 或店名（优先用 waimai_search_shops 返回的 id；用户消息已明确店名时可直接传店名）'},
            },
            'required': ['shop_id'],
        },
    },
    {
        'name': 'waimai_order',
        'description': '外卖点餐下单：confirm=false 仅预览（返回菜品小计/餐盒费/配送费/满减/合计与预计送达时间，'
                       '生成费用确认表单等用户确认）；confirm=true 正式提交订单（参数必须与预览时完全一致）。'
                       '提交成功返回订单号、预计送达时间与状态。',
        'input_schema': {
            'type': 'object',
            'properties': {
                'shop_id': {'type': 'string', 'description': '商家 id 或店名（优先用 waimai_search_shops 返回的 id；用户消息已明确店名时可直接传店名）'},
                'items': {
                    'type': 'array',
                    'description': '购买清单，每项 {name, price, quantity, specs?}，'
                                   'name/price 取菜单返回值，specs 为规格选项原文数组（可空）',
                    'items': {
                        'type': 'object',
                        'properties': {
                            'name': {'type': 'string', 'description': '菜品名（菜单返回值）'},
                            'price': {'type': 'number', 'description': '单价（元，菜单返回值，含规格加价时以加价后为准）'},
                            'quantity': {'type': 'integer', 'description': '数量，正整数'},
                            'specs': {
                                'type': 'array',
                                'description': '规格选项原文数组，如 ["大份", "微辣", "加多宝"]，必须取菜单 specs 的选项 label',
                                'items': {'type': 'string'},
                            },
                        },
                        'required': ['name', 'price', 'quantity'],
                    },
                },
                'address': {'type': 'string', 'description': '配送地址（用户表单提交，不得编造）'},
                'phone': {'type': 'string', 'description': '11 位联系电话（用户表单提交，不得编造）'},
                'remark': {'type': 'string', 'description': '订单备注，如"少辣""放门口"，可空'},
                'confirm': {'type': 'boolean', 'description': 'false=预览，true=提交（用户明确确认后）'},
            },
            'required': ['shop_id', 'items', 'address', 'phone', 'confirm'],
        },
    },
    {
        'name': 'waimai_order_status',
        'description': '查询外卖订单状态（商家备餐中/骑手配送中/已送达）。用户询问订单进展时调用。',
        'input_schema': {
            'type': 'object',
            'properties': {
                'order_no': {'type': 'string', 'description': '订单号（waimai_order 提交成功后返回）'},
            },
            'required': ['order_no'],
        },
    },
]

# 工具中文名：前端工具 chip 展示用
TOOL_LABELS = {
    'waimai_search_shops': '商家搜索',
    'waimai_menu': '菜单查询',
    'waimai_order': '外卖下单',
    'waimai_order_status': '订单查询',
}


def build_waimai_system_prompt() -> str:
    return (
        '你是外卖点餐助手（演示环境：商家、菜单与下单均为模拟数据，无真实交易与配送，需向用户说明这一点）。\n'
        '【输出规范】严禁展示英文字段名、JSON、技术细节；联系电话一律脱敏（138****5678）；'
        '金额精确到分（如 18.00 元）；商家/菜单/订单卡片由系统自动附在回答上方，正文只需简明中文说明。\n'
        '【安全门控】点餐是消费行为：必须先 waimai_order confirm=false 预览，'
        '并生成费用确认表单（A2UI）展示订单明细，等用户点击"确认下单"按钮（或明确回复"确认"）后才 confirm=true 提交，'
        '提交参数与预览完全一致；单笔合计 > 100 元时额外提醒用户一次；'
        '配送地址或联系电话缺失时必须生成点餐信息表单（A2UI）让用户填写，不得用文字追问或编造；对话中用户已给过地址与电话时优先复用并向用户复述确认。\n'
        '【工作流程】\n'
        '① 用户说出想吃什么后先用 waimai_search_shops 搜索（关键词可用品类如"奶茶""汉堡"，或店名）。'
        '正文用简短引导语（参考："已为你找到附近 N 家黄焖鸡米饭店铺，请看看有没有心仪的：你可以直接告诉我想选哪家店，'
        '或者需要我帮你推荐一下？"），挑 1~2 家做简评（招牌菜/评分），不要罗列全部字段。\n'
        '② 用户选定商家后 waimai_menu 获取菜单（shop_id 可直接传店名），与用户确认要买的菜品和数量。'
        '若所选菜品带规格组（specs），生成规格选择表单（A2UI）：顶部用 Text 展示菜名与价格，'
        '每个规格组用一个 ChoicePicker（label 用组名，options 的 value 必须用菜单返回的选项原文，'
        'label 可附加价如"大份（+3.00 元）"），底部按钮 action name 为 submit_waimai_order（文案"选好了"），'
        'updateDataModel 给出全部初始值；菜品无规格时直接与用户确认后进入下一步。'
        '注意：用户消息若已自带完整菜品与规格参数（如"我要点「一品黄焖鸡米饭」的黄焖鸡米饭（大份/中辣/加多宝）×2"，'
        '来自前端规格弹窗提交），无需再查菜单或生成规格表单，直接按 ③ 处理地址与电话。\n'
        '③ 若对话中还没有配送地址或联系电话，先不要预览，生成点餐信息表单（A2UI）：'
        '配送地址（必填）与联系电话（必填）用 TextField（placeholder 给示例，如"示例路88号3栋502室"、"11 位手机号"），'
        '备注（可选）用 TextField，底部按钮 action name 为 submit_waimai_order（文案"提交"）。\n'
        '④ 收到规格表单或点餐信息表单的 [A2UI_EVENT] 提交后，直接用提交内容组装 items 与收货信息'
        '调 waimai_order confirm=false 预览（菜品 specs 传选项原文数组，如 ["大份","微辣","可乐"]），不得再次生成相同表单，'
        '也不得再调 waimai_search_shops / waimai_menu（shop_id 可直接传用户消息中的店名；进入下单流程后不再展示商家/菜品卡片）。\n'
        '⑤ 预览成功后生成费用确认表单（A2UI）：用 Text 逐行展示订单明细'
        '（商家/菜品与规格/餐盒费/配送费/满减优惠/合计/送达时间"立即配送，预计约 30 分钟后送达"/配送地址/联系电话/备注），'
        '下方两个按钮：confirm_waimai（primary，文案"确认下单"）与 cancel_waimai（secondary，文案"取消"）。\n'
        '⑥ 用户点击"确认下单"后 waimai_order confirm=true 提交，'
        '正文风格参考："好的，已为你选好商品并提交订单，共优惠 X 元，预计 30 分钟送达。"'
        '用户询问订单进展时用 waimai_order_status 查询并告知最新状态（商家备餐中/骑手配送中/已送达）。\n'
        '【参数规范】shop_id 支持商家 id 或店名：用户消息已明确店名时直接传店名，无需先 waimai_search_shops；'
        'items 中每个菜品的 name 与 price 必须取 waimai_menu 返回值（price 单位元），'
        'quantity 为正整数；specs 数组必须取菜单规格选项的原文（不含加价后缀），每组至多一项。\n'
        '用户偏好纯文字交流时按文字流程进行（逐项确认菜品、规格、地址与电话，预览与确认门控不变）。\n'
        '工具返回错误时按错误信息说明原因并给出替代建议。'
    )


class WaimaiService:
    """外卖点餐演示服务（模拟商家/菜单数据 + 点餐工具执行器）"""

    MAX_TOOL_ROUNDS = 8  # 点餐流程较长：搜商家→菜单→规格表单→预览→确认→提交

    @property
    def configured(self) -> bool:
        return True  # 演示场景免凭据，开箱即用

    @property
    def tools(self) -> list:
        return WAIMAI_TOOLS

    @staticmethod
    def system_prompt() -> str:
        return build_waimai_system_prompt()

    @staticmethod
    def tool_label(name: str) -> str:
        return TOOL_LABELS.get(name, name)

    @staticmethod
    def card_event_type(card: dict) -> str:
        return 'waimai_card'

    # ---------- 各工具执行：均返回 (给模型的数据, 给前端的卡片) ----------

    async def _exec_search(self, args: dict):
        keyword = str(args.get('keyword', '') or '')
        shops = _search_shops(keyword)
        if not shops:
            return {'error': f'没有找到与「{keyword}」相关的商家，建议换个品类关键词（如奶茶/汉堡/面食）'}, None
        return {'keyword': keyword, 'shops': shops}, {
            'kind': 'shops',
            'title': f'「{keyword}」附近商家' if keyword else '附近推荐商家',
            'shops': shops,
        }

    async def _exec_menu(self, args: dict):
        shop = _resolve_shop(args.get('shop_id'))
        if not shop:
            return {'error': '未找到该商家（可传商家 id 或店名），请用 waimai_search_shops 搜索后重试'}, None
        summary = _shop_summary(shop)
        dishes = [_dish_out(shop, d) for d in shop['menu']]
        card = {
            'kind': 'menu',
            'title': f'{shop["name"]} 菜单',
            'shop_name': shop['name'],
            'icon': shop['icon'],
            'category': shop['category'],
            'rating': summary['rating'],
            'min_order': summary['min_order'],
            'delivery_fee': summary['delivery_fee'],
            'deal': summary['deal'],
            'dishes': dishes,
        }
        return {**card, 'notice': '演示模拟数据，无真实交易'}, card

    async def _exec_order(self, args: dict):
        shop = _resolve_shop(args.get('shop_id'))
        if not shop:
            return {'error': '未找到该商家（可传商家 id 或店名），请用 waimai_search_shops 搜索后重试'}, None
        items, err = _validate_items(args.get('items'), shop)
        if err:
            return {'error': err}, None
        address = str(args.get('address', '')).strip()
        if not address:
            return {'error': '配送地址缺失，请向用户询问完整地址后重试'}, None
        phone = str(args.get('phone', '')).strip()
        if not PHONE_PATTERN.match(phone):
            return {'error': '联系电话格式不正确：需要 11 位、1 开头的国内手机号，请向用户确认后重试'}, None
        remark = str(args.get('remark', '') or '').strip()

        amounts = _order_amounts(shop, items)
        arrival = f'立即配送，预计 {datetime.now() + timedelta(minutes=30):%H:%M} 送达'
        base = {
            'shop_id': shop['id'],
            'shop_name': shop['name'],
            'icon': shop['icon'],
            'items_summary': _items_summary(items),
            'address': address,
            'phone': mask_phone(phone),
            'remark': remark,
            'arrival': arrival,
            **amounts,
        }

        # 预览：返回订单明细等用户确认，不生成订单
        if not args.get('confirm'):
            return {
                'action': 'preview',
                **base,
                'notice': '预览成功，请生成费用确认表单等用户确认后再提交',
            }, None

        # 提交：内存记账生成订单，状态随时间流转
        order_no = _new_order_no()
        order = {**base, 'order_no': order_no, 'created_at': time.time()}
        _ORDERS[order_no] = order
        card = _order_card(order, '外卖下单成功')
        saved = amounts['discount']  # 共优惠金额（满减），供正文播报
        return {**base, 'action': 'submitted', 'order_no': order_no,
                'status': card['status'], 'saved': saved,
                'notice': '演示模拟数据，无真实交易'}, card

    async def _exec_order_status(self, args: dict):
        order_no = str(args.get('order_no', '')).strip()
        order = _ORDERS.get(order_no)
        if not order:
            return {'error': f'未找到订单{f"「{order_no}」" if order_no else ""}，请核对订单号'}, None
        card = _order_card(order, '外卖订单状态')
        result = {k: v for k, v in order.items() if k != 'created_at'}
        result['status'] = card['status']
        result['eta'] = card['eta']
        result['notice'] = '演示模拟数据，无真实交易'
        return result, card

    # ---------- 对外入口 ----------

    _HANDLERS = {
        'waimai_search_shops': '_exec_search',
        'waimai_menu': '_exec_menu',
        'waimai_order': '_exec_order',
        'waimai_order_status': '_exec_order_status',
    }

    async def execute_tool(self, name: str, args: dict):
        """执行一次外卖工具调用，返回 (给模型的数据, 给前端的卡片)"""
        handler = self._HANDLERS.get(name)
        if not handler:
            return {'error': f'未知工具: {name}'}, None
        try:
            return await getattr(self, handler)(args)
        except Exception as err:  # 兜底，避免中断 agent 循环
            return {'error': f'外卖点餐服务异常：{err}'}, None

    @staticmethod
    def summarize_result(name: str, result: dict) -> str:
        """工具结果的一句话摘要，供前端工具 chip 展示"""
        if result.get('error'):
            return '失败'
        if name == 'waimai_search_shops':
            count = len(result.get('shops', []))
            return f'{count} 家商家' if count else '无结果'
        if name == 'waimai_menu':
            return f'{len(result.get("dishes", []))} 道菜品'
        if name == 'waimai_order':
            return '已提交' if result.get('action') == 'submitted' else '预览'
        if name == 'waimai_order_status':
            return result.get('status', '成功')
        return ''
