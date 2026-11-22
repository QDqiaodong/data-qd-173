"""园区上游需量电费账单复核的命令行入口。

用法（在项目根目录执行）：

    python -m charging.demand.cli \\
        --state run/state.json \\
        --audit fixtures/demand/audit-headline.json \\
        --output run/demand-report.json

一次只核对一张账单和一个园区；本命令只读结算 state 与审计输入文件，绝不
写回任何状态。输入非法时退出码为 2（错误码 ``invalid_input``），且**不会
创建或替换**任何输出文件；各类正常审计结论（包括计量冲突
``metering_conflict``）都以报告表示，退出码 0，并向 stdout 打印一行摘要
JSON。
"""

import argparse
import json
import os
import tempfile
from pathlib import Path

from .engine import DemandError, audit_bill


def _atomic_write_json(path, payload):
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=target.name + ".", suffix=".tmp",
                                    dir=target.parent)
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(payload, f, indent=2, ensure_ascii=False)
            f.write("\n")
        os.replace(tmp_name, target)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def run(p, args):
    try:
        state = json.loads(Path(args.state).read_text())
        audit = json.loads(Path(args.audit).read_text())

        # 全部校验与求解在内存中完成；只有成功才原子落盘，失败保留原文件。
        report = audit_bill(state, audit)

        if args.output is not None:
            _atomic_write_json(args.output, report)

        summary = {
            "period_id": report["period_id"],
            "status": report["status"],
        }
        if report["status"] == "feasible":
            summary["verdict"] = report["verdict"]
            summary["peak_wh"] = report["analysis"]["peak_wh"]
            summary["charge_cents"] = report["analysis"]["charge_cents"]
        print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    except DemandError as exc:
        p.exit(2, f"charge-demand: {exc.code}: {exc.message}\n")
    except (OSError, ValueError, KeyError, TypeError) as exc:
        p.exit(2, f"charge-demand: invalid_input: {exc}\n")


def main():
    p = argparse.ArgumentParser(
        prog="charge-demand",
        description="园区上游需量电费账单复核：先核对计量证据（逐站逐刻钟可行"
                    "解释），再复核账单峰值与计价，交付可独立核查的 JSON 报告"
                    "（本地、只读、不写回状态）。")
    p.add_argument("--state", required=True,
                   help="charging.engine.apply_batch 产生的 state JSON 路径（只读）")
    p.add_argument("--audit", required=True,
                   help="审计输入 JSON：账期、站点上限、上游读数、账单")
    p.add_argument("--output",
                   help="报告 JSON 输出路径（省略则只打印摘要）；仅在全部校验"
                        "与求解成功后原子写入")
    args = p.parse_args()
    run(p, args)


if __name__ == "__main__":
    main()
