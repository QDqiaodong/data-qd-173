"""收款核销 / 充电准入工作台的命令行入口。

用法（首轮可省略 ``--state``）：

    python -m charging.workbench.cli \
        --state run/wb-state.json \
        --round fixtures/workbench/round-01.json \
        --output-state run/wb-state.json \
        --output run/wb-result-01.json

整轮失败时退出码为 2，且不会创建或替换任何输出文件，父状态与输入的
结算 source_state 保持原样。``--source-state`` 可选，用于在 round 中以
``"source_state_ref"`` 引用磁盘上的计量结算 state（文件只读）。
"""

import argparse
import json
import os
import tempfile
from pathlib import Path

from .engine import WorkbenchError, apply_round


def _atomic_write_json(path, payload):
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=target.name + ".", suffix=".tmp", dir=target.parent)
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


def _resolve_source_refs(round_input, source_state_paths, round_path):
    """把 round 里的 source_state_ref 替换成只读加载的 source_state 内容。

    相对路径优先相对于 round 文件所在目录解析，其次相对于当前工作目录。
    """
    snapshot = round_input.get("import")
    if not snapshot:
        return round_input
    if "source_state" in snapshot:
        return round_input
    ref = snapshot.get("source_state_ref")
    if ref is None and source_state_paths:
        ref = source_state_paths[0]
    if ref is None:
        raise WorkbenchError("invalid_import",
                             "import 需要 source_state 内联对象或 source_state_ref 路径")
    path = Path(ref)
    if not path.is_absolute():
        round_dir = Path(round_path).resolve().parent
        candidates = [round_dir / ref, Path.cwd() / ref]
        path = next((p for p in candidates if p.exists()), candidates[0])
    source = json.loads(path.read_text())
    if not isinstance(source, dict) or "closed_lines" not in source:
        raise WorkbenchError("invalid_import",
                             f"source_state 文件 {ref!r} 不是 charging.engine.apply_batch 产出的 state")
    snapshot = dict(snapshot)
    snapshot.pop("source_state_ref", None)
    snapshot["source_state"] = source
    round_input = dict(round_input)
    round_input["import"] = snapshot
    return round_input


def run_round(p, args):
    try:
        round_input = json.loads(Path(args.round).read_text())
        if args.source_state:
            round_input = _resolve_source_refs(round_input, args.source_state, args.round)
        else:
            round_input = _resolve_source_refs(round_input, [], args.round)

        parent_state = None
        if args.state is not None and Path(args.state).exists():
            parent_state = json.loads(Path(args.state).read_text())

        # 全部校验在内存中完成；只有整轮被接受才落盘
        next_state, result = apply_round(parent_state, round_input)

        if args.output_state is not None:
            _atomic_write_json(args.output_state, next_state)
        if args.output is not None:
            _atomic_write_json(args.output, result)

        grants = [r for r in result["receipts_this_round"] if r["op"] == "grant"]
        begins = [r for r in result["receipts_this_round"] if r["op"] == "begin"]
        print(json.dumps({
            "round_id": result["round_id"],
            "evaluation_at": result["evaluation_at"],
            "lines": len(result["lines"]),
            "receipts": len(result["receipts"]),
            "permits": len(result["permits"]),
            "granted": [r["permit_id"] for r in grants if r.get("granted")],
            "grant_denied": [r["permit_id"] for r in grants if r.get("granted") is False],
            "begun": [r["permit_id"] for r in begins],
            "invalidated_this_round": result["invalidated_this_round"],
        }, ensure_ascii=False, sort_keys=True))
    except WorkbenchError as exc:
        p.exit(2, f"charge-access: {exc.code}: {exc.message}\n")
    except (OSError, ValueError, KeyError, TypeError) as exc:
        p.exit(2, f"charge-access: {exc}\n")


def main():
    p = argparse.ArgumentParser(
        prog="charge-access",
        description="收款核销驱动的 EV 充电准入工作台（独立于计量结算的可恢复状态机）。")
    p.add_argument("--round", required=True, help="本轮输入 JSON 路径")
    p.add_argument("--state", help="父工作台状态 JSON 路径（首轮可省略）")
    p.add_argument("--output-state", required=True, help="下一状态 JSON 输出路径")
    p.add_argument("--output", required=True, help="本轮结果 JSON 输出路径")
    p.add_argument("--source-state", action="append",
                   help="计量结算 state 路径（供 import.source_state_ref 解析，只读，可多次给出）")
    args = p.parse_args()
    run_round(p, args)


if __name__ == "__main__":
    main()
