"""美团跑腿 Skill（meituan-paotui，技术开放平台版）封装：paotui.js CLI 执行器。

官方 Skill 包（美团开放平台 AI Hub 下载）内含 paotui.js 与 references/，
本模块在服务端以子进程方式执行 CLI，覆盖：鉴权检查/确认、地址簿、POI 搜索、
费用预览、确认下单、订单状态查询。

鉴权约定（aihub 渠道）：
- 环境变量 MEITUAN_PAOTUI_SOURCE_FROM=aihub（非 qclaw 渠道无需 apiKey）；
- 用户级授权走依赖的 pt-passport CLI（Skill 包自带安装脚本）：
  paotui_login 在 paotui.js login 返回 AUTH_FAILED 时回退
  `pt-passport auth get-code` 拿 AUTH_LINK；用户扫码后 paotui_confirm_auth
  执行 `pt-passport auth poll-token`；Token 缓存于
  ~/.xiaomei-workspace/mt_passport_auth.json，执行 CLI 前经
  `pt-passport get-token` 取出注入 MCP_ACCESS_TOKEN。

与 AmapService / TravelService 同款三层结构：
- 模型只决定"何时调用、传什么参数"（PAOTUI_TOOLS 定义）；
- PaotuiService 真正执行 CLI 并回传结果；
- 每次工具执行同时产出两份结果：给模型的结构化数据、给前端的状态卡片。

⚠️ 跑腿下单是真实消费：系统提示词强制"预览 → 用户明确确认 → 提交"两步门控。
"""

import asyncio
import json
import os
import shutil
import subprocess
from pathlib import Path

SKILL_DIR = Path(os.getenv('MEITUAN_PAOTUI_DIR', '')).expanduser() if os.getenv('MEITUAN_PAOTUI_DIR') else Path(__file__).resolve().parent / 'skills' / 'meituan-paotui'
CLI_TIMEOUT_SECONDS = int(os.getenv('PAOTUI_TIMEOUT', '120'))
CONFIRM_AUTH_TIMEOUT_SECONDS = int(os.getenv('PAOTUI_CONFIRM_TIMEOUT', '620'))
# Skill 包 skill-dependencies 中声明的 passport 应用标识（prod 环境）
PASSPORT_CLIENT_ID = os.getenv('MEITUAN_PASSPORT_CLIENT_ID', 'ac76c22d257c4c6d9164719eea64ed4b')
PASSPORT_ENV = os.getenv('MEITUAN_PASSPORT_ENV', 'prod')

