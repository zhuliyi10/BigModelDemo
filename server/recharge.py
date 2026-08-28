"""话费充值（演示场景）服务封装：模拟运营商话费数据 + 充值下单工具执行器。

与 AmapService / WeatherService 同款三层结构：
- 模型只决定"何时调用、传什么参数"（RECHARGE_TOOLS 定义）；
- RechargeService 在内存中模拟运营商侧数据（余额/档位/订单）并回传结果；
- 每次工具执行同时产出两份结果：给模型的结构化数据、给前端的卡片。

模拟数据约定（无真实扣费，界面与卡片明确标注"演示数据"）：
- 同一手机号的归属地/余额/档位售价由号码确定性生成，多次查询结果一致；
- 下单仅在服务内存记账，30 秒后查询订单状态由"充值中"流转为"充值成功"。

⚠️ 充值是消费行为：系统提示词强制"预览 → 用户明确确认 → 提交"两步门控。
"""

import hashlib
import re
import time
from datetime import datetime

# 演示用号段 → 运营商映射（号段规律真实，归属地城市为模拟）
CARRIER_PREFIXES = {
    '移动': ('134', '135', '136', '137', '138', '139', '147', '148', '150', '151',
             '152', '157', '158', '159', '178', '182', '183', '184', '187', '188', '198'),
    '联通': ('130', '131', '132', '145', '146', '155', '156', '166', '175', '176', '185', '186'),
    '电信': ('133', '149', '153', '173', '174', '177', '180', '181', '189', '190', '191', '199'),
}

# 归属地城市池（演示模拟，与跑腿城市表保持一致）
CITIES = ['北京', '上海', '广州', '深圳', '杭州', '成都', '武汉', '南京', '西安', '重庆']

# 可充值面值档位（元）
FACE_VALUES = [30, 50, 100, 200, 300, 500]

# 充值到账模拟时长（秒）：期间查询订单状态显示"充值中"
SETTLE_SECONDS = 30

PHONE_PATTERN = re.compile(r'^1[3-9]\d{9}$')

# 已提交订单（内存记账，单进程演示足够）
_ORDERS = {}


def _stable_num(text: str) -> int:
    """文本 → 稳定伪随机整数（同一手机号多次查询结果一致）"""
    return int(hashlib.md5(text.encode('utf-8')).hexdigest(), 16)


def mask_phone(phone: str) -> str:
    """手机号脱敏：138****5678（提示词与卡片统一使用脱敏形式）"""
    return f'{phone[:3]}****{phone[7:]}' if PHONE_PATTERN.match(phone) else phone


def _validate_phone(phone: str) -> str:
    """手机号校验，不合法时返回错误信息，合法返回空串"""
    if not PHONE_PATTERN.match(phone):
        return '手机号格式不正确：需要 11 位、1 开头的国内手机号，请向用户确认后重试'
    return ''


def _phone_info(phone: str) -> dict:
    """模拟运营商账户信息：归属地/运营商/余额均由号码确定性生成"""
    n = _stable_num(phone)
    carrier = next((name for name, prefixes in CARRIER_PREFIXES.items() if phone[:3] in prefixes), '移动')
    return {
        'phone': mask_phone(phone),
        'carrier': carrier,
        'city': CITIES[n % len(CITIES)],
        'balance': round(3.5 + n % 7000 / 100, 2),  # 3.50 ~ 73.49 元
    }


def _plan(phone: str, face_value: int) -> dict:
    """模拟某面值档位的优惠售价：面值减 0~2.5 元立减（同号同档位稳定）"""
    discount = round(0.5 * (_stable_num(f'{phone}:{face_value}') % 6), 2)
    return {'face_value': face_value, 'discount': discount, 'price': round(face_value - discount, 2)}


def _new_order_no() -> str:
    """生成演示订单号：RC + 时间戳"""
    return f'RC{datetime.now().strftime("%Y%m%d%H%M%S")}{int(time.time() * 1000) % 1000:03d}'


