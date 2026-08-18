"""美团酒旅 Skill（meituan-travel）封装：mttravel CLI + 酒旅助手工具（function calling）执行器。

官方 Skill（美团开放平台 AI Hub）将酒旅供给封装为 CLI：
- 安装：npm i -g @meituan-travel/travel-cli
- 调用：mttravel [城市] "<查询>"（CLI 自动读取 ~/.config/meituan-travel/config.json 的 key）
- Token 获取：美团开发者中心 → 个人开发者控制台 → Token 管理
  https://developer.meituan.com/zh/v2/dev/doc
本模块在服务端执行 CLI（Token 放 server/.env 的 MEITUAN_TOKEN，不下发到浏览器），
覆盖景点推荐 / 酒店推荐 / 火车票机票 / 行程规划等场景；单次查询耗时约 1-2 分钟。

与 AmapService 同款三层结构：
- 模型只决定"何时调用、传什么参数"（TRAVEL_TOOLS 定义）；
- TravelService 真正执行 CLI 并回传完整结果（含图片链接的 Markdown 富文本）；
- 每次工具执行同时产出两份结果：给模型的结构化数据、给前端的酒旅结果卡片。
"""

import asyncio
import json
import os
import shutil
from pathlib import Path

CLI_COMMAND = 'mttravel'
CLI_TIMEOUT_SECONDS = int(os.getenv('MTTRAVEL_TIMEOUT', '150'))  # 官方说明约 1-2 分钟
CLI_CONFIG = Path.home() / '.config' / 'meituan-travel' / 'config.json'

# Anthropic tools 定义：单一酒旅查询入口，自然语言驱动
TRAVEL_TOOLS = [
    {
        'name': 'mt_travel_query',
        'description': (
            '美团酒旅查询：基于美团真实供给，查询并推荐酒店、机票、火车票、汽车票、景点门票、'
            '跟团度假商品，或生成定制化旅行攻略/行程规划。query 应包含用户的完整需求'
            '（目的地、时间、人数、预算、旅行风格等），信息越具体推荐越精准。'
            '单次查询耗时约 1-2 分钟。'
        ),
        'input_schema': {
            'type': 'object',
            'properties': {
                'city': {
                    'type': 'string',
                    'description': '用户当前所在城市（出发地参考，获取不到时默认"北京"；用户明确指定出发地时以用户为准）',
                },
                'query': {
                    'type': 'string',
                    'description': '自然语言查询，转述用户的完整需求，如"明天北京到武汉的火车票"、"上海外滩五星酒店预算800以内"',
                },
            },
            'required': ['city', 'query'],
        },
    },
]

TOOL_LABELS = {
    'mt_travel_query': '美团酒旅查询',
}


def build_travel_system_prompt() -> str:
    return (
        '你是美团酒旅助手，已接入美团酒旅 Skill（景点/酒店/机票/火车票/门票/度假/行程规划，真实供给数据）。\n'
        '工作流程：\n'
        '1. 从用户消息提取「所在城市」（获取不到默认北京，用户明确指定出发地时以用户为准）与查询需求；'
        '城市无法判断时主动询问，不要猜测；\n'
        '2. 调用 mt_travel_query，query 参数转述用户完整需求（目的地、时间、人数、预算、旅行风格等）；'
        '工具耗时约 1-2 分钟，不要重复调用同一查询；\n'
        '3. 工具返回的内容是已排版的 Markdown 富文本（含图片链接），系统会自动将其完整渲染为结果卡片，'
        '正文严禁再复述结果内容，只需在卡片后附一两句简短建议或提醒；'
        '引用其中数据时价格原样输出，占位符（X/XX/XXX）不得还原，'
        '评分以「X.X分（美团真实评分）」、星级以「美团X星级」呈现；\n'
        '4. 工具返回错误时按错误信息说明原因'
        '（超时建议换问法或稍后再试、鉴权失败提示重新配置 Token）。\n'
        '不适用于出国签证、护照办理等非旅行类问题，遇到时直接说明。'
    )


