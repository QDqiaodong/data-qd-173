"""车队补能与电网降载邀约的联合排程（fleet charging / grid load-shed planning）。

见 :mod:`charging.planning.engine` 与 ``docs/planning.md``。本入口只处理
未来计划，不发控制指令，也不生成计费用的实际电量。
"""

from .engine import DEFAULT_SEARCH_LIMIT, PLAN_VERSION, PlanningError, plan

__all__ = ["DEFAULT_SEARCH_LIMIT", "PLAN_VERSION", "PlanningError", "plan"]
