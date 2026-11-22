"""园区上游需量电费账单复核（upstream demand bill audit）。

见 :mod:`charging.demand.engine` 与 ``docs/demand.md``。本入口只读
:func:`charging.engine.apply_batch` 的 state 与本次审计输入，核对计量证据
后复核账单，交付可独立核查的 JSON 报告；不写回任何状态。
"""

from .engine import AUDIT_VERSION, DemandError, audit_bill

__all__ = ["AUDIT_VERSION", "DemandError", "audit_bill"]