# Anthropic tools 定义：对应官方 references/commands.md 的命令面
PAOTUI_TOOLS = [
    {
        'name': 'paotui_login',
        'description': '检查美团跑腿登录/授权状态（每个会话仅在首次需要授权时调用一次）。返回"已登录"或授权链接（AUTH_LINK）。'
                       '返回授权链接时把链接展示给用户，引导其用美团 App 完成授权。'
                       '⚠️ 用户回复"已授权/扫码完成"等确认语后，严禁再调用本工具，必须改调 paotui_confirm_auth。',
        'input_schema': {'type': 'object', 'properties': {}},
    },
    {
        'name': 'paotui_confirm_auth',
        'description': '用户在美团 App 完成扫码授权后（用户回复"已授权"等确认语时）必须调用本工具：'
                       '轮询授权状态并把 Token 写入本地缓存（最长等待约 10 分钟）。成功返回"授权成功"。',
        'input_schema': {'type': 'object', 'properties': {}},
    },
    {
        'name': 'paotui_address_list',
        'description': '拉取用户地址簿（含坐标/电话/标签/最近使用时间）。scene=send 帮送场景，scene=buy 帮买场景。'
                       '用户未给地址时按最近使用展示前 3 条供选择。',
        'input_schema': {
            'type': 'object',
            'properties': {'scene': {'type': 'string', 'enum': ['send', 'buy'], 'description': 'send=帮取送/帮忙，buy=帮买'}},
            'required': ['scene'],
        },
    },
    {
        'name': 'paotui_search_poi',
        'description': 'POI 地址搜索：把用户描述的新地址转为下单所需坐标。返回候选地址列表（含 lat/lng/cityId）。',
        'input_schema': {
            'type': 'object',
            'properties': {
                'keyword': {'type': 'string', 'description': '地址关键词，如"融新科技中心"'},
                'city': {'type': 'string', 'description': '城市名，默认北京'},
            },
            'required': ['keyword'],
        },
    },
    {
        'name': 'paotui_order',
        'description': '跑腿下单：confirm=false 仅预览（返回费用/距离/时效，展示费用卡片等用户确认）；'
                       'confirm=true 提交订单（参数必须与预览时完全一致）。'
                       '帮取送需 sender+recipient+goods；帮买需 recipient+goods(可空)+purchase_detail；'
                       '帮忙场景 sender 与 recipient 填同一地址，goods 留空、内容写 remark。',
        'input_schema': {
            'type': 'object',
            'properties': {
                'sender': {'type': 'object', 'description': '取件地址 JSON：address/lat/lng/phone/cityId，houseNumber 与 name 固定留空'},
                'recipient': {'type': 'object', 'description': '收件地址 JSON，结构同 sender'},
                'goods': {'type': 'object', 'description': '物品 JSON：goodsName/goodsWeight(公斤)/goodTypes/goodTypeNames（按映射表整行取）；帮忙场景留空对象'},
                'business_type': {'type': 'integer', 'enum': [1, 2], 'description': '1=帮取送/帮忙，2=帮买'},
                'biz_type_scene_tag': {'type': 'integer', 'description': '0 帮取送/帮买默认，1 餐厅取号，2 医院帮忙，3 其他取号，4 帮搬装，5 其他帮忙，6 帮扔杂物'},
                'business_type_tag': {'type': 'integer', 'description': '仅帮买生效：0 指定购买地址，1 就近购买（sender 传收件地址）'},
                'remark': {'type': 'string', 'description': '订单备注；帮忙场景填帮忙内容'},
                'purchase_detail': {'type': 'string', 'description': '帮买场景必传，如"一瓶矿泉水"'},
                'confirm': {'type': 'boolean', 'description': 'false=预览，true=提交（用户明确确认后）'},
            },
            'required': ['sender', 'recipient', 'confirm'],
        },
    },
    {
        'name': 'paotui_order_status',
        'description': '查询跑腿订单状态（下单成功后返回的 orderViewId）。',
        'input_schema': {
            'type': 'object',
            'properties': {'order_id': {'type': 'string', 'description': '订单 ID（orderViewId）'}},
            'required': ['order_id'],
        },
    },
]

TOOL_LABELS = {
    'paotui_login': '跑腿授权检查',
    'paotui_confirm_auth': '跑腿授权确认',
    'paotui_address_list': '地址簿',
    'paotui_search_poi': '地址搜索',
    'paotui_order': '跑腿下单',
    'paotui_order_status': '订单查询',
}