class TravelService:
    """美团酒旅 Skill 客户端（mttravel CLI 执行器）"""

    MAX_TOOL_ROUNDS = 3  # 酒旅查询耗时长，限制轮数防止重复调用

    @property
    def token(self) -> str:
        """Token 优先读 MEITUAN_TOKEN 环境变量；否则回退 CLI 自身的 config.json"""
        env_token = os.getenv('MEITUAN_TOKEN', '').strip()
        if env_token:
            return env_token
        try:
            return json.loads(CLI_CONFIG.read_text(encoding='utf-8')).get('key', '').strip()
        except (OSError, json.JSONDecodeError):
            return ''

    def ensure_cli_token(self) -> None:
        """CLI 只认 ~/.config/meituan-travel/config.json：
        若 .env 配了 MEITUAN_TOKEN 而 CLI 配置缺失/不一致，自动同步写入，用户只需维护 .env 一处"""
        env_token = os.getenv('MEITUAN_TOKEN', '').strip()
        if not env_token:
            return
        try:
            existing = json.loads(CLI_CONFIG.read_text(encoding='utf-8')).get('key', '').strip()
        except (OSError, json.JSONDecodeError):
            existing = ''
        if existing == env_token:
            return
        CLI_CONFIG.parent.mkdir(parents=True, exist_ok=True)
        CLI_CONFIG.write_text(json.dumps({'key': env_token}, ensure_ascii=False, indent=2), encoding='utf-8')

    @property
    def configured(self) -> bool:
        return bool(self.token)

    @property
    def cli_available(self) -> bool:
        return shutil.which(CLI_COMMAND) is not None

    @property
    def tools(self) -> list:
        return TRAVEL_TOOLS

    @staticmethod
    def system_prompt() -> str:
        return build_travel_system_prompt()

    @staticmethod
    def tool_label(name: str) -> str:
        return TOOL_LABELS.get(name, name)

    @staticmethod
    def card_event_type(card: dict) -> str:
        return 'mt_travel_card'

    # ---------- 工具执行：返回 (给模型的数据, 给前端的卡片) ----------

    async def _exec_query(self, args: dict):
        if not self.cli_available:
            return {'error': '服务端未安装美团酒旅 CLI，请执行：npm i -g @meituan-travel/travel-cli'}, None
        if not self.token:
            return {'error': '未配置美团酒旅 Token：请在美团开发者中心个人开发者控制台创建 Token，'
                             '并填入 server/.env 的 MEITUAN_TOKEN（或 mttravel 首次运行时按引导配置）'}, None
        self.ensure_cli_token()
        city = (args.get('city') or '北京').strip()
        query = (args.get('query') or '').strip()
        if not query:
            return {'error': '查询内容不能为空'}, None
        try:
            proc = await asyncio.create_subprocess_exec(
                CLI_COMMAND, city, query,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=CLI_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            return {'error': f'查询超时（超过 {CLI_TIMEOUT_SECONDS} 秒），当前查询人数可能较多，请换个问法或稍后再试'}, None
        except FileNotFoundError:
            return {'error': '服务端未安装美团酒旅 CLI，请执行：npm i -g @meituan-travel/travel-cli'}, None

        output = stdout.decode('utf-8', errors='replace').strip()
        err = stderr.decode('utf-8', errors='replace').strip()
        if proc.returncode != 0 or not output:
            detail = err or output or f'退出码 {proc.returncode}'
            return {'error': f'美团酒旅查询失败：{detail}'}, None
        return {'city': city, 'query': query, 'content': output}, {
            'kind': 'travel_result',
            'title': '美团酒旅',
            'city': city,
            'content': output,
        }

    # ---------- 对外入口 ----------

    _HANDLERS = {
        'mt_travel_query': '_exec_query',
    }

    async def execute_tool(self, name: str, args: dict):
        """执行一次酒旅工具调用，返回 (给模型的数据, 给前端的结果卡片)"""
        handler = self._HANDLERS.get(name)
        if not handler:
            return {'error': f'未知工具: {name}'}, None
        try:
            return await getattr(self, handler)(args)
        except Exception as err:  # CLI 执行兜底，避免中断 agent 循环
            return {'error': f'美团酒旅查询异常：{err}'}, None

    @staticmethod
    def summarize_result(name: str, result: dict) -> str:
        """工具结果的一句话摘要，供前端工具 chip 展示"""
        if result.get('error'):
            return '失败'
        if name == 'mt_travel_query':
            return f'{len(result.get("content", ""))} 字结果'
        return ''