# Anthropic tools 定义：模型据此规划"查余额 → 取档位 → 预览 → 确认提交"的调用链
RECHARGE_TOOLS = [
    {
        'name': 'recharge_query',
        'description': '查询手机号话费账户（归属地、运营商、当前余额、账户状态）。用户提出充话费后先用本工具核实手机号，余额卡片由系统自动展示。',
        'input_schema': {
            'type': 'object',
            'properties': {
                'phone': {'type': 'string', 'description': '11 位手机号，如 13800138000'},
            },
            'required': ['phone'],
        },
    },
    {
        'name': 'recharge_plans',
        'description': '查询该手机号可充值的面值档位与当前优惠售价。生成充值表单（A2UI）前必须调用，ChoicePicker 的选项要用返回的档位。',
        'input_schema': {
            'type': 'object',
            'properties': {
                'phone': {'type': 'string', 'description': '11 位手机号'},
            },
            'required': ['phone'],
        },
    },
    {
        'name': 'recharge_order',
        'description': '话费充值下单：confirm=false 仅预览（返回充值明细与应付金额，生成确认表单等用户确认）；'
                       'confirm=true 正式提交充值（参数必须与预览时完全一致）。提交成功返回订单号与预计到账时间。',
        'input_schema': {
            'type': 'object',
            'properties': {
                'phone': {'type': 'string', 'description': '11 位手机号'},
                'face_value': {'type': 'integer', 'description': '充值面值（元），必须是 recharge_plans 返回的档位'},
                'confirm': {'type': 'boolean', 'description': 'false=预览，true=提交（用户明确确认后）'},
            },
            'required': ['phone', 'face_value', 'confirm'],
        },
    },
    {
        'name': 'recharge_order_status',
        'description': '查询充值订单状态（充值中/充值成功）。用户询问是否到账时调用。',
        'input_schema': {
            'type': 'object',
            'properties': {
                'order_no': {'type': 'string', 'description': '订单号（recharge_order 提交成功后返回）'},
            },
            'required': ['order_no'],
        },
    },
]

# 工具中文名：前端工具 chip 展示用
TOOL_LABELS = {
    'recharge_query': '余额查询',
    'recharge_plans': '充值档位',
    'recharge_order': '充值下单',
    'recharge_order_status': '订单查询',
}


def build_recharge_system_prompt() -> str:
    return (
        '你是话费充值助手（演示环境：话费数据与充值下单均为模拟数据，无真实扣费，需向用户说明这一点）。\n'
        '【输出规范】严禁展示英文字段名、JSON、技术细节；手机号一律脱敏（138****5678）；'
        '金额精确到分（如 99.50 元）；余额卡片与结果卡片由系统自动附在回答上方，正文只需简明中文说明。\n'
        '【安全门控】充值是消费行为：必须先 recharge_order confirm=false 预览，'
        '并生成费用确认表单（A2UI）展示充值明细，等用户点击"确认充值"按钮（或明确回复"确认"）后才 confirm=true 提交，'
        '提交参数与预览完全一致；单笔应付 > 200 元时额外提醒用户一次；'
        '手机号缺失或格式不正确（11 位、1 开头）时必须向用户询问，不得编造。\n'
        '【工作流程】① 用户提供手机号后先 recharge_query 核实并展示余额；'
        '② recharge_plans 获取档位与优惠价；'
        '③ 生成充值表单（A2UI）：手机号 TextField（用户已提供则预填脱敏号码，提醒用户可改）、'
        '充值面值 ChoicePicker（options 取 recharge_plans 返回的档位，label 形如"100 元（售 99.00 元）"、value 用面值数字）、'
        '支付方式 ChoicePicker（options 的 label 与 value 均用中文：支付宝/微信/云闪付），'
        '提交按钮 action name 为 submit_recharge，updateDataModel 给出全部初始值；'
        '④ 收到 [A2UI_EVENT] 表单提交后，直接用提交数据 recharge_order confirm=false 预览，不得再次生成相同表单；'
        '⑤ 预览成功后生成费用确认表单（A2UI）：用 Text 逐行展示充值明细'
        '（充值号码/运营商归属地/当前余额/充值面值/优惠/应付金额/支付方式），'
        '下方两个按钮：confirm_recharge（primary，文案"确认充值"）与 cancel_recharge（secondary，文案"取消"）；'
        '⑥ 用户点击"确认充值"后 recharge_order confirm=true 提交，成功后提示预计 10 分钟内到账；'
        '用户询问到账状态时用 recharge_order_status 查询并告知最新状态。\n'
        '【参数规范】face_value 必须取 recharge_plans 返回的档位面值（整数元）。\n'
        '用户偏好纯文字交流时按文字流程进行（逐项确认手机号与面值，预览与确认门控不变）。\n'
        '工具返回错误时按错误信息说明原因并给出替代建议。'
    )