def build_paotui_system_prompt() -> str:
    return (
        '你是美团跑腿下单助手，已接入美团跑腿 Skill（帮取送/帮买/帮忙三大场景，真实下单）。\n'
        '【输出规范】严禁展示英文字段名、JSON、命令、脚本路径等技术细节；手机号一律脱敏（138****5678）；'
        '费用卡片逐行展示（服务类型/取件地址/收件地址/物品/距离/配送费/预计时效，每字段独占一行）。\n'
        '【安全门控】下单是真实消费：必须先 confirm=false 预览，并在费用确认表单中展示费用卡片，'
        '等用户点击表单"确认下单"按钮（或明确回复"确认"）后才 confirm=true 提交，'
        '提交参数与预览完全一致；费用 > 100 元需额外确认；取件/收件电话缺失时必须询问用户，不得编造。\n'
        '【工作流程】① 先 paotui_login 检查授权；返回授权链接时展示链接并引导用户用美团 App 扫码；'
        '用户回复"已授权/授权完成/扫码了/好了"等任何确认语后，必须调用 paotui_confirm_auth 轮询 Token 写入缓存，'
        '严禁再次调用 paotui_login（它不会取回 Token，还会重新生成授权码导致当前扫码失效）；confirm_auth 成功后继续业务流程；'
        '② 识别场景：帮取送（A取B送）/帮买（代购）/帮忙（取号、搬装、扔杂物等，只需一个地址，sender 与 recipient 填同一地址且不向用户说明）；'
        '③ 生成订单表单（A2UI）收集信息，用户提交后直接用表单数据解析地址、构造参数并 paotui_order 预览，不得逐项追问已知信息；'
        '④ 预览成功后生成费用确认表单（A2UI），用户点击"确认下单"按钮后才提交；下单成功后提示"15 分钟内打开美团 App → 我的订单完成支付"。\n'
        '【参数规范】地址 JSON：{address, houseNumber:"", lat, lng, name:"", phone, cityId}，lat/lng 为整数×1e6，'
        'houseNumber/name 固定留空（填写会触发风控）；地址簿返回的 lat/lng 可直接使用。\n'
        '物品映射表（goodTypes 与 goodTypeNames 整行取）：餐饮外卖[2]["餐饮"]、文件合同[4]["文件"]、生鲜水果[3]["生鲜"]、'
        '蛋糕[9]["蛋糕"]、鲜花[1]["鲜花"]、数码[5]["数码"]、服饰[7]["服饰"]、快递/其他[8]["快递"/"其他"]；'
        'goodsWeight 即公斤数，用户说了以实际为准。\n'
        'business_type：1=帮取送/帮忙，2=帮买；biz_type_scene_tag：0 默认、1 餐厅取号、2 医院帮忙、3 其他取号、4 帮搬装、5 其他帮忙、6 帮扔杂物；'
        '帮买必传 purchase_detail，business_type_tag：0 指定购买地址、1 就近购买（sender 传收件地址）。\n'
        '城市表（cityId/参考坐标）：北京 110100、上海 310100、广州 440100、深圳 440300、成都 510100、杭州 330100、'
        '武汉 420100、南京 320100、西安 610100、重庆 500100；不在表中的城市告知用户暂不支持。跨城市配送不支持。\n'
        '【A2UI 交互】授权完成后，信息收集与确认一律优先用 A2UI 表单：'
        '① 订单表单：场景 ChoicePicker（帮取送/帮买/帮忙，用户已提及则预选；帮忙场景取件/收件地址预填同一地址）、'
        '取件地址/收件地址/联系电话 TextField、物品名称 TextField、物品类别 ChoicePicker'
        '（餐饮外卖/文件合同/生鲜水果/蛋糕/鲜花/数码/服饰/快递其他）、备注 TextField（可选），'
        '提交按钮 action name 为 submit_paotui_order，updateDataModel 给出全部初始值；'
        '② 费用确认表单：预览成功后用 Text 逐行展示费用卡片（服务类型/取件地址/收件地址/物品/距离/配送费/预计时效），'
        '下方两个按钮：confirm_order（primary，文案"确认下单"）与 cancel_order（secondary，文案"取消"）；'
        '③ 收到 [A2UI_EVENT] 表单提交后直接用提交数据继续流程（解析地址→预览，或确认提交），不得再次生成相同表单；'
        '物品类别按物品映射表换算 goodTypes；用户偏好纯文字交流时按文字流程进行。\n'
        '工具返回错误时按错误信息说明原因并给出替代建议。'
    )


