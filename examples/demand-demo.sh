#!/usr/bin/env bash
# 园区上游需量电费账单复核的可运行示例（在项目根目录执行）。
#
# 串联：
#   1) 用 charging.engine.apply_batch 注册站点进口表读数，得到真实 state；
#   2) 用 charge-demand 从同一份 state 与不同账单复核需量电费：
#      题面主场景（尚无法确证）、增加读数后证据确认、改报峰值证据不支持、
#      计价不符、计量冲突、峰值并列、有效更正取代原记录、非法输入保留原输出。
# 全部产物写入临时目录，结束后清理；只读 state，绝不写回。
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

echo "== 1) 从计量批次构造 state（四个刻钟的小例子，各表从 0 Wh 开始） =="
python3 - "$TMP" <<'PY'
import json, sys
from charging.engine import apply_batch
out = sys.argv[1]
state, _ = apply_batch(None, json.load(open("fixtures/demand/batch-register.json")))
json.dump(state, open(f"{out}/state.json", "w"), ensure_ascii=False)
confirm, _ = apply_batch(None, json.load(open("fixtures/demand/batch-register-confirm.json")))
json.dump(confirm, open(f"{out}/state-confirm.json", "w"), ensure_ascii=False)
pre, _ = apply_batch(None, json.load(open("fixtures/demand/batch-register-precorrection.json")))
corr, _ = apply_batch(pre, json.load(open("fixtures/demand/batch-correct.json")))
json.dump(corr, open(f"{out}/state-corrected.json", "w"), ensure_ascii=False)
tie, _ = apply_batch(None, json.load(open("fixtures/demand/batch-register-tie.json")))
json.dump(tie, open(f"{out}/state-tie.json", "w"), ensure_ascii=False)
print("state 就绪：register / confirm / corrected / tie")
PY

run() {
  local state="$1"; local audit="$2"; local label="$3"
  echo
  echo "== $label =="
  python3 -m charging.demand.cli \
    --state "$TMP/$state" \
    --audit "fixtures/demand/$audit" \
    --output "$TMP/report.json"
  echo "  报告: $TMP/report.json"
}

run state.json audit-headline.json \
  "2) 题面主场景：票面第四段峰值 1000 Wh；独立峰值范围 1000..2000，尚无法确证"
run state-confirm.json audit-headline.json \
  "3) 增加 A 站第一段末 1000 Wh 有效读数：峰值固定 1000，证据确认"
run state.json audit-unsupported.json \
  "4) 改报第四段 1250 Wh（在峰值范围内、金额仍 600）：证据不支持"
run state.json audit-billing-mismatch.json \
  "5) 票面计价用量/金额算错：计价不符，给出应该填的值，仍给独立计量范围"
run state.json audit-conflict.json \
  "6) 总电量相符但上游第一段区间无解：计量冲突（正常审计结论，退出码 0）"
run state-tie.json audit-tie.json \
  "7) 峰值并列：两个时段都被读数锁定为 1000 Wh，申报其一即证据确认"
run state-corrected.json audit-headline.json \
  "8) 有效更正取代原记录（900 -> 1000）：报告用头记录 rec-a-0030-c2"

echo
echo "== 9) 非法输入（缺少账期结束读数）：退出码 2 且保留原输出 =="
python3 - "$TMP" <<'PY'
import json
bad = json.load(open("fixtures/demand/audit-headline.json"))
bad["upstream"]["readings"] = bad["upstream"]["readings"][:-1]
json.dump(bad, open(f"{__import__('sys').argv[1]}/bad.json", "w"))
PY
python3 -m charging.demand.cli \
  --state "$TMP/state.json" --audit fixtures/demand/audit-headline.json \
  --output "$TMP/kept.json" > /dev/null
before=$(cat "$TMP/kept.json")
if python3 -m charging.demand.cli \
    --state "$TMP/state.json" --audit "$TMP/bad.json" \
    --output "$TMP/kept.json"; then
  echo "ERROR: 应当失败却成功" >&2
  exit 1
else
  rc=$?
  [ "$rc" -eq 2 ] || { echo "ERROR: 退出码 $rc != 2" >&2; exit 1; }
  [ "$(cat "$TMP/kept.json")" = "$before" ] || { echo "ERROR: 原输出被改动" >&2; exit 1; }
  echo "  如预期返回退出码 2，原输出文件原样保留"
fi
