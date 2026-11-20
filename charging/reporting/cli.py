"""闭账周期充电报表保密发布的命令行入口。

用法（在项目根目录执行）：

    python -m charging.reporting.cli \\
        --state run/state.json \\
        --request fixtures/reporting/request-headline.json \\
        --public-output run/report-public.json \\
        --review-output run/report-review.json

一次只处理一个已关闭 ``period_id``；本命令只读结算 state 与请求文件，
绝不写回状态。整轮失败退出码为 2，且**不会创建或替换**任何输出文件：

* ``invalid_input``        —— 非法周期、缺失归属、未知坐标、越上限等；
* ``conflicting_history``  —— 历史公开值互相冲突或与本期汇总不一致；
* ``prior_risk``           —— 仅凭公开上限与历史事实保密余量已不成立。

成功时退出码 0，向 stdout 打印一行决策摘要 JSON，并原子落盘两份输出。
"""

import argparse
import json
import os
import tempfile
from pathlib import Path

from .engine import ReportingError, compile_report


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
        request = json.loads(Path(args.request).read_text())

        # 全部校验与求解在内存中完成；只有成功才原子落盘，失败保留原文件。
        public_report, internal_review = compile_report(state, request)

        _atomic_write_json(args.public_output, public_report)
        _atomic_write_json(args.review_output, internal_review)

        print(json.dumps({
            "period_id": public_report["period_id"],
            "selected_package_ids": internal_review["selected_package_ids"],
            "selected_count": len(internal_review["selected_package_ids"]),
            "total_value": internal_review["total_value"],
        }, ensure_ascii=False, sort_keys=True))
    except ReportingError as exc:
        p.exit(2, f"charge-report: {exc.code}: {exc.message}\n")
    except (OSError, ValueError, KeyError, TypeError) as exc:
        p.exit(2, f"charge-report: invalid_input: {exc}\n")


def main():
    p = argparse.ArgumentParser(
        prog="charge-report",
        description="闭账周期充电报表的保密发布编制：从闭账 state 与公开口径/"
                    "历史事实/候选包编制可一起发布的报表及内部审核说明（只读）。")
    p.add_argument("--state", required=True,
                   help="charging.engine.apply_batch 产生的 state JSON 路径（只读）")
    p.add_argument("--request", required=True,
                   help="发布请求 JSON：车队归属、公开口径、历史事实、候选包、保护格")
    p.add_argument("--public-output", required=True,
                   help="对外报表 JSON 输出路径（仅含周期、选中包及其发布数值）")
    p.add_argument("--review-output", required=True,
                   help="内部审核 JSON 输出路径（选中标识、总价值、未选包加入后果）")
    args = p.parse_args()
    run(p, args)


if __name__ == "__main__":
    main()
