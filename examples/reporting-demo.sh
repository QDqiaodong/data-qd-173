#!/usr/bin/env bash
# 闭账周期充电报表保密发布的可运行示例（在项目根目录执行）。
#
# 串联：
#   1) 用 charging.engine.apply_batch 注册计量/会话并关闭周期，得到闭账 state；
#   2) 用 charge-report 从同一份 state 与不同发布请求编制对外报表与内部审核。
# 全部产物写入临时目录，结束后清理；只读 state，绝不写回。
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

echo "== 1) 从计量批次构造闭账 state =="
python3 - "$TMP" <<'PY'
import json, sys
from charging.engine import apply_batch
out = sys.argv[1]
state = None
for name in ("batch-register.json", "batch-close.json"):
    batch = json.load(open(f"fixtures/reporting/{name}"))
    state, _ = apply_batch(state, batch)
with open(f"{out}/state.json", "w") as f:
    json.dump(state, f, ensure_ascii=False)
billable = [l for l in state["closed_lines"].values() if l["billable"]]
print(f"闭账周期 P-2026-01：合格计费行 {len(billable)} 条，"
      f"另有 {len(state['closed_lines']) - len(billable)} 条未计费行不参与发布汇总")
PY

run() {
  local request="$1"; local label="$2"
  echo
  echo "== $label =="
  python3 -m charging.reporting.cli \
    --state "$TMP/state.json" \
    --request "fixtures/reporting/$request" \
    --public-output "$TMP/public.json" \
    --review-output "$TMP/review.json"
  echo "  对外报表: $TMP/public.json"
  echo "  内部审核: $TMP/review.json"
}

run request-headline.json "2) 题面主场景：历史仅全表总计 20，乙+丙可发、甲锁死保护格"
run request-history-station.json "3) A 站小计已公开：乙、丙都不能发（B 站小计可被减出）"
run request-tie.json "4) 同价裁决：按排序后包标识序列字典序"
run request-empty.json "5) 无安全包：正常输出空发布（区别于已有风险错误）"

echo
echo "== 6) 已有风险（历史已公开 A/F 精确值）：可区分的 prior_risk 错误 =="
if python3 -m charging.reporting.cli \
    --state "$TMP/state.json" \
    --request fixtures/reporting/request-prior-risk.json \
    --public-output "$TMP/should-not-exist.json" \
    --review-output "$TMP/should-not-exist-2.json"; then
  echo "ERROR: 应当失败却成功" >&2
  exit 1
else
  echo "  如预期返回退出码 2 且未写出任何输出文件"
fi
