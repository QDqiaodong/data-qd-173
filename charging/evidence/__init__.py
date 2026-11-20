"""充电启动证据核验（charge-start evidence verification）。

见 :mod:`charging.evidence.engine` 与 ``docs/evidence.md``。本入口只读
请求日志与桩端日志，交付客服证据清单与运营报告；不读写计量结算、收款
准入或未来排程的任何状态。
"""

from .engine import EVIDENCE_VERSION, EvidenceError, analyze

__all__ = ["EVIDENCE_VERSION", "EvidenceError", "analyze"]
