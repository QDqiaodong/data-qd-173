"""车队补能 / 降载邀约联合排程的命令行入口。

用法：

    python -m charging.planning.cli \\
        --fleet fixtures/planning/fleet-headline.json \\
        --offers fixtures/planning/offers-headline.json \\
        --output run/plan.json

整轮失败时退出码为 2，且**不会创建或替换**任何输出文件：

* ``invalid_input``      —— 非法输入（结构、时间边界、整数范围、窗口一致、
                            重复身份、扣减超过正常额度等）。
* ``baseline_infeasible`` —— 输入合法，但常态计划本身无法满足全部车辆。
* ``search_limit_exceeded`` —— 联合回溯超出节点预算（可加 --search-limit）。

成功时退出码为 0，并向 stdout 打印一行决策摘要 JSON。
"""

import argparse
import json
import os
import tempfile
from pathlib import Path

from .engine import DEFAULT_SEARCH_LIMIT, PlanningError, plan


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
        fleet_input = json.loads(Path(args.fleet).read_text())
        offers_input = json.loads(Path(args.offers).read_text())

        # 全部校验与求解在内存中完成；只有成功才原子落盘，失败保留原文件。
        result = plan(fleet_input, offers_input, search_limit=args.search_limit)

        if args.output is not None:
            _atomic_write_json(args.output, result)

        print(json.dumps({
            "station_id": result["station_id"],
            "slot_count": result["slot_count"],
            "accepted_offer_ids": result["decision"]["accepted_offer_ids"],
            "accepted_count": result["decision"]["accepted_count"],
            "total_reward": result["decision"]["total_reward"],
        }, ensure_ascii=False, sort_keys=True))
    except PlanningError as exc:
        p.exit(2, f"charge-plan: {exc.code}: {exc.message}\n")
    except (OSError, ValueError, KeyError, TypeError) as exc:
        p.exit(2, f"charge-plan: invalid_input: {exc}\n")


def main():
    p = argparse.ArgumentParser(
        prog="charge-plan",
        description="车队补能与电网降载邀约的联合排程（本地、纯计划、无控制指令）。")
    p.add_argument("--fleet", required=True, help="车队需求 JSON 路径")
    p.add_argument("--offers", required=True, help="电网降载邀约 JSON 路径")
    p.add_argument("--output", help="排程结果 JSON 输出路径（省略则只打印摘要）")
    p.add_argument("--search-limit", type=int, default=DEFAULT_SEARCH_LIMIT,
                   help=f"联合可行性回溯节点预算（默认 {DEFAULT_SEARCH_LIMIT}）")
    args = p.parse_args()
    run(p, args)


if __name__ == "__main__":
    main()
