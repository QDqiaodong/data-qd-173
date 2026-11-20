"""充电启动证据核验的命令行入口。

用法：

    python -m charging.evidence.cli \\
        --requests fixtures/evidence/two-channel-requests.json \\
        --results fixtures/evidence/two-channel-results.json \\
        --window-start 2026-11-20T10:00:00Z \\
        --window-end 2026-11-20T10:02:00Z \\
        --target-bp 5000 \\
        --output run/evidence.json

失败时退出码为 2，且**不会创建或替换**任何输出文件（错误码
``invalid_input``，涵盖结构、字段、站点口径、时刻、重复身份、窗口与
目标基点等全部非法输入）。成功时退出码为 0，并向 stdout 打印一行摘要
JSON。本命令只读两份日志，不读写计量结算、收款准入或排程的任何状态。
"""

import argparse
import json
import os
import tempfile
from pathlib import Path

from .engine import EvidenceError, analyze


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
        requests_doc = json.loads(Path(args.requests).read_text())
        results_doc = json.loads(Path(args.results).read_text())

        # 全部校验与求解在内存中完成；只有成功才原子落盘，失败保留原文件。
        result = analyze(requests_doc, results_doc,
                         args.window_start, args.window_end, args.target_bp)

        if args.output is not None:
            _atomic_write_json(args.output, result)

        groups = result["operations"]["groups"]
        print(json.dumps({
            "station_id": result["station_id"],
            "strong_pairs": result["matching_summary"]["strong_pairs"],
            "total_pairs": result["matching_summary"]["total_pairs"],
            "confirmed_pairs": result["matching_summary"]["confirmed_pairs"],
            "status": {g: groups[g]["status"] for g in
                       ("station", "mobile", "terminal")},
        }, ensure_ascii=False, sort_keys=True))
    except EvidenceError as exc:
        p.exit(2, f"charge-evidence: {exc.code}: {exc.message}\n")
    except (OSError, ValueError, KeyError, TypeError) as exc:
        p.exit(2, f"charge-evidence: invalid_input: {exc}\n")


def main():
    p = argparse.ArgumentParser(
        prog="charge-evidence",
        description="充电启动证据核验：请求日志与桩端日志的最优关联，"
                    "交付客服证据清单与运营报告（本地、只读日志）。")
    p.add_argument("--requests", required=True, help="请求日志 JSON 路径")
    p.add_argument("--results", required=True, help="桩端结果日志 JSON 路径")
    p.add_argument("--window-start", required=True,
                   help="半开请求时间窗起点（带显式偏移的 ISO 时间）")
    p.add_argument("--window-end", required=True,
                   help="半开请求时间窗终点；observed_until 必须不早于其 +120 秒")
    p.add_argument("--target-bp", required=True, type=int,
                   help="目标基点值，0 到 10000 的整数")
    p.add_argument("--output", help="结果 JSON 输出路径（省略则只打印摘要）")
    args = p.parse_args()
    run(p, args)


if __name__ == "__main__":
    main()