class RechargeService:
    """话费充值演示服务（模拟运营商数据 + 充值工具执行器）"""

    MAX_TOOL_ROUNDS = 8  # 充值流程较长：核实号码→档位→表单→预览→确认→提交

    @property
    def configured(self) -> bool:
        return True  # 演示场景免凭据，开箱即用

    @property
    def tools(self) -> list:
        return RECHARGE_TOOLS

    @staticmethod
    def system_prompt() -> str:
        return build_recharge_system_prompt()

    @staticmethod
    def tool_label(name: str) -> str:
        return TOOL_LABELS.get(name, name)

    @staticmethod
    def card_event_type(card: dict) -> str:
        return 'recharge_card'

    # ---------- 各工具执行：均返回 (给模型的数据, 给前端的卡片) ----------

    async def _exec_query(self, args: dict):
        phone = str(args.get('phone', '')).strip()
        err = _validate_phone(phone)
        if err:
            return {'error': err}, None
        info = _phone_info(phone)
        low = info['balance'] < 10
        card = {
            'kind': 'balance',
            'title': '话费余额查询',
            **info,
            'status': '余额偏低' if low else '正常',
        }
        result = {**info, 'status': '余额偏低，建议充值' if low else '正常', 'notice': '演示模拟数据，无真实扣费'}
        return result, card

    async def _exec_plans(self, args: dict):
        phone = str(args.get('phone', '')).strip()
        err = _validate_phone(phone)
        if err:
            return {'error': err}, None
        plans = [_plan(phone, fv) for fv in FACE_VALUES]
        return {'phone': mask_phone(phone), 'plans': plans}, None

    async def _exec_order(self, args: dict):
        phone = str(args.get('phone', '')).strip()
        face_value = args.get('face_value')
        err = _validate_phone(phone)
        if err:
            return {'error': err}, None
        try:
            face_value = int(face_value)
        except (TypeError, ValueError):
            face_value = None
        if face_value not in FACE_VALUES:
            return {'error': f'充值面值仅支持 {"/".join(str(v) for v in FACE_VALUES)} 元档位'}, None

        plan = _plan(phone, face_value)
        info = _phone_info(phone)

        # 预览：返回充值明细等用户确认，不生成订单
        if not args.get('confirm'):
            return {
                'action': 'preview',
                'phone': info['phone'],
                'carrier': info['carrier'],
                'city': info['city'],
                'balance': info['balance'],
                'face_value': face_value,
                'discount': plan['discount'],
                'payable': plan['price'],
                'notice': '预览成功，请生成费用确认表单等用户确认后再提交',
            }, None

        # 提交：内存记账生成订单，30 秒后状态流转为"充值成功"
        order_no = _new_order_no()
        order = {
            'order_no': order_no,
            'phone': info['phone'],
            'face_value': face_value,
            'payable': plan['price'],
            'status': '充值中',
            'created_at': time.time(),
        }
        _ORDERS[order_no] = order
        card = {
            'kind': 'result',
            'title': '充值提交成功',
            'order_no': order_no,
            'phone': order['phone'],
            'face_value': face_value,
            'payable': plan['price'],
            'status': order['status'],
            'eta': '预计 10 分钟内到账',
        }
        return {
            'action': 'submitted',
            'order_no': order_no,
            'phone': order['phone'],
            'face_value': face_value,
            'payable': plan['price'],
            'status': order['status'],
            'eta': '预计 10 分钟内到账',
            'notice': '演示模拟数据，无真实扣费',
        }, card

    async def _exec_order_status(self, args: dict):
        order_no = str(args.get('order_no', '')).strip()
        order = _ORDERS.get(order_no)
        if not order:
            return {'error': f'未找到订单{f"「{order_no}」" if order_no else ""}，请核对订单号'}, None
        # 模拟到账：下单超过 SETTLE_SECONDS 秒后状态流转为"充值成功"
        order['status'] = '充值成功' if time.time() - order['created_at'] >= SETTLE_SECONDS else '充值中'
        card = {
            'kind': 'result',
            'title': '充值订单状态',
            'order_no': order['order_no'],
            'phone': order['phone'],
            'face_value': order['face_value'],
            'payable': order['payable'],
            'status': order['status'],
            'eta': '' if order['status'] == '充值成功' else '预计 10 分钟内到账',
        }
        result = {k: v for k, v in order.items() if k != 'created_at'}
        result['notice'] = '演示模拟数据，无真实扣费'
        return result, card

    # ---------- 对外入口 ----------

    _HANDLERS = {
        'recharge_query': '_exec_query',
        'recharge_plans': '_exec_plans',
        'recharge_order': '_exec_order',
        'recharge_order_status': '_exec_order_status',
    }

    async def execute_tool(self, name: str, args: dict):
        """执行一次充值工具调用，返回 (给模型的数据, 给前端的卡片)"""
        handler = self._HANDLERS.get(name)
        if not handler:
            return {'error': f'未知工具: {name}'}, None
        try:
            return await getattr(self, handler)(args)
        except Exception as err:  # 兜底，避免中断 agent 循环
            return {'error': f'话费充值服务异常：{err}'}, None

    @staticmethod
    def summarize_result(name: str, result: dict) -> str:
        """工具结果的一句话摘要，供前端工具 chip 展示"""
        if result.get('error'):
            return '失败'
        if name == 'recharge_query':
            balance = result.get('balance')
            return f'余额 {balance} 元' if balance is not None else '成功'
        if name == 'recharge_plans':
            return f'{len(result.get("plans", []))} 个档位'
        if name == 'recharge_order':
            return '已提交' if result.get('action') == 'submitted' else '预览'
        if name == 'recharge_order_status':
            return result.get('status', '成功')
        return ''