class PaotuiService:
    """美团跑腿 Skill 客户端（paotui.js CLI 执行器）"""

    MAX_TOOL_ROUNDS = 8  # 跑腿流程较长：授权→地址→预览→确认→提交

    @property
    def skill_dir(self) -> Path:
        return SKILL_DIR

    @property
    def cli_entry(self):
        """定位 CLI 入口：paotui.js / dist/paotui.js / dist/run.sh"""
        for rel in ('paotui.js', Path('dist') / 'paotui.js'):
            p = SKILL_DIR / rel
            if p.exists():
                return ['node', str(p)]
        run_sh = SKILL_DIR / 'dist' / 'run.sh'
        if run_sh.exists():
            return ['sh', str(run_sh)]
        return None

    @property
    def configured(self) -> bool:
        return self.cli_entry is not None

    @property
    def tools(self) -> list:
        return PAOTUI_TOOLS

    @staticmethod
    def system_prompt() -> str:
        return build_paotui_system_prompt()

    @staticmethod
    def tool_label(name: str) -> str:
        return TOOL_LABELS.get(name, name)

    @staticmethod
    def card_event_type(card: dict) -> str:
        return 'paotui_card'

    def _env(self) -> dict:
        env = os.environ.copy()
        env['MEITUAN_PAOTUI_SOURCE_FROM'] = os.getenv('MEITUAN_PAOTUI_SOURCE_FROM', 'aihub')
        token = self._passport_token()
        if token:
            env['MCP_ACCESS_TOKEN'] = token
        return env

    @staticmethod
    def _passport_token() -> str:
        """用户级 Token：优先 MEITUAN_PASSPORT_TOKEN 环境变量，
        否则从 pt-passport 本地缓存（扫码授权后写入）取出"""
        env_token = os.getenv('MEITUAN_PASSPORT_TOKEN', '').strip()
        if env_token:
            return env_token
        try:
            proc = subprocess.run(
                ['pt-passport', 'get-token', '--client_id', PASSPORT_CLIENT_ID, '--env', PASSPORT_ENV],
                capture_output=True, text=True, timeout=15,
            )
            if proc.returncode != 0:
                return ''
            lines = [line.strip() for line in proc.stdout.splitlines() if line.strip()]
            return lines[-1] if lines else ''
        except (OSError, subprocess.TimeoutExpired):
            return ''

    async def _run_passport(self, args: list, timeout: int):
        """执行 pt-passport 子命令（授权链路），返回结构与 _run_cli 一致"""
        try:
            proc = await asyncio.create_subprocess_exec(
                'pt-passport', *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            return {'error': f'授权等待超时（{timeout} 秒），请重新发起授权'}, None
        except FileNotFoundError:
            return {'error': '未安装 pt-passport 授权 CLI：请执行跑腿 Skill 包自带的 '
                             'references/meituan-passport-user-auth/scripts/install.sh'}, None
        output = stdout.decode('utf-8', errors='replace').strip()
        err = stderr.decode('utf-8', errors='replace').strip()
        if proc.returncode != 0:
            return {'error': f'授权命令执行失败：{err or output or f"退出码 {proc.returncode}"}', 'raw': output}, None
        return {'output': output}, None

    async def _run_cli(self, args: list, timeout: int = CLI_TIMEOUT_SECONDS):
        entry = self.cli_entry
        if entry is None:
            return {'error': '服务端未部署美团跑腿 Skill 包：请从美团开放平台 AI Hub 下载 meituan-paotui，'
                             f'解压到 {SKILL_DIR}（或用 MEITUAN_PAOTUI_DIR 指定路径）'}, None
        try:
            proc = await asyncio.create_subprocess_exec(
                *entry, *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=self._env(),
                cwd=str(SKILL_DIR),
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            return {'error': f'跑腿命令执行超时（{timeout} 秒），请稍后重试'}, None
        except FileNotFoundError:
            return {'error': '未找到 node/sh 可执行文件，请检查服务端运行环境'}, None
        output = stdout.decode('utf-8', errors='replace').strip()
        err = stderr.decode('utf-8', errors='replace').strip()
        if proc.returncode != 0:
            detail = err or output or f'退出码 {proc.returncode}'
            return {'error': f'跑腿命令执行失败：{detail}', 'raw': f'{output}\n{err}'}, None
        return {'output': output}, None

    # ---------- 各工具执行 ----------

    @staticmethod
    def _extract_auth_link(text: str) -> str:
        """从 CLI 输出中提取 AUTH_LINK 行（pt-passport get-code / paotui.js login 通用格式）"""
        for line in text.splitlines():
            if line.strip().startswith('AUTH_LINK'):
                return line.split('AUTH_LINK', 1)[-1].strip().lstrip(':：').strip()
        return ''

    async def _exec_login(self, args: dict):
        result, _ = await self._run_cli(['login'])
        output = result.get('output') or result.get('raw', '')
        if not result.get('error'):
            link = self._extract_auth_link(output)
            if link:
                return result, {'kind': 'auth', 'title': '美团跑腿授权', 'auth_link': link, 'content': output}
            return result, None
        # paotui.js login 返回 AUTH_FAILED：回退 pt-passport 扫码授权链路
        if 'AUTH_FAILED' in output or 'AUTH_FAILED' in result.get('error', ''):
            # 自愈：可能已存在"用户扫过码但尚未轮询"的待处理 session（典型场景：模型误调 login），
            # 先短轮询把 Token 取回写缓存（已扫码则秒级返回），避免"明明已授权却一直显示待授权"；
            # poll-token 消费 session 文件，轮询未命中后再走 get-code 生成新链接
            if any(Path('/tmp').glob('pt_passport_session_*.json')):
                poll, _ = await self._run_passport(
                    ['auth', 'poll-token', '--client_id', PASSPORT_CLIENT_ID, '--timeout', '10'],
                    timeout=20,
                )
                if not poll.get('error'):
                    check, _ = await self._run_cli(['login'])
                    check_out = check.get('output') or ''
                    if (not check.get('error') and 'AUTH_FAILED' not in check_out
                            and not self._extract_auth_link(check_out)):
                        return {'output': '已登录，授权有效'}, None
            code, _ = await self._run_passport(
                ['auth', 'get-code', '--client_id', PASSPORT_CLIENT_ID, '--env', PASSPORT_ENV], timeout=60)
            if code.get('error'):
                return code, None
            link = self._extract_auth_link(code['output'])
            if not link:
                return {'error': f'未获取到授权链接：{code["output"][:200]}'}, None
            return code, {'kind': 'auth', 'title': '美团跑腿授权', 'auth_link': link, 'content': code['output']}
        return result, None

    async def _exec_confirm_auth(self, args: dict):
        poll, _ = await self._run_passport(
            ['auth', 'poll-token', '--client_id', PASSPORT_CLIENT_ID,
             '--timeout', str(max(CONFIRM_AUTH_TIMEOUT_SECONDS - 20, 60))],
            timeout=CONFIRM_AUTH_TIMEOUT_SECONDS,
        )
        if poll.get('error'):
            return poll, None
        # Token 已写入本地缓存：回跑 login 校验授权链路是否打通
        check, card = await self._exec_login(args)
        if check.get('error'):
            return {'error': f'授权已返回 Token 但登录校验失败：{check["error"][:200]}'}, None
        return {'output': '授权成功，可以继续跑腿下单'}, None

    async def _exec_address_list(self, args: dict):
        scene = args.get('scene', 'send')
        business_type = '2' if scene == 'buy' else '1'
        return await self._run_cli(['get_address_list', '--address-type', '1', '--business-type', business_type, '--scene', '2'])

    async def _exec_search_poi(self, args: dict):
        cmd = ['search_poi', '--keyword', args.get('keyword', ''), '--city', args.get('city') or '北京']
        return await self._run_cli(cmd)

    async def _exec_order(self, args: dict):
        cmd = ['preview_and_submit',
               '--sender', json.dumps(args.get('sender') or {}, ensure_ascii=False),
               '--recipient', json.dumps(args.get('recipient') or {}, ensure_ascii=False)]
        goods = args.get('goods')
        if goods:
            cmd += ['--goods', json.dumps(goods, ensure_ascii=False)]
        cmd += ['--business-type', str(args.get('business_type', 1))]
        if args.get('biz_type_scene_tag') is not None:
            cmd += ['--biz-type-scene-tag', str(args['biz_type_scene_tag'])]
        if args.get('business_type_tag') is not None:
            cmd += ['--business-type-tag', str(args['business_type_tag'])]
        if args.get('remark'):
            cmd += ['--remark', args['remark']]
        if args.get('purchase_detail'):
            cmd += ['--purchase-detail', args['purchase_detail']]
        if args.get('confirm'):
            cmd += ['--confirm']
        return await self._run_cli(cmd)

    async def _exec_order_status(self, args: dict):
        return await self._run_cli(['get_order_status', '--order-id', args.get('order_id', '')])

    # ---------- 对外入口 ----------

    _HANDLERS = {
        'paotui_login': '_exec_login',
        'paotui_confirm_auth': '_exec_confirm_auth',
        'paotui_address_list': '_exec_address_list',
        'paotui_search_poi': '_exec_search_poi',
        'paotui_order': '_exec_order',
        'paotui_order_status': '_exec_order_status',
    }

    async def execute_tool(self, name: str, args: dict):
        """执行一次跑腿工具调用，返回 (给模型的数据, 给前端的卡片)"""
        handler = self._HANDLERS.get(name)
        if not handler:
            return {'error': f'未知工具: {name}'}, None
        try:
            return await getattr(self, handler)(args)
        except Exception as err:  # CLI 执行兜底，避免中断 agent 循环
            return {'error': f'跑腿服务异常：{err}'}, None

    @staticmethod
    def summarize_result(name: str, result: dict) -> str:
        """工具结果的一句话摘要，供前端工具 chip 展示"""
        if result.get('error'):
            return '失败'
        output = result.get('output', '')
        if name == 'paotui_login':
            return '待授权' if 'AUTH_LINK' in output else '已登录'
        if name == 'paotui_order':
            return '已提交' if '订单' in output and '提交' in output else '预览'
        return f'{len(output)} 字结果' if output else '成功'
