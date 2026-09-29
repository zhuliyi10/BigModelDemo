"""通用智能助手：聚合全部场景服务的组合 agent 服务。

单入口智能体自动识别用户意图（天气 / 话费充值 / 外卖点餐 / 美团跑腿 / 美团酒旅 / 出行助手），
路由到对应场景的工具执行，用户无需在多个场景间手动切换。子服务按可用性动态聚合：
- 恒可用（免凭据开箱即用）：weather（Open-Meteo）、recharge（演示模拟）、waimai（演示模拟）
- 按配置加入：amap（AMAP_KEY）、travel（mttravel CLI + MEITUAN_TOKEN）、paotui（Skill 包部署）

AssistantService 实现 agent_stream 约定的接口：tools / MAX_TOOL_ROUNDS / configured /
system_prompt / tool_label / card_event_type / execute_tool / summarize_result。
工具调用与卡片事件按工具名路由到子服务，各场景工具名前缀唯一（maps_* / weather_* /
recharge_* / waimai_* / mt_travel_* / paotui_*），前端卡片渲染逻辑无需任何改动。
"""

# 各场景在通用助手中的能力简介（提示词能力清单用；key 为场景名，与 main.py 传入顺序对应）
SCENARIOS = {
    'weather': '天气查询：weather_geocode / weather_forecast 工具（城市实况与 7 天逐日预报，免凭据）',
    'recharge': '话费充值（演示模拟数据）：recharge_query / recharge_plans / recharge_order / recharge_order_status 工具，两步确认充值',
    'waimai': '外卖点餐（演示模拟数据）：waimai_search_shops / waimai_menu / waimai_order / waimai_order_status 工具，两步确认下单',
    'amap': '出行助手（高德）：maps_geo / maps_text_search / maps_around_search / maps_direction_* 工具（地理编码 / POI / 公交驾车路线）',
    'travel': '美团酒旅：mt_travel_query 工具（酒店 / 机票 / 火车票 / 门票 / 行程规划，真实供给数据）',
    'paotui': '美团跑腿（真实下单）：paotui_login / paotui_confirm_auth / paotui_address_list / paotui_search_poi / paotui_order / paotui_order_status 工具，两步确认下单',
}

SCENARIO_TITLES = {
    'weather': '天气查询场景规范',
    'recharge': '话费充值场景规范',
    'waimai': '外卖点餐场景规范',
    'amap': '出行助手场景规范',
    'travel': '美团酒旅场景规范',
    'paotui': '美团跑腿场景规范',
}


def _fallback_card_event(card: dict) -> str:
    """未知卡片的兜底事件类型：按卡片特征路由（正常路径由 execute_tool 打标记，极少走到）"""
    kind = card.get('kind')
    if kind == 'poi_list':
        return 'amap_poi_list'
    if kind == 'auth':
        return 'paotui_card'
    if kind == 'forecast':
        return 'weather_card'
    if kind in ('shops', 'menu') or card.get('items_summary'):
        return 'waimai_card'
    if kind == 'balance' or card.get('face_value'):
        return 'recharge_card'
    # kind=result 时充值 / 外卖订单卡片字段高度相似，按外卖特征优先、充值兜底
    if card.get('shop_name'):
        return 'waimai_card'
    return 'recharge_card'


class AssistantService:
    """组合多个场景服务：工具合并、按工具名路由执行、卡片事件类型随子服务下发"""

    MAX_TOOL_ROUNDS = 12  # 通用场景单轮对话可能串多个场景（如查天气→充值），轮次上限高于单场景

    def __init__(self, services: list[tuple[str, object]]):
        """services 为 (场景名, 服务实例) 列表；configured 且 CLI 就绪的子服务才参与聚合"""
        self._entries = [
            (name, svc) for name, svc in services
            if svc.configured and getattr(svc, 'cli_available', True)
        ]
        # 工具名 → 子服务映射（各场景工具名前缀唯一，无冲突）
        self._tool_map = {
            tool['name']: svc
            for _, svc in self._entries
            for tool in svc.tools
        }

    @property
    def configured(self) -> bool:
        return bool(self._entries)

    @property
    def tools(self) -> list:
        return [tool for _, svc in self._entries for tool in svc.tools]

    @property
    def system_prompt(self) -> str:
        """通用助手系统提示词：意图路由总则 + 各可用子服务的场景规范"""
        abilities = '\n'.join(f'- {SCENARIOS[name]}' for name, _ in self._entries)
        sections = '\n\n'.join(
            f'【{SCENARIO_TITLES[name]}】\n{svc.system_prompt()}' for name, svc in self._entries
        )
        return f"""你是一个通用生活服务智能助手，能自动识别用户意图并调用对应场景的工具完成任务，用户无需选择场景。

当前具备以下生活服务能力（按需选用，禁止调用能力清单之外的工具）：
{abilities}

工作准则：
1. 先根据用户消息判断意图属于哪个场景，只调用该场景的工具；一个场景的任务未完成前不切换场景。
2. 一条消息包含多个需求（如"查下杭州天气，再帮我充 50 话费"）时按顺序分步完成：先完成第一个场景并给出结果，再开始下一个场景。
3. 用户继续追问已完成场景的后续进展（如查订单状态）时，继续使用对应场景的工具。
4. 意图不明确时先用一句话澄清再调用工具（如"点外卖"没说吃什么就先推荐商家）；用户已给全参数时直接执行，不要重复追问。
5. 各场景的安全门控（两步确认、表单收集信息、参数校验、禁止编造）是最强约束，以下方各场景规范为准。

下面是本次可用的各场景详细规范，调用对应场景工具时必须严格遵守：
{sections}"""

    def tool_label(self, name: str) -> str:
        svc = self._tool_map.get(name)
        return svc.tool_label(name) if svc else name

    def card_event_type(self, card: dict) -> str:
        """卡片下发的事件类型：优先读 execute_tool 打上的子服务标记，未知卡片按特征兜底"""
        marked = card.pop('_event', None)
        return marked or _fallback_card_event(card)

    async def execute_tool(self, name: str, args: dict):
        """执行一次工具调用：按工具名路由到子服务；卡片打上事件类型标记供 card_event_type 读取"""
        svc = self._tool_map.get(name)
        if svc is None:
            return {'error': f'未知工具：{name}'}, None
        result, card = await svc.execute_tool(name, args)
        if card:
            card['_event'] = svc.card_event_type(card)
        return result, card

    def summarize_result(self, name: str, result: dict) -> str:
        """工具结果摘要：路由到子服务生成，保留各场景 chip 摘要质量"""
        svc = self._tool_map.get(name)
        if svc is None:
            return result.get('error') or '完成'
        return svc.summarize_result(name, result)
