"""闭账周期充电报表的保密发布编制（protected charging report release）。

见 :mod:`charging.reporting.engine` 与 ``docs/reporting.md``。本入口只读
:func:`charging.engine.apply_batch` 的闭账 state，不写回任何状态。
"""

from .engine import REPORT_VERSION, ReportingError, compile_report

__all__ = ["REPORT_VERSION", "ReportingError", "compile_report"]
