#!/usr/bin/env bash
# 收款核销 / 充电准入工作台多轮可运行示例。
#
# 用法（在项目根目录执行）：
#   bash examples/workbench-demo.sh
#
# 脚本在临时目录中串联 5 轮 CLI 调用，演示：
#   r1 收 60 核销、欠款 40 -> 许可 G 被拒
#   r2 再收 20、欠款 20 -> G 签发
#   r3 撤回第二笔 20 -> 欠款 40，G 在单独一轮失效
#   r4 单独提交 begin -> 证明失效许可不能启动
#   r5 退单与非法 begin 同轮 -> 整轮失败，父状态保持不变
# 运行结束后打印关键结果；生成的工作台状态留在临时目录（不提交仓库）。
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
STATE="$TMP/wb-state.json"
RESULT="$TMP/wb-result.json"

run() {
  local round_file="$1"; shift
  echo "== $round_file =="
  python3 -m charging.workbench.cli \
    --round "$ROOT/fixtures/workbench/$round_file" \
    --state "$STATE" \
    --output-state "$STATE" \
    --output "$RESULT" "$@"
  echo
}

( cd "$ROOT"
  run round-01.json
  run round-02.json
  run round-03.json

  echo "== round-04.json （失效许可 begin，预期退出 2） =="
  if python3 -m charging.workbench.cli \
      --round "$ROOT/fixtures/workbench/round-04.json" \
      --state "$STATE" --output-state "$STATE" --output "$RESULT"; then
    echo "ERROR: 本轮应当失败" >&2
    exit 1
  else
    echo "已按预期拒绝（退出码 2），父状态未改动"
  fi
  echo

  echo "== 同轮退单 + 非法 begin（预期退出 2 且回滚） =="
  cp "$STATE" "$TMP/state-before-r5.json"
  cat > "$TMP/round-05.json" <<'JSON'
{
  "round_id": "wb-r5-chargeback-and-illegal-begin",
  "evaluation_at": "2026-02-14T00:00:00+00:00",
  "import": {
    "source_state": {
      "closed_lines": {
        "P1:S1": {"line_id": "P1:S1", "session_id": "S1", "period_id": "P1",
                  "charge_cents": 100, "reflected_charge_cents": 100}
      },
      "adjustments": []
    },
    "session_accounts": {"S1": "A1"},
    "period_due_at": {"P1": "2026-01-31T00:00:00+00:00"}
  },
  "finance_operations": [
    {"op": "chargeback", "operation_id": "fin-cb-1", "receipt_id": "R-001",
     "unallocated_cents": 0,
     "withdrawals": [{"line_id": "P1:S1", "amount_cents": 10}]}
  ],
  "service_operations": [
    {"op": "begin", "operation_id": "svc-begin-g-again", "permit_id": "G",
     "station_id": "ST-ALPHA", "session_id": "SESSION-9002"}
  ]
}
JSON
  if python3 -m charging.workbench.cli \
      --round "$TMP/round-05.json" \
      --state "$STATE" --output-state "$STATE" --output "$RESULT"; then
    echo "ERROR: 本轮应当失败" >&2
    exit 1
  else
    echo "已按预期整轮拒绝（退出码 2）"
  fi
  if cmp -s "$STATE" "$TMP/state-before-r5.json"; then
    echo "父工作台状态与拒绝前字节一致（回滚成功）"
  else
    echo "ERROR: 父状态被修改" >&2
    exit 1
  fi
)

echo "全部示例轮次完成。临时目录：$TMP"
